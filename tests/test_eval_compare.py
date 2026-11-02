"""结果集等价判定的单元测试。

这些用例是评测框架的地基：任何一条变红，报告里的 EX 就不能信。
覆盖需求里的 12 条硬性规则，另加重复行、空白、大小写、差异说明长度等边界。
"""
from __future__ import annotations

import pytest

from app.core.models import ResultSet
from app.eval.compare import compare, compare_covering


def rs(columns: list[str], rows: list[list]) -> ResultSet:
    return ResultSet(columns=columns, rows=rows, rowcount=len(rows))


def test_identical_results_are_equal() -> None:
    gold = rs(["分类", "书籍数"], [["文学小说", 139], ["哲学宗教", 30]])
    assert compare(gold, gold) == (True, "结果一致")


def test_row_order_is_ignored() -> None:
    gold = rs(["分类", "书籍数"], [["文学小说", 139], ["哲学宗教", 30]])
    pred = rs(["分类", "书籍数"], [["哲学宗教", 30], ["文学小说", 139]])
    ok, message = compare(gold, pred)
    assert ok is True
    assert message == "结果一致"


def test_column_names_are_ignored() -> None:
    gold = rs(["分类", "书籍数"], [["文学小说", 139]])
    pred = rs(["category", "cnt"], [["文学小说", 139]])
    assert compare(gold, pred)[0] is True


def test_single_cell_count_with_different_alias() -> None:
    gold = rs(["书籍数"], [[250]])
    pred = rs(["COUNT(*)"], [[250]])
    assert compare(gold, pred)[0] is True


def test_numeric_difference_within_tolerance_is_equal() -> None:
    # 容差 = max(1e-6, 1e-6*100) = 1e-4；1e-4 的差刚好落在边界内。
    gold = rs(["x"], [[100.0]])
    pred = rs(["x"], [[100.0001]])
    assert compare(gold, pred)[0] is True


def test_numeric_difference_above_tolerance_is_not_equal() -> None:
    # 1e-3 > 1e-4，必须判不等。
    gold = rs(["x"], [[100.0]])
    pred = rs(["x"], [[100.001]])
    ok, message = compare(gold, pred)
    assert ok is False
    assert "第 1 行第 1 列" in message


def test_large_number_uses_relative_tolerance() -> None:
    # 1e9 时容差放大到 1000：差 500 等价，差 2000 不等价。
    gold = rs(["x"], [[1_000_000_000]])
    assert compare(gold, rs(["x"], [[1_000_000_500]]))[0] is True
    assert compare(gold, rs(["x"], [[1_000_002_000]]))[0] is False


def test_row_count_mismatch_is_not_equal() -> None:
    gold = rs(["x"], [[1], [2]])
    pred = rs(["x"], [[1]])
    ok, message = compare(gold, pred)
    assert ok is False
    assert message == "行数不同：2 vs 1"


def test_column_count_mismatch_is_not_equal() -> None:
    gold = rs(["a", "b"], [[1, 2]])
    pred = rs(["a", "b", "c"], [[1, 2, 3]])
    ok, message = compare(gold, pred)
    assert ok is False
    assert message == "列数不同：2 vs 3"


def test_none_and_empty_string_are_not_equal() -> None:
    gold = rs(["x"], [[None]])
    pred = rs(["x"], [[""]])
    ok, message = compare(gold, pred)
    assert ok is False
    assert "NULL" in message and "空串" in message


def test_none_equals_none() -> None:
    assert compare(rs(["x"], [[None]]), rs(["x"], [[None]]))[0] is True


def test_boolean_is_treated_as_number() -> None:
    gold = rs(["flag"], [[True], [False]])
    pred = rs(["flag"], [[1], [0]])
    assert compare(gold, pred)[0] is True


def test_empty_results_are_equal() -> None:
    assert compare(rs(["x"], []), rs(["x"], []))[0] is True
    assert compare(ResultSet(), ResultSet())[0] is True


def test_empty_versus_non_empty_is_not_equal() -> None:
    ok, message = compare(rs(["x"], []), rs(["x"], [[1]]))
    assert ok is False
    assert message == "行数不同：0 vs 1"


def test_string_whitespace_is_stripped_but_case_is_kept() -> None:
    assert compare(rs(["x"], [["  北京 "]]), rs(["x"], [["北京"]]))[0] is True
    assert compare(rs(["x"], [["ABC"]]), rs(["x"], [["abc"]]))[0] is False


def test_duplicate_rows_are_compared_as_multiset() -> None:
    gold = rs(["x"], [[1], [1]])
    pred = rs(["x"], [[1], [2]])
    assert compare(gold, pred)[0] is False
    # 同样的两行、顺序打乱，仍然等价。
    assert compare(gold, rs(["x"], [[1], [1]]))[0] is True


def test_cell_diff_message_is_human_readable_and_short() -> None:
    gold = rs(["书名", "评论数"], [["活着", 38]])
    pred = rs(["书名", "评论数"], [["活着", 40]])
    ok, message = compare(gold, pred)
    assert ok is False
    assert message == "第 1 行第 2 列：38 vs 40"
    assert len(message) <= 120


def test_long_string_diff_is_truncated() -> None:
    long_text = "存" * 200
    ok, message = compare(rs(["x"], [[long_text]]), rs(["x"], [[long_text + "在"]]))
    assert ok is False
    assert len(message) <= 120


def test_number_and_numeric_string_are_not_equal() -> None:
    # 不做隐式类型转换：SQLite 返回 1 与 "1" 是两种类型。
    assert compare(rs(["x"], [[1]]), rs(["x"], [["1"]]))[0] is False


@pytest.mark.parametrize(
    "gold_value,pred_value",
    [
        (0, 0.0),
        (1, True),
        (False, 0),
        (2.5, 2.5),
    ],
)
def test_numeric_type_mixing(gold_value, pred_value) -> None:
    assert compare(rs(["x"], [[gold_value]]), rs(["x"], [[pred_value]]))[0] is True


# --------------------------------------------------------------------------
# 列覆盖判定：严格 EX 之外的第二个指标
# --------------------------------------------------------------------------
class TestCovering:
    """覆盖判定回答的是「答对了但多展示了几列」这一类，不是放宽 EX。"""

    def test_identical_results_cover(self) -> None:
        gold = rs(["分类", "书籍数"], [["文学小说", 139], ["哲学宗教", 30]])
        assert compare_covering(gold, gold) == (True, "结果一致")

    def test_one_extra_column_is_covered(self) -> None:
        gold = rs(["分类", "书籍数"], [["文学小说", 139], ["哲学宗教", 30]])
        pred = rs(
            ["分类", "书籍数", "占比"],
            [["文学小说", 139, 0.82], ["哲学宗教", 30, 0.18]],
        )
        ok, message = compare_covering(gold, pred)
        assert ok is True
        assert "多出 1 列" in message
        assert "占比" in message

    def test_column_order_does_not_matter(self) -> None:
        gold = rs(["书名", "评分"], [["A", 9.1], ["B", 8.7]])
        pred = rs(["评分", "备注", "书名"], [[9.1, "x", "A"], [8.7, "y", "B"]])
        assert compare_covering(gold, pred)[0] is True

    def test_fewer_columns_is_not_covered(self) -> None:
        gold = rs(["分类", "书籍数", "用户数"], [["文学", 139, 12]])
        pred = rs(["分类", "书籍数"], [["文学", 139]])
        ok, message = compare_covering(gold, pred)
        assert ok is False
        assert "列数不足" in message

    def test_wrong_value_is_not_covered(self) -> None:
        gold = rs(["分类", "书籍数"], [["文学小说", 139]])
        pred = rs(["分类", "书籍数", "备注"], [["文学小说", 140, "x"]])
        ok, message = compare_covering(gold, pred)
        assert ok is False
        assert "找不到" in message

    def test_row_misalignment_is_caught(self) -> None:
        """列各自都对、但行与行错位——只看列值多重集会漏掉，投影重比才能抓住。"""
        gold = rs(["书名", "评分"], [["A", 9.1], ["B", 8.7]])
        pred = rs(["书名", "评分", "备注"], [["A", 8.7, "x"], ["B", 9.1, "y"]])
        ok, message = compare_covering(gold, pred)
        assert ok is False
        assert "行内容不同" in message or "找不到" in message

    def test_row_count_must_match(self) -> None:
        gold = rs(["书名"], [["A"], ["B"]])
        pred = rs(["书名", "备注"], [["A", "x"]])
        ok, message = compare_covering(gold, pred)
        assert ok is False
        assert "行数不同" in message

    def test_select_star_still_covers(self) -> None:
        """近似 SELECT * 也会被覆盖判定放过——这正是它不能替代 EX 的原因。

        两个指标必须并列报告：只看 cover 会掩盖投影失控，只看 EX 会误伤
        「多给一列」的合理答案。
        """
        gold = rs(["书名"], [["A"]])
        pred = rs(["书名"] + [f"c{i}" for i in range(14)], [["A"] + list(range(14))])
        assert compare_covering(gold, pred)[0] is True
        assert compare(gold, pred)[0] is False

    def test_empty_result_covers_when_both_empty(self) -> None:
        """两边都空是「一致」，与 compare 保持同一语义；预测完全无列才算失败。"""
        gold = rs(["书名"], [])
        assert compare_covering(gold, rs(["书名"], []))[0] is True
        assert compare_covering(gold, rs([], []))[0] is False
