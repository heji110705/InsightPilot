"""SQLite-backed checkpoints and the semantic cache.

Both live in the same database file (``SETTINGS.state_db_path``) because they
share one lifecycle: anything we persist between requests goes here.

Design notes
------------
* One long-lived connection per store with ``check_same_thread=False`` and a
  fresh cursor per operation. FastAPI dispatches a request across a thread pool,
  so two threads can hit the same store concurrently; a single connection plus
  an ``RLock`` is enough for a single-machine deployment and avoids the
  bookkeeping a pool would need. No pool on purpose.
* ``seq`` is 0-based and monotonically increasing per turn, so replaying a turn
  is ``load(turn_id, 0)``, ``load(turn_id, 1)``, ... in order.
* Writes are best-effort: ``Graph.run`` wraps ``save`` so a failed checkpoint
  never turns a usable answer into a 500.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.models import TurnState

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints(
  turn_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  state_json TEXT NOT NULL,
  PRIMARY KEY(turn_id, seq));
CREATE TABLE IF NOT EXISTS semantic_cache(
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cache_counters(
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL DEFAULT 0);
"""


def _now() -> str:
    """Wall-clock stamp for humans reading the table; never used for timing math."""
    return datetime.now().isoformat(timespec="seconds")


class _SqliteStore:
    """Shared connection plumbing for the two stores in this module."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False: the FastAPI worker threads are not the thread
        # that built the app. The lock, not the sqlite3 module, is what keeps a
        # multi-threaded caller sane here.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @contextlib.contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        """Yield a fresh cursor, committing on success and rolling back on error."""
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

    def close(self) -> None:
        """Release the connection. Safe to call more than once."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:  # already closed
                pass


class Checkpoint(_SqliteStore):
    """Replayable snapshots of :class:`TurnState`, one row per executed step.

    We store the whole serialised state rather than a diff: a turn is a few
    kilobytes at most, and a full snapshot means ``load`` never has to replay
    earlier rows to rebuild the object.
    """

    def save(self, turn_id: str, seq: int, state: TurnState) -> None:
        """Upsert the snapshot taken right after step ``seq`` completed."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO checkpoints(turn_id, seq, created_at, state_json)"
                " VALUES (?, ?, ?, ?)",
                (turn_id, int(seq), _now(), state.model_dump_json()),
            )

    def load(self, turn_id: str, seq: int) -> TurnState | None:
        """Rebuild the state as it was after step ``seq``; ``None`` if absent."""
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT state_json FROM checkpoints WHERE turn_id = ? AND seq = ?",
                (turn_id, int(seq)),
            ).fetchone()
        if row is None:
            return None
        return TurnState.model_validate_json(row[0])

    def latest_seq(self, turn_id: str) -> int:
        """Highest step stored for a turn, or ``-1`` when the turn is unknown.

        Useful for resuming after a crash: the caller does ``load(latest_seq())``.
        """
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT MAX(seq) FROM checkpoints WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        return -1 if row is None or row[0] is None else int(row[0])


class SemanticCache(_SqliteStore):
    """Key/value cache for generated SQL, keyed by question+schema digest.

    Hit/miss counters are persisted next to the entries instead of kept in
    memory: the numbers are reported per deployment (hit rate is a talking
    point in the eval), and an in-process counter would reset on every reload.
    """

    def get(self, key: str) -> dict[str, Any] | None:
        """Return the cached payload, or ``None`` on miss. Counts the lookup."""
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT value_json FROM semantic_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            self._bump("misses")
            return None
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError:  # pragma: no cover - would mean a corrupted row
            logger.warning("cache entry %s is not valid JSON; treating as miss", key)
            self._bump("misses")
            return None
        if not isinstance(value, dict):
            value = {"value": value}
        self._bump("hits")
        return value

    def put(self, key: str, value: dict[str, Any]) -> None:
        """Insert or replace an entry. Overwrites are intentional (same key, newer SQL)."""
        payload = json.dumps(value, ensure_ascii=False, default=str)
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO semantic_cache(key, value_json, created_at)"
                " VALUES (?, ?, ?)",
                (key, payload, _now()),
            )
        self._bump("writes")

    def hit_rate(self) -> float:
        """``hits / (hits + misses)`` as a fraction in ``[0, 1]``; 0.0 before any lookup."""
        counters = self._counters()
        lookups = counters["hits"] + counters["misses"]
        if lookups <= 0:
            return 0.0
        return round(counters["hits"] / lookups, 4)

    def stats(self) -> dict[str, Any]:
        """Entry count plus hit/miss/write counters and the derived hit rate."""
        with self._cursor() as cur:
            entries = int(
                cur.execute("SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
            )
        counters = self._counters()
        return {
            "entries": entries,
            "hits": counters["hits"],
            "misses": counters["misses"],
            "writes": counters["writes"],
            "hit_rate": self.hit_rate(),
        }

    # -- internals ---------------------------------------------------------
    def _bump(self, name: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO cache_counters(name, value) VALUES (?, 1)"
                " ON CONFLICT(name) DO UPDATE SET value = value + 1",
                (name,),
            )

    def _counters(self) -> dict[str, int]:
        with self._cursor() as cur:
            rows = cur.execute("SELECT name, value FROM cache_counters").fetchall()
        data = {"hits": 0, "misses": 0, "writes": 0}
        for name, value in rows:
            if name in data:
                data[name] = int(value)
        return data
