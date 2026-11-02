"""A hand-rolled state-graph engine: nodes, edges, conditional edges.

Why not a graph framework
-------------------------
The pipeline is one linear spine plus exactly two loops (SQL retry, critic
bounce). A framework would buy us scheduling we do not need, and it would hide
the three things that actually matter here -- the ``max_steps`` brake, the
per-node trace span and the per-step checkpoint -- behind callbacks. Owning the
~40-line loop keeps those visible, keeps the dependency list empty (the runtime
has no package manager) and keeps the whole engine readable in one sitting.

Contract (docs/01-实现契约.md 2.1)
---------------------------------
* A node is ``(TurnState, RunContext) -> NodeResult | dict``. It must not mutate
  the state it receives; it returns a patch and the engine merges it with
  ``TurnState.merge`` (which records overwrites in ``state.notes``).
* A node never decides where to go next. Routing lives in the graph: a plain
  ``edge`` or a ``branch`` whose router reads ``(state, NodeResult)``. That is
  what lets the retry loop and the reject loop be data, not ``goto`` calls
  scattered through the agents.
* ``run`` does not raise. Node bugs become ``RunReport(status="error")`` with
  the exception text, and the returned state is the last successfully merged
  one -- the API layer decides whether that is a 500 or a degradation.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.models import TurnState

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from app.harness.context import RunContext

logger = logging.getLogger(__name__)

_DEFAULT_MAX_STEPS = 24


def _describe(exc: BaseException) -> str:
    """``KeyError: 'x'`` style text, good enough to debug from the trace table."""
    return f"{type(exc).__name__}: {exc}"


def _elapsed_ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


@dataclass
class NodeResult:
    """What a node hands back: a state patch plus a self-report.

    ``status`` is the node's own verdict (``ok | retry | reject | error``) and
    is routed on, not acted on by the engine: a node reporting ``retry`` keeps
    the run alive so the branch can send it back. Raising is reserved for bugs.
    """

    patch: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    meta: dict[str, Any] = field(default_factory=dict)


NodeFn = Callable[[TurnState, "RunContext"], NodeResult | dict[str, Any]]
RouterFn = Callable[[TurnState, NodeResult], str]


@dataclass
class RunReport:
    """Post-mortem of one ``run``: what was walked, how it ended, how long it took."""

    path: list[str] = field(default_factory=list)
    status: str = "ok"
    steps: int = 0
    elapsed_ms: int = 0
    error: str = ""
    edge_taken: list[str] = field(default_factory=list)


class Graph:
    """Mutable builder + synchronous executor for one named state graph."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._nodes: dict[str, NodeFn] = {}
        self._edges: dict[str, str] = {}
        self._branches: dict[str, tuple[RouterFn, dict[str, str]]] = {}
        self._entry: str | None = None
        self._terminals: set[str] = set()

    # -- building ----------------------------------------------------------
    def node(self, name: str, fn: NodeFn) -> Graph:
        """Register a node. Names are unique; re-registering is a wiring bug."""
        if name in self._nodes:
            raise ValueError(f"graph {self.name!r}: node {name!r} already registered")
        self._nodes[name] = fn
        return self

    def edge(self, src: str, dst: str) -> Graph:
        """Unconditional transition. A branch on ``src`` takes precedence over it."""
        self._edges[src] = dst
        return self

    def branch(
        self,
        src: str,
        router: RouterFn,
        mapping: dict[str, str],
    ) -> Graph:
        """Conditional transition: ``router`` picks a key, ``mapping`` resolves it.

        Keeping the router outside the node is deliberate -- the retry loop
        (``retry`` when ``retries < max_retries``) and the reject loop (back to
        the analyst) are then plain data, and a node stays reusable in a graph
        with a different policy.
        """
        if not mapping:
            raise ValueError(f"graph {self.name!r}: branch from {src!r} has an empty mapping")
        self._branches[src] = (router, dict(mapping))
        return self

    def entry(self, name: str) -> Graph:
        """Set the first node executed by ``run``."""
        self._entry = name
        return self

    def terminal(self, *names: str) -> Graph:
        """Mark nodes that end the run. Their function still executes, then we stop."""
        self._terminals.update(names)
        return self

    # -- validation --------------------------------------------------------
    def validate(self) -> None:
        """Fail fast on a broken wiring, before any node runs.

        Checks the three things that make a graph un-runnable or un-finishable:
        an entry, resolvable targets, and at least one terminal (without one,
        the only way out would be ``max_steps``).
        """
        if self._entry is None:
            raise ValueError(f"graph {self.name!r}: entry node is not set")
        if self._entry not in self._nodes:
            raise ValueError(f"graph {self.name!r}: entry {self._entry!r} is not a node")
        if not self._terminals:
            raise ValueError(f"graph {self.name!r}: at least one terminal node is required")
        for name in sorted(self._terminals):
            if name not in self._nodes:
                raise ValueError(f"graph {self.name!r}: terminal {name!r} is not a node")
        for src, dst in self._edges.items():
            if src not in self._nodes:
                raise ValueError(f"graph {self.name!r}: edge from unknown node {src!r}")
            if dst not in self._nodes:
                raise ValueError(f"graph {self.name!r}: edge {src!r} -> unknown node {dst!r}")
        for src, (_, mapping) in self._branches.items():
            if src not in self._nodes:
                raise ValueError(f"graph {self.name!r}: branch from unknown node {src!r}")
            for key, dst in mapping.items():
                if dst not in self._nodes:
                    raise ValueError(
                        f"graph {self.name!r}: branch {src!r}[{key!r}] -> unknown node {dst!r}"
                    )

    # -- execution ---------------------------------------------------------
    def run(self, state: TurnState, ctx: RunContext) -> tuple[TurnState, RunReport]:
        """Walk the graph from ``entry`` until a terminal or a stop condition.

        Returns the last successfully merged state (never ``None``) and a report.
        ``max_steps`` is the only thing standing between a mis-wired loop and an
        infinite request; exceeding it is reported, not raised.
        """
        started = time.perf_counter()
        report = RunReport()

        try:
            self.validate()
        except Exception as exc:
            report.status = "error"
            report.error = _describe(exc)
            report.elapsed_ms = _elapsed_ms(started)
            logger.error("graph %s is not runnable: %s", self.name, report.error)
            return state, report

        max_steps = int(getattr(ctx.settings, "max_steps", _DEFAULT_MAX_STEPS) or _DEFAULT_MAX_STEPS)
        current: str | None = self._entry
        seq = 0  # checkpoint index, one per executed step

        while current is not None:
            if report.steps >= max_steps:
                report.status = "max_steps"
                report.error = f"max_steps={max_steps} reached, next node was {current!r}"
                logger.warning("graph %s: %s", self.name, report.error)
                break

            fn = self._nodes[current]
            report.path.append(current)

            try:
                result = self._run_node(current, fn, state, ctx)
            except Exception as exc:
                # The span already recorded status="error"; the engine only
                # decides what the caller sees. State stays where it last merged.
                report.status = "error"
                report.error = _describe(exc)
                report.steps += 1
                logger.exception("graph %s: node %s failed", self.name, current)
                break

            state = state.merge(result.patch)
            report.steps += 1
            self._save_checkpoint(ctx, state, seq)
            seq += 1

            if current in self._terminals:
                current = None
                break

            try:
                current, key = self._advance(current, state, result)
            except Exception as exc:
                report.status = "error"
                report.error = _describe(exc)
                logger.error("graph %s: routing after %s failed: %s", self.name, current, report.error)
                break

            if key is not None:
                report.edge_taken.append(key)
            if current is None:
                logger.warning("graph %s: node %r has no outgoing edge", self.name, report.path[-1])

        report.elapsed_ms = _elapsed_ms(started)
        logger.info(
            "graph %s finished: status=%s steps=%s path=%s",
            self.name, report.status, report.steps, "->".join(report.path),
        )
        return state, report

    # -- internals ---------------------------------------------------------
    def _run_node(self, node: str, fn: NodeFn, state: TurnState, ctx: RunContext) -> NodeResult:
        """Execute one node inside a span and normalise whatever it returned."""
        with ctx.trace.span(node) as span:
            try:
                raw = fn(state, ctx)
            except Exception as exc:
                # Record first, re-raise second: the span must be persisted even
                # when the node blew up, otherwise the trace explains nothing.
                span.status = "error"
                span.error = _describe(exc)
                raise
            result = raw if isinstance(raw, NodeResult) else NodeResult(patch=dict(raw or {}))
            span.status = result.status
            if result.meta:
                span.meta.update(result.meta)
            span.meta["patch_keys"] = sorted(result.patch)
        return result

    def _advance(
        self, node: str, state: TurnState, result: NodeResult
    ) -> tuple[str | None, str | None]:
        """Resolve the next node (and the branch key, if a branch was used)."""
        if node in self._branches:
            router, mapping = self._branches[node]
            key = router(state, result)
            if key not in mapping:
                raise KeyError(
                    f"branch after {node!r} returned {key!r}; expected one of {sorted(mapping)}"
                )
            return mapping[key], key
        return self._edges.get(node), None

    def _save_checkpoint(self, ctx: RunContext, state: TurnState, seq: int) -> None:
        """Best-effort snapshot; a broken checkpoint must not break the answer."""
        checkpoint = getattr(ctx, "checkpoint", None)
        if checkpoint is None:
            return
        try:
            checkpoint.save(state.turn_id, seq, state)
        except Exception:
            logger.exception("checkpoint save failed (turn=%s seq=%s)", state.turn_id, seq)
