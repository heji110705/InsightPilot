"""HTTP 契约层：请求校验 + 响应模型。

设计取舍
--------
1. 响应里的 ``answer`` 直接复用 ``app.core.models.Answer``，不再手写一份字段镜像。
   字段以引擎为准，API 层一旦复制一份，模型一改就会漂移。
2. 请求侧只做"边界校验"（空问题、超长问题、history 条数），业务校验留给引擎。
   引擎自己会 raise ``ValueError("question 不能为空")``，但那条路径是 500 语义，
   在入口处拦成 422 更准确。
3. ``ReportOut`` 是 ``RunReport`` 的只读投影，顺手加上 ``edge_taken``，
   前端画链路时不必再猜分支走向。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.models import Answer

#: 问题长度上限。超过就 422，而不是让 sqlite / 模板引擎去处理一段 5000 字的噪声。
MAX_QUESTION_CHARS = 2000
#: 历史轮次上限，避免被塞进一条巨大的请求体。
MAX_HISTORY_TURNS = 20


class HistoryItem(BaseModel):
    """上一轮的问答摘要，只保留指代消解真正需要的两列。"""

    model_config = ConfigDict(extra="ignore")

    question: str = ""
    sql: str = ""


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = ""
    session_id: str = Field(default="", max_length=128)
    history: list[HistoryItem] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)

    @field_validator("question")
    @classmethod
    def _clean_question(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("question 不能为空")
        if len(text) > MAX_QUESTION_CHARS:
            raise ValueError(
                f"question 过长：最多 {MAX_QUESTION_CHARS} 字，当前 {len(text)} 字"
            )
        return text


class ReportOut(BaseModel):
    """``RunReport`` 的投影。``path`` 保留原始节点序列（含重试造成的重复）。"""

    path: list[str] = Field(default_factory=list)
    status: str = "ok"
    steps: int = 0
    elapsed_ms: int = 0
    error: str = ""
    edge_taken: list[str] = Field(default_factory=list)

    @classmethod
    def from_report(cls, report: Any) -> "ReportOut":
        return cls(
            path=list(getattr(report, "path", []) or []),
            status=str(getattr(report, "status", "ok")),
            steps=int(getattr(report, "steps", 0) or 0),
            elapsed_ms=int(getattr(report, "elapsed_ms", 0) or 0),
            error=str(getattr(report, "error", "") or ""),
            edge_taken=list(getattr(report, "edge_taken", []) or []),
        )


class AskResponse(BaseModel):
    answer: Answer
    report: ReportOut


class TurnHeader(BaseModel):
    """``TraceStore.list_turns`` 的一行。"""

    model_config = ConfigDict(extra="ignore")

    turn_id: str = ""
    session_id: str = ""
    question: str = ""
    mode: str = ""
    status: str = ""
    elapsed_ms: int = 0
    created_at: str = ""


class SpanOut(BaseModel):
    """``TraceStore.get_turn`` 的一条 span。``status`` 是节点的自评（ok/pass/fail/retry/error）。"""

    model_config = ConfigDict(extra="ignore")

    turn_id: str = ""
    seq: int = 0
    node: str = ""
    status: str = "ok"
    ms: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class TracesResponse(BaseModel):
    turns: list[TurnHeader] = Field(default_factory=list)
    count: int = 0
    limit: int = 30


class TurnDetail(BaseModel):
    turn: TurnHeader
    spans: list[SpanOut] = Field(default_factory=list)


class CacheStats(BaseModel):
    """两个口径都给出：引擎自己的 lookups 命中率，以及按 turn 统计的"走了缓存"占比。"""

    hit_rate: float = 0.0
    enabled: bool = True
    cached_turns: int = 0
    turns: int = 0
    cached_share: float = 0.0


class StatsOut(BaseModel):
    turns: int = 0
    ok: int = 0
    success_rate: float = 0.0
    avg_elapsed_ms: int = 0
    avg_steps: float = 0.0
    spans: int = 0
    cache: CacheStats = Field(default_factory=CacheStats)
    mode: str = "stub"


class HealthOut(BaseModel):
    status: str = "ok"
    mode: str = "stub"
    model: str = ""
    inferencer: str = ""
    variant: str = "full"
    tables: int = 0
    metrics: int = 0
    fewshots: int = 0
    db_path: str = ""
    db_exists: bool = False
    semantic_dir: str = ""
    cache_enabled: bool = True
