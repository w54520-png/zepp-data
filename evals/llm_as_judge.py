#!/usr/bin/env python3
"""
Mechanism A — true LLM-as-judge eval for zepp-data skill.

Loads 21 cases from 3 yaml files, sends each user_query to MiniMax-M3
with a minimal system prompt (no SKILL.md exposed), parses the JSON
decision, and scores against expected_action.
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime

import yaml

EVAL_DIR = "/var/minis/skills/zepp-data/evals"
SYSTEM_PROMPT_FILE = f"{EVAL_DIR}/llm_system_prompt.txt"
TIMEOUT_SEC = 30
MODEL = "MiniMax-M3"

YAML_FILES = [
    ("trigger", f"{EVAL_DIR}/should-trigger.yaml"),
    ("not-trigger", f"{EVAL_DIR}/should-not-trigger.yaml"),
    ("quality", f"{EVAL_DIR}/task-quality.yaml"),
]


def load_system_prompt():
    with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
        return f.read()


def load_all_cases():
    cases = []
    for cat, path in YAML_FILES:
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        for c in doc["cases"]:
            c["_category_group"] = cat
            cases.append(c)
    return cases


def extract_json(text):
    """Pull a {should_trigger_zepp_data, tool_call, reason} dict out of text.
    Falls back to regex heuristics when the model didn't emit clean JSON.
    """
    if text is None:
        return None
    text = text.strip()

    # 1) Try a clean JSON parse of the whole string.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # 2) Strip ```json ... ``` fences and re-parse.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass

    # 3) Find first balanced {...} and try to parse.
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                cand = text[start : i + 1]
                try:
                    obj = json.loads(cand)
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    continue

    # 4) Regex heuristics: pull should_trigger_zepp_data and tool_call.
    trig = None
    m = re.search(r"should_trigger_zepp_data\s*[:=]\s*(true|false|True|False|1|0)", text)
    if m:
        trig = m.group(1).lower() in ("true", "1")
    tool = "none"
    m = re.search(r'tool_call\s*[:=]\s*"([^"]*)"', text)
    if m:
        tool = m.group(1)
    else:
        m = re.search(r"tool_call\s*[:=]\s*`([^`]+)`", text)
        if m:
            tool = m.group(1)
    reason = ""
    m = re.search(r'reason\s*[:=]\s*"([^"]*)"', text)
    if m:
        reason = m.group(1)

    if trig is not None:
        return {
            "should_trigger_zepp_data": trig,
            "tool_call": tool,
            "reason": reason,
        }

    return None


def call_llm(user_query, system_prompt):
    """Call MiniMax-M3 via minis-model-use. Returns (parsed_json, raw_text, error)."""
    input_payload = {
        "messages": [
            {"role": "user", "content": user_query}
        ]
    }
    # Write input to a temp file to avoid command-line quoting issues
    input_file = f"/tmp/llm_judge_input_{int(time.time()*1000)}.json"
    with open(input_file, "w", encoding="utf-8") as f:
        json.dump(input_payload, f, ensure_ascii=False)

    cmd = [
        "minis-model-use", "run",
        "--model", MODEL,
        "--system", system_prompt,
        "--input", input_file,
        "--max-tokens", "1024",
        "--temperature", "0",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired:
        return None, None, f"timeout after {TIMEOUT_SEC}s"
    raw_stdout = proc.stdout
    if proc.returncode != 0:
        return None, raw_stdout, f"exit code {proc.returncode}: {(proc.stderr or '')[:200]}"

    # minis-model-use wraps the LLM text in a JSON envelope: {"model", "text", "usage"}.
    # We must unwrap `.text` before trying to parse the model's JSON.
    model_text = raw_stdout
    try:
        env = json.loads(raw_stdout)
        if isinstance(env, dict) and "text" in env and isinstance(env["text"], str):
            model_text = env["text"]
    except Exception:
        pass

    return extract_json(model_text), model_text, None


# Keywords that mark "correct command family" for quality cases
QUALITY_KEYWORDS = {
    "quality_001_weekly_report": ["insight", "--weekly"],
    "quality_002_workouts_top_climb": ["workouts", "climb"],
    "quality_003_bmr_calorie_total": ["compute_calorie_total", "zepp_med"],
    "quality_004_dashboard_html": ["dashboard"],
}


def score_case(case, decision, raw, error):
    """Return (status, details_dict). status ∈ {'pass','fail','error'}."""
    cat = case["_category_group"]
    cid = case["id"]
    if error or not isinstance(decision, dict):
        return "error", {
            "error": error or "json parse failed",
            "raw_excerpt": (raw or "")[:300],
        }

    llm_trigger = bool(decision.get("should_trigger_zepp_data"))
    llm_tool = str(decision.get("tool_call", "none"))
    llm_reason = str(decision.get("reason", ""))

    expected_trigger = bool(case.get("should_trigger", True))  # quality cases default True

    if cat == "trigger":
        ok = llm_trigger is True
        return ("pass" if ok else "fail"), {
            "expected_trigger": True,
            "llm_trigger": llm_trigger,
            "llm_tool": llm_tool,
            "llm_reason": llm_reason,
        }

    if cat == "not-trigger":
        ok = llm_trigger is False
        return ("pass" if ok else "fail"), {
            "expected_trigger": False,
            "llm_trigger": llm_trigger,
            "llm_tool": llm_tool,
            "llm_reason": llm_reason,
        }

    # quality
    kws = QUALITY_KEYWORDS.get(cid, [])
    tool_lc = llm_tool.lower()
    keyword_ok = all(k.lower() in tool_lc for k in kws)
    trigger_ok = llm_trigger is True
    ok = trigger_ok and keyword_ok
    return ("pass" if ok else "fail"), {
        "expected_trigger": True,
        "expected_keywords": kws,
        "llm_trigger": llm_trigger,
        "llm_tool": llm_tool,
        "llm_reason": llm_reason,
        "keyword_match": keyword_ok,
    }


def main():
    print(f"# Mechanism A · LLM-as-judge for zepp-data")
    print(f"# model: {MODEL}")
    print(f"# started: {datetime.now().isoformat()}")
    print(f"# timeout per case: {TIMEOUT_SEC}s\n")

    system_prompt = load_system_prompt()
    cases = load_all_cases()
    print(f"# loaded {len(cases)} cases\n")

    results = []
    for i, case in enumerate(cases, 1):
        cid = case["id"]
        cat = case["_category_group"]
        q = case["user_query"]
        print(f"[{i:02d}/{len(cases)}] {cid} ({cat})")
        print(f"    Q: {q[:80]}{'…' if len(q)>80 else ''}")

        t0 = time.time()
        decision, raw, error = call_llm(q, system_prompt)
        dt = time.time() - t0

        status, det = score_case(case, decision, raw, error)
        det["elapsed_sec"] = round(dt, 2)
        det["user_query"] = q
        det["raw_excerpt"] = (raw or "")[:400]

        print(f"    → {status.upper()}  ({dt:.1f}s)")
        if error:
            print(f"    ERR: {error}")
        elif isinstance(decision, dict):
            print(f"    LLM: trigger={decision.get('should_trigger_zepp_data')}  "
                  f"tool={str(decision.get('tool_call',''))[:60]}")
        print()

        results.append({
            "id": cid,
            "category_group": cat,
            "expected_trigger": case.get("should_trigger", True),
            "status": status,
            "details": det,
        })

    # Summary
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    errored = sum(1 for r in results if r["status"] == "error")

    by_cat = {}
    for r in results:
        c = r["category_group"]
        by_cat.setdefault(c, {"pass": 0, "fail": 0, "error": 0})
        by_cat[c][r["status"]] += 1

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"total : {total}")
    print(f"pass  : {passed}  ({passed/total*100:.1f}%)")
    print(f"fail  : {failed}  ({failed/total*100:.1f}%)")
    print(f"error : {errored} ({errored/total*100:.1f}%)")
    for c, s in by_cat.items():
        sub = s["pass"] + s["fail"] + s["error"]
        print(f"  - {c:14s}: pass {s['pass']}/{sub}  fail {s['fail']}  error {s['error']}")

    # Dump full JSON for downstream use
    out_path = "/tmp/llm_judge_A_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "model": MODEL,
            "timestamp": datetime.now().isoformat(),
            "total": total,
            "pass": passed,
            "fail": failed,
            "error": errored,
            "by_category": by_cat,
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n(full results → {out_path})")


if __name__ == "__main__":
    main()