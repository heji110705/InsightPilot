"""End-to-end trace: one row per turn, one row per node span.

Two tables, deliberately flat (see contract 2.3): ``turns`` is what the history
list renders, ``spans`` is what the drill-down renders. No nesting, no JSON
document per turn -- the API layer can page them independently and a failed
span is still visible even if the turn row was never written.

The recorder is what the harness actually talks to. It owns the span sequence
numbers and the turn's wall clock, so ``Graph.run`` can stay ignorant of both.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.models import TurnState

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns(
  turn_id TEXT PRIMARY KEY,
  session_id TEXT,
  question TEXT,
  mode TEXT,
  status TEXT,
  elapsed_ms INTEGER,
  created_at TEXT,
  payload_json TEXT);
CREATE TABLE IF NOT EXISTS spans(
  turn_id TEXT,
  seq INTEGER,
  node TEXT,
  status TEXT,
  ms INTEGER,
  meta_json TEXT,
  error TEXT,
  PRIMARY KEY(turn_id, seq));
CREATE INDEX IF NOT EXISTS idx_turns_created ON turns(created_at);
"""

_TURN_KEYS = (
    "turn_id", "session_id", "question", "mode", "status", "elapsed_ms",
    "created_at", "payload_json",
)
_SPAN_KEYS = ("turn_id", "seq", "node", "status", "ms", "meta_json", "error")


def _now() -> str:
    """Human-readable stamp for display and ordering; durations come from perf_counter."""
    return datetime.now().isoformat(timespec="seconds")


class TraceStore:
    """SQLite persistence for turns and spans.

    A single connection with ``check_same_thread=False`` plus a lock: FastAPI
    serves requests from a thread pool, and one writer per process is plenty for
    a local analytics demo. Writes are short, so no pool and no WAL tuning.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- writes ------------------------------------------------------------
    def save_span(self, turn_id: str, seq: int, node: str, status: str,
                  ms: int, meta: dict, error: str = "") -> None:
        """Upsert one node span. Re-running a turn overwrites it by ``(turn_id, seq)``."""
        meta_json = json.dumps(meta or {}, ensure_ascii=False, default=str)
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO spans(turn_id, seq, node, status, ms, meta_json, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (turn_id, int(seq), node, status, int(ms), meta_json, error or ""),
            )

    def save_turn(self, turn_id: str, session_id: str, question: str, mode: str,
                  status: str, elapsed_ms: int, payload_json: str) -> None:
        """Upsert the turn header. ``created_at`` is stamped here, on first and later writes."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO turns(turn_id, session_id, question, mode, status,"
                " elapsed_ms, created_at, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (turn_id, session_id, question, mode, status, int(elapsed_ms),
                 _now(), payload_json),
            )

    # -- reads -------------------------------------------------------------
    def list_turns(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first. Keys match what the API layer renders (no payload here)."""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT turn_id, session_id, question, mode, status, elapsed_ms, created_at"
                " FROM turns ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        keys = ("turn_id", "session_id", "question", "mode", "status", "elapsed_ms", "created_at")
        return [dict(zip(keys, row)) for row in rows]

    def get_turn(self, turn_id: str) -> dict[str, Any]:
        """``{"turn": {...}, "spans": [...]}``; empty containers when unknown."""
        with self._cursor() as cur:
            turn_row = cur.execute(
                f"SELECT {', '.join(_TURN_KEYS)} FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            span_rows = cur.execute(
                "SELECT turn_id, seq, node, status, ms, meta_json, error FROM spans"
                " WHERE turn_id = ? ORDER BY seq ASC",
                (turn_id,),
            ).fetchall()
        return {
            "turn": dict(zip(_TURN_KEYS, turn_row)) if turn_row else {},
            "spans": [self._span_dict(row) for row in span_rows],
        }

    def stats(self) -> dict[str, Any]:
        """Aggregate numbers for the header tiles.

        ``avg_steps`` averages over turns that produced spans, so a turn that
        died before its first span does not drag the mean towards zero.
        """
        with self._cursor() as cur:
            total, elapsed_sum, ok = cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(elapsed_ms), 0),"
                " COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0) FROM turns"
            ).fetchone()
            span_total = cur.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
            avg_steps = cur.execute(
                "SELECT AVG(n) FROM (SELECT COUNT(*) AS n FROM spans GROUP BY turn_id)"
            ).fetchone()[0]
        total = int(total)
        return {
            "turns": total,
            "ok": int(ok),
            "success_rate": round(ok / total, 4) if total else 0.0,
            "avg_elapsed_ms": int(round(elapsed_sum / total)) if total else 0,
            "avg_steps": round(float(avg_steps), 2) if avg_steps else 0.0,
            "spans": int(span_total),
        }

    def close(self) -> None:
        """Release the connection. Safe to call twice."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:  # already closed
                pass

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _span_dict(row: tuple) -> dict[str, Any]:
        data = dict(zip(_SPAN_KEYS, row))
        data["meta"] = _loads(data.pop("meta_json"))
        return data

    @contextlib.contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()


def _loads(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - corrupted row only
        logger.warning("span meta is not valid JSON: %r", raw[:80])
        return {}
    return value if isinstance(value, dict) else {"value": value}


class SpanHandle:
    """Mutable view of an open span; whatever ends up here is what gets written."""

    __slots__ = ("node", "meta", "status", "error")

    def __init__(self, node: str) -> None:
        self.node = node
        self.meta: dict[str, Any] = {}
        self.status = "ok"
        self.error = ""


class TraceRecorder:
    """Implements ``core.protocols.Trace`` for exactly one turn.

    The span context manager writes in ``finally`` and re-raises, so a node that
    crashes still leaves a row behind -- the trace is the only place that
    survives a 500. Storage errors are logged and swallowed inside ``finally``
    so a broken trace can never mask the node's real exception.
    """

    def __init__(self, store: TraceStore, state: TurnState) -> None:
        self.store = store
        self.state = state
        self.turn_id = state.turn_id
        self._seq = 0
        self._stack: list[SpanHandle] = []
        self._pending: dict[str, Any] = {}
        self._started = time.perf_counter()

    @contextlib.contextmanager
    def span(self, node: str) -> Iterator[SpanHandle]:
        """Open a span; on exit (ok or not) persist it with status, ms, meta, error."""
        handle = SpanHandle(node)
        if self._pending:
            handle.meta.update(self._pending)
            self._pending.clear()
        self._stack.append(handle)
        started = time.perf_counter()
        try:
            yield handle
        except BaseException as exc:
            handle.status = "error"
            handle.error = handle.error or f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if handle in self._stack:
                self._stack.remove(handle)
            ms = int(round((time.perf_counter() - started) * 1000))
            seq = self._seq
            self._seq += 1
            try:
                self.store.save_span(
                    self.turn_id, seq, node, handle.status, ms, handle.meta, handle.error
                )
            except Exception:  # pragma: no cover - disk full / closed handle
                logger.exception("failed to persist span turn=%s node=%s", self.turn_id, node)

    def annotate(self, **fields: Any) -> None:
        """Attach fields to the innermost open span; buffered if none is open yet."""
        if self._stack:
            self._stack[-1].meta.update(fields)
        else:
            self._pending.update(fields)

    def finish(self, status: str, payload: dict[str, Any] | None = None) -> None:
        """Write the turn row. ``payload`` carries the answer for the detail view."""
        data: dict[str, Any] = dict(payload or {})
        data.setdefault("spans", self._seq)
        if self._pending:
            data.update(self._pending)
            self._pending.clear()
        elapsed_ms = int(round((time.perf_counter() - self._started) * 1000))
        self.store.save_turn(
            self.turn_id,
            self.state.session_id,
            self.state.question,
            self.state.mode,
            status,
            elapsed_ms,
            json.dumps(data, ensure_ascii=False, default=str),
        )
