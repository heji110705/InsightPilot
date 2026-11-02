"""etl 产物的契约测试：行长、日期形态、维度取值范围、视图与幂等性。

fixture 是 session 级的：整个测试会话只跑一次 ETL，其余用例共享同一个快照，
避免每建一次库就多花几秒（也避免用例之间因为重建而互相踩到文件锁）。
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ETL_PATH = ROOT / "scripts" / "etl.py"
SCHEMA_PATH = ROOT / "data" / "schema.sql"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

EXPECTED_ROWS = {
    "dim_book": 250,
    "fact_rating": 4202,
    "fact_comment": 1976,
    "fact_click": 9028,
    "fact_shelf": 2532,
}

DATE_COLUMNS = {
    "fact_rating": "rated_on",
    "fact_comment": "created_on",
    "fact_click": "clicked_on",
    "fact_shelf": "created_on",
}


def _load_etl():
    """按路径加载脚本 —— scripts 不是包，直接 import 会失败。"""
    spec = importlib.util.spec_from_file_location("insightpilot_etl", ETL_PATH)
    assert spec and spec.loader, f"无法加载 {ETL_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def etl():
    return _load_etl()


@pytest.fixture(scope="session")
def warehouse(etl, tmp_path_factory):
    """会话级建库：产出放在临时目录，不干扰仓库里已提交的 data/warehouse.sqlite。"""
    out = tmp_path_factory.mktemp("warehouse") / "warehouse.sqlite"
    stats = etl.run(etl.DEFAULT_SOURCE, out, SCHEMA_PATH)
    return out, stats


@pytest.fixture()
def conn(warehouse):
    path, _ = warehouse
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def test_all_tables_exist_and_are_non_empty(conn):
    """7 张表 + 1 个视图，任一为空都说明这一层没接上。"""
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    expected = {
        "dim_book",
        "dim_author",
        "dim_date",
        "fact_rating",
        "fact_comment",
        "fact_click",
        "fact_shelf",
    }
    assert expected <= tables, f"缺表：{expected - tables}"
    for table in expected:
        assert _scalar(conn, f"SELECT COUNT(*) FROM {table}") > 0, f"{table} 为空"

    views = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
    }
    assert "v_book_overview" in views
    assert _scalar(conn, "SELECT COUNT(*) FROM v_book_overview") > 0


def test_dim_book_rowcount_matches_source(conn):
    assert _scalar(conn, "SELECT COUNT(*) FROM dim_book") == 250


@pytest.mark.parametrize("table,expected", sorted(EXPECTED_ROWS.items()))
def test_fact_and_dim_rowcounts(conn, table, expected):
    assert _scalar(conn, f"SELECT COUNT(*) FROM {table}") == expected


def test_fact_dates_are_iso_day_text(conn):
    for table, column in DATE_COLUMNS.items():
        values = [
            row[0]
            for row in conn.execute(
                f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
            )
        ]
        assert values, f"{table}.{column} 全为空，不合理"
        bad = [v for v in values if not DATE_RE.match(str(v))]
        assert not bad, f"{table}.{column} 存在非法日期：{bad[:5]}"


def test_dim_book_pub_year_within_range(conn):
    bad = _scalar(
        conn,
        "SELECT COUNT(*) FROM dim_book "
        "WHERE pub_year IS NOT NULL AND (pub_year < 1900 OR pub_year > 2027)",
    )
    assert bad == 0
    # pub_year 允许为空（源数据确实给不出年份时），但不允许是 0 这类占位值。
    assert _scalar(conn, "SELECT COUNT(*) FROM dim_book WHERE pub_year = 0") == 0


def test_dim_book_cleaning_rules(conn):
    # 价格/页数的脏值应该是 NULL 而不是 0，也不该整行消失。
    assert _scalar(conn, "SELECT COUNT(*) FROM dim_book WHERE price <= 0") == 0
    assert _scalar(conn, "SELECT COUNT(*) FROM dim_book WHERE pages <= 0") == 0
    # 分类不允许空白。
    assert _scalar(
        conn, "SELECT COUNT(*) FROM dim_book WHERE category IS NULL OR TRIM(category) = ''"
    ) == 0
    # 每本书都要有作者名（M2M 或文本回落至少命中一条路径）。
    assert _scalar(
        conn, "SELECT COUNT(*) FROM dim_book WHERE author_name IS NULL OR author_name = ''"
    ) == 0


def test_v_book_overview_returns_all_books(conn):
    assert _scalar(conn, "SELECT COUNT(*) FROM v_book_overview") == 250
    # 视图的聚合列不能因为 LEFT JOIN 放大行数（GROUP BY book_id 的副作用）。
    assert _scalar(
        conn, "SELECT COUNT(*) FROM (SELECT book_id FROM v_book_overview GROUP BY book_id)"
    ) == 250


def test_dim_date_columns_consistent(conn):
    row = conn.execute(
        "SELECT date_key, year, quarter, month, day, weekday, weekday_cn, is_weekend "
        "FROM dim_date ORDER BY date_key LIMIT 1"
    ).fetchone()
    assert row is not None
    date_key, year, quarter, month, day, weekday, weekday_cn, is_weekend = row
    assert DATE_RE.match(date_key)
    assert int(date_key[:4]) == year
    assert quarter == (month - 1) // 3 + 1
    assert 0 <= weekday <= 6
    assert weekday_cn == [
        "周一",
        "周二",
        "周三",
        "周四",
        "周五",
        "周六",
        "周日",
    ][weekday]
    assert is_weekend == (1 if weekday >= 5 else 0)


def test_etl_is_idempotent(etl, warehouse):
    """连跑两次：第二次覆盖同名文件不报错，各表行数保持不变。"""
    path, stats = warehouse
    second = etl.run(etl.DEFAULT_SOURCE, path, SCHEMA_PATH)

    with sqlite3.connect(path) as conn:
        for table in ("dim_book", "dim_author", "dim_date", "fact_rating",
                      "fact_comment", "fact_click", "fact_shelf"):
            assert _scalar(conn, f"SELECT COUNT(*) FROM {table}") == stats["tables"][table]["target"]
        assert _scalar(conn, "SELECT COUNT(*) FROM v_book_overview") == 250

    assert second["tables"]["dim_book"]["target"] == 250
    # 第二次跑不应该出现「越跑越多」的情况。
    assert second["tables"]["fact_click"]["target"] == 9028
