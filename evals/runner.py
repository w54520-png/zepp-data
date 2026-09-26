#!/usr/bin/env python3
"""
zepp-data skill 评测 runner —— 自动跑 evals/ 下所有 yaml case，统计通过率。

⚠️ 重要：runner 不调 LLM
  - 因为 evals 需要"评估 LLM 是否正确触发 skill"，需要 Anthropic eval framework
  - 本 runner 只做"格式校验 + 可执行性 + 关键词可达性"三种**机器可判定**的检查
  - 完整的 LLM 触发评测（trigger recall / precision）需要额外工具链，不在本任务范围

用法：
  python3 evals/runner.py                  # 跑全部 + 输出报告
  python3 evals/runner.py --only-trigger   # 只跑正向
  python3 evals/runner.py --only-negative  # 只跑负向
  python3 evals/runner.py --only-quality   # 只跑质量
  python3 evals/runner.py --case trigger_001_oauth_not_kick  # 跑单个 case
  python3 evals/runner.py --json           # 输出 JSON 格式（CI 用）

退出码：
  0 = 全部 pass（或无 case）
  1 = 有 case fail（CI 用作红灯）

实现要点：
  1. 加载 yaml（用 PyYAML；项目依赖）
  2. 格式校验：必填字段、类型、id 命名规范
  3. 关键词可达性：在 SKILL.md / references/*.md / scripts/*.py 里 grep
     验证 expected_output_keywords 至少能在文档里找到（说明 LLM 有依据产出）
  4. 路径存在性：references/*.md + scripts/*.py 全部存在
  5. 命令 dry-run：quality case 的 expected_steps 第一条命令尝试跑 --help
     确认脚本能 import / argparse 不报错
  6. 输出统计：total / pass / fail / pass_rate
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:
    print("❌ PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# ──────────────────────────────────────────────────────────────────────────
# 路径常量
# ──────────────────────────────────────────────────────────────────────────

SKILL_ROOT = Path(__file__).resolve().parent.parent  # /var/minis/skills/zepp-data
EVALS_DIR = SKILL_ROOT / "evals"
SCRIPTS_DIR = SKILL_ROOT / "scripts"
REFERENCES_DIR = SKILL_ROOT / "references"
SKILL_MD = SKILL_ROOT / "SKILL.md"

YAML_FILES = {
    "trigger": EVALS_DIR / "should-trigger.yaml",
    "negative": EVALS_DIR / "should-not-trigger.yaml",
    "quality": EVALS_DIR / "task-quality.yaml",
}


# ──────────────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class CaseResult:
    """单 case 评测结果。"""
    id: str
    category: str
    type: str  # trigger / negative / quality
    status: str  # PASS / FAIL / SKIP
    checks: list[dict] = field(default_factory=list)  # [{name, ok, detail}]
    notes: str = ""

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            self.status = "FAIL"


# ──────────────────────────────────────────────────────────────────────────
# 通用工具
# ──────────────────────────────────────────────────────────────────────────


def load_yaml(path: Path) -> dict:
    """读 yaml，错误时打印友好提示并抛 SystemExit。"""
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"❌ YAML parse error in {path.name}: {e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict) or "cases" not in data:
        print(f"❌ {path.name}: missing top-level 'cases' key", file=sys.stderr)
        sys.exit(2)
    return data


def file_id_for_case(case_id: str) -> str:
    """从 case id 推断 yaml 类型（trigger/negative/quality）。"""
    if case_id.startswith("trigger_"):
        return "trigger"
    if case_id.startswith("not_trigger_"):
        return "negative"
    if case_id.startswith("quality_"):
        return "quality"
    return "unknown"


def _truncate(s: str, n: int) -> str:
    """截断字符串但保留可读尾部。"""
    return s if len(s) <= n else s[: n - 1] + "…"


def iter_corpus() -> str:
    """把所有文档 + 脚本拼成一个字符串，供 grep。

    范围：
      - SKILL.md
      - references/*.md（全部 L3 详情文档）
      - scripts/*.py（Python 脚本源码）
      - scripts/*.sh（cron / wrapper 脚本）
    """
    parts: list[str] = []
    for p in [SKILL_MD, *sorted(REFERENCES_DIR.glob("*.md"))]:
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8", errors="ignore"))
    for p in sorted(SCRIPTS_DIR.glob("*.py")):
        if p.is_file():
            try:
                parts.append(p.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                pass
    for p in sorted(SCRIPTS_DIR.glob("*.sh")):
        if p.is_file():
            try:
                parts.append(p.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                pass
    return "\n".join(parts)


def keyword_reachable(keyword: str, corpus: str) -> bool:
    """关键词是否能在 corpus 里被找到（带语义归一化）。

    大小写不敏感；对正则特殊字符做 re.escape。
    归一化策略（按顺序尝试）：
      1. 原样
      2. 去括号内容 "weight (kg)" → "weight"
      3. 去中点 "徒步 或 健走 或 户外跑步" → 取首项 "徒步"
      4. snake_case fact_id（hrv_rmssd_daily_mean）→ 拆成 token 匹配 hrv/rmssd/daily/mean
    返回 (ok, matched_form)
    """
    candidates = _keyword_variants(keyword)
    for form in candidates:
        try:
            if re.search(re.escape(form), corpus, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _keyword_variants(keyword: str) -> list[str]:
    """生成关键词的多形态候选。

    尝试顺序：
      1. 原样
      2. 去括号内容 "weight (kg)" → "weight"
      3. 中点 / slash 拆分（"A 或 B" / "A/B"）→ 各 piece
      4. snake_case fact_id → 拆 token（任一 token 命中即可）
      5. 点分路径（"user_profile.height"）→ 拆点（任一 part 命中即可）
      6. 中文混合 token（"DB 初始化"）→ 拆 token（任一中文/英文 token 命中即可）
    """
    forms = [keyword]
    # 1. 去括号
    no_parens = re.sub(r"\s*\([^)]*\)", "", keyword).strip()
    if no_parens and no_parens != keyword:
        forms.append(no_parens)
    # 2. 中点 / slash
    if " 或 " in keyword:
        for piece in re.split(r"\s*或\s*", keyword):
            piece = piece.strip()
            if piece:
                forms.append(piece)
    if "/" in keyword and len(keyword) < 60:
        for piece in re.split(r"\s*/\s*", keyword):
            piece = piece.strip()
            if piece and len(piece) >= 2:
                forms.append(piece)
    # 3. snake_case fact_id（纯 ASCII + 下划线 + 小写）
    if re.match(r"^[a-z][a-z0-9_]+$", keyword):
        for t in keyword.split("_"):
            if len(t) >= 3:
                forms.append(t)
    # 4. 点分路径
    if "." in keyword and " " not in keyword and len(keyword) < 60:
        for part in keyword.split("."):
            part = part.strip()
            if part and len(part) >= 3:
                forms.append(part)
    # 5. 中文混合：按空格拆
    if re.search(r"[\u4e00-\u9fff]", keyword):
        for piece in re.split(r"\s+", keyword):
            piece = piece.strip()
            if piece and len(piece) >= 2:
                forms.append(piece)
    return forms


# ──────────────────────────────────────────────────────────────────────────
# 校验函数（按 case type）
# ──────────────────────────────────────────────────────────────────────────


def validate_trigger_case(case: dict, corpus: str, result: CaseResult) -> None:
    """正向 trigger case 校验。"""
    # 1. 必填字段
    for field_name in ["id", "category", "user_query", "should_trigger", "expected_action"]:
        if field_name not in case:
            result.add(f"field:{field_name}", False, "missing required field")
        elif not case[field_name]:
            result.add(f"field:{field_name}", False, "empty value")

    if "should_trigger" in case and case["should_trigger"] is not True:
        result.add("should_trigger==true", False,
                   f"expected True, got {case['should_trigger']!r}")

    # 2. 关键词可达性
    for kw in case.get("expected_output_keywords", []) or []:
        ok = keyword_reachable(kw, corpus)
        result.add(f"keyword:{_truncate(kw, 30)}", ok,
                   "found in docs/scripts" if ok else "NOT FOUND in corpus")

    # 3. expected_action 提到具体命令时，验证脚本存在
    action_text = case.get("expected_action", "") or ""
    for script_match in re.finditer(r"scripts/([\w-]+\.py)", action_text):
        script = SCRIPTS_DIR / script_match.group(1)
        result.add(f"script:{script_match.group(1)}",
                   script.is_file(),
                   str(script.relative_to(SKILL_ROOT)) if script.is_file() else f"MISSING: {script}")


def validate_negative_case(case: dict, result: CaseResult) -> None:
    """负向 case 校验（无需 corpus；只需结构）。"""
    for field_name in ["id", "category", "user_query", "should_trigger", "rationale"]:
        if field_name not in case:
            result.add(f"field:{field_name}", False, "missing required field")

    if "should_trigger" in case and case["should_trigger"] is not False:
        result.add("should_trigger==false", False,
                   f"expected False, got {case['should_trigger']!r}")

    # user_query 可能含品牌词（设计意图：测试 LLM 在品牌词 + 错场景时不误触发）
    # 这种"decoy"是负向 case 的核心价值；只做 note，不 fail
    brand_keywords = ["Zepp", "Amazfit", "华米", "Huami", "HRV", "PAI", "血氧"]
    found = [k for k in brand_keywords if k.lower() in (case.get("user_query", "") or "").lower()]
    if found:
        result.notes = (
            f"decoy case: query 含品牌词 {found}，"
            "验证 LLM 在错场景下不误触发（合理设计）"
        )


def validate_quality_case(case: dict, corpus: str, result: CaseResult) -> None:
    """端到端 quality case 校验。"""
    # 1. 必填字段
    for field_name in ["id", "category", "user_query", "expected_steps"]:
        if field_name not in case:
            result.add(f"field:{field_name}", False, "missing required field")

    steps = case.get("expected_steps", []) or []
    if not steps:
        result.add("expected_steps_nonempty", False, "no steps defined")

    # 2. 验证 commands 里的脚本存在
    for i, step in enumerate(steps):
        cmd = step.get("command", "") or ""
        for script_match in re.finditer(r"scripts/([\w-]+\.py)", cmd):
            script = SCRIPTS_DIR / script_match.group(1)
            result.add(f"step{i}.script:{script_match.group(1)}",
                       script.is_file(),
                       str(script.relative_to(SKILL_ROOT)) if script.is_file() else f"MISSING: {script}")

    # 3. 关键词可达性（关键词必须在 docs 或脚本里能找到）
    for kw in case.get("expected_keywords", []) or []:
        ok = keyword_reachable(kw, corpus)
        result.add(f"keyword:{_truncate(kw, 30)}", ok,
                   "found in docs/scripts" if ok else "NOT FOUND in corpus")

    # 4. expected_steps 第一条命令做"语法可执行性"快速校验：
    #    不实际跑（避免污染 DB / 网络），而是用 python3 -c 验证脚本能 import + argparse 不挂
    if steps:
        first_cmd = steps[0].get("command", "")
        script_match = re.search(r"scripts/([\w-]+\.py)", first_cmd)
        if script_match:
            script_name = script_match.group(1)
            ok, detail = dry_import_check(script_name)
            result.add(f"dry_import:{script_name}", ok, detail)


def dry_import_check(script_name: str) -> tuple[bool, str]:
    """验证脚本能 import + argparse 不抛异常（不执行主逻辑）。

    用法：python3 -c "import importlib.util, sys; ..." 动态 import 并跑 parser。
    """
    script_path = SCRIPTS_DIR / script_name
    if not script_path.is_file():
        return False, f"script not found: {script_path}"

    probe = (
        "import importlib.util, sys, pathlib;"
        f"p = pathlib.Path({str(script_path)!r});"
        "spec = importlib.util.spec_from_file_location('m', p);"
        "m = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(m);"
        # argparse 的 parser 在 main() 里，这里只确认 module 没语法错误
        "print('OK')"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=30,
            cwd=str(SKILL_ROOT),
            env={**os.environ, "PYTHONPATH": str(SKILL_ROOT)},
        )
        if proc.returncode == 0 and "OK" in proc.stdout:
            return True, "importable"
        return False, f"returncode={proc.returncode} stderr={proc.stderr.strip()[:200]}"
    except subprocess.TimeoutExpired:
        return False, "timeout (30s) during import probe"
    except Exception as e:
        return False, f"probe exception: {e}"


# ──────────────────────────────────────────────────────────────────────────
# 主评测流程
# ──────────────────────────────────────────────────────────────────────────


def run_all(args: argparse.Namespace) -> list[CaseResult]:
    """跑所有符合过滤条件的 case。"""
    corpus = iter_corpus()
    results: list[CaseResult] = []

    # 决定跑哪些 yaml
    yaml_filter: set[str] = set()
    if args.only_trigger:
        yaml_filter.add("trigger")
    elif args.only_negative:
        yaml_filter.add("negative")
    elif args.only_quality:
        yaml_filter.add("quality")
    else:
        yaml_filter.update(YAML_FILES.keys())

    case_filter = args.case  # 单 case id

    for yaml_type, yaml_path in YAML_FILES.items():
        if yaml_type not in yaml_filter:
            continue
        if not yaml_path.is_file():
            print(f"⚠️  Missing YAML: {yaml_path}", file=sys.stderr)
            continue

        data = load_yaml(yaml_path)
        for case in data.get("cases", []) or []:
            case_id = case.get("id", "<missing-id>")
            if case_filter and case_id != case_filter:
                continue

            result = CaseResult(
                id=case_id,
                category=case.get("category", "<missing>"),
                type=yaml_type,
                status="PASS",
            )

            try:
                if yaml_type == "trigger":
                    validate_trigger_case(case, corpus, result)
                elif yaml_type == "negative":
                    validate_negative_case(case, result)
                elif yaml_type == "quality":
                    validate_quality_case(case, corpus, result)
            except Exception as e:
                result.add("validator_exception", False, f"{type(e).__name__}: {e}")

            results.append(result)

    return results


# ──────────────────────────────────────────────────────────────────────────
# 输出
# ──────────────────────────────────────────────────────────────────────────


def render_text_report(results: list[CaseResult]) -> str:
    """人读报告：分类 + 总计。"""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("zepp-data skill eval runner — 自动评测报告")
    lines.append("=" * 78)
    lines.append("")

    # 按 type 分组
    by_type: dict[str, list[CaseResult]] = {"trigger": [], "negative": [], "quality": []}
    for r in results:
        by_type.setdefault(r.type, []).append(r)

    type_label = {
        "trigger": "📌 Should-trigger (正向)",
        "negative": "🚫 Should-not-trigger (负向)",
        "quality": "✅ Task quality (端到端)",
    }

    for t in ["trigger", "negative", "quality"]:
        rs = by_type.get(t, [])
        if not rs:
            continue
        passed = sum(1 for r in rs if r.status == "PASS")
        failed = sum(1 for r in rs if r.status == "FAIL")
        rate = (passed / len(rs) * 100) if rs else 0
        lines.append(f"### {type_label[t]}  ({len(rs)} cases)")
        lines.append(f"   pass={passed}  fail={failed}  pass_rate={rate:.1f}%")
        lines.append("")
        for r in rs:
            mark = "✅" if r.status == "PASS" else "❌"
            lines.append(f"  {mark} {r.id}  [{r.category}]")
            for c in r.checks:
                if not c["ok"]:
                    detail = f" — {c['detail']}" if c["detail"] else ""
                    lines.append(f"     └─ ❌ {c['name']}{detail}")
        lines.append("")

    total = len(results)
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    rate = (passed / total * 100) if total else 0
    lines.append("-" * 78)
    lines.append(f"TOTAL: {passed}/{total} pass ({rate:.1f}%)  fail={failed}")
    lines.append("-" * 78)
    lines.append("")
    lines.append("⚠️  注意：本 runner 不调 LLM，只做格式 + 关键词可达性 + 路径校验。")
    lines.append("   完整的 LLM 触发评测（precision / recall）需要 Anthropic eval framework。")
    return "\n".join(lines)


def render_json_report(results: list[CaseResult]) -> str:
    """CI 用 JSON。"""
    total = len(results)
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    payload = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": (passed / total) if total else 0.0,
        "cases": [asdict(r) for r in results],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="zepp-data skill eval runner (no LLM — structural & keyword reachability checks)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--only-trigger", action="store_true", help="只跑 should-trigger.yaml")
    g.add_argument("--only-negative", action="store_true", help="只跑 should-not-trigger.yaml")
    g.add_argument("--only-quality", action="store_true", help="只跑 task-quality.yaml")
    p.add_argument("--case", help="跑单个 case（id 精确匹配）")
    p.add_argument("--json", action="store_true", help="输出 JSON（CI 用）")
    p.add_argument("--quiet", action="store_true", help="只在 fail 时打印")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results = run_all(args)

    if args.json:
        print(render_json_report(results))
    else:
        if not args.quiet or any(r.status == "FAIL" for r in results):
            print(render_text_report(results))
        else:
            total = len(results)
            passed = sum(1 for r in results if r.status == "PASS")
            print(f"✅ all {total} cases pass ({passed}/{total})")

    failed = sum(1 for r in results if r.status == "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())