"""Wiring: dependencies, graph topology, and the single `ask()` entry point.

This module is the only place that knows the graph shape. Everything else is
reusable, which is what allows the ablation runs to swap one node or the whole
inferencer without touching the agents.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace

from app.agents import nodes
from app.agents.dims import DimIndex
from app.agents.inferencers import TemplateInferencer, build_inferencer
from app.core.config import Settings, load_settings
from app.core.models import Answer, TurnState
from app.core.protocols import Inferencer, LLMProvider
from app.db.repo import WarehouseRepo
from app.harness.checkpoint import Checkpoint, SemanticCache
from app.harness.context import RunContext
from app.harness.graph import Graph, RunReport
from app.llm.openai_compat import OpenAICompatProvider
from app.obs.trace import TraceRecorder, TraceStore
from app.semantic.loader import SemanticBundle, load_semantic
from app.semantic.retriever import SemanticRetriever

logger = logging.getLogger(__name__)


VARIANTS = ("full", "single", "no_schema", "no_critic")


@dataclass
class Deps:
    settings: Settings
    bundle: SemanticBundle
    retriever: SemanticRetriever
    repo: WarehouseRepo
    dims: DimIndex
    trace_store: TraceStore
    inferencer: Inferencer
    provider: LLMProvider | None = None
    checkpoint: Checkpoint | None = None
    cache: SemanticCache | None = None
    variant: str = "full"


def build_provider(settings: Settings) -> LLMProvider | None:
    if settings.is_stub:
        return None
    return OpenAICompatProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_s=settings.llm_timeout_s,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
    )


def build_deps(
    settings: Settings | None = None,
    *,
    force_template: bool = False,
    variant: str = "full",
) -> Deps:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; expected one of {VARIANTS}")
    settings = settings or load_settings()
    if force_template and settings.llm_mode != "stub":
        # 强制模板模式时必须把 settings 一起降级，否则 /api/health 会报 "api"
        # 并带出一个模型名，而实际跑的是规则模板——对外宣称与事实不符。
        # 评测侧同理：mode 字段会污染成 api，让模板基线被误读成模型成绩。
        settings = replace(settings, llm_mode="stub", llm_api_key="")
    bundle = load_semantic(settings.semantic_dir)
    repo = WarehouseRepo(settings.db_path, settings.row_limit, settings.query_timeout_ms)
    retriever = SemanticRetriever(bundle)
    dims = DimIndex.build(repo)
    trace_store = TraceStore(settings.state_db_path)
    provider = None if force_template else build_provider(settings)
    inferencer = build_inferencer(dims, provider)
    return Deps(
        settings=settings,
        bundle=bundle,
        retriever=retriever,
        repo=repo,
        dims=dims,
        trace_store=trace_store,
        inferencer=inferencer,
        provider=provider,
        checkpoint=Checkpoint(settings.state_db_path),
        cache=SemanticCache(settings.state_db_path),
        variant=variant,
    )


_DEPS: Deps | None = None


def get_deps() -> Deps:
    global _DEPS
    if _DEPS is None:
        _DEPS = build_deps()
    return _DEPS


def reset_deps() -> None:
    """Drop the process-wide container (tests and eval runs need a clean one)."""
    global _DEPS
    _DEPS = None


def build_graph(settings: Settings, variant: str = "full") -> Graph:
    """Build one of four graphs. The variants exist so the ablation is real work.

    `single`   : flat schema dump, no retry, no critic — the naive baseline.
    `no_schema`: full pipeline but relevance ranking removed.
    `no_critic`: full pipeline but the self-check always passes.
    `full`     : the system described in the README.
    """
    linker_fn = nodes.linker_wide if variant in ("single", "no_schema") else nodes.linker
    critic_fn = nodes.critic_pass if variant in ("single", "no_critic") else nodes.critic
    max_retries = 0 if variant == "single" else settings.max_retries

    graph = Graph(f"askdata-{variant}")
    graph.node("intake", nodes.intake)
    graph.node("linker", linker_fn)
    graph.node("sqlcraft", nodes.sqlcraft)
    graph.node("executor", nodes.executor)
    graph.node("analyst", nodes.analyst)
    graph.node("critic", critic_fn)
    graph.node("clarify", nodes.clarify)
    graph.node("failure", nodes.failure)
    graph.node("reporter", nodes.reporter)

    graph.entry("intake")
    graph.branch("intake", nodes.route_intake, {"query": "linker", "clarify": "clarify"})
    graph.branch("linker", nodes.route_linker, {"ok": "sqlcraft", "clarify": "clarify"})
    graph.branch("sqlcraft", nodes.route_sqlcraft, {"ok": "executor", "clarify": "failure"})
    graph.branch(
        "executor",
        nodes.make_route_executor(max_retries),
        {"ok": "analyst", "retry": "sqlcraft", "give_up": "failure"},
    )
    graph.edge("analyst", "critic")
    graph.branch(
        "critic",
        nodes.make_route_critic(settings.max_critiques),
        {"pass": "reporter", "rework": "analyst", "exhausted": "reporter"},
    )
    graph.terminal("reporter", "clarify", "failure")
    graph.validate()
    return graph


def cache_key(question: str, schema_digest: str, mode: str, inferencer_name: str) -> str:
    raw = "|".join([question.strip().lower(), schema_digest, mode, inferencer_name])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def ask(
    question: str,
    *,
    session_id: str = "",
    history: list[dict] | None = None,
    deps: Deps | None = None,
    use_cache: bool = True,
) -> tuple[Answer, RunReport]:
    """Answer one question end to end. The only public entry point."""
    question = (question or "").strip()
    if not question:
        raise ValueError("question 不能为空")

    deps = deps or get_deps()
    settings = deps.settings
    state = TurnState(
        question=question,
        session_id=session_id,
        mode=settings.effective_mode,
        history=list(history or []),
    )
    recorder = TraceRecorder(deps.trace_store, state)
    key = cache_key(
        question, deps.repo.schema_digest(), settings.effective_mode,
        f"{deps.inferencer.name}:{deps.variant}",
    )

    if use_cache and deps.cache is not None and settings.cache_enabled:
        hit = deps.cache.get(key)
        if hit:
            try:
                cached = Answer.model_validate(hit)
            except Exception:  # stale payload shape after a model change
                logger.info("cache payload no longer valid, ignoring")
            else:
                cached = cached.model_copy(update={"cached": True, "trace_id": state.turn_id})
                with recorder.span("cache") as span:
                    span.meta["hit"] = True
                recorder.finish("ok", cached.model_dump())
                return cached, RunReport(path=["cache"], status="ok", steps=0)

    ctx = RunContext(
        settings=settings,
        provider=deps.provider,
        retriever=deps.retriever,
        repo=deps.repo,
        trace=recorder,
        inferencer=deps.inferencer,
        checkpoint=deps.checkpoint,
        cache=deps.cache,
    )
    final_state, report = build_graph(settings, deps.variant).run(state, ctx)

    answer = final_state.answer
    if answer is None:
        detail = report.error or "流程异常终止"
        answer = Answer(
            question=question,
            status="failed",
            message=f"这条问题没有跑完：{detail}",
            mode=settings.effective_mode,
        )
    answer = answer.model_copy(
        update={
            "elapsed_ms": report.elapsed_ms,
            "trace_id": final_state.turn_id,
            "mode": settings.effective_mode,
        }
    )
    recorder.finish(report.status if report.status != "error" else "error", answer.model_dump())

    if (
        use_cache
        and deps.cache is not None
        and settings.cache_enabled
        and answer.status == "ok"
        and not final_state.cached
    ):
        deps.cache.put(key, answer.model_dump())

    return answer, report
