"""零行结果的"取值探测"：查不到数，先判断是**数据没有**还是**值不存在**。

为什么需要（2026-11-16 的真实故障）
-----------------------------------
用户问「四大名著有什么」，模型写了 ``WHERE category = '四大名著'``。
库里没有这个分类（只有 12 个，四大名著是书名不是分类），于是 SQL 返回 0 行。
到了下游，0 行被当成"数据里没有"，结论写成"该条件下没有查到数据"——
用户看到的是"你没有这本书"，而实际上四本都在。

这类错误的共同点是：**0 行不等于没有数据，它往往只是一个不存在的过滤值**。
所以这里在"零行"时回查一次：模型用作等值条件的那个值，库里到底有没有？
没有就把它对应的真实取值列表带回去，让模型改一版再跑。

边界（重要）
------------
1. 只在**单表**查询上做。多表时同一个列名可能属于不同表，判断不可靠，宁可不猜。
2. 只处理 ``列 = '字符串字面量'`` 这一种形态。范围条件、LIKE、IN 不在内。
3. 最多探测 2 个条件，值长超过 40 字符直接跳过（那是搜索词，不是枚举值）。
4. 任何异常都吞掉返回空——探测是"锦上添花"，不该让一次本来能答的查询变成 500。
"""
from __future__ import annotations

import logging

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from app.db.repo import WarehouseRepo

__all__ = ["missing_equalities", "hint_text"]

logger = logging.getLogger(__name__)

_MAX_PROBES = 2
_MAX_VALUE_LEN = 40


def _column_literal(node: exp.Expression) -> tuple[str, str] | None:
    """``col = 'x'`` 或 ``'x' = col`` 两种写法都认，返回 (列名, 值)。"""
    left, right = node.this, node.expression
    for col_side, lit_side in ((left, right), (right, left)):
        if isinstance(col_side, exp.Column) and isinstance(lit_side, exp.Literal):
            if lit_side.is_string:
                value = str(lit_side.this or "").strip()
                if value and len(value) <= _MAX_VALUE_LEN:
                    return col_side.name, value
    return None


def missing_equalities(sql: str, repo: WarehouseRepo) -> list[dict[str, object]]:
    """返回``[{"table","column","value","candidates"}]``，只含"值不存在"的条件。"""
    try:
        tree = parse_one(sql, read="sqlite")
    except (SqlglotError, ValueError, TypeError):
        return []
    if tree is None:
        return []

    tables = {t.name for t in tree.find_all(exp.Table) if t.name}
    if len(tables) != 1:
        return []
    table = next(iter(tables))

    found: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for node in tree.find_all(exp.EQ):
        pair = _column_literal(node)
        if pair is None:
            continue
        column, value = pair
        if (column, value) in seen:
            continue
        seen.add((column, value))
        try:
            if repo.count_where(table, column, value) > 0:
                continue
            candidates = repo.distinct_values(table, column, limit=8)
        except Exception:  # noqa: BLE001 - 探测失败不该影响主流程
            logger.debug("probe skipped for %s.%s", table, column, exc_info=True)
            continue
        found.append(
            {"table": table, "column": column, "value": value, "candidates": candidates}
        )
        if len(found) >= _MAX_PROBES:
            break
    return found


def hint_text(found: list[dict[str, object]]) -> str:
    """把探测结果拼成给模型看的纠错提示。"""
    lines = ["下面这些过滤值在库里**不存在**，所以查出来是 0 行（不代表没有数据）："]
    for item in found:
        candidates = item.get("candidates") or []
        shown = "、".join(str(c) for c in candidates) or "（该列没有非空取值）"
        lines.append(
            f"- `{item['table']}.{item['column']} = '{item['value']}'` 不存在；"
            f"该列实际取值有：{shown}"
        )
    lines.append(
        "请改用实际存在的取值，或换一个能匹配的字段（例如按书名/作者 LIKE 匹配）；"
        "如果用户说的确实不是这个维度的取值，就在结论里如实说明，不要造一个值去凑。"
    )
    return "\n".join(lines)
