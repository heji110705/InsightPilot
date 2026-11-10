"""空集守卫：0 行不等于 0。

为什么需要这一层
----------------
「骑行有多少本」在 SQL 层面是合法的：``WHERE category = '骑行'`` 能跑，只是
返回 0。但 0 有两种完全不同的含义：

1. 维度里**有**这个值，只是当前没有数据（"没有评论的书有 0 本"这类）；
2. 维度里**没有**这个值（骑行的确不是分类），问题本身的前提就不成立。

模型和用户都看不到这个区别，于是"0 本"被当成事实读走了。这一层在答案已经
生成之后做一次反向校验：把 WHERE 里的等值字面量拿去维表里比对一次，查不到
就把"这个值不存在，可能是口径对不上"写进答案，而不是让它静默变成 0。

刻意只处理**单个等值过滤**，不做通用意图理解——它的定位是最后一道兜底，
不是理解层。判不准的时候宁可不提示，也不能把正常的 0 说成错误。
"""
from __future__ import annotations

from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

__all__ = ["missing_filter_values", "note_for"]

_SUGGEST_LIMIT = 8
_DOMAIN_SCAN = 200


def _filters(sql: str) -> list[tuple[str, str, str, str]]:
    """从 SQL 里抽出 (表名, 列名, 字面量, 形态) 四元组。

    只认两种形态：``col = '字面量'``（eq）与 ``col LIKE '%字面量%'``（like）。
    ``IN``、``OR``、带前缀/后缀的 LIKE 一律跳过——那些形态下"查不到"不能
    直接等价于"前提不成立"，宁可放过也不误报。
    """
    try:
        tree = parse_one(sql, dialect="sqlite")
    except (SqlglotError, ValueError):
        return []
    if tree is None:
        return []

    # 别名 -> 真名：`FROM dim_book b WHERE b.category = '骑行'` 要能还原成 dim_book
    alias: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = table.name
        if name:
            alias[table.alias or name] = name
    tables = list(dict.fromkeys(t for t in alias.values() if t))

    def resolve(col_node: exp.Column) -> str:
        table = alias.get(col_node.table or "", "")
        if table:
            return table
        return tables[0] if len(tables) == 1 else ""   # 多表且列没带前缀，不猜

    hits: list[tuple[str, str, str, str]] = []
    for eq in tree.find_all(exp.EQ):
        for col_node, lit_node in ((eq.left, eq.right), (eq.right, eq.left)):
            if not isinstance(col_node, exp.Column):
                continue
            if not (isinstance(lit_node, exp.Literal) and lit_node.is_string):
                continue
            value = lit_node.this
            if not isinstance(value, str) or not value.strip():
                continue
            table = resolve(col_node)
            if table:
                hits.append((table, col_node.name, value, "eq"))

    for like in tree.find_all(exp.Like):
        col_node, pat_node = like.this, like.expression
        if not isinstance(col_node, exp.Column):
            continue
        if not (isinstance(pat_node, exp.Literal) and pat_node.is_string):
            continue
        pattern = pat_node.this
        if not isinstance(pattern, str) or not (pattern.startswith("%") and pattern.endswith("%")):
            continue
        core = pattern.strip("%").strip()
        if not core:                     # '%%' 等于没过滤
            continue
        table = resolve(col_node)
        if table:
            hits.append((table, col_node.name, core, "like"))
    return hits


def _domain(repo: Any, table: str, column: str) -> list[str]:
    sql = (
        f'SELECT "{column}", COUNT(*) AS n FROM "{table}" '
        f'WHERE "{column}" IS NOT NULL GROUP BY "{column}" '
        f'ORDER BY n DESC LIMIT {_DOMAIN_SCAN}'
    )
    result = repo.run_query(sql)
    return [str(r[0]) for r in result.rows if r and r[0] not in (None, "")]


def missing_filter_values(sql: str, repo: Any) -> list[dict[str, Any]]:
    """返回 SQL 里"值根本不存在于该列"的过滤条件。

    每项含 ``table`` / ``column`` / ``value`` / ``suggestions``。查询失败一律
    当作"没发现问题"，不让兜底逻辑自己变成新的故障点。
    """
    out: list[dict[str, Any]] = []
    for table, column, value, kind in _filters(sql):
        try:
            domain = _domain(repo, table, column)
        except Exception:  # noqa: BLE001 —— 列名/表名不对时静默放过
            continue
        if not domain:
            continue
        if kind == "eq" and value in domain:
            continue
        if kind == "like" and any(value in item for item in domain):
            continue
        out.append(
            {
                "table": table,
                "column": column,
                "value": value,
                "kind": kind,
                "domain_size": len(domain),
                "suggestions": domain[:_SUGGEST_LIMIT],
            }
        )
    return out


def note_for(issue: dict[str, Any]) -> str:
    """把一条空集问题写成用户能读懂的一句话。"""
    column = issue["column"]
    table = issue["table"]
    where = {
        "category": "分类",
        "binding": "装帧",
        "author_name": "作者",
        "name": "作者姓名",
        "title": "书名",
        "publisher": "出版社",
    }.get(column, column)
    suggestions = issue.get("suggestions") or []
    if issue.get("kind") == "like":
        head = f"{table}.{column}（{where}）里没有任何一行含「{issue['value']}」"
        # 高基数列（书名）列候选没有参考价值，只有低基数列（分类/装帧）才给
        if suggestions and issue.get("domain_size", 0) <= 30:
            head += f"，这一列只有这些取值：{'、'.join(suggestions[:8])}"
        return head + "。这个 0 是「查无此物」，不是「有但为 0」。"
    head = f"「{issue['value']}」不是 {table}.{column}（{where}）里的取值"
    if suggestions and issue.get("domain_size", 0) <= 30:
        head += f"，这一列的取值有：{'、'.join(suggestions)}"
    return head + "。所以这个 0 不是答案，是口径对不上——问题里的说法在库里没有对应实体。"
