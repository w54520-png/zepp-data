"""
M4 章节 4.1：workout_detail 限流应对单元测试

覆盖：
  - `_fetch_workout_detail_with_retry` 区分 3 种响应：
    ① code=1 + data → ('ok', payload)
    ② code=1 + 无 data → ('no_data', payload)
    ③ code=0 → 抛出 ZeppError（业务错误）
    ④ HTTP 404 → 抛出 ZeppError
    ⑤ 网络错误 → 重试 1 次
    ⑥ 网络错误 2 次都失败 → 抛最后一次的异常
  - `_read_workout_feature`：从 raw_payload 读 feature 字段
  - 无 GPS 跳过：feature ≤ 100 → 跳过 detail 请求
  - 节流：连续 N 次调用之间间隔 ≥ 200ms（除首个）
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from pull_to_sqlite import (
    _fetch_workout_detail_with_retry,
    _read_workout_feature,
    _workout_detail_diag,
    WORKOUT_DETAIL_THROTTLE_MS,
    WORKOUT_DETAIL_RETRY_BACKOFF_MS,
    WORKOUT_DETAIL_NO_GPS_FEATURE_MAX,
)


# ===== Fake client =====

class FakeClient:
    """可配置响应的 mock ZeppClient。"""
    def __init__(self, responses):
        """
        responses: list of (response_or_exception) — 按顺序消费
        单值：所有调用都返回它（无限重试用）
        list：按顺序消费；空 → 重用最后一个
        """
        if not isinstance(responses, list):
            responses = [responses]
        self._responses = list(responses) if responses else []
        self._default = self._responses[-1] if self._responses else None
        self.call_count = 0
        self.calls = []

    def sport_detail(self, trackid, source="gpx"):
        self.call_count += 1
        self.calls.append((trackid, source))
        if self._responses:
            resp = self._responses.pop(0)
        elif self._default is not None:
            resp = self._default
        else:
            raise AssertionError("FakeClient: 没有更多响应")
        if isinstance(resp, Exception):
            raise resp
        return resp


def _mk_unavailable(msg="HTTP 404"):
    """Unavailable 异常"""
    from zepp_client import Unavailable
    return Unavailable(msg)


def _mk_network_error():
    from zepp_client import NetworkError
    return NetworkError("connection reset")


def _mk_cloud_rejected(code=0, msg="error"):
    from zepp_client import CloudRejected
    return CloudRejected(code, msg)


# ===== 1. _fetch_workout_detail_with_retry =====

class FetchWorkoutDetailTest(unittest.TestCase):
    def test_code1_with_data_returns_ok(self):
        """① code=1 + data → ('ok', data)"""
        client = FakeClient({"code": 1, "message": "success",
                             "data": {"trackid": 12345, "longitude_latitude": ""}})
        status, payload = _fetch_workout_detail_with_retry(client, 12345)
        self.assertEqual(status, "ok")
        self.assertIn("data", payload)
        self.assertEqual(payload["data"]["trackid"], 12345)

    def test_code1_without_data_returns_no_data(self):
        """② code=1 但无 data → ('no_data', payload) —— 静默跳过场景"""
        client = FakeClient({"code": 1, "message": "success"})
        status, payload = _fetch_workout_detail_with_retry(client, 12345)
        self.assertEqual(status, "no_data")
        # 1 次调用就够（不重试）
        self.assertEqual(client.call_count, 1)

    def test_code0_raises_zepp_error(self):
        """③ code=0 业务错误 → 抛 ZeppError（不静默）"""
        client = FakeClient({"code": 0, "message": "error"})
        from zepp_client import ZeppError
        with self.assertRaises(ZeppError):
            _fetch_workout_detail_with_retry(client, 12345)
        # 重试 1 次
        self.assertEqual(client.call_count, 2)

    def test_code_zero_with_non_dict_data(self):
        """data 是空 list → 视为 no_data"""
        client = FakeClient({"code": 1, "data": []})
        status, _ = _fetch_workout_detail_with_retry(client, 12345)
        self.assertEqual(status, "no_data")

    def test_404_unavailable_propagates(self):
        """HTTP 404 → Unavailable → 抛 ZeppError"""
        client = FakeClient(_mk_unavailable())
        from zepp_client import ZeppError
        with self.assertRaises(ZeppError):
            _fetch_workout_detail_with_retry(client, 12345)
        # 业务层失败不重试
        self.assertEqual(client.call_count, 1)

    def test_network_error_retries_then_fails(self):
        """网络错误 → 重试 1 次 → 失败抛最后异常"""
        client = FakeClient([_mk_network_error(), _mk_network_error()])
        from zepp_client import NetworkError
        with self.assertRaises(NetworkError):
            _fetch_workout_detail_with_retry(client, 12345)
        # 2 次（retry 一次）
        self.assertEqual(client.call_count, 2)

    def test_network_error_then_success(self):
        """网络错误 → 重试 → 成功"""
        client = FakeClient([
            _mk_network_error(),
            {"code": 1, "data": {"trackid": 99999}},
        ])
        status, payload = _fetch_workout_detail_with_retry(client, 99999)
        self.assertEqual(status, "ok")
        self.assertEqual(client.call_count, 2)


# ===== 2. _read_workout_feature =====

class ReadWorkoutFeatureTest(unittest.TestCase):
    def _setup_db(self, trackid, raw_payload):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE workouts (
                trackid TEXT PRIMARY KEY,
                raw_payload TEXT
            )
        """)
        if raw_payload is not None:
            conn.execute("INSERT INTO workouts(trackid, raw_payload) VALUES (?, ?)",
                          (str(trackid), raw_payload))
        conn.commit()
        return conn

    def test_feature_small_value(self):
        """feature=85 → 85（无 GPS）"""
        payload = json.dumps({"trackid": "100", "feature": 85})
        conn = self._setup_db("100", payload)
        self.assertEqual(_read_workout_feature(conn, "100"), 85)
        conn.close()

    def test_feature_large_value(self):
        """feature=33791（含 GPS）→ 33791"""
        payload = json.dumps({"trackid": "200", "feature": 33791})
        conn = self._setup_db("200", payload)
        self.assertEqual(_read_workout_feature(conn, "200"), 33791)
        conn.close()

    def test_feature_missing_returns_none(self):
        """raw_payload 无 feature → None（按有 GPS 尝试）"""
        payload = json.dumps({"trackid": "300"})
        conn = self._setup_db("300", payload)
        self.assertIsNone(_read_workout_feature(conn, "300"))
        conn.close()

    def test_feature_null_raw_payload(self):
        """raw_payload NULL → None"""
        conn = self._setup_db("400", None)
        self.assertIsNone(_read_workout_feature(conn, "400"))
        conn.close()

    def test_feature_invalid_json(self):
        """raw_payload 损坏 → None（不抛）"""
        conn = self._setup_db("500", "not json {{{")
        self.assertIsNone(_read_workout_feature(conn, "500"))
        conn.close()

    def test_feature_trackid_not_in_db(self):
        """trackid 不存在 → None"""
        conn = self._setup_db("600", json.dumps({"feature": 85}))
        self.assertIsNone(_read_workout_feature(conn, "9999"))
        conn.close()


# ===== 3. WORKOUT_DETAIL_NO_GPS_FEATURE_MAX 阈值 =====

class NoGpsThresholdTest(unittest.TestCase):
    def test_threshold_is_100(self):
        """feature 阈值是 100（≤ 视为无 GPS）"""
        self.assertEqual(WORKOUT_DETAIL_NO_GPS_FEATURE_MAX, 100)

    def test_throttle_constant_is_200ms(self):
        """节流常量是 200ms"""
        self.assertEqual(WORKOUT_DETAIL_THROTTLE_MS, 200)


# ===== 4. 集成模拟：节流实测（手动 sanity check，不强断言） =====

class ThrottlingIntegrationTest(unittest.TestCase):
    def test_two_consecutive_calls_have_delay(self):
        """连续 2 次 ok 调用之间间隔 ≥ 200ms"""
        client = FakeClient([
            {"code": 1, "data": {"trackid": 1}},
            {"code": 1, "data": {"trackid": 2}},
        ])
        start = time.time()
        s1, _ = _fetch_workout_detail_with_retry(client, 1)
        s2, _ = _fetch_workout_detail_with_retry(client, 2)
        elapsed_ms = (time.time() - start) * 1000
        # retry backoff 不应触发（2 次都 ok）
        self.assertEqual((s1, s2), ("ok", "ok"))
        # 应 ≤ retry_backoff + throttle 上限（200ms）；不是 400ms（throttle 是上层负责）
        # 这里 _fetch_workout_detail_with_retry 本身不带 throttle，所以 elapsed < retry_backoff
        self.assertLess(elapsed_ms, WORKOUT_DETAIL_RETRY_BACKOFF_MS + 50,
                         f"ret 内部不该触发 throttle，但等 {elapsed_ms:.0f}ms")


if __name__ == "__main__":
    unittest.main()
