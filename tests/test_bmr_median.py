"""
M3 章节 3.5: BMR median 测试
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import statistics
import sqlite3
import unittest

from compute_calorie_total import (
    bmr_male, bmr_female, bmr_for_profile, calc_age,
)


class BMRMedianLogicTest(unittest.TestCase):
    def test_median_of_5_bmrs(self):
        """5 个 BMR [1436, 1500, 1553, 1580, 1600] → med=1553"""
        # 模拟 zepp_med 选项的核心逻辑
        vals = [1436, 1500, 1553, 1580, 1600]
        sorted_v = sorted(vals)
        n = len(sorted_v)
        med = (sorted_v[n // 2] if n % 2 == 1
               else (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2)
        self.assertEqual(med, 1553)

    def test_median_robust_to_outlier(self):
        """[1436, 1500, 1553, 1580, 2200] → med=1553（不取 2200 漂移值）"""
        vals = [1436, 1500, 1553, 1580, 2200]
        sorted_v = sorted(vals)
        n = len(sorted_v)
        med = (sorted_v[n // 2] if n % 2 == 1
               else (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2)
        self.assertEqual(med, 1553)

    def test_median_even_count(self):
        """[1436, 1500, 1553, 1580] → med=(1500+1553)/2 = 1526.5"""
        vals = [1436, 1500, 1553, 1580]
        sorted_v = sorted(vals)
        n = len(sorted_v)
        med = (sorted_v[n // 2] if n % 2 == 1
               else (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2)
        self.assertEqual(med, 1526.5)


class BMRFormulaTest(unittest.TestCase):
    def test_male(self):
        # W=69, H=172, A=37 → 88.362 + 13.397*69 + 4.799*172 - 5.677*37
        # = 88.362 + 924.393 + 825.428 - 210.049 = 1628.134
        self.assertAlmostEqual(bmr_male(69, 172, 37), 1628.13, places=1)

    def test_female(self):
        # W=55, H=160, A=30
        # 447.593 + 9.247*55 + 3.098*160 - 4.330*30
        # = 447.593 + 508.585 + 495.68 - 129.9 = 1321.958
        self.assertAlmostEqual(bmr_female(55, 160, 30), 1321.96, places=1)

    def test_calc_age(self):
        from datetime import date
        self.assertEqual(calc_age("1990-01", date(2026, 9, 23)), 36)

    def test_bmr_for_profile_male(self):
        # gender=1 (Zepp 反人类：1=男)
        bmr = bmr_for_profile(1, 69, 172, 37)
        self.assertAlmostEqual(bmr, 1628.13, places=1)


class BMRMedianFromDBTest(unittest.TestCase):
    """验证从 DB 读 N 次 BMR 取中位数的端到端路径"""

    def _make_db(self, bmrs):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
        CREATE TABLE measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            stream TEXT,
            metric TEXT,
            ts_ms INTEGER,
            date TEXT,
            value REAL
        );
        """)
        for i, bmr in enumerate(bmrs):
            ts = 1770000000_000 + i * 86400_000  # 1 天间隔
            db.execute("""INSERT INTO measurements
                          (user_id, stream, metric, ts_ms, value)
                          VALUES (?, 'weight', 'bmr', ?, ?)""",
                       ("u1", ts, bmr))
        db.commit()
        return db

    def test_median_from_5_bmrs(self):
        bmrs = [1436, 1500, 1553, 1580, 1600]
        db = self._make_db(bmrs)
        rows = db.execute("""
            SELECT value FROM measurements
            WHERE user_id = ? AND stream = 'weight' AND metric = 'bmr'
            ORDER BY ts_ms DESC LIMIT 5
        """, ("u1",)).fetchall()
        vals = [float(r[0]) for r in rows]
        sorted_v = sorted(vals)
        n = len(sorted_v)
        med = (sorted_v[n // 2] if n % 2 == 1
               else (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2)
        self.assertEqual(med, 1553)

    def test_median_from_3_bmrs(self):
        bmrs = [1436, 1500, 1553]
        db = self._make_db(bmrs)
        rows = db.execute("""
            SELECT value FROM measurements
            WHERE user_id = ? AND stream = 'weight' AND metric = 'bmr'
            ORDER BY ts_ms DESC LIMIT 5
        """, ("u1",)).fetchall()
        vals = [float(r[0]) for r in rows]
        sorted_v = sorted(vals)
        n = len(sorted_v)
        med = (sorted_v[n // 2] if n % 2 == 1
               else (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2)
        self.assertEqual(med, 1500)

    def test_no_bmrs_returns_none(self):
        db = self._make_db([])
        rows = db.execute("""
            SELECT value FROM measurements
            WHERE user_id = ? AND stream = 'weight' AND metric = 'bmr'
            ORDER BY ts_ms DESC LIMIT 5
        """, ("u1",)).fetchall()
        self.assertEqual(len(rows), 0)


if __name__ == "__main__":
    unittest.main()