"""repo 的契约测试：直接打真实 warehouse.sqlite（只读 URI，安全）。

每个用例都在验证"出错时调用方拿到的是可操作的信息"，而不只是"抛了异常"。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.core.config import SETTINGS
from app.db.repo import QueryFailed, WarehouseRepo

SLOW_SQL = (
    "WITH RECURSIVE c(x) AS ("
    "SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 20000000"
    ") SELECT COUNT(*) AS n FROM c"
)


@pytest.fixture()
def repo():
    instance = WarehouseRepo(SETTINGS.db_path, row_limit=2000, timeout_ms=3000)
    yield instance
    instance.close()


@pytest.fixture()
def tiny_repo():
    instance = WarehouseRepo(SETTINGS.db_path, row_limit=5, timeout_ms=3000)
    yield instance
    instance.close()


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------
def test_run_query_returns_resultset(repo: WarehouseRepo):
    result = repo.run_query(
        "SELECT category, COUNT(*) AS cnt FROM dim_book GROUP BY category ORDER BY cnt DESC"
    )
    assert result.columns == ["category", "cnt"]
    assert result.rowcount == len(result.rows) > 0
    assert result.truncated is False
    assert result.elapsed_ms >= 0
    assert all(len(row) == 2 for row in result.rows)


def test_guard_wrapper_is_applied_by_repo(tiny_repo: WarehouseRepo):
    # 原 SQL 没有 LIMIT，repo 必须先过 guard（外包 LIMIT 5）再执行。
    result = tiny_repo.run_query("SELECT book_id FROM dim_book")
    assert len(result.rows) == 5
    assert result.truncated is True


# --------------------------------------------------------------------------
# guard 拒绝
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO dim_book(book_id, title) VALUES (999999, 'x')",
        "DELETE FROM dim_book",
        "SELECT * FROM sqlite_master",
        "SELECT 1; DROP TABLE dim_book",
        "SELECT * FROM django_session",
    ],
)
def test_guard_rejection_raises_query_failed(repo: WarehouseRepo, sql: str):
    with pytest.raises(QueryFailed) as excinfo:
        repo.run_query(sql)
    failed = excinfo.value
    assert failed.stage == "guard"
    assert failed.message
    assert failed.hint
    assert failed.sql == sql
    assert str(failed) == failed.message


# --------------------------------------------------------------------------
# 超时
# --------------------------------------------------------------------------
def test_timeout_is_interrupted_with_actionable_hint():
    repo = WarehouseRepo(SETTINGS.db_path, row_limit=1000, timeout_ms=50)
    try:
        with pytest.raises(QueryFailed) as excinfo:
            repo.run_query(SLOW_SQL)
        failed = excinfo.value
        assert failed.stage == "execute"
        assert "超时" in failed.message
        assert "超时" in failed.hint
        # 超时后连接必须还能用：progress handler 要在 finally 里清掉。
        assert repo.run_query("SELECT COUNT(*) AS n FROM dim_book").rows[0][0] == 250
    finally:
        repo.close()


# --------------------------------------------------------------------------
# 截断 / 空结果
# --------------------------------------------------------------------------
def test_truncated_flag(tiny_repo: WarehouseRepo):
    result = tiny_repo.run_query("SELECT * FROM fact_rating")
    assert result.truncated is True
    assert result.rowcount == 5 == len(result.rows)


def test_empty_result_set(repo: WarehouseRepo):
    result = repo.run_query("SELECT book_id, title FROM dim_book WHERE book_id = -1")
    assert result.columns == ["book_id", "title"]
    assert result.rows == []
    assert result.rowcount == 0
    assert result.truncated is False
    assert result.numeric_cols == []       # 没有样本就不该推断成数值列


# --------------------------------------------------------------------------
# numeric_cols
# --------------------------------------------------------------------------
def test_numeric_cols_inference(repo: WarehouseRepo):
    result = repo.run_query("SELECT book_id, title, price FROM dim_book")
    assert "book_id" in result.numeric_cols
    assert "price" in result.numeric_cols
    assert "title" not in result.numeric_cols


def test_numeric_cols_ignores_mixed_column(repo: WarehouseRepo):
    # pub_year 是 INTEGER，但混进字符串后整列不再是纯数值。
    result = repo.run_query(
        "SELECT book_id, CAST(pub_year AS TEXT) AS py FROM dim_book"
    )
    assert "py" not in result.numeric_cols


# --------------------------------------------------------------------------
# schema_digest
# --------------------------------------------------------------------------
def test_schema_digest_is_stable_and_short(repo: WarehouseRepo):
    first = repo.schema_digest()
    second = repo.schema_digest()
    other = WarehouseRepo(SETTINGS.db_path, row_limit=10, timeout_ms=1000)
    try:
        assert first == second == other.schema_digest()
    finally:
        other.close()
    assert len(first) == 12
    assert all(ch in "0123456789abcdef" for ch in first)


def test_table_names_covers_tables_and_views(repo: WarehouseRepo):
    names = repo.table_names()
    assert {"dim_book", "fact_rating", "v_book_overview"} <= names
    assert not any(name.startswith("sqlite_") for name in names)


# --------------------------------------------------------------------------
# 只读保护
# --------------------------------------------------------------------------
def test_readonly_connection_rejects_writes(repo: WarehouseRepo):
    repo.table_names()                       # 触发建连
    conn = repo._conn
    assert conn is not None
    with pytest.raises(sqlite3.Error):
        conn.execute("INSERT INTO dim_book(book_id, title) VALUES (999999, 'x')")
    with pytest.raises(sqlite3.Error):
        conn.execute("DROP TABLE dim_book")


# --------------------------------------------------------------------------
# 执行期错误提示
# --------------------------------------------------------------------------
def test_unknown_column_gives_schema_hint(repo: WarehouseRepo):
    with pytest.raises(QueryFailed) as excinfo:
        repo.run_query("SELECT nosuchcol FROM dim_book")
    failed = excinfo.value
    assert failed.stage == "execute"
    assert "no such column" in failed.message.lower()
    assert "字段" in failed.hint


def test_explain_returns_plan_text(repo: WarehouseRepo):
    plan = repo.explain("SELECT * FROM dim_book WHERE category = '小说'")
    assert "dim_book" in plan
    assert "SCAN" in plan.upper() or "SEARCH" in plan.upper()
