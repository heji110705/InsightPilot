"""Graph engine tests: routing, loops, the step brake, and failure handling.

Everything runs against a real ``TraceStore``/``Checkpoint`` in ``tmp_path`` --
no writes into ``data/`` -- because the point of these tests is also to prove
the persistence side effects happen at the right moment.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.models import SQLDraft, TurnState
from app.harness.checkpoint import Checkpoint
from app.harness.context import RunContext
from app.harness.graph import Graph, NodeResult
from app.obs.trace import TraceRecorder, TraceStore


class _StubRetriever:
    """Unused by the graph, but ``RunContext`` demands one."""

    def search(self, question: str, *, budget: int, hints: list[str] | None = None) -> Any:
        raise NotImplementedError

    def all_tables(self) -> list[Any]:
        return []

    def metric(self, key: str) -> Any:
        return None

    def metrics(self) -> list[Any]:
        return []


class _StubRepo:
    def table_names(self) -> set[str]:
        return set()

    def run_query(self, sql: str) -> Any:
        raise NotImplementedError

    def schema_digest(self) -> str:
        return "stub"


def make_ctx(
    tmp_path: Path,
    state: TurnState,
    *,
    checkpoint: Checkpoint | None = None,
    max_steps: int = 24,
) -> tuple[RunContext, TraceStore]:
    """Build a context backed by temp SQLite files and return it with its store."""
    store = TraceStore(tmp_path / "trace.sqlite")
    ctx = RunContext(
        settings=Settings(max_steps=max_steps),
        provider=None,
        retriever=_StubRetriever(),
        repo=_StubRepo(),
        trace=TraceRecorder(store, state),
        checkpoint=checkpoint,
        cache=None,
    )
    return ctx, store


def append_note(name: str):
    """Node factory: appends its name to ``notes`` without touching the input state."""

    def fn(state: TurnState, ctx: RunContext) -> NodeResult:
        return NodeResult(patch={"notes": state.notes + [name]})

    return fn


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
def test_linear_graph_runs_nodes_in_order(tmp_path: Path) -> None:
    graph = Graph("linear")
    graph.node("a", append_note("a")).node("b", append_note("b")).node("c", append_note("c"))
    graph.entry("a").edge("a", "b").edge("b", "c").terminal("c")

    state = TurnState(question="q")
    ctx, _ = make_ctx(tmp_path, state)
    out, report = graph.run(state, ctx)

    assert report.path == ["a", "b", "c"]
    assert report.steps == 3
    assert report.status == "ok"
    assert report.error == ""
    assert report.edge_taken == []
    assert out.notes == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("answer", "expected_path", "expected_key"),
    [("left", ["start", "a"], "left"), ("right", ["start", "b"], "right")],
)
def test_branch_routes_by_router_key(
    tmp_path: Path, answer: str, expected_path: list[str], expected_key: str
) -> None:
    graph = Graph("fork")
    graph.node("start", append_note("start"))
    graph.node("a", append_note("a")).node("b", append_note("b"))
    graph.branch("start", lambda state, result: answer, {"left": "a", "right": "b"})
    graph.entry("start").terminal("a", "b")

    state = TurnState(question="q")
    ctx, _ = make_ctx(tmp_path, state)
    out, report = graph.run(state, ctx)

    assert report.path == expected_path
    assert report.edge_taken == [expected_key]
    assert report.status == "ok"


def test_node_may_return_plain_dict(tmp_path: Path) -> None:
    graph = Graph("dict-node")
    graph.node("a", lambda state, ctx: {"notes": ["from-dict"]})
    graph.node("end", append_note("end"))
    graph.entry("a").edge("a", "end").terminal("end")

    state = TurnState(question="q")
    ctx, _ = make_ctx(tmp_path, state)
    out, report = graph.run(state, ctx)

    assert report.status == "ok"
    assert out.notes == ["from-dict", "end"]


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------
def test_retry_loop_repeats_node_until_success(tmp_path: Path) -> None:
    """A failing node reports ``status='retry'`` (raising is reserved for bugs)."""
    attempts: list[int] = []

    def craft(state: TurnState, ctx: RunContext) -> NodeResult:
        attempts.append(state.retries + 1)
        if len(attempts) < 3:
            return NodeResult(status="retry", meta={"error": "syntax error"},
                              patch={"retries": state.retries + 1})
        return NodeResult(patch={"draft": SQLDraft(sql="SELECT 1")})

    graph = Graph("retry")
    graph.node("craft", craft).node("done", append_note("done"))
    graph.branch(
        "craft",
        lambda state, result: "again" if result.status == "retry" else "ok",
        {"again": "craft", "ok": "done"},
    )
    graph.entry("craft").terminal("done")

    state = TurnState(question="q")
    ctx, _ = make_ctx(tmp_path, state)
    out, report = graph.run(state, ctx)

    assert len(attempts) == 3
    assert report.path == ["craft", "craft", "craft", "done"]
    assert report.edge_taken == ["again", "again", "ok"]
    assert report.status == "ok"
    assert out.retries == 2
    assert out.draft is not None and out.draft.sql == "SELECT 1"


def test_max_steps_stops_self_loop(tmp_path: Path) -> None:
    graph = Graph("spin")
    graph.node("spin", append_note("spin")).node("end", append_note("end"))
    graph.edge("spin", "spin").terminal("end")
    graph.entry("spin")

    state = TurnState(question="q")
    ctx, _ = make_ctx(tmp_path, state, max_steps=5)
    out, report = graph.run(state, ctx)

    assert report.status == "max_steps"
    assert report.steps == 5
    assert report.path == ["spin"] * 5
    assert out.notes == ["spin"] * 5
    assert "max_steps=5" in report.error


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------
def test_node_exception_returns_error_report_and_keeps_state(tmp_path: Path) -> None:
    def boom(state: TurnState, ctx: RunContext) -> NodeResult:
        raise RuntimeError("kaboom")

    graph = Graph("boom")
    graph.node("boom", boom).node("end", append_note("end"))
    graph.entry("boom").edge("boom", "end").terminal("end")

    state = TurnState(question="q", notes=["before"])
    ctx, _ = make_ctx(tmp_path, state)
    out, report = graph.run(state, ctx)

    assert report.status == "error"
    assert "RuntimeError" in report.error and "kaboom" in report.error
    assert report.steps == 1
    assert report.path == ["boom"]
    assert out.notes == ["before"], "state must stay at the last merged state"


def test_failed_node_span_is_persisted_as_error(tmp_path: Path) -> None:
    def boom(state: TurnState, ctx: RunContext) -> NodeResult:
        raise ValueError("bad sql")

    graph = Graph("boom-span")
    graph.node("boom", boom).node("end", append_note("end"))
    graph.entry("boom").edge("boom", "end").terminal("end")

    state = TurnState(question="q")
    ctx, store = make_ctx(tmp_path, state)
    graph.run(state, ctx)

    spans = store.get_turn(state.turn_id)["spans"]
    assert len(spans) == 1
    assert spans[0]["node"] == "boom"
    assert spans[0]["status"] == "error"
    assert "bad sql" in spans[0]["error"]


def test_span_meta_contains_patch_keys(tmp_path: Path) -> None:
    graph = Graph("meta")
    graph.node("a", lambda state, ctx: NodeResult(patch={"retries": 1, "cached": True}))
    graph.node("end", append_note("end"))
    graph.entry("a").edge("a", "end").terminal("end")

    state = TurnState(question="q")
    ctx, store = make_ctx(tmp_path, state)
    graph.run(state, ctx)

    span = store.get_turn(state.turn_id)["spans"][0]
    assert span["meta"]["patch_keys"] == ["cached", "retries"]
    assert span["status"] == "ok"


def test_branch_unknown_key_writes_error(tmp_path: Path) -> None:
    graph = Graph("bad-key")
    graph.node("start", append_note("start")).node("end", append_note("end"))
    graph.branch("start", lambda state, result: "nope", {"ok": "end"})
    graph.entry("start").terminal("end")

    state = TurnState(question="q")
    ctx, _ = make_ctx(tmp_path, state)
    out, report = graph.run(state, ctx)

    assert report.status == "error"
    assert report.error.startswith("KeyError:")
    assert "nope" in report.error
    assert report.path == ["start"]
    assert out.notes == ["start"]


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------
def test_validate_requires_entry() -> None:
    graph = Graph("no-entry")
    graph.node("a", append_note("a")).terminal("a")
    with pytest.raises(ValueError, match="entry"):
        graph.validate()


def test_validate_rejects_edge_to_unknown_node() -> None:
    graph = Graph("dangling")
    graph.node("a", append_note("a")).node("b", append_note("b"))
    graph.entry("a").edge("a", "ghost").terminal("b")
    with pytest.raises(ValueError, match="unknown node"):
        graph.validate()


def test_validate_requires_terminal() -> None:
    graph = Graph("no-terminal")
    graph.node("a", append_note("a")).node("b", append_note("b"))
    graph.entry("a").edge("a", "b")
    with pytest.raises(ValueError, match="terminal"):
        graph.validate()


def test_invalid_graph_returns_error_report_instead_of_raising(tmp_path: Path) -> None:
    """``run`` is the no-raise boundary; validate failures are reported."""
    graph = Graph("no-terminal")
    graph.node("a", append_note("a"))
    graph.entry("a")

    state = TurnState(question="q")
    ctx, _ = make_ctx(tmp_path, state)
    out, report = graph.run(state, ctx)

    assert report.status == "error"
    assert "terminal" in report.error
    assert report.steps == 0
    assert out is state


# ---------------------------------------------------------------------------
# Merge semantics + checkpoints
# ---------------------------------------------------------------------------
def test_merge_records_overwrite_and_keeps_input_state_untouched(tmp_path: Path) -> None:
    original = SQLDraft(sql="SELECT 0")

    def rewrite(state: TurnState, ctx: RunContext) -> NodeResult:
        return NodeResult(patch={"draft": SQLDraft(sql="SELECT 1")})

    graph = Graph("overwrite")
    graph.node("rewrite", rewrite).node("end", append_note("end"))
    graph.entry("rewrite").edge("rewrite", "end").terminal("end")

    state = TurnState(question="q", draft=original)
    ctx, _ = make_ctx(tmp_path, state)
    out, report = graph.run(state, ctx)

    assert report.status == "ok"
    assert "overwrite:draft" in out.notes
    assert out.draft is not None and out.draft.sql == "SELECT 1"
    assert state.draft is not None and state.draft.sql == "SELECT 0"


def test_checkpoint_roundtrip_restores_each_step(tmp_path: Path) -> None:
    graph = Graph("linear")
    graph.node("a", append_note("a")).node("b", append_note("b")).node("c", append_note("c"))
    graph.entry("a").edge("a", "b").edge("b", "c").terminal("c")

    state = TurnState(question="q")
    checkpoint = Checkpoint(tmp_path / "state.sqlite")
    ctx, _ = make_ctx(tmp_path, state, checkpoint=checkpoint)
    out, report = graph.run(state, ctx)

    assert report.steps == 3
    first = checkpoint.load(state.turn_id, 0)
    assert first is not None and first.notes == ["a"]
    second = checkpoint.load(state.turn_id, 1)
    assert second is not None and second.notes == ["a", "b"]
    final = checkpoint.load(state.turn_id, 2)
    assert final == out
    assert checkpoint.latest_seq(state.turn_id) == 2
    assert checkpoint.load(state.turn_id, 99) is None


def test_run_without_checkpoint_or_cache_is_allowed(tmp_path: Path) -> None:
    graph = Graph("bare")
    graph.node("only", append_note("only"))
    graph.entry("only").terminal("only")

    state = TurnState(question="q")
    ctx, _ = make_ctx(tmp_path, state, checkpoint=None)
    assert ctx.checkpoint is None and ctx.cache is None
    out, report = graph.run(state, ctx)

    assert report.status == "ok"
    assert out.notes == ["only"]
