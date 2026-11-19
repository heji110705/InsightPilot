"""评测中台的核心单测。

这个包的价值全在「判据稳定」上：如果 hit() 的语义被悄悄改动，或者 metrics()
的口径变了，历史 run 之间就不再可比，所有趋势图都是假的。所以判据必须被钉住。

跑法：
    python eval_platform/tests/test_core.py
    python -m unittest discover -s eval_platform/tests -t .
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from eval_platform import compare, runner, schema  # noqa: E402
from eval_platform.schema import Item, Run  # noqa: E402


class TestHit(unittest.TestCase):
    """关键词判定：组间 AND、组内 OR。这条语义一改，所有历史分数作废。"""

    def test_empty_groups_is_true(self):
        self.assertTrue(runner.hit("任何回答", []))

    def test_single_group_single_alt(self):
        self.assertTrue(runner.hit("粒度是一单一书", [["一单一书"]]))
        self.assertFalse(runner.hit("粒度是订单", [["一单一书"]]))

    def test_group_inner_is_or(self):
        # 组内任一命中即算这组过
        self.assertTrue(runner.hit("这是一单一书一行", [["一单一书", "订单行"]]))
        self.assertTrue(runner.hit("这是订单行", [["一单一书", "订单行"]]))

    def test_across_groups_is_and(self):
        # 两组必须都命中：只说 28% 不说订单，不算对
        groups = [["28%"], ["订单"]]
        self.assertTrue(runner.hit("订单占到 28%", groups))
        self.assertFalse(runner.hit("占到 28%", groups))
        self.assertFalse(runner.hit("订单很多", groups))


class TestPassed(unittest.TestCase):
    def test_fact_uses_kw_hit(self):
        self.assertTrue(Item(id="Q1", type="fact", kw_hit=True).passed)
        self.assertFalse(Item(id="Q1", type="fact", kw_hit=False).passed)

    def test_fact_leak_fails_even_if_hit(self):
        it = Item(id="Q1", type="fact", kw_hit=True, kw_leak=True)
        self.assertFalse(it.passed)

    def test_refuse_uses_refuse_ok(self):
        self.assertTrue(Item(id="Q9", type="refuse", refuse_ok=True).passed)
        self.assertFalse(Item(id="Q9", type="refuse", refuse_ok=False).passed)

    def test_error_always_fails(self):
        it = Item(id="Q1", type="fact", kw_hit=True, error="HTTP 500")
        self.assertFalse(it.passed)

    def test_unknown_when_unscored(self):
        self.assertIsNone(Item(id="Q1", type="fact").passed)


def _mk(items):
    return Run(suite="documind", items=items)


class TestMetrics(unittest.TestCase):
    def test_answerable_rate_excludes_refuse(self):
        run = _mk([
            Item(id="Q1", type="fact", kw_hit=True, elapsed_ms=100, cost_cny=0.001),
            Item(id="Q2", type="fact", kw_hit=False, elapsed_ms=300, cost_cny=0.001),
            Item(id="Q9", type="refuse", refused=True, refuse_ok=True, elapsed_ms=200, cost_cny=0.001),
        ])
        m = run.metrics()
        self.assertEqual(m["n_answerable"], 2)
        self.assertEqual(m["n_answerable_pass"], 1)
        self.assertEqual(m["answerable_rate"], 0.5)   # 1/2，不是 2/3
        self.assertEqual(m["refuse_accuracy"], 1.0)

    def test_error_excluded_from_denominator(self):
        """失败题不进分母 —— 一次网络抖动不该被读成"质量下降"。"""
        run = _mk([
            Item(id="Q1", type="fact", kw_hit=True, elapsed_ms=100),
            Item(id="Q2", type="fact", error="HTTP 504"),
            Item(id="Q3", type="fact", error="timeout"),
        ])
        m = run.metrics()
        self.assertEqual(m["n_error"], 2)
        self.assertEqual(m["n_answerable"], 1)      # 只剩 Q1
        self.assertEqual(m["answerable_rate"], 1.0)  # 而不是 0.33

    def test_leak_rate_uses_valid_items(self):
        run = _mk([
            Item(id="Q1", type="fact", kw_hit=True, kw_leak=True),
            Item(id="Q2", type="fact", kw_hit=True, kw_leak=False),
            Item(id="Q3", type="fact", error="x"),
        ])
        self.assertEqual(run.metrics()["leak_rate"], 0.5)   # 1/2 有效题

    def test_p95_matches_legacy_formula(self):
        """p95 必须与旧 basic_stats 逐位一致，否则 diff 里会出现假变化。"""
        lat = list(range(1, 21))          # 20 个数，int(20*0.95)-1 = 18 → 第 19 个 = 19
        run = _mk([Item(id=f"Q{i}", type="fact", kw_hit=True, elapsed_ms=v)
                   for i, v in enumerate(lat, 1)])
        self.assertEqual(run.metrics()["latency_p95_ms"], 19)

    def test_p95_none_below_20(self):
        run = _mk([Item(id="Q1", type="fact", kw_hit=True, elapsed_ms=5)])
        self.assertIsNone(run.metrics()["latency_p95_ms"])

    def test_metrics_fallback_for_summary_only_run(self):
        """只有汇总没有逐题的老文件，metrics() 要退回读入时带进来的数。"""
        run = Run(suite="documind", items=[])
        run._metrics = {"answerable_rate": 0.7391, "n": 30}
        self.assertEqual(run.metrics()["answerable_rate"], 0.7391)

    def test_cost_is_none_when_no_item_reports_cost(self):
        """InsightPilot 的接口不返回成本：给 None，不给 ¥0。

        0 会被读成「问数不要钱」，那是另一种假数字；None 在报告里显示成「—」。
        """
        run = _mk([Item(id="Q1", type="fact", kw_hit=True, elapsed_ms=100)])
        self.assertIsNone(run.metrics()["cost_per_q_cny"])
        self.assertIsNone(run.metrics()["cost_total_cny"])

    def test_non_refuse_types_count_as_answerable(self):
        """InsightPilot 的题型是 T1..T6：只认 `fact` 会把分母清空，可答率变 None。"""
        run = _mk([
            Item(id="Q01", type="T1", kw_hit=True),
            Item(id="Q02", type="T3", kw_hit=False),
        ])
        m = run.metrics()
        self.assertEqual(m["n_answerable"], 2)
        self.assertEqual(m["answerable_rate"], 0.5)
        self.assertEqual(m["n_refuse"], 0)

    def test_cover_rate_only_when_judge_present(self):
        run = _mk([
            Item(id="Q1", type="fact", kw_hit=True, judge={"ex": True, "cover": True}),
            Item(id="Q2", type="fact", kw_hit=False, judge={"ex": False, "cover": True}),
        ])
        m = run.metrics()
        self.assertEqual(m["cover_rate"], 1.0)
        self.assertEqual(m["n_cover"], 2)
        self.assertEqual(m["answerable_rate"], 0.5)   # 覆盖不到 100% 也不许抬高可答率
        self.assertNotIn("cover_rate", _mk([Item(id="Q1", type="fact", kw_hit=True)]).metrics())


class TestLoad(unittest.TestCase):
    def _write(self, payload) -> Path:
        f = Path(tempfile.mkdtemp()) / "run.json"
        f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return f

    def test_load_legacy_with_items(self):
        f = self._write({
            "meta": {"created_at": "2026-11-18T15:08:39", "mode": "mix", "n": 2},
            "items": [
                {"id": "Q001", "type": "fact", "question": "粒度?", "answer": "一单一书",
                 "kw_hit": True, "kw_leak": False, "elapsed_ms": 1000, "cost_cny": 0.001},
                {"id": "Q095", "type": "refuse", "question": "对比?", "answer": "证据不足",
                 "refused": True, "refuse_ok": True, "elapsed_ms": 500, "cost_cny": 0.001},
            ],
        })
        r = schema.load(f)
        self.assertEqual(r.suite, "documind")
        self.assertEqual(len(r.items), 2)
        self.assertEqual(r.config.get("mode"), "mix")
        self.assertTrue(r.items[0].passed)
        self.assertTrue(r.items[1].passed)

    def test_load_legacy_summary_maps_keyword_hit(self):
        """旧评分文件的 keyword_hit 要映射成 answerable_rate，否则看板全是空值。"""
        f = self._write({
            "meta": {"created_at": "2026-11-19T00:57:42", "mode": "mix", "n": 30},
            "basic": {"n": 30, "keyword_hit": 0.7391, "refuse_accuracy": 1.0,
                      "latency_p50_ms": 1577},
            "ragas": {"faithfulness": 0.9033, "n_scored": 25},
        })
        r = schema.load(f)
        self.assertEqual(r.items, [])
        m = r.metrics()
        self.assertEqual(m["answerable_rate"], 0.7391)
        self.assertEqual(m["ragas"]["faithfulness"], 0.9033)

    def test_roundtrip_new_format(self):
        run = _mk([Item(id="Q1", type="fact", question="q", answer="a",
                        kw_hit=True, elapsed_ms=10, cost_cny=0.002)])
        run.tag = "v7"
        f = schema.save(run, Path(tempfile.mkdtemp()) / "x.json")
        back = schema.load(f)
        self.assertEqual(back.tag, "v7")
        self.assertEqual(len(back.items), 1)
        self.assertEqual(back.metrics()["answerable_rate"], 1.0)


class TestDiff(unittest.TestCase):
    def _run(self, mapping):
        return _mk([Item(id=k, type="fact", question=f"q{k}", answer=f"a{k}", kw_hit=v)
                    for k, v in mapping.items()])

    def test_classification(self):
        a = self._run({"Q1": True, "Q2": True, "Q3": False, "Q4": False})
        b = self._run({"Q1": True, "Q2": False, "Q3": True, "Q4": False, "Q5": True})
        d = compare.align(a, b)
        self.assertEqual(d["new_bad"], ["Q2"])       # 从对变错 = 退化，最该关注
        self.assertEqual(d["new_ok"], ["Q3"])        # 从错变对
        self.assertEqual(d["still_ok"], ["Q1"])
        self.assertEqual(d["still_bad"], ["Q4"])
        self.assertEqual(d["only_b"], ["Q5"])

    def test_diff_text_mentions_regression(self):
        a = self._run({"Q1": True})
        b = self._run({"Q1": False})
        txt = compare.diff_text(a, b)
        self.assertIn("新错", txt)
        self.assertIn("Q1", txt)


class TestReport(unittest.TestCase):
    def test_filters_tiny_runs(self):
        """2 题打 100% 的 run 不该进趋势，否则曲线出现假尖峰。"""
        big = _mk([Item(id=f"Q{i}", type="fact", kw_hit=(i % 2 == 0),
                        elapsed_ms=100, cost_cny=0.001) for i in range(30)])
        big.created_at = "2026-11-19T10:00:00"
        tiny = _mk([Item(id="Q1", type="fact", kw_hit=True, elapsed_ms=10)])
        tiny.created_at = "2026-11-19T11:00:00"
        html = compare.report_html([big, tiny])
        self.assertIn("50.0%", html)   # 30 题里 15 题对（i%2==0）
        self.assertIn("未展示", html)

    def test_empty_is_safe(self):
        self.assertIn("没有样本量", compare.report_html([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
