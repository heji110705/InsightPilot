"""guard 的契约测试：每一条攻击面一个用例，外加"不能误杀"的正面用例。

断言的重点不是"返回了 False"，而是"拒绝理由能直接回灌给模型"——所以每个用例
都会检查 reason 里是否提到了被拦下的东西（表名 / 语句类型 / 数量）。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.core.config import SETTINGS
from app.db.guard import GuardResult, validate_sql

ROW_LIMIT = 2000
ALLOWED = {"dim_book", "fact_rating", "fact_click", "v_book_overview"}


def check(sql: str, allowed: set[str] | None = None, limit: int = ROW_LIMIT) -> GuardResult:
    return validate_sql(sql, ALLOWED if allowed is None else allowed, limit)


# --------------------------------------------------------------------------
# 0. 合法 SQL 必须通过（否则守卫就成了摆设）
# --------------------------------------------------------------------------
def test_plain_select_passes_and_is_wrapped():
    result = check("SELECT book_id, title FROM dim_book")
    assert result.ok is True
    assert result.reason == ""
    assert result.tables == ["dim_book"]
    assert result.sql == "SELECT * FROM (SELECT book_id, title FROM dim_book) LIMIT 2000"


def test_join_and_aggregate_passes():
    sql = (
        "SELECT b.category, COUNT(*) AS cnt FROM dim_book b "
        "JOIN fact_rating r ON r.book_id = b.book_id GROUP BY b.category ORDER BY cnt DESC"
    )
    result = check(sql)
    assert result.ok is True
    assert result.tables == ["dim_book", "fact_rating"]


def test_view_name_is_whitelisted():
    result = check("SELECT * FROM v_book_overview WHERE rating_cnt > 0")
    assert result.ok is True
    assert result.tables == ["v_book_overview"]


# --------------------------------------------------------------------------
# 1. 空字符串 / 纯注释 / 纯空白
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sql", "needle"),
    [
        ("", "空"),
        ("   \n\t  ", "空"),
        ("-- 只是一行注释\n", "注释"),
        ("/* 块注释\n 多行 */", "注释"),
        (";;", "语句"),
    ],
)
def test_empty_or_comment_only_is_rejected(sql: str, needle: str):
    result = check(sql)
    assert result.ok is False
    assert result.sql == ""
    assert result.tables == []
    assert needle in result.reason


# --------------------------------------------------------------------------
# 2. 多语句
# --------------------------------------------------------------------------
def test_multiple_statements_rejected():
    result = check("SELECT 1; DROP TABLE dim_book")
    assert result.ok is False
    assert "2 条语句" in result.reason
    assert "DROP" in result.reason


def test_trailing_semicolon_is_not_a_second_statement():
    assert check("SELECT book_id FROM dim_book;").ok is True


# --------------------------------------------------------------------------
# 3. 非 SELECT 根节点
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO dim_book(book_id, title) VALUES (1, 'x')",
        "UPDATE dim_book SET price = 0",
        "DELETE FROM dim_book",
        "CREATE TABLE evil(a INT)",
        "CREATE INDEX idx_evil ON dim_book(title)",
        "DROP TABLE dim_book",
        "ALTER TABLE dim_book ADD COLUMN z INT",
        "TRUNCATE TABLE dim_book",
        "MERGE INTO dim_book USING dim_author ON 1 = 1 WHEN MATCHED THEN DELETE",
        "REPLACE INTO dim_book(book_id) VALUES (1)",
    ],
)
def test_non_select_root_rejected(sql: str):
    result = check(sql)
    assert result.ok is False
    assert result.sql == ""
    assert "SELECT" in result.reason


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH DATABASE 'other.db' AS other",
        "DETACH DATABASE other",
        "PRAGMA table_info(dim_book)",
        "PRAGMA writable_schema = ON",
        "VACUUM",
        "REINDEX",
        "ANALYZE",
    ],
)
def test_admin_statements_rejected(sql: str):
    result = check(sql)
    assert result.ok is False
    keyword = sql.split()[0].upper()
    assert keyword in result.reason


# --------------------------------------------------------------------------
# 5. 系统表
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sql", "needle"),
    [
        ("SELECT * FROM sqlite_master", "sqlite_master"),
        ("SELECT name, sql FROM sqlite_schema", "sqlite_schema"),
        ("SELECT * FROM sqlite_temp_master", "sqlite_temp_master"),
        ("SELECT * FROM main.sqlite_master", "sqlite_master"),
        ("SELECT * FROM temp.dim_book", "temp"),
        ("SELECT * FROM sqlite_sequence", "sqlite_sequence"),
    ],
)
def test_system_tables_rejected(sql: str, needle: str):
    result = check(sql)
    assert result.ok is False
    assert needle in result.reason


def test_system_table_inside_subquery_rejected():
    result = check("SELECT * FROM (SELECT name FROM sqlite_master)")
    assert result.ok is False
    assert "sqlite_master" in result.reason


# --------------------------------------------------------------------------
# 6. 白名单
# --------------------------------------------------------------------------
def test_unknown_table_rejected_with_available_list():
    result = check("SELECT * FROM django_session")
    assert result.ok is False
    assert "django_session" in result.reason
    assert "可用：" in result.reason
    assert "dim_book" in result.reason


def test_case_insensitive_whitelist():
    assert check("SELECT * FROM DIM_BOOK").ok is True


# --------------------------------------------------------------------------
# 7. 子查询里的黑名单表
# --------------------------------------------------------------------------
def test_blacklisted_table_in_subquery_rejected():
    result = check("SELECT * FROM (SELECT * FROM django_session)")
    assert result.ok is False
    assert "django_session" in result.reason


def test_blacklisted_table_in_cte_body_rejected():
    result = check("WITH t AS (SELECT * FROM django_session) SELECT * FROM t")
    assert result.ok is False
    assert "django_session" in result.reason


# --------------------------------------------------------------------------
# 8. UNION 夹带非法表
# --------------------------------------------------------------------------
def test_union_with_second_illegal_table_rejected():
    result = check("SELECT book_id FROM dim_book UNION SELECT id FROM django_session")
    assert result.ok is False
    assert "django_session" in result.reason


# --------------------------------------------------------------------------
# 9. LIMIT 兜底
# --------------------------------------------------------------------------
def test_missing_limit_is_wrapped():
    result = check("SELECT book_id FROM dim_book")
    assert result.sql.startswith("SELECT * FROM (")
    assert result.sql.endswith("LIMIT 2000")


def test_limit_above_cap_is_rewritten():
    result = check("SELECT book_id FROM dim_book ORDER BY book_id LIMIT 5000 OFFSET 10")
    assert result.ok is True
    assert "LIMIT 2000" in result.sql
    assert "5000" not in result.sql
    assert "OFFSET 10" in result.sql


def test_limit_at_or_below_cap_is_untouched():
    assert check("SELECT book_id FROM dim_book LIMIT 10").sql == "SELECT book_id FROM dim_book LIMIT 10"
    assert check("SELECT book_id FROM dim_book LIMIT 2000").sql.endswith("LIMIT 2000")


def test_negative_limit_is_wrapped():
    # SQLite 里 LIMIT -1 等于"不限"，必须靠外层兜住。
    result = check("SELECT book_id FROM dim_book LIMIT -1")
    assert result.ok is True
    assert result.sql.startswith("SELECT * FROM (")
    assert result.sql.endswith("LIMIT 2000")


def test_non_literal_limit_is_wrapped():
    result = check("SELECT book_id FROM dim_book LIMIT (SELECT 5)")
    assert result.ok is True
    assert result.sql.startswith("SELECT * FROM (")


def test_wrapped_sql_actually_runs():
    result = check("SELECT book_id, title FROM dim_book")
    conn = sqlite3.connect(f"file:{SETTINGS.db_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(result.sql).fetchall()
    finally:
        conn.close()
    assert len(rows) == 250
    assert all(len(row) == 2 for row in rows)


# --------------------------------------------------------------------------
# 10. CTE / 递归 CTE 不能被误杀
# --------------------------------------------------------------------------
def test_cte_name_is_treated_as_legal_table():
    result = check("WITH t AS (SELECT book_id FROM dim_book) SELECT * FROM t")
    assert result.ok is True
    assert result.tables == ["dim_book"]      # CTE 名不进 tables


def test_recursive_cte_is_allowed():
    sql = (
        "WITH RECURSIVE seq(n) AS ("
        "SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 5"
        ") SELECT n FROM seq"
    )
    result = check(sql)
    assert result.ok is True
    assert result.tables == []
    assert "WITH RECURSIVE" in result.sql.upper()


def test_recursive_cte_still_checks_tables_inside():
    sql = (
        "WITH RECURSIVE seq(n) AS ("
        "SELECT 1 UNION ALL SELECT n + 1 FROM django_session WHERE n < 5"
        ") SELECT n FROM seq"
    )
    result = check(sql)
    assert result.ok is False
    assert "django_session" in result.reason


# --------------------------------------------------------------------------
# 反绕过：注释 / 大小写 / Unicode 空白
# --------------------------------------------------------------------------
def test_comment_cannot_hide_a_second_statement():
    result = check("SELECT book_id FROM dim_book /* ; DROP TABLE dim_book */")
    assert result.ok is True
    conn = sqlite3.connect(f"file:{SETTINGS.db_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(result.sql).fetchall()
    finally:
        conn.close()
    # 注释被 sqlglot 重新渲染成块注释，外包的 LIMIT 没被注释掉，仍然是 250 行。
    assert len(rows) == 250


def test_line_comment_cannot_smuggle_dml():
    result = check("SELECT book_id FROM dim_book WHERE 1 = 1 -- ; DROP TABLE dim_book")
    assert result.ok is True
    assert "limit" in result.sql.lower()


def test_keyword_case_does_not_bypass():
    result = check("SeLeCt * FrOm SQLITE_MASTER")
    assert result.ok is False
    assert "sqlite_master" in result.reason.lower()


def test_unicode_whitespace_does_not_bypass():
    result = check("SELECT\u00a0*\u00a0FROM\u00a0django_session")
    assert result.ok is False


def test_file_reading_function_rejected():
    result = check("SELECT readfile('/etc/passwd')")
    assert result.ok is False
    assert "readfile" in result.reason


def test_unparsable_sql_rejected():
    result = check("SELECT FROM WHERE ((")
    assert result.ok is False
    assert "语法" in result.reason
