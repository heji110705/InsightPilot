"""Trace store / recorder tests, plus the semantic cache that shares its file.

All databases are created under ``tmp_path`` so a test run never touches
``data/runtime.sqlite``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.models import TurnState
from app.harness.checkpoint import SemanticCache
from app.obs.trace import TraceRecorder, TraceStore


def _store(tmp_path: Path) -> TraceStore:
    return TraceStore(tmp_path / "trace.sqlite")


def _turn(store: TraceStore, turn_id: str, status: str = "ok", elapsed_ms: int = 10,
          question: str = "q") -> None:
    store.save_turn(turn_id, "sess-1", question, "stub", status, elapsed_ms, "{}")


# ---------------------------------------------------------------------------
# TraceStore
# ---------------------------------------------------------------------------
def test_save_span_then_get_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _turn(store, "turn_a")
    store.save_span("turn_a", 0, "intake", "ok", 12, {"patch_keys": ["intent"]})
    store.save_span("turn_a", 1, "executor", "error", 30, {"patch_keys": []}, "no such column")

    payload = store.get_turn("turn_a")
    assert payload["turn"]["turn_id"] == "turn_a"
    assert payload["turn"]["status"] == "ok"
    assert [s["seq"] for s in payload["spans"]] == [0, 1]
    assert payload["spans"][0]["meta"] == {"patch_keys": ["intent"]}
    assert payload["spans"][1]["status"] == "error"
    assert "no such column" in payload["spans"][1]["error"]
    assert payload["spans"][1]["ms"] == 30


def test_get_turn_unknown_returns_empty_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_turn("nope") == {"turn": {}, "spans": []}


def test_list_turns_is_newest_first_and_exposes_api_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _turn(store, "turn_1", question="first")
    _turn(store, "turn_2", question="second")
    _turn(store, "turn_3", question="third")

    rows = store.list_turns()
    assert [r["turn_id"] for r in rows] == ["turn_3", "turn_2", "turn_1"]
    assert set(rows[0]) == {
        "turn_id", "session_id", "question", "mode", "status", "elapsed_ms", "created_at",
    }
    assert rows[0]["question"] == "third"
    assert store.list_turns(limit=2) == rows[:2]


def test_stats_counts_turns_spans_success_and_steps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _turn(store, "turn_1", status="ok", elapsed_ms=10)
    _turn(store, "turn_2", status="ok", elapsed_ms=20)
    _turn(store, "turn_3", status="error", elapsed_ms=30)
    store.save_span("turn_1", 0, "a", "ok", 1, {})
    store.save_span("turn_1", 1, "b", "ok", 1, {})
    store.save_span("turn_2", 0, "a", "ok", 1, {})

    stats = store.stats()
    assert stats["turns"] == 3
    assert stats["ok"] == 2
    assert stats["success_rate"] == 0.6667
    assert stats["avg_elapsed_ms"] == 20
    assert stats["avg_steps"] == 1.5
    assert stats["spans"] == 3


def test_stats_on_empty_store_is_zeroed(tmp_path: Path) -> None:
    stats = _store(tmp_path).stats()
    assert stats == {
        "turns": 0, "ok": 0, "success_rate": 0.0,
        "avg_elapsed_ms": 0, "avg_steps": 0.0, "spans": 0,
    }


# ---------------------------------------------------------------------------
# TraceRecorder
# ---------------------------------------------------------------------------
def test_recorder_finish_writes_turn_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = TurnState(question="多少本书", session_id="sess-9", mode="api")
    recorder = TraceRecorder(store, state)

    with recorder.span("intake"):
        recorder.annotate(kind="query")
    recorder.finish("ok", {"answer": "42"})

    payload = store.get_turn(state.turn_id)
    assert payload["turn"]["session_id"] == "sess-9"
    assert payload["turn"]["question"] == "多少本书"
    assert payload["turn"]["mode"] == "api"
    assert payload["turn"]["status"] == "ok"
    assert payload["turn"]["elapsed_ms"] >= 0
    assert payload["turn"]["created_at"]
    assert '"answer": "42"' in payload["turn"]["payload_json"]
    assert payload["spans"][0]["meta"]["kind"] == "query"


def test_recorder_span_is_persisted_even_when_the_node_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = TurnState(question="q")
    recorder = TraceRecorder(store, state)

    with pytest.raises(RuntimeError):
        with recorder.span("executor"):
            raise RuntimeError("kaboom")

    spans = store.get_turn(state.turn_id)["spans"]
    assert len(spans) == 1
    assert spans[0]["seq"] == 0
    assert spans[0]["node"] == "executor"
    assert spans[0]["status"] == "error"
    assert "RuntimeError" in spans[0]["error"] and "kaboom" in spans[0]["error"]


def test_recorder_numbers_spans_in_order_and_buffers_annotations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = TurnState(question="q")
    recorder = TraceRecorder(store, state)

    recorder.annotate(turn_level="yes")  # before any span: must not be lost
    with recorder.span("a") as span:
        span.meta["custom"] = 1
    with recorder.span("b"):
        pass

    spans = store.get_turn(state.turn_id)["spans"]
    assert [s["seq"] for s in spans] == [0, 1]
    assert spans[0]["node"] == "a" and spans[1]["node"] == "b"
    assert spans[0]["meta"]["turn_level"] == "yes"
    assert spans[0]["meta"]["custom"] == 1


# ---------------------------------------------------------------------------
# SemanticCache
# ---------------------------------------------------------------------------
def test_semantic_cache_roundtrip_hit_rate_and_stats(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    cache = SemanticCache(path)
    cache.put("k1", {"sql": "SELECT 1", "tables": ["t"]})

    assert cache.get("k1") == {"sql": "SELECT 1", "tables": ["t"]}
    assert cache.get("missing") is None
    assert cache.hit_rate() == 0.5

    stats = cache.stats()
    assert stats["entries"] == 1
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["writes"] == 1
    assert stats["hit_rate"] == 0.5

    # Counters and entries survive a new instance: same file, same numbers.
    again = SemanticCache(path)
    assert again.get("k1") is not None
    assert again.hit_rate() == round(2 / 3, 4)


def test_semantic_cache_overwrites_value_but_counts_the_write(tmp_path: Path) -> None:
    cache = SemanticCache(tmp_path / "runtime.sqlite")
    cache.put("k", {"sql": "SELECT 1"})
    cache.put("k", {"sql": "SELECT 2"})

    assert cache.get("k") == {"sql": "SELECT 2"}
    assert cache.stats()["entries"] == 1
    assert cache.stats()["writes"] == 2
