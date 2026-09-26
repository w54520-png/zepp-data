# Changelog

All notable changes to **zepp-data** are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ·
Versioning: [SemVer](https://semver.org/)

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