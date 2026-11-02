"""Protocols that decouple the harness from concrete implementations.

The harness only ever sees these shapes. That is what lets the same graph run
against a real LLM, a template-based fallback, or a test double without a
single `if mode == ...` inside the agents themselves.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.core.models import (
    Insight,
    Intent,
    MetricCard,
    ResultSet,
    SQLContext,
    SQLDraft,
    TableCard,
    TurnState,
    Verdict,
)


class LLMResult:
    __slots__ = ("text", "tokens_in", "tokens_out", "model", "ms")

    def __init__(self, text: str, tokens_in: int = 0, tokens_out: int = 0,
                 model: str = "", ms: int = 0) -> None:
        self.text = text
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.model = model
        self.ms = ms

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LLMResult(model={self.model!r}, in={self.tokens_in}, out={self.tokens_out})"


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(self, *, system: str, user: str, json_mode: bool = False) -> LLMResult:
        ...


@runtime_checkable
class Retriever(Protocol):
    def search(self, question: str, *, budget: int,
               hints: list[str] | None = None) -> SQLContext:
        ...

    def all_tables(self) -> list[TableCard]:
        ...

    def metric(self, key: str) -> MetricCard | None:
        ...

    def metrics(self) -> list[MetricCard]:
        ...


@runtime_checkable
class Repo(Protocol):
    def table_names(self) -> set[str]:
        ...

    def run_query(self, sql: str) -> ResultSet:
        ...

    def schema_digest(self) -> str:
        ...


@runtime_checkable
class Trace(Protocol):
    def span(self, node: str) -> Any:
        ...

    def annotate(self, **fields: Any) -> None:
        ...

    def finish(self, status: str, payload: dict[str, Any] | None = None) -> None:
        ...


@runtime_checkable
class Inferencer(Protocol):
    """Everything that needs a model, behind one seam.

    `TemplateInferencer` satisfies this without any network call, which is how
    the whole pipeline stays runnable (and the ablation honest) when no API key
    is configured.
    """

    name: str

    def intent(self, state: TurnState) -> Intent:
        ...

    def sql(self, state: TurnState, feedback: str = "") -> SQLDraft:
        ...

    def insight(self, state: TurnState) -> Insight:
        ...

    def critique(self, state: TurnState) -> Verdict:
        ...
