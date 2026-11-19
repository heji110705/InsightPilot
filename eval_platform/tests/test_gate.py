"""门禁的单测：阈值、分母护栏、套件护栏、报告措辞。

门禁是这份代码里唯一会让 CI 变红的东西，所以它的边界必须逐条写死：
  * 掉 **超过** 2pp 才失败（正好 2pp 通过）
  * 有效题数缩水 → 判"不可比"，不许拿缩小的分母算出一个好看的百分比
  * 两个套件的分数不许比
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from eval_platform import gate  # noqa: E402
from eval_platform.schema import Item, Run  # noqa: E402


def _run(n=20, n_pass=20, suite="documind", n_error=0, cover=None):
    items = [Item(id=f"Q{i:03d}", type="fact", question=f"q{i}", answer=f"a{i}",
                  kw_hit=(i <= n_pass))
             for i in range(1, n + 1)]
    for i, it in enumerate(items[-n_error:] if n_error else []):
        it.error = "HTTP 504"
        it.kw_hit = None
    if cover is not None:
        for it, ok in zip(items, cover):
            it.judge = {"ex": bool(it.kw_hit), "cover": ok}
    return Run(suite=suite, tag="t", created_at="2026-11-19T10:00:00", items=items)


class TestCheck(unittest.TestCase):
    def test_no_drop_passes(self):
        ok, text = gate.check(_run(), _run())
        self.assertTrue(ok)
        self.assertIn("✅ 通过", text)

    def test_exactly_two_pp_passes(self):
        """「掉超过 2pp」——正好 2pp 不失败。50 题里差 1 题。"""
        base = _run(n=50, n_pass=50)
        new = _run(n=50, n_pass=49)          # 100% → 98% = 2pp
        ok, _ = gate.check(base, new, max_drop_pp=2.0)
        self.assertTrue(ok)

    def test_over_threshold_fails(self):
        base = _run(n=50, n_pass=50)
        new = _run(n=50, n_pass=48)          # 100% → 96% = 4pp
        ok, text = gate.check(base, new, max_drop_pp=2.0)
        self.assertFalse(ok)
        self.assertIn("❌ 不通过", text)
        self.assertIn("超过阈值", text)

    def test_one_flip_in_twenty_fails(self):
        """20 题的 run 一题就是 5pp —— 这不是 bug，是 20 题这个样本量的算术事实。"""
        ok, text = gate.check(_run(n=20, n_pass=20), _run(n=20, n_pass=19))
        self.assertFalse(ok)
        self.assertIn("新错", text)
        self.assertIn("Q020", text)

    def test_error_shrunk_denominator_is_not_comparable(self):
        base = _run(n=20, n_pass=16)                     # 80%
        new = _run(n=20, n_pass=10, n_error=12)          # 8 题有效，10 题失败
        ok, text = gate.check(base, new)
        self.assertFalse(ok)
        self.assertIn("不可比", text)
        self.assertIn("n_error", text)

    def test_small_error_drop_still_compares(self):
        base = _run(n=20, n_pass=20)
        new = _run(n=20, n_pass=19, n_error=1)           # 19 题有效，1 题失败
        ok, text = gate.check(base, new)
        # 分母只少了 1 题（19/20 = 95% ≥ 80%），照常比：19/19 = 100%，不掉
        self.assertTrue(ok)

    def test_suite_mismatch_fails(self):
        ok, text = gate.check(_run(suite="documind"), _run(suite="insightpilot"))
        self.assertFalse(ok)
        self.assertIn("套件不同", text)

    def test_missing_rate_fails(self):
        base = Run(suite="documind", items=[Item(id="Q1", type="refuse", refuse_ok=True)])
        ok, text = gate.check(base, base)
        self.assertFalse(ok)
        self.assertIn("没有可答率", text)

    def test_cover_rate_is_reported(self):
        base = _run(cover=[True] * 20)
        new = _run(n_pass=19, cover=[True] * 20)
        _, text = gate.check(base, new)
        self.assertIn("列覆盖率", text)

    def test_cover_rate_absent_for_keyword_suite(self):
        _, text = gate.check(_run(), _run())
        self.assertNotIn("列覆盖率", text)


class TestDropNRule(unittest.TestCase):
    """20 题的 CI 用「掉几题」，不用 pp：16 道可答题里掉 1 题就是 6.25pp。"""

    def test_one_question_down_passes(self):
        base = _run(n=20, n_pass=14)      # 14/16 可答
        new = _run(n=20, n_pass=13)       # 13/16
        ok, text = gate.check(base, new, max_drop_n=2)
        self.assertTrue(ok)
        self.assertIn("未跌满 2 题", text)

    def test_two_questions_down_fails(self):
        base = _run(n=20, n_pass=14)
        new = _run(n=20, n_pass=12)
        ok, text = gate.check(base, new, max_drop_n=2)
        self.assertFalse(ok)
        self.assertIn("掉 2 题", text)

    def test_same_pp_drop_but_different_n(self):
        """同一条 pp 阈值会误伤：掉 1 题算 6.25pp，用 --max-drop 2.0 必然红。"""
        base = _run(n=20, n_pass=14)
        new = _run(n=20, n_pass=13)
        ok_pp, _ = gate.check(base, new, max_drop_pp=2.0)
        ok_n, _ = gate.check(base, new, max_drop_n=2)
        self.assertFalse(ok_pp)
        self.assertTrue(ok_n)

    def test_both_thresholds_is_an_error(self):
        with self.assertRaises(ValueError):
            gate.check(_run(), _run(), max_drop_pp=2.0, max_drop_n=2)

    def test_default_is_still_2pp(self):
        ok, text = gate.check(_run(n=50, n_pass=50), _run(n=50, n_pass=48))
        self.assertFalse(ok)
        self.assertIn("超过阈值 2.00pp", text)

    def test_cli_rejects_both_flags(self):
        from eval_platform import cli
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["gate", "a.json", "b.json",
                                           "--max-drop", "2", "--max-drop-n", "2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
