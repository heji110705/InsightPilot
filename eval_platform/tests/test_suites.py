"""套件层的单测：金标归一化、取样、请求体、响应解析、判分、真值结果集。

硬约束也在这里钉着：
  * eval_platform 不许 import DocuMind / InsightPilot 的任何模块（要能同时服务两个项目）
  * 两个套件的地址与路径是需求里写死的，不许"顺手改一下"
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from eval_platform import suites  # noqa: E402
from eval_platform.schema import Item  # noqa: E402


class TestSuiteRegistry(unittest.TestCase):
    def test_endpoints_are_the_required_ones(self):
        self.assertEqual(suites.DOCUMIND.base_url, "http://8.163.90.97")
        self.assertEqual(suites.DOCUMIND.ask_path, "/v1/ask")
        self.assertEqual(suites.INSIGHT_PILOT.base_url, "http://8.163.90.97:8001")
        self.assertEqual(suites.INSIGHT_PILOT.ask_path, "/api/ask")

    def test_unknown_suite_exits(self):
        with self.assertRaises(SystemExit):
            suites.get("nope")

    def test_no_project_imports(self):
        """本包要能整体搬进任意仓库：任何一句 import 项目模块都会让它只在原地能跑。"""
        pkg = ROOT / "eval_platform"
        bad = []
        for f in pkg.rglob("*.py"):
            for no, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if re.match(r"^\s*(from|import)\s+app(\.|\s|$)", line):
                    bad.append(f"{f.relative_to(pkg)}:{no}: {line.strip()}")
        self.assertEqual(bad, [], f"eval_platform 里出现了项目模块的 import：{bad}")


class TestLoadGold(unittest.TestCase):
    def _tmp(self) -> Path:
        return Path(tempfile.mkdtemp())

    def test_json_object_format(self):
        d = self._tmp()
        (d / "gold.json").write_text(json.dumps({"items": [
            {"id": "Q1", "type": "fact", "q": "粒度?", "must_include": [["一单一书"]],
             "task": "口径"},
            {"id": "Q9", "type": "refuse", "q": "对比?", "must_absent": ["更高"]},
        ]}, ensure_ascii=False), encoding="utf-8")
        _, items = suites.load_gold(suites.DOCUMIND, "gold.json", d)
        self.assertEqual([i["id"] for i in items], ["Q1", "Q9"])
        self.assertEqual(items[0]["question"], "粒度?")
        self.assertEqual(items[0]["must_include"], [["一单一书"]])
        self.assertEqual(items[1]["must_absent"], ["更高"])

    def test_jsonl_format(self):
        d = self._tmp()
        (d / "gold.jsonl").write_text(
            '{"id": "Q01", "type": "T1", "question": "多少本书", "sql": "SELECT 1", "note": "计数"}\n',
            encoding="utf-8")
        _, items = suites.load_gold(suites.INSIGHT_PILOT, "gold.jsonl", d)
        self.assertEqual(items[0]["question"], "多少本书")
        self.assertEqual(items[0]["sql"], "SELECT 1")
        self.assertEqual(items[0]["task"], "计数")

    def test_bad_json_line_exits(self):
        d = self._tmp()
        (d / "bad.jsonl").write_text("{oops\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            suites.load_gold(suites.INSIGHT_PILOT, "bad.jsonl", d)

    def test_missing_gold_exits(self):
        with self.assertRaises(SystemExit):
            suites.load_gold(suites.INSIGHT_PILOT, None, Path(tempfile.mkdtemp()))


class TestSample(unittest.TestCase):
    def test_documind_keeps_refuse_share(self):
        items = ([{"id": f"F{i}", "type": "fact"} for i in range(94)]
                 + [{"id": f"R{i}", "type": "refuse"} for i in range(30)])
        picked = suites.sample(suites.DOCUMIND, items, 20)
        self.assertEqual(len(picked), 20)
        self.assertEqual(sum(1 for x in picked if x["type"] == "refuse"), 4)  # 20*30/124≈4

    def test_insightpilot_samples_across_types(self):
        """金标按类型分块排列，取前 N 会只拿到 T1/T2，分数会虚高。"""
        items = [{"id": f"{t}{i}", "type": t}
                 for t in ("T1", "T2", "T3", "T4", "T5", "T6") for i in range(10)]
        picked = suites.sample(suites.INSIGHT_PILOT, items, 20)
        self.assertEqual(len(picked), 20)
        per_type = {t: sum(1 for x in picked if x["type"] == t) for t in ("T1", "T2", "T3", "T4", "T5", "T6")}
        self.assertEqual(per_type, {"T1": 4, "T2": 4, "T3": 3, "T4": 3, "T5": 3, "T6": 3})

    def test_documind_spreads_across_the_whole_gold_set(self):
        """不能只取前 16 个 fact：那 16 题全是 schema/口径/陷阱题，比全量难 18pp，
        门禁会因此对"文档类问题变差"完全失明。"""
        items = ([{"id": f"F{i:03d}", "type": "fact"} for i in range(94)]
                 + [{"id": f"R{i:03d}", "type": "refuse"} for i in range(30)])
        picked = suites.sample(suites.DOCUMIND, items, 20)
        facts = [x["id"] for x in picked if x["type"] == "fact"]
        self.assertEqual(len(facts), 16)
        self.assertNotEqual(facts, [f"F{i:03d}" for i in range(16)])
        self.assertGreaterEqual(int(facts[-1][1:]), 80)   # 末题落在金标后段

    def test_insightpilot_spreads_inside_each_type(self):
        items = [{"id": f"{t}{i}", "type": t}
                 for t in ("T1", "T2", "T3", "T4", "T5", "T6") for i in range(10)]
        picked = suites.sample(suites.INSIGHT_PILOT, items, 6)
        # 每类各 1 题，且取的是层内等距位置（0 / 10 步长的整数倍），不是各自第 0 题
        for t in ("T1", "T2", "T3", "T4", "T5", "T6"):
            got = [x["id"] for x in picked if x["type"] == t]
            self.assertEqual(got, [f"{t}0"])

    def test_limit_bigger_than_items_returns_all(self):
        items = [{"id": "Q1", "type": "fact"}]
        self.assertEqual(suites.sample(suites.DOCUMIND, items, 50), items)

    def test_only_filters_type(self):
        items = [{"id": "Q1", "type": "fact"}, {"id": "Q2", "type": "refuse"}]
        self.assertEqual([x["id"] for x in suites.sample(suites.DOCUMIND, items, 0, "refuse")], ["Q2"])


class TestBodyAndParse(unittest.TestCase):
    def test_documind_body_carries_mode(self):
        body = suites.build_body(suites.DOCUMIND, {"question": "q"}, {"mode": "mix"})
        self.assertEqual(body, {"question": "q", "mode": "mix"})

    def test_insightpilot_body_has_no_mode(self):
        body = suites.build_body(suites.INSIGHT_PILOT, {"question": "q"}, {"mode": "mix"})
        self.assertEqual(body, {"question": "q", "session_id": ""})

    def test_parse_documind(self):
        p = suites.parse(suites.DOCUMIND, {
            "answer": "粒度是一单一书", "refused": False, "elapsed_ms": 1200,
            "cost_cny": 0.001, "tokens": 900,
            "chunks": [{"snippet": "片段A"}, {"snippet": "片段B"}],
            "citations": [1],
        })
        self.assertEqual(p["answer"], "粒度是一单一书")
        self.assertEqual(p["contexts"], ["片段A", "片段B"])
        self.assertEqual(p["cost_cny"], 0.001)

    def test_parse_insightpilot(self):
        p = suites.parse(suites.INSIGHT_PILOT, {
            "answer": {"sql": "SELECT COUNT(*) FROM dim_book", "columns": ["书籍数"],
                       "rows": [[250]], "status": "ok", "elapsed_ms": 4321,
                       "insight": {"headline": "库存 250 本"}},
            "report": {"status": "ok", "path": ["intake", "executor", "executor", "reporter"]},
        })
        self.assertEqual(p["columns"], ["书籍数"])
        self.assertEqual(p["rows"], [[250]])
        self.assertEqual(p["answer"], "库存 250 本")
        self.assertEqual(p["attempts"], 2)
        self.assertEqual(p["elapsed_ms"], 4321)
        self.assertIsNone(p["cost_cny"])       # 接口不返回成本，不许编一个 0 出来

    def test_parse_insightpilot_without_insight(self):
        p = suites.parse(suites.INSIGHT_PILOT, {"answer": {"columns": ["a"], "rows": [[1]]}})
        self.assertEqual(p["answer"], "1 行 × 1 列")


class TestJudge(unittest.TestCase):
    def test_keyword_judge(self):
        it = Item(id="Q1", type="fact")
        suites.judge(suites.DOCUMIND, it, {"must_include": [["28%"], ["订单"]], "must_absent": []},
                     {"answer": "订单占 28%", "refused": False}, None)
        self.assertTrue(it.kw_hit)
        self.assertFalse(it.kw_leak)
        self.assertTrue(it.passed)

    def test_keyword_leak_fails(self):
        it = Item(id="Q1", type="fact")
        suites.judge(suites.DOCUMIND, it, {"must_include": [["28%"]], "must_absent": ["更高"]},
                     {"answer": "订单更高，占 28%", "refused": False}, None)
        self.assertTrue(it.kw_hit)
        self.assertTrue(it.kw_leak)
        self.assertFalse(it.passed)

    def test_refuse_judge(self):
        it = Item(id="Q9", type="refuse")
        suites.judge(suites.DOCUMIND, it, {"must_include": [], "must_absent": []},
                     {"answer": "证据不足", "refused": True}, None)
        self.assertTrue(it.refuse_ok)
        self.assertTrue(it.passed)

    def test_resultset_exact(self):
        it = Item(id="Q01", type="T1")
        gold = {"Q01": suites.ResultSet(columns=["书籍数"], rows=[[250]])}
        suites.judge(suites.INSIGHT_PILOT, it, {"id": "Q01", "sql": "SELECT 250"},
                     {"columns": ["n"], "rows": [[250]], "sql": "SELECT COUNT(*)",
                      "status": "ok", "attempts": 1, "cached": False, "model": "deepseek-chat"},
                     gold)
        self.assertTrue(it.kw_hit)              # ex → kw_hit，于是 passed/可答率直接可用
        self.assertTrue(it.passed)
        self.assertTrue(it.judge["ex"])
        self.assertTrue(it.judge["cover"])
        self.assertEqual(it.judge["diff"], "结果一致")

    def test_resultset_extra_column_counts_as_cover_only(self):
        it = Item(id="Q01", type="T1")
        gold = {"Q01": suites.ResultSet(columns=["书籍数"], rows=[[250]])}
        suites.judge(suites.INSIGHT_PILOT, it, {"id": "Q01", "sql": "SELECT 250"},
                     {"columns": ["书籍数", "备注"], "rows": [[250, "x"]], "sql": "",
                      "status": "ok", "attempts": 1, "cached": False, "model": ""},
                     gold)
        self.assertFalse(it.passed)             # 严格判定不过
        self.assertTrue(it.judge["cover"])      # 覆盖判定通过
        self.assertIn("多出 1 列", it.judge["cover_diff"])

    def test_resultset_wrong_value(self):
        it = Item(id="Q01", type="T1")
        gold = {"Q01": suites.ResultSet(columns=["书籍数"], rows=[[250]])}
        suites.judge(suites.INSIGHT_PILOT, it, {"id": "Q01", "sql": "SELECT 250"},
                     {"columns": ["书籍数"], "rows": [[251]], "sql": "",
                      "status": "ok", "attempts": 1, "cached": False, "model": ""},
                     gold)
        self.assertFalse(it.passed)
        self.assertFalse(it.judge["cover"])


class TestGoldResults(unittest.TestCase):
    def _suite(self, db: Path) -> suites.Suite:
        return suites.Suite(name="t", kind="resultset", base_url="", ask_path="",
                            gold_candidates=(), db_candidates=(db.name,), default_sleep=0)

    def test_executes_sql_readonly(self):
        d = Path(tempfile.mkdtemp())
        db = d / "w.sqlite"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        con.executemany("INSERT INTO t VALUES (?,?)", [(1, "x"), (2, "y")])
        con.commit()
        con.close()

        got = suites.load_gold_results(self._suite(db), [{"id": "Q1", "sql": "SELECT * FROM t ORDER BY a"}], d)
        self.assertEqual(got["Q1"].columns, ["a", "b"])
        self.assertEqual(got["Q1"].rows, [[1, "x"], [2, "y"]])

    def test_bad_sql_exits(self):
        d = Path(tempfile.mkdtemp())
        db = d / "w.sqlite"
        sqlite3.connect(db).close()
        with self.assertRaises(SystemExit):
            suites.load_gold_results(self._suite(db), [{"id": "Q1", "sql": "SELECT * FROM nope"}], d)

    def test_keyword_suite_needs_no_db(self):
        self.assertIsNone(suites.load_gold_results(suites.DOCUMIND, [], Path(".")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
