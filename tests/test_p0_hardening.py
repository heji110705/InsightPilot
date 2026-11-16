"""P0 加固的回归测试（2026-11-16）。

四条都对应线上暴露过的问题，所以测试也按"故障现象"写，而不是按函数名写：

1. 谁都能读到别人问过什么 → traces 必须按会话隔离，不传会话号就是空；
2. 过滤值写错却报"没有数据" → 零行时要能识别出"值不存在"；
3. 旧 SQL 对着新数据出结论 → 缓存必须会过期，且数据变了就换键；
4. 公开的 /docs 等于接口地图 → 默认不暴露。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db.probe import hint_text, missing_equalities
from app.db.repo import WarehouseRepo
from app.harness.checkpoint import SemanticCache
from app.obs.trace import TraceStore

WAREHOUSE = Path("data/warehouse.sqlite")


# ---------------------------------------------------------------------------
# 1. traces 按会话隔离
# ---------------------------------------------------------------------------
def test_list_turns_filters_by_session(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "runtime.sqlite")
    store.save_turn("t1", "s_aaa", "问题 A", "api", "ok", 10, "{}")
    store.save_turn("t2", "s_bbb", "问题 B", "api", "ok", 20, "{}")

    only_a = store.list_turns(10, session_id="s_aaa")
    assert [t["turn_id"] for t in only_a] == ["t1"]

    everyone = store.list_turns(10)
    assert {t["turn_id"] for t in everyone} == {"t1", "t2"}


def test_traces_api_returns_nothing_without_session(tmp_path: Path) -> None:
    """不传会话号时必须是空列表——这是这次修复的**核心断言**。"""
    from app.api import server
    from app.agents.pipeline import build_deps
    from app.core.config import load_settings
    from dataclasses import replace

    settings = replace(load_settings(), state_db_path=tmp_path / "runtime.sqlite")
    deps = build_deps(settings=settings, force_template=True)
    deps.trace_store.save_turn("t9", "s_someone", "别人的问题", "api", "ok", 5, "{}")

    server.engine_deps = lambda: deps                     # type: ignore[assignment]
    client = TestClient(server.app)

    anonymous = client.get("/api/traces")
    assert anonymous.status_code == 200
    assert anonymous.json()["count"] == 0

    mine = client.get("/api/traces", params={"session": "s_someone"})
    assert mine.json()["count"] == 1
    assert mine.json()["turns"][0]["question"] == "别人的问题"


def test_auto_docs_are_off_by_default(tmp_path: Path) -> None:
    from app.api import server

    client = TestClient(server.app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# ---------------------------------------------------------------------------
# 2. 零行探测：值不存在 vs 数据没有
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not WAREHOUSE.exists(), reason="数仓不存在")
def test_missing_category_is_detected() -> None:
    """「四大名著」不是 category 的取值 —— 这正是 11-16 那次故障的形态。"""
    repo = WarehouseRepo(WAREHOUSE, row_limit=2000, timeout_ms=3000)
    sql = "SELECT title FROM dim_book WHERE category = '四大名著' LIMIT 50"

    found = missing_equalities(sql, repo)
    assert len(found) == 1
    assert found[0]["column"] == "category"
    assert found[0]["value"] == "四大名著"
    assert "文学小说" in (found[0]["candidates"] or [])

    text = hint_text(found)
    assert "不存在" in text and "文学小说" in text


@pytest.mark.skipif(not WAREHOUSE.exists(), reason="数仓不存在")
def test_existing_value_is_not_flagged() -> None:
    """真有这个分类时说"没有数据"是对的，不该被误判成值不存在。"""
    repo = WarehouseRepo(WAREHOUSE, row_limit=2000, timeout_ms=3000)
    sql = "SELECT title FROM dim_book WHERE category = '文学小说' LIMIT 5"
    assert missing_equalities(sql, repo) == []


@pytest.mark.skipif(not WAREHOUSE.exists(), reason="数仓不存在")
def test_multi_table_query_is_left_alone() -> None:
    """多表时列名归属不可靠，宁可不猜。"""
    repo = WarehouseRepo(WAREHOUSE, row_limit=2000, timeout_ms=3000)
    sql = (
        "SELECT b.title FROM dim_book b JOIN fact_comment c ON c.book_id = b.book_id "
        "WHERE b.category = '不存在的分类' LIMIT 5"
    )
    assert missing_equalities(sql, repo) == []


# ---------------------------------------------------------------------------
# 3. 缓存过期与数据指纹
# ---------------------------------------------------------------------------
def test_cache_entry_expires(tmp_path: Path) -> None:
    cache = SemanticCache(tmp_path / "runtime.sqlite", ttl_s=3600)
    cache.put("k", {"answer": "x"})
    assert cache.get("k") is not None

    stale = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    with cache._cursor() as cur:
        cur.execute("UPDATE semantic_cache SET created_at = ? WHERE key = ?", (stale, "k"))

    assert cache.get("k") is None, "超过 TTL 的条目必须当未命中"
    with cache._cursor() as cur:
        left = cur.execute("SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
    assert left == 0, "过期条目应被顺手删掉，否则表只增不减"


def test_cache_never_expires_when_ttl_is_zero(tmp_path: Path) -> None:
    cache = SemanticCache(tmp_path / "runtime.sqlite", ttl_s=0)
    cache.put("k", {"answer": "x"})
    stale = (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")
    with cache._cursor() as cur:
        cur.execute("UPDATE semantic_cache SET created_at = ? WHERE key = ?", (stale, "k"))
    assert cache.get("k") is not None


@pytest.mark.skipif(not WAREHOUSE.exists(), reason="数仓不存在")
def test_data_digest_tracks_file_metadata() -> None:
    repo = WarehouseRepo(WAREHOUSE, row_limit=2000, timeout_ms=3000)
    digest = repo.data_digest()
    assert digest and "-" in digest


def test_cache_key_changes_with_data_digest() -> None:
    from app.agents.pipeline import cache_key

    a = cache_key("问题", "schema", "api", "llm:full", "sem", "100-200")
    b = cache_key("问题", "schema", "api", "llm:full", "sem", "100-201")
    assert a != b, "数仓变了，缓存键必须跟着变"
