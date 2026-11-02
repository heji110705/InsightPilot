"""SQL 守卫：用 AST 而不是正则决定一条 SQL 能不能跑。

为什么不用正则：任何基于文本的过滤都能被注释注入（``/*x*/DROP``）、大小写、
Unicode 空白、字符串字面量里的假关键字绕过。这里所有判断都发生在
``sqlglot`` 解析出的语法树上：

1. 只接受单条语句，根节点必须是 SELECT 形态（Select / Union / Intersect /
   Except / Subquery），DDL、DML、PRAGMA、ATTACH 等一律拒绝；
2. 每个 ``exp.Table`` 节点必须命中白名单（或命中同一条语句里的 CTE 名），
   SQLite 系统表与 ``temp.`` / 其它库前缀一律拒绝；
3. 最后才把树重新序列化成 SQL，并按 ``row_limit`` 补/改 LIMIT。

文本只用于生成人可读的 ``reason``（会被回灌给模型当纠错提示），从不参与
安全判定。返回的 ``sql`` 是最终要执行的 SQL，调用方不需要再做任何改写。
"""
from __future__ import annotations

from sqlglot import exp, parse
from sqlglot.errors import SqlglotError
from pydantic import BaseModel, Field

__all__ = ["GuardResult", "validate_sql"]

# 允许作为语句根节点的类型。Union / Intersect / Except 都是 SetOperation，
# Subquery 是为了兼容 `(SELECT ...)` 这种被整体括起来的查询。
_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.SetOperation,
    exp.Subquery,
)

# 明确点名的危险语句类型 -> 回给用户的关键字。类型判断先于文本判断。
_DENIED_ROOTS: dict[type[exp.Expression], str] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Create: "CREATE",
    exp.Drop: "DROP",
    exp.Alter: "ALTER",
    exp.TruncateTable: "TRUNCATE",
    exp.Merge: "MERGE",
    exp.Pragma: "PRAGMA",
    exp.Attach: "ATTACH",
    exp.Detach: "DETACH",
    exp.Analyze: "ANALYZE",
    exp.Transaction: "BEGIN",
    exp.Commit: "COMMIT",
    exp.Rollback: "ROLLBACK",
}

# 能读写宿主机文件的 SQLite 扩展函数。不在契约清单里，但只读连接也挡不住
# load_extension 加载本地动态库，代价只有一行，所以一并拦掉。
_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fsdir"}

_SYSTEM_TABLE_PREFIX = "sqlite_"
_MAX_TABLES_IN_HINT = 10


class GuardResult(BaseModel):
    ok: bool
    sql: str = ""                                  # 通过时为规范化 + 已补 LIMIT 的 SQL
    reason: str = ""                               # 拒绝原因，面向人可读
    tables: list[str] = Field(default_factory=list)  # 实际引用的物理表（不含 CTE 名）
    # 上限是守卫自己加的（原本没有 LIMIT，或原 LIMIT 被改小）。repo 用它区分
    # "用户只要 10 行" 和 "被截断到 2000 行"。
    capped: bool = False


def validate_sql(sql: str, allowed_tables: set[str], row_limit: int) -> GuardResult:
    """校验 ``sql``，返回可直接执行的 SQL 或一条人可读的拒绝原因。

    成功时 ``sql`` 已被规范化并补过 LIMIT；失败时 ``sql`` 为空串、``tables``
    为空列表，``reason`` 可以直接拼进重试 prompt。
    """
    text = (sql or "").strip()
    if not text:
        return _reject("SQL 为空：请给出一条完整的 SELECT 查询。")

    try:
        parsed = parse(text, read="sqlite")
    except SqlglotError as exc:
        first = str(exc).splitlines()[0][:160] if str(exc) else "解析失败"
        return _reject(
            f"SQL 语法无法解析（{first}）。请检查括号、引号与关键字顺序，"
            "并确保整条语句是一条完整的 SELECT。"
        )
    except Exception as exc:  # noqa: BLE001 - 解析器的任何异常都按“无法解析”处理
        return _reject(f"SQL 语法无法解析：{type(exc).__name__}: {str(exc)[:160]}")

    statements = [
        node for node in parsed
        if node is not None and not isinstance(node, exp.Semicolon)
    ]
    if not statements:
        return _reject("SQL 里没有任何可执行语句（只有空白、分号或注释）。")

    if len(statements) > 1:
        kinds = "、".join(_statement_label(node) for node in statements[:3])
        return _reject(
            f"只允许单条 SELECT 查询，检测到 {len(statements)} 条语句（{kinds}）。"
            "请把多条语句合并成一条 SELECT。"
        )

    root = statements[0]

    # 整棵树扫一遍：即使根节点是 SELECT，也不允许树里夹带任何写操作/管理命令。
    for node in root.walk():
        label = _denied_label(node)
        if label:
            return _reject(
                f"只允许只读 SELECT 查询，检测到 {label} 语句已被拦截。"
                "本系统只能查数据，不能改结构或改数据。"
            )

    if not isinstance(root, _ALLOWED_ROOTS):
        return _reject(
            f"只允许只读 SELECT 查询，检测到 {_statement_label(root)} 语句。"
            "请改写成 SELECT ... FROM <白名单表> 的形式。"
        )

    # 文件读写类扩展函数：只读连接挡不住 load_extension 加载本地动态库。
    for func in root.find_all(exp.Anonymous):
        func_name = str(func.this or "").strip().lower()
        if func_name in _DENIED_FUNCTIONS:
            return _reject(
                f"不允许调用 {func_name}()：该函数能读写宿主机文件或加载外部扩展。"
            )

    # ---- 表名检查 -------------------------------------------------------
    cte_names = {
        cte.alias_or_name.lower()
        for cte in root.find_all(exp.CTE)
        if cte.alias_or_name
    }
    allowed_lower = {name.lower() for name in allowed_tables}
    used: list[str] = []

    for table in root.find_all(exp.Table):
        name = table.name or ""
        db = (table.db or "").lower()
        lowered = name.lower()

        if db == "temp":
            return _reject(f"不允许访问 temp 库（{_render(table)}）：临时表同样可以创建并读出库外数据。")
        if db and db != "main":
            return _reject(f"不允许跨库访问（{_render(table)}），只允许查询主库里的白名单表。")
        if not name:
            return _reject("FROM 子句里出现了不支持的表值函数，请只引用 context 中给出的白名单表。")
        if lowered.startswith(_SYSTEM_TABLE_PREFIX):
            return _reject(
                f"不允许访问 SQLite 系统表 {name}：系统表会暴露整个库的表结构与索引信息。"
            )
        if lowered in cte_names:
            continue
        if lowered not in allowed_lower:
            return _reject(_unknown_table_reason(name, allowed_tables))
        if name not in used:
            used.append(name)

    # ---- LIMIT 兜底 -----------------------------------------------------
    final_sql, capped = _apply_limit(root, row_limit)
    return GuardResult(ok=True, sql=final_sql, tables=used, capped=capped)


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------
def _reject(reason: str) -> GuardResult:
    return GuardResult(ok=False, sql="", reason=reason, tables=[])


def _apply_limit(root: exp.Expression, row_limit: int) -> tuple[str, bool]:
    """无 LIMIT 就外包一层；已有 LIMIT 超限就改写成上限。

    返回 ``(sql, capped)``；``capped`` 表示"这个上限是我加的"，repo 据此判断
    结果是否可能被截断。

    外包而不是往 ORDER BY 后面插 LIMIT：AST 里 ``args["limit"]`` 只有一个位置，
    但把 LIMIT 插在 UNION / 子查询 / WITH 的语义边界上很容易改错。外包
    ``SELECT * FROM (...) LIMIT n`` 对任何 SELECT 形态都成立。
    """
    limit = row_limit if isinstance(row_limit, int) and row_limit > 0 else 1
    node = root.args.get("limit")
    if node is None:
        return f"SELECT * FROM ({root.sql(dialect='sqlite')}) LIMIT {limit}", True

    literal = _limit_value(node)
    if literal is not None and 0 <= literal <= limit:
        return root.sql(dialect="sqlite"), False
    if literal is not None and literal > limit:
        # 直接改字面量：OFFSET / ORDER BY 都原样保留，语义不变。
        node.set("expression", exp.Literal.number(limit))
        return root.sql(dialect="sqlite"), True
    # LIMIT -1（SQLite 里等于不限）、LIMIT 表达式、LIMIT (SELECT ...)：
    # 数值不可静态判断，只能靠外层兜住。
    return f"SELECT * FROM ({root.sql(dialect='sqlite')}) LIMIT {limit}", True


def _limit_value(node: exp.Limit) -> int | None:
    expr = node.expression
    if isinstance(expr, exp.Literal) and expr.is_int:
        try:
            return int(expr.this)
        except (TypeError, ValueError):
            return None
    return None


def _denied_label(node: exp.Expression) -> str:
    """返回该节点对应的危险语句关键字；不是危险节点则返回空串。"""
    if isinstance(node, exp.Command):
        return str(node.this or "COMMAND").strip().upper()
    for cls, label in _DENIED_ROOTS.items():
        if isinstance(node, cls):
            return label
    return ""


def _statement_label(root: exp.Expression) -> str:
    """给非 SELECT 语句起一个人类看得懂的名字（REINDEX 之类没有专属节点）。"""
    label = _denied_label(root)
    if label:
        return label
    head = root.sql(dialect="sqlite").split(None, 1)
    keyword = head[0].strip("(;").upper() if head else ""
    return keyword or type(root).__name__.upper()


def _render(table: exp.Table) -> str:
    return table.sql(dialect="sqlite")


def _unknown_table_reason(name: str, allowed_tables: set[str]) -> str:
    names = sorted(name.lower() for name in allowed_tables)
    shown = ", ".join(names[:_MAX_TABLES_IN_HINT])
    if len(names) > _MAX_TABLES_IN_HINT:
        shown += ", ..."
    return (
        f"表 {name} 不在可查询范围（可用：{shown or '（空）'}）。"
        "请只用 context 里给出的表名，不要臆造表。"
    )
