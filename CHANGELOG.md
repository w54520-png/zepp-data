# Changelog

All notable changes to **zepp-data** are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ·
Versioning: [SemVer](https://semver.org/)

---

## [4.0.3] — 2026-09-26

User-experience improvements based on testing feedback from non-PRoot
Linux and read-only home-dir environments.

### Added

- **`scripts/init.py`** — proactively detects when the default
  `~/.zepp-data/` parent directory is **not writable** (e.g. read-only
  home in corporate sandboxes, immutable `/home`, etc.) and prints a
  clear `export ZEPP_DATA_DIR=/path/to/writable/dir` hint **before**
  any other action. Previously the user would only discover the
  problem after hitting `PermissionError` from a deep call stack.
- **`scripts/pull_to_sqlite.py`** — when starting a sync, checks
  whether the `workouts` table is empty and prints a clear hint
  reminding the user to also run `fetch_workouts.py` (which pulls the
  separate `workout_history` + `workout_detail` flows). Previously
  the only symptom was a silent `no workouts in window` message from
  the `workout_detail` stream and confusion about "where did my
  exercise history go?".
- **README** — added an explicit note under `pull_to_sqlite.py`:
  `sync` does not pull workouts, and the user must run
  `fetch_workouts.py` separately.

### Test status

- 484 passed, 0 regressions.
- Same 4 pre-existing failures as v4.0.2 (DST ×2, HR zone ×1, stress ×1).

---

## [4.0.2] — 2026-09-26

Fixes the **most-reported v4.0.0/v4.0.1 issue**: when a user sets
`ZEPP_DATA_DIR=/custom/path`, the SQLite database follows the new path,
but `zepp_client.py` keeps reading `~/.zepp-data/.secrets/token.json`.
If that home-dir token happens to be from an old (failed) login, every
sync returns 401 even though `pull_to_sqlite.py` opens the right DB.

### Fixed

- **`scripts/zepp_client.py::_resolve_secrets_path()`** — added
  `ZEPP_DATA_DIR` as a 2nd-tier fallback (after `ZEPP_SECRETS_PATH`,
  before `~/.zepp-data/.secrets/token.json`). The token now follows the
  data dir consistently. `ZEPP_SECRETS_PATH` is preserved for backward
  compatibility.

### Added

- **`references/path-handling.md`** — single source of truth for the
  `ZEPP_DATA_DIR` / `ZEPP_SECRETS_PATH` precedence chain, including the
  recommended layout for read-only home dirs and migration notes for
  users hitting the v4.0.0 401 bug.

### Test status

- 484 passed, 0 regressions.
- Same 4 pre-existing failures as v4.0.1 (DST ×2, HR zone ×1, stress ×1).

---

## [4.0.1] — 2026-09-26

Bug-fix release addressing user-reported issues from `v4.0.0` testing on
non-PRoot Linux / macOS systems.

### Fixed

- **DB path respects `ZEPP_DATA_DIR`** — `daily_report.py`,
  `query_zepp.py` (`ensure_fresh`), `compute_calorie_total.py`,
  `fetch_workouts.py`, `fix_workout_altitude_units.py`, `insight.py` and
  `tests/generate_daily_report_sample.py` no longer hardcode
  `/root/.zepp-data/zepp.db`. They now read `ZEPP_DATA_DIR` first, then fall
  back to `~/.zepp-data/zepp.db`. **Fixes the bug where non-root users (or
  any user with `~/.zepp-data/` protected) couldn't run daily report, weekly
  insight, calorie computation, or workout fetching.**
- **README** — added a "数据目录自定义 (可选)" section explaining
  `ZEPP_DATA_DIR`, and a "手机号格式" section clarifying that the `+86`
  prefix is **added by the code** for 国服 accounts (and that international
  users should use `--email` instead).
- **`scripts/zepp_oauth.py login` help text** — corrected two misleading
  claims: (a) login does **not** kick the phone Zepp App (it uses
  `APP_NAME=com.xiaomi.hm.health` which is a separate session), (b) the
  `--phone` arg receives a bare 11-digit number and the script automatically
  prepends `+86`.
- **`scripts/init.py`** — error message now interpolates the actual
  `DATA_DIR` instead of hardcoding `~/.zepp-data`.
- **`tests/test_sync_silent_reauth.py`** — the 5 failing tests now also mock
  `_maybe_refresh_token_before_sync`. Without this mock the test was actually
  invoking the real `zepp_oauth.py refresh` subprocess, which was using the
  user's real (expired) token and surfacing real account info in test
  output. All 11 silent-reauth tests now pass cleanly without any network
  access.
- **`tests/generate_daily_report_sample.py`** — restored 4 lines that had
  been accidentally corrupted during the v4.0.0 PII redaction step
  (`v["value"] for v in rows`, `completeness =`, `time_in_bed_secs']`,
  weight section), and removed a duplicate `if __name__ == "__main__":`
  block. The sample report script is now syntactically valid.

### Changed

- The PII-redacted values inside `USER_PROFILE_ROW` were also adjusted so the
  Mifflin BMR self-consistency checks still hold:
  `172 cm / 69 kg / 37 y/o / 男 → 1,585 kcal` is now
  `175 cm / 70 kg / 36 y/o / 男 → 1,619 kcal` (same Mifflin formula,
  obviously fake values). See `references/calorie-bmr.md` for derivation.

### Test status

- 484 passed (+11 silent-reauth tests now runnable), 0 regressions.
- The same 4 pre-existing failures remain (DST ×2, HR zone ×1, stress ×1)
  and are unrelated to v4.0.1 — they document real Zepp-app-vs-recomputed
  discrepancies and are tracked for the next milestone.

---

## [4.0.0] — 2026-09-26

Initial open-source release. Contains all milestones M1–M9 from the internal
development line (2026-09-08 → 2026-09-26).

### Highlights

- **21 Zepp Cloud data flows** — HRV/RMSSD, heart rate, SpO2, stress, sleep
  (band_data stages), weight & body composition, PAI, workouts + workout detail
  (GPS/splits/HR drift), VO2max, food, devices, members, sport load, etc.
- **2 OAuth login modes** — `login` (interactive) and `refresh` (cron-friendly,
  no app kick-out).
- **Mifflin-St Jeor BMR** in `compute_calorie_total.py` with `--bmr-source` flag.
- **Daily health report** (`scripts/daily_report.py`) — 13 markdown sections,
  webhook push to WeCom / Feishu / Slack, defaults to yesterday (t-1).
- **HTML dashboards** — 30-day (`dashboard.py`) and 7-day (`dashboard_7d.py`).
- **Weekly insight** (`insight.py --weekly`) — stable JSON with facts / baseline
  / direction.
- **484 pytest tests**, zero regressions across M1–M9.

### Milestones

- **M1** — workout summary field completion + unit-test scaffolding + altitude
  unit fix (Zepp returns cm; divide by 100).
- **M2** — sleep parsing (band_data) + weight/body-composition flows + BMR
  enhancement + 14-flow field mapping reference.
- **M3** — workout detail (GPS/splits/HR drift) + weekly insights + nap session
  detection + dashboard upgrade.
- **M4** — workout-detail rate-limit handling + Food macro flow + VO2max +
  international-region fallback + dashboard 30-day trend lines.
- **M5** — `silent_reauth` + sync error visibility (`stderr` instead of silent
  zero-record).
- **M6** — `time_utils` unified + DST handling (in-progress; **DST diagnostic
  flag only**, no full auto-correction).
- **M7** — `ensure_fresh_token` + `silent_reauth` test coverage.
- **M8** — HR zone breakdown (Zepp thresholds 113/141/154/162/173/190 bpm) +
  stress breakdown + daily-report template design.
- **M9** — daily-report CLI + Mifflin-St Jeor BMR integration + weight-snapshot
  independent section.

### Known Limitations

- `device_max_hr` is hardcoded (187 bpm default) — should read from
  `measurements` flow in a future milestone.
- Sleep naps (e.g. afternoon nap + night sleep merged into one session) are not
  yet split by `is_nap=1`.
- HR-zone boundaries (Zepp internal thresholds) are not publicly documented;
  we use proportional + absolute bpm thresholds.
- Stress "all-day recompute" vs "Zepp App direct read" can differ noticeably;
  `--use-app-percent` flag planned.
- DST handling is partial (M6); the diagnostic flag exists but auto-correction
  is not yet deployed.
- Webhook delivery has no retry; `--webhook-retries N` flag planned.

### Security / Privacy

- No real user data, account IDs, phone numbers, or personal measurements are
  shipped in this repository.
- All example values use obvious placeholders: `example_user`, `186XXXXXXXX`,
  `175.0 cm`, `70.0 kg`, `35 y/o`.
- All tokens and SQLite databases are git-ignored (`*.db`, `*.secrets/`).
- See **SECURITY.md** for reporting vulnerabilities.

---

[4.0.0]: https://github.com/<owner>/zepp-data/releases/tag/v4.0.0