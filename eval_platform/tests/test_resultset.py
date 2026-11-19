"""结果集等价判定 + 列覆盖的单测。

这批用例是 InsightPilot 那套判据的**地基**：任何一条变红，`ex` 与 `cover` 就不能信。
用例与原仓库 `tests/test_eval_compare.py` 一一对应——判据在搬运中变没变，看这里。

跑法：
    python eval_platform/tests/test_resultset.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from eval_platform.resultset import ResultSet, compare, compare_covering  # noqa: E402


def rs(columns, rows) -> ResultSet:
    return ResultSet(columns=list(columns), rows=[list(r) for r in rows])


class TestCompare(unittest.TestCase):
    def test_identical_results_are_equal(self):
        gold = rs(["分类", "书籍数"], [["文学小说", 139], ["哲学宗教", 30]])
        self.assertEqual(compare(gold, gold), (True, "结果一致"))

    def test_row_order_is_ignored(self):
        gold = rs(["分类", "书籍数"], [["文学小说", 139], ["哲学宗教", 30]])
        pred = rs(["分类", "书籍数"], [["哲学宗教", 30], ["文学小说", 139]])
        self.assertEqual(compare(gold, pred), (True, "结果一致"))

    def test_column_names_are_ignored(self):
        gold = rs(["分类", "书籍数"], [["文学小说", 139]])
        pred = rs(["category", "cnt"], [["文学小说", 139]])
        self.assertTrue(compare(gold, pred)[0])

    def test_single_cell_count_with_different_alias(self):
        self.assertTrue(compare(rs(["书籍数"], [[250]]), rs(["COUNT(*)"], [[250]]))[0])

    def test_numeric_difference_within_tolerance_is_equal(self):
        # 容差 = max(1e-6, 1e-6*100) = 1e-4；1e-4 的差刚好落在边界内。
        self.assertTrue(compare(rs(["x"], [[100.0]]), rs(["x"], [[100.0001]]))[0])

    def test_numeric_difference_above_tolerance_is_not_equal(self):
        ok, message = compare(rs(["x"], [[100.0]]), rs(["x"], [[100.001]]))
        self.assertFalse(ok)
        self.assertIn("第 1 行第 1 列", message)

    def test_large_number_uses_relative_tolerance(self):
        # 1e9 时容差放大到 1000：差 500 等价，差 2000 不等价。
        self.assertTrue(compare(rs(["x"], [[1_000_000_000]]), rs(["x"], [[1_000_000_500]]))[0])
        self.assertFalse(compare(rs(["x"], [[1_000_000_000]]), rs(["x"], [[1_000_002_000]]))[0])

    def test_row_count_mismatch_is_not_equal(self):
        ok, message = compare(rs(["x"], [[1], [2]]), rs(["x"], [[1]]))
        self.assertFalse(ok)
        self.assertEqual(message, "行数不同：2 vs 1")

    def test_column_count_mismatch_is_not_equal(self):
        ok, message = compare(rs(["a", "b"], [[1, 2]]), rs(["a", "b", "c"], [[1, 2, 3]]))
        self.assertFalse(ok)
        self.assertEqual(message, "列数不同：2 vs 3")

    def test_none_and_empty_string_are_not_equal(self):
        ok, message = compare(rs(["x"], [[None]]), rs(["x"], [[""]]))
        self.assertFalse(ok)
        self.assertIn("NULL", message)
        self.assertIn("空串", message)

    def test_none_equals_none(self):
        self.assertTrue(compare(rs(["x"], [[None]]), rs(["x"], [[None]]))[0])

    def test_boolean_is_treated_as_number(self):
        self.assertTrue(compare(rs(["flag"], [[True], [False]]), rs(["flag"], [[1], [0]]))[0])

    def test_empty_results_are_equal(self):
        self.assertTrue(compare(rs(["x"], []), rs(["x"], []))[0])
        self.assertTrue(compare(ResultSet(), ResultSet())[0])

    def test_empty_versus_non_empty_is_not_equal(self):
        ok, message = compare(rs(["x"], []), rs(["x"], [[1]]))
        self.assertFalse(ok)
        self.assertEqual(message, "行数不同：0 vs 1")

    def test_string_whitespace_is_stripped_but_case_is_kept(self):
        self.assertTrue(compare(rs(["x"], [["  北京 "]]), rs(["x"], [["北京"]]))[0])
        self.assertFalse(compare(rs(["x"], [["ABC"]]), rs(["x"], [["abc"]]))[0])

    def test_duplicate_rows_are_compared_as_multiset(self):
        gold = rs(["x"], [[1], [1]])
        self.assertFalse(compare(gold, rs(["x"], [[1], [2]]))[0])
        self.assertTrue(compare(gold, rs(["x"], [[1], [1]]))[0])

    def test_cell_diff_message_is_human_readable_and_short(self):
        ok, message = compare(rs(["书名", "评论数"], [["活着", 38]]),
                              rs(["书名", "评论数"], [["活着", 40]]))
        self.assertFalse(ok)
        self.assertEqual(message, "第 1 行第 2 列：38 vs 40")
        self.assertLessEqual(len(message), 120)

    def test_long_string_diff_is_truncated(self):
        long_text = "存" * 200
        ok, message = compare(rs(["x"], [[long_text]]), rs(["x"], [[long_text + "在"]]))
        self.assertFalse(ok)
        self.assertLessEqual(len(message), 120)

    def test_number_and_numeric_string_are_not_equal(self):
        # 不做隐式类型转换：SQLite 返回 1 与 "1" 是两种类型。
        self.assertFalse(compare(rs(["x"], [[1]]), rs(["x"], [["1"]]))[0])

    def test_numeric_type_mixing(self):
        for gold_value, pred_value in [(0, 0.0), (1, True), (False, 0), (2.5, 2.5)]:
            with self.subTest(gold=gold_value, pred=pred_value):
                self.assertTrue(compare(rs(["x"], [[gold_value]]),
                                        rs(["x"], [[pred_value]]))[0])

    def test_accepts_dict_shaped_results(self):
        """金标真值从 sqlite 出来是 dict/list，判据不该只认某一个类。"""
        gold = {"columns": ["书籍数"], "rows": [[250]]}
        pred = {"columns": ["n"], "rows": [[250]]}
        self.assertTrue(compare(gold, pred)[0])


class TestCovering(unittest.TestCase):
    """覆盖判定回答的是「答对了但多展示了几列」，不是放宽 EX。"""

    def test_identical_results_cover(self):
        gold = rs(["分类", "书籍数"], [["文学小说", 139], ["哲学宗教", 30]])
        self.assertEqual(compare_covering(gold, gold), (True, "结果一致"))

    def test_one_extra_column_is_covered(self):
        gold = rs(["分类", "书籍数"], [["文学小说", 139], ["哲学宗教", 30]])
        pred = rs(["分类", "书籍数", "占比"],
                  [["文学小说", 139, 0.82], ["哲学宗教", 30, 0.18]])
        ok, message = compare_covering(gold, pred)
        self.assertTrue(ok)
        self.assertIn("多出 1 列", message)
        self.assertIn("占比", message)

    def test_column_order_does_not_matter(self):
        gold = rs(["书名", "评分"], [["A", 9.1], ["B", 8.7]])
        pred = rs(["评分", "备注", "书名"], [[9.1, "x", "A"], [8.7, "y", "B"]])
        self.assertTrue(compare_covering(gold, pred)[0])

    def test_fewer_columns_is_not_covered(self):
        ok, message = compare_covering(rs(["分类", "书籍数", "用户数"], [["文学", 139, 12]]),
                                      rs(["分类", "书籍数"], [["文学", 139]]))
        self.assertFalse(ok)
        self.assertIn("列数不足", message)

    def test_wrong_value_is_not_covered(self):
        ok, message = compare_covering(rs(["分类", "书籍数"], [["文学小说", 139]]),
                                      rs(["分类", "书籍数", "备注"], [["文学小说", 140, "x"]]))
        self.assertFalse(ok)
        self.assertIn("找不到", message)

    def test_row_misalignment_is_caught(self):
        """列各自都对、但行与行错位——只看列值多重集会漏掉，投影重比才能抓住。"""
        gold = rs(["书名", "评分"], [["A", 9.1], ["B", 8.7]])
        pred = rs(["书名", "评分", "备注"], [["A", 8.7, "x"], ["B", 9.1, "y"]])
        ok, message = compare_covering(gold, pred)
        self.assertFalse(ok)
        self.assertTrue("行内容不同" in message or "找不到" in message)

    def test_row_count_must_match(self):
        ok, message = compare_covering(rs(["书名"], [["A"], ["B"]]),
                                      rs(["书名", "备注"], [["A", "x"]]))
        self.assertFalse(ok)
        self.assertIn("行数不同", message)

    def test_select_star_still_covers(self):
        """近似 SELECT * 也会被覆盖判定放过——这正是它不能替代 EX 的原因。

        两个指标必须并列报告：只看 cover 会掩盖投影失控，只看 EX 会误伤
        「多给一列」的合理答案。
        """
        gold = rs(["书名"], [["A"]])
        pred = rs(["书名"] + [f"c{i}" for i in range(14)], [["A"] + list(range(14))])
        self.assertTrue(compare_covering(gold, pred)[0])
        self.assertFalse(compare(gold, pred)[0])

    def test_empty_result_covers_when_both_empty(self):
        self.assertTrue(compare_covering(rs(["书名"], []), rs(["书名"], []))[0])
        self.assertFalse(compare_covering(rs(["书名"], []), rs([], []))[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
