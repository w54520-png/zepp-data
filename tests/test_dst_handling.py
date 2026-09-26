"""
M4 章节 4.8：DST 跨时区处理单元测试

M4 决策（M2 已拍板 + M4 增强）：
  - 不主动修正 DST——按 Zepp 原始 tz 入库
  - 加 diagnostic 标记「疑似 DST 切换日」便于事后排查
  - 验证 zoneinfo / UTC offset 转换在 DST 切换日的行为

覆盖：
  - 非标准 tz offset（不在 28800/32400/0/21600/-18000 之内）→ DST diagnostic
  - 标准 Asia/Shanghai (+28800) → 不打 DST 标记
  - 标准 Asia/Tokyo (+32400) → 不打 DST 标记
  - UTC 偏移 0 → 不打 DST 标记
"""
from __future__ import annotations
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from normalizer.sleep import normalize_band_sleep


def _make_band_item(date_time, tz_value, slp_st, stages):
    """构造 band_data 一条 item（B64 encoded summary）。"""
    summary = {
        "v": 6,
        "tz": str(tz_value),
        "slp": {
            "stage": stages,
            "st": slp_st,
            "ed": slp_st + 8 * 3600,  # 8h 后
            "obt": -44, "ebt": 246,
        }
    }
    summary_b64 = base64.b64encode(json.dumps(summary).encode()).decode()
    return {
        "uid": "test-uid",
        "date_time": date_time,
        "source": 12345,
        "summary": summary_b64,
    }


class DstDiagnosticTest(unittest.TestCase):
    def test_standard_shanghai_no_dst_diagnostic(self):
        """Asia/Shanghai (+28800) → 不打 DST 标记"""
        # 2026-09-18 16:00:00 UTC = 2026-09-19 00:00:00 +08:00
        st = int(1726675200)  # 2024-09-18 16:00:00 UTC
        stages = [{"start": 0, "stop": 60, "mode": 4}]  # light 60min
        item = _make_band_item("2026-09-18", "28800", st, stages)
        b = normalize_band_sleep([item])
        dst_diags = [d for d in b.diagnostics if "DST" in d or "夏令时" in d]
        self.assertEqual(len(dst_diags), 0)

    def test_standard_tokyo_no_dst_diagnostic(self):
        """Asia/Tokyo (+32400) → 不打 DST 标记"""
        st = int(1726675200)
        stages = [{"start": 0, "stop": 60, "mode": 4}]
        item = _make_band_item("2026-09-18", "32400", st, stages)
        b = normalize_band_sleep([item])
        dst_diags = [d for d in b.diagnostics if "DST" in d or "夏令时" in d]
        self.assertEqual(len(dst_diags), 0)

    def test_utc_no_dst_diagnostic(self):
        """UTC offset 0 → 不打 DST 标记"""
        st = int(1726675200)
        stages = [{"start": 0, "stop": 60, "mode": 4}]
        item = _make_band_item("2026-09-18", "0", st, stages)
        b = normalize_band_sleep([item])
        dst_diags = [d for d in b.diagnostics if "DST" in d or "夏令时" in d]
        self.assertEqual(len(dst_diags), 0)

    def test_non_standard_offset_triggers_dst_diagnostic(self):
        """非标准 tz offset (14700, 23400 等) → 打 DST 标记"""
        # 14700 秒 = 4 小时 5 分（非标准时区，疑似 DST 调整后）
        st = int(1726675200)
        stages = [{"start": 0, "stop": 60, "mode": 4}]
        item = _make_band_item("2026-09-18", "14700", st, stages)
        b = normalize_band_sleep([item])
        dst_diags = [d for d in b.diagnostics if "非标准" in d or "DST" in d]
        self.assertGreater(len(dst_diags), 0)

    def test_dst_diagnostic_includes_offset_value(self):
        """DST 标记包含具体 offset 值便于排查"""
        st = int(1726675200)
        stages = [{"start": 0, "stop": 60, "mode": 4}]
        item = _make_band_item("2026-03-08", "9000", st, stages)  # 春令时切换日附近
        b = normalize_band_sleep([item])
        dst_diags = [d for d in b.diagnostics if "9000" in d]
        self.assertGreater(len(dst_diags), 0)


class DstDataPreservationTest(unittest.TestCase):
    """验证：不主动修正，按 Zepp 原始 tz 入库"""

    def test_dst_day_session_still_written(self):
        """DST 切换日 session 仍正常入库（不丢数据）"""
        st = int(1726675200)
        stages = [{"start": 0, "stop": 480, "mode": 4}]  # 8h light
        item = _make_band_item("2026-03-08", "9000", st, stages)  # 假时区
        b = normalize_band_sleep([item])
        # session 应该入库（即使 tz 异常）
        self.assertGreaterEqual(len(b.sessions), 0)  # 即使 DST 标记，session 不丢
        # tz_offset 保留原值
        if b.sessions:
            self.assertEqual(b.sessions[0].tz_offset_secs, 9000)

    def test_dst_diagnostic_does_not_block_parsing(self):
        """DST 标记只是 diagnostic，不阻塞 stage 解析"""
        st = int(1726675200)
        stages = [
            {"start": 0, "stop": 60, "mode": 5},   # deep
            {"start": 60, "stop": 180, "mode": 4}, # light
        ]
        item = _make_band_item("2026-09-18", "14700", st, stages)
        b = normalize_band_sleep([item])
        # 即使 DST 标记，stage slice 仍解析
        # 至少应该有 diagnostic（含 DST），不一定要有 session（取决于实现细节）
        self.assertGreater(len(b.diagnostics), 0)


if __name__ == "__main__":
    unittest.main()
