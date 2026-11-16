"""HTTP 外壳：把 ``pipeline.ask()`` 包成同步接口 + SSE 流式接口，并托管单页前端。

关于"节点级 SSE"的实现方式（重要）
----------------------------------
``ask()`` 是同步函数，且在函数内部自己 ``TurnState()``（turn_id 由此生成），
外部既拿不到预分配的 turn_id，也没有节点开始/结束的回调。唯一合法且不修改引擎
的接缝是 **依赖注入**：``ask()`` 把 ``deps.trace_store`` 交给 ``TraceRecorder``，
每个节点结束时 recorder 会调用 ``store.save_span(...)``。

所以这里传入一个 ``Deps`` 的浅拷贝，把 ``trace_store`` 换成 ``SpanBridge`` 代理：
记录照常落库，同时把 ``(node, status, ms, meta, error)`` 投进本请求私有的队列。
事件是**真实的**（与 trace 表里那一行同时产生），不是轮询数据库猜的，也不是假的。

代价（如实记录）：引擎没有"节点开始"的信号，span 只在节点结束时写库，因此推给
前端的是**完成事件**（ok/error/fail/retry + 耗时），没有成对的 start。前端把未收到
事件的节点显示为 pending，链路会按真实顺序逐行落下。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.agents.pipeline import Deps, ask, get_deps
from app.limits import build_from_env
from app.api.schemas import (
    AskRequest,
    AskResponse,
    CacheStats,
    HealthOut,
    ReportOut,
    SpanOut,
    StatsOut,
    TracesResponse,
    TurnDetail,
    TurnHeader,
)

logger = logging.getLogger("insightpilot.api")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "web"

#: 队列空时让出事件循环的间隔。span 由工作线程投递，这里只是轮询队列，不碰数据库。
STREAM_POLL_S = 0.03
#: 单轮问数的硬上限，防止前端永远挂在 pending。
STREAM_DEADLINE_S = 120.0

#: Windows 上 ``mimetypes`` 会把 .js 猜成 text/plain，静态资源的类型在这里写死。
_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}

# 自动文档默认关闭：/docs 会把所有接口（含 /api/traces）列在公网上，
# 等于给访客一张"这里还能这么调"的地图。需要时显式 IP_DOCS=1 打开。
_DOCS_ON = os.environ.get("IP_DOCS", "0").strip() == "1"

app = FastAPI(
    title="InsightPilot",
    version="1.0.0",
    description="问数引擎的 HTTP 外壳：同步问数、SSE 节点流、trace 回放、单页前端。",
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
)

# ---------------------------------------------------------------------------
# 公开演示的防刷闸门
# ---------------------------------------------------------------------------
#: 只拦问数。``/api/stats`` 是前端每几秒轮询一次的，把它也拦了等于页面自己刷自己。
_LIMITED_PATHS = frozenset({"/api/ask", "/api/ask/stream"})
_limiter = build_from_env()


def _client_ip(request: Request) -> str:
    """取访客 IP。

    容器用 ``ports: 8001:8000`` 直通发布，iptables DNAT 会保留源地址，
    所以 ``request.client.host`` 通常就是真实 IP（实测访问日志里是公网 IP）。
    仍然先看 ``X-Forwarded-For``：将来若在前面挂 nginx，这一行不用改。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def demo_rate_limit(request: Request, call_next):
    """开放演示的限流。只对问数接口生效，且**成功处理后才计数**。

    失败开放（fail-open）：限流器自己出异常时放行。宁可少挡一次，
    也不要让一个 bug 把整个 Demo 变成 500。
    """
    if request.method != "POST" or request.url.path not in _LIMITED_PATHS:
        return await call_next(request)

    try:
        ip = _client_ip(request)
        allowed, wait_s, reason = _limiter.check(ip)
    except Exception:                                  # noqa: BLE001
        logger.exception("rate limiter failed, fail-open")
        return await call_next(request)

    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": reason},
            headers={"Retry-After": str(wait_s)},
        )

    response = await call_next(request)
    # 参数校验失败（422）不该消耗访客的额度，所以只在成功时记账。
    if response.status_code < 400:
        try:
            _limiter.hit(ip)
        except Exception:                              # noqa: BLE001
            logger.exception("rate limiter hit failed")
    return response


# ---------------------------------------------------------------------------
# 引擎接入点（测试替换这一个函数即可，不必碰全局 get_deps 的缓存）
# ---------------------------------------------------------------------------
def engine_deps() -> Deps:
    return get_deps()


def run_ask(
    question: str,
    *,
    session_id: str = "",
    history: list[dict[str, Any]] | None = None,
    deps: Deps | None = None,
) -> tuple[Any, Any]:
    """薄封装。单独成函数是为了让测试能整体替换掉引擎调用。"""
    return ask(question, session_id=session_id, history=history, deps=deps, use_cache=True)


class SpanBridge:
    """``TraceStore`` 的透明代理：照常落库，同时把 span 原子地送进队列。

    只拦 ``save_span`` / ``save_turn``，其余属性原样转发，所以对 ``TraceRecorder``
    来说它就是一个 TraceStore。
    """

    def __init__(self, inner: Any, sink: Callable[[dict[str, Any]], None]) -> None:
        self._inner = inner
        self._sink = sink

    def save_span(self, turn_id: str, seq: int, node: str, status: str, ms: int,
                  meta: dict[str, Any], error: str = "") -> None:
        self._inner.save_span(turn_id, seq, node, status, ms, meta, error)
        try:
            self._sink({
                "turn_id": turn_id,
                "seq": int(seq),
                "node": node,
                "status": status,
                "ms": int(ms),
                "meta": meta or {},
                "error": error or "",
            })
        except Exception:  # 推送失败绝不能影响一次已经成功的问数
            logger.exception("span sink failed turn=%s node=%s", turn_id, node)

    def save_turn(self, *args: Any, **kwargs: Any) -> None:
        self._inner.save_turn(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# 页面与静态资源
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    target = WEB_DIR / "index.html"
    if not target.is_file():
        raise HTTPException(status_code=503, detail="前端文件缺失：web/index.html")
    return FileResponse(target, media_type="text/html; charset=utf-8")


@app.get("/static/{name}", include_in_schema=False)
def static_file(name: str) -> FileResponse:
    """只允许 web/ 下的一层文件名，拒绝 ``..`` 与子目录。"""
    target = (WEB_DIR / name).resolve()
    if target.parent != WEB_DIR.resolve() or not target.is_file():
        raise HTTPException(status_code=404, detail="静态资源不存在")
    media_type = _MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(target, media_type=media_type)


# ---------------------------------------------------------------------------
# 问数
# ---------------------------------------------------------------------------
def _history(req: AskRequest) -> list[dict[str, Any]]:
    return [item.model_dump() for item in req.history]


@app.post("/api/ask", response_model=AskResponse)
async def ask_once(req: AskRequest) -> AskResponse:
    deps = engine_deps()
    try:
        answer, report = await asyncio.to_thread(
            run_ask,
            req.question,
            session_id=req.session_id,
            history=_history(req),
            deps=deps,
        )
    except ValueError as exc:                      # 引擎侧入参校验
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:                       # 引擎兜底；图节点异常通常已被包成 failed
        logger.exception("ask failed")
        raise HTTPException(
            status_code=500, detail=f"问数失败：{type(exc).__name__}: {exc}"
        ) from exc
    return AskResponse(answer=answer, report=ReportOut.from_report(report))


def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


async def _stream(req: AskRequest) -> AsyncIterator[str]:
    deps = engine_deps()
    outbox: queue.Queue[dict[str, Any]] = queue.Queue()
    stream_deps = replace(deps, trace_store=SpanBridge(deps.trace_store, outbox.put))

    yield _sse("start", {
        "session_id": req.session_id,
        "question": req.question,
        "mode": deps.settings.effective_mode,
        "variant": deps.variant,
    })

    task = asyncio.create_task(asyncio.to_thread(
        run_ask,
        req.question,
        session_id=req.session_id,
        history=_history(req),
        deps=stream_deps,
    ))

    seen: set[tuple[str, int]] = set()
    deadline = time.monotonic() + STREAM_DEADLINE_S
    try:
        while True:
            while True:                            # 先排空队列，保证顺序
                try:
                    event = outbox.get_nowait()
                except queue.Empty:
                    break
                key = (event.get("turn_id", ""), int(event.get("seq", -1)))
                if key in seen:
                    continue
                seen.add(key)
                yield _sse("node", event)
            if task.done() and outbox.empty():
                break
            if time.monotonic() > deadline:
                yield _sse("error", {"message": f"超时：{int(STREAM_DEADLINE_S)} 秒内没有跑完"})
                return
            await asyncio.sleep(STREAM_POLL_S)
        answer, report = task.result()
    except Exception as exc:
        logger.exception("stream ask failed")
        yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
        return
    finally:
        # 客户端关页面 / 点停止时，生成器会在 yield 处被关闭，走到这里。
        # 注意能力边界：ask() 跑在 to_thread 里，Python 无法杀线程，
        # cancel 只是让 asyncio 侧立刻放手（不再挂着等待、不刷 "task pending"），
        # 真正的止损要等引擎支持协作式取消。这里把话说清楚，不假装能省下那几次调用。
        if not task.done():
            task.cancel()

    yield _sse("done", {
        "answer": answer.model_dump(mode="json"),
        "report": ReportOut.from_report(report).model_dump(),
    })


@app.post("/api/ask/stream")
async def ask_stream(req: AskRequest) -> StreamingResponse:
    return StreamingResponse(
        _stream(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 可观测性
# ---------------------------------------------------------------------------
@app.get("/api/traces", response_model=TracesResponse)
def list_traces(
    session: str = Query("", max_length=64, description="会话号；不传则返回空列表"),
    limit: int = Query(30, ge=1, le=200),
) -> TracesResponse:
    """按会话号返回历史轮次。

    不传 ``session`` 时返回空列表，而不是"全部"——这是公开演示，
    列出全部等于把所有访客问过的问题摊开给别人看（2026-11-16 修）。
    单个 turn 的详情仍可按 turn_id 取，那串是随机 12 位十六进制，猜不到。
    """
    if not session:
        return TracesResponse(turns=[], count=0, limit=limit)
    rows = engine_deps().trace_store.list_turns(limit, session_id=session)
    turns = [TurnHeader(**row) for row in rows]
    return TracesResponse(turns=turns, count=len(turns), limit=limit)


@app.get("/api/traces/{turn_id}", response_model=TurnDetail)
def trace_detail(turn_id: str) -> TurnDetail:
    payload = engine_deps().trace_store.get_turn(turn_id)
    if not payload.get("turn") and not payload.get("spans"):
        raise HTTPException(status_code=404, detail=f"未找到 turn：{turn_id}")
    turn = TurnHeader(**payload["turn"]) if payload.get("turn") else TurnHeader(turn_id=turn_id)
    spans = [SpanOut(**span) for span in payload.get("spans", [])]
    return TurnDetail(turn=turn, spans=spans)


def _cached_turns(state_db: Path) -> int:
    """走过缓存的轮次数。缓存命中时 ask() 只写一条 ``node='cache'`` 的 span。"""
    if not state_db.exists():
        return 0
    uri = f"file:{state_db.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        logger.warning("state db is not readable: %s", state_db)
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT turn_id) FROM spans WHERE node = 'cache'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    return int(row[0]) if row else 0


@app.get("/api/stats", response_model=StatsOut)
def stats() -> StatsOut:
    deps = engine_deps()
    base = deps.trace_store.stats()
    turns = int(base.get("turns", 0))
    cached = _cached_turns(Path(deps.settings.state_db_path))
    rate = deps.cache.hit_rate() if deps.cache is not None else 0.0
    return StatsOut(
        **base,
        cache=CacheStats(
            hit_rate=float(rate),
            enabled=bool(deps.settings.cache_enabled),
            cached_turns=cached,
            turns=turns,
            cached_share=round(cached / turns, 4) if turns else 0.0,
        ),
        mode=deps.settings.effective_mode,
    )


@app.get("/api/health", response_model=HealthOut)
def health() -> HealthOut:
    deps = engine_deps()
    settings = deps.settings
    db_path = Path(settings.db_path)
    return HealthOut(
        status="ok",
        mode=settings.effective_mode,
        model="" if settings.is_stub else settings.llm_model,
        inferencer=getattr(deps.inferencer, "name", ""),
        variant=deps.variant,
        tables=len(deps.bundle.tables),
        metrics=len(deps.bundle.metrics),
        fewshots=len(deps.bundle.fewshots),
        db_path=str(db_path),
        db_exists=db_path.exists(),
        semantic_dir=str(settings.semantic_dir),
        cache_enabled=bool(settings.cache_enabled),
    )
