"""Core data contracts.

Every agent takes and returns Pydantic models. No agent talks to another
through free-form dicts, and no agent mutates the shared state in place:
each one returns a `StatePatch` that the harness merges. That constraint is
what makes the pipeline testable node-by-node and what makes the trace
readable after the fact.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NodeName = Literal[
    "intake", "linker", "sqlcraft", "executor", "analyst", "critic",
    "clarify", "reporter", "end",
]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Intake
# --------------------------------------------------------------------------
class Ambiguity(BaseModel):
    """A vague term in the question plus the reading we committed to."""
    term: str
    options: list[str] = Field(default_factory=list)
    chosen: str = ""
    reason: str = ""


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["query", "chat", "out_of_scope"] = "query"
    rewritten: str = ""                      # 指代消解后的完整问题
    entities: dict[str, Any] = Field(default_factory=dict)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    defaults_applied: list[str] = Field(default_factory=list)
    needs_clarify: bool = False
    clarify_question: str = ""


# --------------------------------------------------------------------------
# Semantic layer
# --------------------------------------------------------------------------
class TableCard(BaseModel):
    name: str
    grain: str = ""                          # 一行代表什么
    columns: list[str] = Field(default_factory=list)
    meaning: str = ""
    caveats: list[str] = Field(default_factory=list)
    score: float = 0.0
    hit_reason: str = ""


class MetricCard(BaseModel):
    key: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    definition: str = ""
    formula: str = ""
    unit: str = ""
    tables: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    sql_template: str = ""
    score: float = 0.0
    hit_reason: str = ""


class FewShot(BaseModel):
    question: str
    sql: str
    note: str = ""
    score: float = 0.0


class SQLContext(BaseModel):
    tables: list[TableCard] = Field(default_factory=list)
    metrics: list[MetricCard] = Field(default_factory=list)
    examples: list[FewShot] = Field(default_factory=list)
    join_hints: list[str] = Field(default_factory=list)
    token_est: int = 0
    dropped: int = 0                         # 因预算被裁掉的卡片数
    # 输出约定（行数 / 小数位 / 最小样本），来自 semantic/defaults.yml。
    # 放在上下文里而不是提示词常量里，是为了让它可配置、可测试、可被 trace 记录。
    defaults: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# SQL generation / execution
# --------------------------------------------------------------------------
class SQLDraft(BaseModel):
    sql: str = ""
    rationale: str = ""
    used_tables: list[str] = Field(default_factory=list)
    source: Literal["llm", "template", "fallback"] = "llm"


class ExecutionError(BaseModel):
    stage: Literal["guard", "execute"] = "execute"
    message: str = ""
    hint: str = ""
    sql: str = ""

    def feedback(self) -> str:
        """Text handed back to SQLCraft on the retry path."""
        parts = [f"上一版 SQL 执行失败（阶段：{self.stage}）。", f"错误：{self.message}"]
        if self.hint:
            parts.append(f"提示：{self.hint}")
        return "\n".join(parts)


class ResultSet(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    rowcount: int = 0
    elapsed_ms: int = 0
    truncated: bool = False
    numeric_cols: list[str] = Field(default_factory=list)

    def preview(self, n: int = 20) -> list[list[Any]]:
        return self.rows[:n]


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
ChartType = Literal["bar", "line", "pie", "none"]


class ChartSpec(BaseModel):
    type: ChartType = "none"
    title: str = ""
    x_field: str = ""
    y_field: str = ""
    points: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = ""


InsightShape = Literal["scalar", "ranking", "timeseries", "share", "detail", "empty"]


class Insight(BaseModel):
    shape: InsightShape = "detail"
    headline: str = ""
    bullets: list[str] = Field(default_factory=list)
    footnotes: list[str] = Field(default_factory=list)
    chart: ChartSpec | None = None


class Verdict(BaseModel):
    passed: bool = True
    severity: Literal["info", "warn", "block"] = "info"
    reasons: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Final answer
# --------------------------------------------------------------------------
class Answer(BaseModel):
    question: str = ""
    rewritten: str = ""
    sql: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    rowcount: int = 0
    insight: Insight | None = None
    verdict: Verdict | None = None
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    cached: bool = False
    elapsed_ms: int = 0
    trace_id: str = ""
    mode: str = "stub"
    status: Literal["ok", "clarify", "failed", "rejected"] = "ok"
    message: str = ""


# --------------------------------------------------------------------------
# Turn state
# --------------------------------------------------------------------------
_OVERWRITE_TRACKED = (
    "intent", "context", "draft", "result", "insight", "verdict", "answer", "error",
)


class TurnState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    turn_id: str = Field(default_factory=lambda: new_id("turn"))
    session_id: str = ""
    question: str = ""
    mode: str = "stub"

    intent: Intent | None = None
    context: SQLContext | None = None
    draft: SQLDraft | None = None
    executed_sql: str = ""
    result: ResultSet | None = None
    error: ExecutionError | None = None
    insight: Insight | None = None
    verdict: Verdict | None = None
    answer: Answer | None = None

    retries: int = 0
    critiques: int = 0
    cached: bool = False
    history: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def merge(self, patch: dict[str, Any] | None) -> "TurnState":
        """Merge a node's patch into a copy of this state.

        Overwriting an already-populated slot is allowed (the retry path does
        exactly that) but it gets recorded in `notes`, so a confusing trace can
        always be explained afterwards.
        """
        if not patch:
            return self
        unknown = set(patch) - set(type(self).model_fields)
        if unknown:
            raise KeyError(f"patch contains unknown fields: {sorted(unknown)}")
        data = self.model_dump()
        for key, value in patch.items():
            if key in _OVERWRITE_TRACKED and data.get(key) is not None and value is not None:
                data.setdefault("notes", []).append(f"overwrite:{key}")
            data[key] = value
        return TurnState.model_validate(data)


class RunLimits(BaseModel):
    max_steps: int = 24
    max_retries: int = 3
    max_critiques: int = 2
