"""只读数仓仓库：guard -> 执行 -> ResultSet。

三层保护，任何一层单独失守都还有下一层：

1. ``run_query`` 先过 :func:`app.db.guard.validate_sql`，被拒就抛
   ``QueryFailed(stage="guard")``，危险 SQL 根本到不了 sqlite；
2. 连接串是 ``file:...?mode=ro``（URI 只读），文件系统层面就写不了；
3. 连接建立后立刻 ``PRAGMA query_only=ON``，即使有人绕过 guard 直接拿连接也写不进去。

超时用 ``set_progress_handler``：SQLite 每执行约 2000 条 VM 指令回调一次，回调
超过 deadline 返回非 0 就中断查询，抛 ``OperationalError("interrupted")``。这比
``interrupt()`` + 线程更简单，也不会在被中断后把连接留在半死状态。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Sequence
from urllib.parse import quote

from app.core.models import ResultSet
from app.db.guard import validate_sql

__all__ = ["QueryFailed", "WarehouseRepo"]

_PROGRESS_STEPS = 2000

_GUARD_HINT = (
    "只允许一条只读 SELECT，且只能查询 context 中给出的白名单表；"
    "请删掉写操作/系统表/多语句后重试。"
)


class QueryFailed(Exception):
    """统一的查询失败异常，``stage`` 区分是守卫拦的还是数据库报的。"""

    def __init__(self, message: str, stage: str = "execute", hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.hint = hint
        self.sql = ""          # 由调用方（executor 节点）按需补上，方便回填 ExecutionError

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"QueryFailed(stage={self.stage!r}, message={self.message!r})"


class WarehouseRepo:
    """实现 ``core.protocols.Repo``：所有读操作都经过同一个只读连接。"""

    def __init__(self, path: Path, row_limit: int, timeout_ms: int) -> None:
        self.path = Path(path)
        self.row_limit = int(row_limit)
        self.timeout_ms = int(timeout_ms)
        self._conn: sqlite3.Connection | None = None
        # check_same_thread=False 让 API 线程池可以复用连接，代价是要自己串行化。
        self._lock = threading.RLock()
        self._digest: str | None = None

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------
    def table_names(self) -> set[str]:
        """库里所有可查对象（表 + 视图，排除 sqlite 系统表）。"""
        rows = self._fetch(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'",
            limit=None,
        )[1]
        return {row[0] for row in rows}

    def schema_digest(self) -> str:
        """表名 + 列名排序后取 md5 前 12 位，用作缓存键的一部分。

        列变了、表增删了都会换 digest，缓存自然失效；行数据变化不影响它，
        因为结果缓存的粒度是"问题 + schema"，数据更新时由上层决定是否清缓存。
        """
        if self._digest is None:
            parts: list[str] = []
            for table in sorted(self.table_names()):
                columns = self._columns_of(table)
                parts.append(f"{table}({','.join(sorted(columns))})")
            raw = ";".join(parts).encode("utf-8")
            self._digest = hashlib.md5(raw).hexdigest()[:12]
        return self._digest

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def run_query(self, sql: str) -> ResultSet:
        """guard 通过后执行，返回截断到 ``row_limit`` 行的 ``ResultSet``。"""
        verdict = validate_sql(sql, self.table_names(), self.row_limit)
        if not verdict.ok:
            failed = QueryFailed(verdict.reason, stage="guard", hint=_GUARD_HINT)
            failed.sql = sql
            raise failed

        started = perf_counter()
        columns, rows, truncated = self._fetch(
            verdict.sql, limit=self.row_limit, capped=verdict.capped
        )
        elapsed_ms = int((perf_counter() - started) * 1000)

        return ResultSet(
            columns=columns,
            rows=[list(row) for row in rows],
            rowcount=len(rows),
            elapsed_ms=elapsed_ms,
            truncated=truncated,
            numeric_cols=_numeric_columns(columns, rows),
        )

    def explain(self, sql: str) -> str:
        """``EXPLAIN QUERY PLAN`` 的可读文本，用于 trace 里解释"为什么慢"。"""
        verdict = validate_sql(sql, self.table_names(), self.row_limit)
        if not verdict.ok:
            failed = QueryFailed(verdict.reason, stage="guard", hint=_GUARD_HINT)
            failed.sql = sql
            raise failed
        rows = self._fetch(f"EXPLAIN QUERY PLAN {verdict.sql}", limit=None)[1]
        return "\n".join(" | ".join("" if v is None else str(v) for v in row) for row in rows)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        # 只读 URI：mode=ro 让 SQLite 在打开阶段就拒绝写入，不依赖 PRAGMA。
        uri = f"file:{quote(self.path.as_posix(), safe='/:')}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.execute("PRAGMA query_only=ON")
        except sqlite3.Error as exc:
            raise QueryFailed(
                f"无法以只读方式打开数据库 {self.path.name}：{exc}",
                stage="execute",
                hint="数据库文件不存在或不可读，请检查 IP_DB_PATH 配置。",
            ) from exc
        self._conn = conn
        return conn

    def _fetch(
        self, sql: str, limit: int | None, params: Sequence[Any] = (), capped: bool = False
    ) -> tuple[list[str], list[Sequence[Any]], bool]:
        """执行一条已通过 guard 的 SQL，返回 (列名, 行, 是否被截断)。

        守卫已经把 SQL 的上限压到 ``row_limit``，所以"多取一行"在这种情况下永远
        取不到——真正能证明截断的是 ``capped``：上限由守卫添加且刚好取满，说明
        原查询本来还会返回更多行。
        """
        deadline = perf_counter() + self.timeout_ms / 1000.0
        with self._lock:
            conn = self._connect()
            cursor = conn.cursor()
            conn.set_progress_handler(_deadline_handler(deadline), _PROGRESS_STEPS)
            try:
                cursor.execute(sql, params)
                columns = [col[0] for col in (cursor.description or [])]
                if limit is None:
                    rows = cursor.fetchall()
                    truncated = False
                else:
                    fetched = cursor.fetchmany(limit + 1)
                    overflow = len(fetched) > limit
                    truncated = overflow or (capped and len(fetched) == limit)
                    rows = fetched[:limit]
                return columns, list(rows), truncated
            except sqlite3.OperationalError as exc:
                raise _translate(exc, self.timeout_ms) from exc
            except sqlite3.Error as exc:
                raise _translate(exc, self.timeout_ms) from exc
            finally:
                cursor.close()
                # 必须清掉，否则过期的 deadline 会让下一条查询一启动就被打断。
                conn.set_progress_handler(None, 0)

    def _columns_of(self, table: str) -> list[str]:
        # pragma_table_info 接受绑定参数，避免把表名拼进 SQL 触发注入。
        rows = self._fetch("SELECT name FROM pragma_table_info(?)", None, (table,))[1]
        return [row[0] for row in rows]


def _deadline_handler(deadline: float) -> Callable[[], int]:
    def _check() -> int:
        return 1 if perf_counter() > deadline else 0

    return _check


def _numeric_columns(
    columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> list[str]:
    """该列所有非空值都是 int/float（且至少有一个样本）才算数值列。"""
    numeric: list[str] = []
    for index, name in enumerate(columns):
        seen = False
        ok = True
        for row in rows:
            value = row[index]
            if value is None:
                continue
            seen = True
            # bool 是 int 的子类，但图表里没人想按 True/False 画柱状图。
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                ok = False
                break
        if ok and seen and name not in numeric:
            numeric.append(name)
    return numeric


def _translate(exc: sqlite3.Error, timeout_ms: int) -> QueryFailed:
    message = str(exc)
    lowered = message.lower()
    if "interrupted" in lowered:
        return QueryFailed(
            f"查询超时（超过 {timeout_ms} ms）已被中断",
            stage="execute",
            hint="查询超时，请缩小时间范围或加聚合（先 WHERE 过滤 / GROUP BY 再排序）。",
        )
    if "no such column" in lowered:
        hint = "字段名不在可用 schema 内，请只用 context 里给出的字段。"
    elif "no such table" in lowered:
        hint = "表名不存在，请只用 context 里给出的表名。"
    elif "ambiguous column" in lowered:
        hint = "多表 JOIN 需要加表别名前缀，例如 dim_book.category。"
    elif "no such function" in lowered:
        hint = "该函数 SQLite 不支持，请只用内置聚合 / 字符串 / 日期函数。"
    elif "readonly" in lowered or "attempt to write" in lowered or "query_only" in lowered:
        hint = "当前连接是只读的，只允许 SELECT 查询。"
    elif "syntax error" in lowered:
        hint = "SQL 语法错误，请检查括号、引号与关键字顺序。"
    else:
        hint = "请根据错误信息调整 SQL 后重试。"
    return QueryFailed(message, stage="execute", hint=hint)
