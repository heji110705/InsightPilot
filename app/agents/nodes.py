"""The six agents, plus the two terminal nodes.

Every node is `(TurnState, RunContext) -> NodeResult`. No node mutates the state
it receives, no node calls another node, and no node decides what runs next —
routing lives in the graph definition. That separation is why the retry loop and
the reject loop are data rather than control flow buried inside a function.
"""
from __future__ import annotations

import logging

from app.agents.stats import repair_insight
from app.core.models import Answer, ExecutionError, SQLContext, TurnState, Verdict
from app.db.guard import validate_sql
from app.db.repo import QueryFailed
from app.harness.context import RunContext
from app.harness.graph import NodeResult

logger = logging.getLogger(__name__)

CHAT_REPLY = (
    "你好，我是问数助手。可以这样问我：\n"
    "· 我们库里一共有多少本书？\n"
    "· 各分类的平均评分是多少？\n"
    "· 评分人数最多的前 10 本书是哪些？"
)
OUT_OF_SCOPE_REPLY = (
    "这个问题超出了当前数据范围。我手上的仓库只有书籍、评分、评论、点击和书架五类数据，"
    "其它话题我答不了——编一个答案比说不知道更糟。"
)


def display_sql(sql: str, row_limit: int) -> str:
    """Undo the guard's outer row cap for display only.

    The guard wraps unbounded queries as `SELECT * FROM (<sql>) LIMIT n`, which
    is the right thing to execute but unreadable in a UI. The executed form stays
    in the trace; the user sees what the model actually wrote.
    """
    text = (sql or "").strip()
    prefix = "SELECT * FROM ("
    suffix = f") LIMIT {row_limit}"
    if text.upper().startswith(prefix.upper()) and text.upper().endswith(suffix.upper()):
        return text[len(prefix): -len(suffix)].strip()
    return text


# --------------------------------------------------------------------------
# ① Intake
# --------------------------------------------------------------------------
def intake(state: TurnState, ctx: RunContext) -> NodeResult:
    if ctx.inferencer is None:
        raise RuntimeError("RunContext.inferencer is required")
    intent = ctx.inferencer.intent(state)
    status = "ok"
    if intent.kind != "query" or intent.needs_clarify:
        status = "clarify"
    return NodeResult(
        patch={"intent": intent},
        status=status,
        meta={
            "kind": intent.kind,
            "rewritten": intent.rewritten[:80],
            "entities": sorted(intent.entities),
            "ambiguities": len(intent.ambiguities),
            "needs_clarify": intent.needs_clarify,
        },
    )


# --------------------------------------------------------------------------
# ② SchemaLinker
# --------------------------------------------------------------------------
def linker(state: TurnState, ctx: RunContext) -> NodeResult:
    intent = state.intent
    question = (intent.rewritten if intent and intent.rewritten else state.question) or ""
    hints = [str(v) for v in (intent.entities if intent else {}).values() if isinstance(v, str)]
    context = ctx.retriever.search(
        question, budget=ctx.settings.context_token_budget, hints=hints
    )
    status = "ok" if context.tables else "no_context"
    return NodeResult(
        patch={"context": context},
        status=status,
        meta={
            "tables": [t.name for t in context.tables],
            "metrics": [m.key for m in context.metrics][:4],
            "examples": len(context.examples),
            "token_est": context.token_est,
            "dropped": context.dropped,
        },
    )


# --------------------------------------------------------------------------
# ③ SQLCraft
# --------------------------------------------------------------------------
def sqlcraft(state: TurnState, ctx: RunContext) -> NodeResult:
    if ctx.inferencer is None:
        raise RuntimeError("RunContext.inferencer is required")
    feedback = state.error.feedback() if state.error is not None else ""
    draft = ctx.inferencer.sql(state, feedback)
    sql = (draft.sql or "").strip()
    status = "ok" if sql else "empty"
    return NodeResult(
        patch={"draft": draft, "executed_sql": sql},
        status=status,
        meta={
            "source": draft.source,
            "chars": len(sql),
            "used_tables": draft.used_tables,
            "had_feedback": bool(feedback),
        },
    )


# --------------------------------------------------------------------------
# ④ Executor
# --------------------------------------------------------------------------
def executor(state: TurnState, ctx: RunContext) -> NodeResult:
    draft = state.draft
    if draft is None or not draft.sql.strip():
        error = ExecutionError(stage="guard", message="没有可执行的 SQL", sql="")
        return NodeResult(
            patch={"error": error, "retries": state.retries + 1},
            status="retry",
            meta={"reason": "empty_sql"},
        )

    guard = validate_sql(draft.sql, ctx.repo.table_names(), ctx.settings.row_limit)
    if not guard.ok:
        error = ExecutionError(
            stage="guard", message=guard.reason, sql=draft.sql,
            hint="只允许对上面的表做单条 SELECT，且必须带 LIMIT。",
        )
        return NodeResult(
            patch={"error": error, "retries": state.retries + 1},
            status="retry",
            meta={"guard": "rejected", "reason": guard.reason[:120]},
        )

    try:
        result = ctx.repo.run_query(guard.sql)
    except QueryFailed as exc:
        error = ExecutionError(stage=exc.stage, message=exc.message, hint=exc.hint, sql=guard.sql)
        return NodeResult(
            patch={"error": error, "retries": state.retries + 1},
            status="retry",
            meta={"stage": exc.stage, "message": exc.message[:120]},
        )

    return NodeResult(
        patch={
            "result": result,
            "error": None,
            "executed_sql": guard.sql,
            "retries": state.retries,
        },
        status="ok",
        meta={
            "rows": result.rowcount,
            "cols": len(result.columns),
            "ms": result.elapsed_ms,
            "truncated": result.truncated,
            "tables": guard.tables,
        },
    )


# --------------------------------------------------------------------------
# ⑤ Analyst
# --------------------------------------------------------------------------
def analyst(state: TurnState, ctx: RunContext) -> NodeResult:
    if ctx.inferencer is None:
        raise RuntimeError("RunContext.inferencer is required")
    insight = ctx.inferencer.insight(state)
    repaired = False
    if state.verdict is not None and not state.verdict.passed and state.result is not None:
        insight = repair_insight(insight, state.verdict.reasons, state.result)
        repaired = True
    return NodeResult(
        patch={"insight": insight},
        status="ok",
        meta={
            "shape": insight.shape,
            "chart": insight.chart.type if insight.chart else "none",
            "bullets": len(insight.bullets),
            "repaired": repaired,
        },
    )


# --------------------------------------------------------------------------
# ⑥ Critic
# --------------------------------------------------------------------------
def critic(state: TurnState, ctx: RunContext) -> NodeResult:
    if ctx.inferencer is None:
        raise RuntimeError("RunContext.inferencer is required")
    verdict = ctx.inferencer.critique(state)
    patch: dict[str, object] = {"verdict": verdict}
    if not verdict.passed:
        patch["critiques"] = state.critiques + 1
    return NodeResult(
        patch=patch,
        status="pass" if verdict.passed else "fail",
        meta={
            "passed": verdict.passed,
            "severity": verdict.severity,
            "reasons": verdict.reasons[:3],
        },
    )


# --------------------------------------------------------------------------
# terminals
# --------------------------------------------------------------------------
def linker_wide(state: TurnState, ctx: RunContext) -> NodeResult:
    """Ablation node: hand over every table card instead of ranking relevance.

    This is what "no semantic-layer retrieval" actually looks like in practice —
    not an empty prompt, but a flat dump of the whole schema, which is the most
    common way text-to-SQL is built and the thing this project argues against.
    """
    tables = ctx.retriever.all_tables()
    metrics = ctx.retriever.metrics()[:8]
    context = SQLContext(
        tables=tables,
        metrics=metrics,
        examples=[],
        join_hints=[],
        dropped=max(0, len(ctx.retriever.metrics()) - 8),
    )
    context.token_est = len("".join(t.name + "".join(t.columns) for t in tables)) * 3 // 5
    return NodeResult(
        patch={"context": context},
        status="ok" if tables else "no_context",
        meta={"tables": [t.name for t in tables], "mode": "wide", "token_est": context.token_est},
    )


def critic_pass(state: TurnState, ctx: RunContext) -> NodeResult:
    """Ablation node: the critic is switched off and always passes."""
    verdict = Verdict(passed=True, severity="info", reasons=["（消融）自检已关闭"])
    return NodeResult(
        patch={"verdict": verdict},
        status="pass",
        meta={"passed": True, "ablation": "critic_disabled"},
    )


def clarify(state: TurnState, ctx: RunContext) -> NodeResult:
    intent = state.intent
    if intent is not None and intent.kind == "chat":
        message = CHAT_REPLY
    elif intent is not None and intent.kind == "out_of_scope":
        message = OUT_OF_SCOPE_REPLY
    elif intent is not None and intent.clarify_question:
        message = intent.clarify_question
    else:
        message = "这个问题需要补充一点信息：你想看哪个指标、按什么维度拆？"

    answer = Answer(
        question=state.question,
        rewritten=intent.rewritten if intent else "",
        status="clarify",
        message=message,
        mode=state.mode,
    )
    return NodeResult(patch={"answer": answer}, status="clarify", meta={"message": message[:60]})


def failure(state: TurnState, ctx: RunContext) -> NodeResult:
    """Terminal for the give-up path: report honestly, never invent an answer."""
    detail = state.error.message if state.error is not None else "未能生成可执行的查询"
    hint = state.error.hint if state.error is not None else ""
    message = f"这个问题我没能可靠地回答。原因：{detail}"
    if hint:
        message += f"（{hint}）"
    answer = Answer(
        question=state.question,
        rewritten=state.intent.rewritten if state.intent else "",
        sql=state.executed_sql,
        status="failed",
        message=message,
        mode=state.mode,
    )
    return NodeResult(patch={"answer": answer}, status="failed", meta={"reason": detail[:100]})


def reporter(state: TurnState, ctx: RunContext) -> NodeResult:
    result = state.result
    rejected = (
        state.verdict is not None
        and not state.verdict.passed
        and state.critiques >= ctx.settings.max_critiques
    )
    message = ""
    if rejected and state.verdict is not None:
        message = "结论未通过自检：" + "；".join(state.verdict.reasons[:2])

    answer = Answer(
        question=state.question,
        rewritten=state.intent.rewritten if state.intent else "",
        sql=display_sql(state.executed_sql, ctx.settings.row_limit),
        columns=result.columns if result else [],
        rows=result.preview(200) if result else [],
        rowcount=result.rowcount if result else 0,
        insight=state.insight,
        verdict=state.verdict,
        ambiguities=state.intent.ambiguities if state.intent else [],
        cached=state.cached,
        mode=state.mode,
        status="rejected" if rejected else "ok",
        message=message,
    )
    summary = state.insight.headline if state.insight else message
    return NodeResult(
        patch={"answer": answer},
        status="ok",
        meta={"status": answer.status, "rows": answer.rowcount, "headline": (summary or "")[:80]},
    )


# --------------------------------------------------------------------------
# routers
# --------------------------------------------------------------------------
def route_intake(state: TurnState, result: NodeResult) -> str:
    return "clarify" if result.status == "clarify" else "query"


def route_linker(state: TurnState, result: NodeResult) -> str:
    return "ok" if result.status == "ok" else "clarify"


def route_sqlcraft(state: TurnState, result: NodeResult) -> str:
    return "ok" if result.status == "ok" else "clarify"


def make_route_executor(max_retries: int):
    """Retry at most `max_retries` times, i.e. up to `max_retries + 1` attempts.

    The limit is bound here rather than read from settings inside the router,
    because a router only receives `(state, result)` — keeping the engine free of
    configuration lookups.
    """

    def route(state: TurnState, result: NodeResult) -> str:
        if result.status == "ok" and state.result is not None:
            return "ok"
        return "retry" if state.retries <= max_retries else "give_up"

    return route


def make_route_critic(max_critiques: int):
    """Send the analysis back at most `max_critiques` times before giving up."""

    def route(state: TurnState, result: NodeResult) -> str:
        if state.verdict is not None and state.verdict.passed:
            return "pass"
        return "rework" if state.critiques < max_critiques else "exhausted"

    return route
