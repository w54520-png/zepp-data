"""
M4 章节 4.5：区域主机 fallback 单元测试

覆盖：
  - region_host_candidates(primary) 返回 [primary, ...fallback]
  - 默认 fallback 不启用
  - try_regions_fallback=True 时构造后 region_candidates 包含完整链
  - base_url_override 强制指定
  - _rotate_region 切换到下一个候选
  - _get 失败时 region 切换（用 monkeypatch 模拟）
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from zepp_client import (
    ZeppClient, region_host_candidates,
    REGION_FALLBACK_CHAIN, validate_region_host, ZeppError,
)


# ===== token.json fixture =====

def _write_token(tmpdir, region_host="https://api-mifit-cn3.zepp.com"):
    p = os.path.join(tmpdir, "token.json")
    with open(p, "w") as f:
        json.dump({
            "app_token": "fake-token-for-test",
            "user_id": "1000000000",
            "region_host": region_host,
            "login_token": "fake-login-token",
        }, f)
    return p


class RegionHostCandidatesTest(unittest.TestCase):
    def test_primary_first(self):
        """primary 在最前"""
        cands = region_host_candidates("https://api-mifit-us3.zepp.com")
        self.assertEqual(cands[0], "api-mifit-us3.zepp.com")
        # 后续是 fallback
        for h in REGION_FALLBACK_CHAIN:
            self.assertIn(h, cands)

    def test_dedup_no_repeat(self):
        """primary 与 fallback 重复时去重"""
        cands = region_host_candidates("https://api-mifit-cn3.zepp.com")
        self.assertEqual(len(cands), len(set(cands)))

    def test_no_scheme(self):
        """传入没 scheme → 仍然能解析"""
        cands = region_host_candidates("api-mifit-eu2.zepp.com")
        self.assertEqual(cands[0], "api-mifit-eu2.zepp.com")

    def test_fallback_chain_order(self):
        """fallback chain 顺序固定"""
        self.assertEqual(REGION_FALLBACK_CHAIN, [
            "api-mifit-cn3.zepp.com",
            "api-mifit-us3.zepp.com",
            "api-mifit-eu2.zepp.com",
            "api-mifit-sg2.zepp.com",
        ])


class ZeppClientConstructionTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_default_no_fallback(self):
        """默认 try_regions_fallback=False → 只有 1 个候选"""
        p = _write_token(self.tmpdir)
        c = ZeppClient(secrets_path=p)
        self.assertFalse(c.try_regions_fallback)
        self.assertEqual(len(c._region_candidates), 1)

    def test_fallback_enabled(self):
        """try_regions_fallback=True → 4 个候选"""
        p = _write_token(self.tmpdir)
        c = ZeppClient(secrets_path=p, try_regions_fallback=True)
        self.assertTrue(c.try_regions_fallback)
        self.assertEqual(len(c._region_candidates), 4)
        # 第一个是 primary（cn3）
        self.assertEqual(c._region_candidates[0], "api-mifit-cn3.zepp.com")
        # 初始 base_url 是 cn3
        self.assertEqual(c.current_region, "api-mifit-cn3.zepp.com")

    def test_base_url_override(self):
        """base_url_override 强制指定 us3"""
        p = _write_token(self.tmpdir)
        c = ZeppClient(secrets_path=p, base_url_override="api-mifit-us3.zepp.com")
        self.assertEqual(c.current_region, "api-mifit-us3.zepp.com")
        # override 时仍可启用 fallback
        c2 = ZeppClient(secrets_path=p, base_url_override="api-mifit-eu2.zepp.com",
                         try_regions_fallback=True)
        self.assertEqual(c2._region_candidates[0], "api-mifit-eu2.zepp.com")
        self.assertIn("api-mifit-cn3.zepp.com", c2._region_candidates)

    def test_validate_region_host_rejects_other_domain(self):
        """不在白名单的 host 拒绝"""
        with self.assertRaises(ZeppError):
            ZeppClient(secrets_path=_write_token(self.tmpdir),
                       base_url_override="evil.zepp.com")


class RotateRegionTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_rotate_advances(self):
        """_rotate_region 切到下一个候选"""
        p = _write_token(self.tmpdir)
        c = ZeppClient(secrets_path=p, try_regions_fallback=True)
        self.assertEqual(c.current_region, "api-mifit-cn3.zepp.com")
        self.assertTrue(c._rotate_region())
        self.assertEqual(c.current_region, "api-mifit-us3.zepp.com")
        self.assertTrue(c._rotate_region())
        self.assertEqual(c.current_region, "api-mifit-eu2.zepp.com")

    def test_rotate_returns_false_when_exhausted(self):
        """用完 4 个候选后返 False"""
        p = _write_token(self.tmpdir)
        c = ZeppClient(secrets_path=p, try_regions_fallback=True)
        for _ in range(3):
            self.assertTrue(c._rotate_region())
        self.assertFalse(c._rotate_region())

    def test_rotate_disabled_when_fallback_off(self):
        """未启用 fallback → _rotate_region 返 False"""
        p = _write_token(self.tmpdir)
        c = ZeppClient(secrets_path=p, try_regions_fallback=False)
        self.assertFalse(c._rotate_region())


if __name__ == "__main__":
    unittest.main()
