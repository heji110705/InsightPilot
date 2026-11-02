"""MCP Server：把问数能力暴露成标准工具。

为什么手写而不引 `mcp` SDK
--------------------------
MCP 的 stdio 传输就是 **换行分隔的 JSON-RPC 2.0**，握手只要实现四个方法
（`initialize` / `notifications/initialized` / `tools/list` / `tools/call`）。
引入官方 SDK 会多一条版本漂移的依赖，而这个项目的卖点之一是
「pip install 完就能跑、不联网也能演示」。所以这里按协议文档自己实现，
代价是约 200 行，收益是零新增依赖、且每行行为都可测。

三条容易踩的坑，都在代码里处理了：
1. **stdout 只能有 JSON-RPC**。任何 print / logging 落到 stdout 都会让客户端
   解析失败，所以日志一律重定向到 stderr（见 `serve()`）。
2. **通知没有响应**。`notifications/*` 必须返回 None，回了空对象算协议违规。
3. **错误要分两层**：协议层用 JSON-RPC 的 `error` 码，工具执行失败则用
   `result.isError=true`——后者是「工具跑完了但业务失败」，客户端会展示给模型看，
   前者是「你调错了」，不该让模型看见。
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, Callable, TextIO

from app.agents.pipeline import ask, build_deps

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "insightpilot"
SERVER_VERSION = "1.0.0"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------
TOOLS: list[dict[str, Any]] = [
    {
        "name": "ask_data",
        "description": (
            "用中文问一个关于图书业务数据的问题，返回结论、可复核的 SQL 和结果表格。"
            "适合「哪些书评分最高」「各分类的收藏转化怎么样」这类需要查数的问题。"
            "系统会自动完成语义层检索、SQL 生成与安全校验，不需要你写 SQL。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "中文问题，尽量带上指标和时间范围"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "list_tables",
        "description": "列出数仓里可用的表、每张表的粒度（一行代表什么）和字段。问数前不确定字段时先调它。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_metric",
        "description": "查询某个指标的口径定义、计算公式和已知陷阱。例如「好评率」在本库里是 8 分及以上占比。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "指标名或别名，如「好评率」「收藏数」"},
            },
            "required": ["name"],
        },
    },
]


def _text(payload: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": payload}], "isError": is_error}


def _table_markdown(columns: list[str], rows: list[list[Any]], limit: int = 20) -> str:
    if not columns:
        return "（无结果）"
    head = "| " + " | ".join(str(c) for c in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join("" if v is None else str(v) for v in row) + " |"
        for row in rows[:limit]
    ]
    tail = f"\n（共 {len(rows)} 行，此处展示前 {min(limit, len(rows))} 行）" if len(rows) > limit else ""
    return "\n".join([head, sep, *body]) + tail


class MCPServer:
    """一个最小的 MCP 服务端：四个方法 + 三个工具。"""

    def __init__(self, deps: Any | None = None) -> None:
        self._deps = deps
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "initialize": self._initialize,
            "ping": lambda params: {},
            "tools/list": lambda params: {"tools": TOOLS},
            "tools/call": self._call_tool,
        }

    # -- 依赖懒加载：list_tables 不需要 LLM，问数才需要 ---------------
    @property
    def deps(self) -> Any:
        if self._deps is None:
            self._deps = build_deps()
        return self._deps

    # -- 协议方法 -------------------------------------------------------
    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        # 回显客户端版本：MCP 允许服务端在支持的范围内沿用对方的版本号，
        # 硬编码自己的版本会让老客户端直接断开。
        return {
            "protocolVersion": requested or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "arguments 必须是对象")

        handler = {
            "ask_data": self._tool_ask,
            "list_tables": self._tool_list_tables,
            "describe_metric": self._tool_describe_metric,
        }.get(name or "")
        if handler is None:
            raise _RpcError(INVALID_PARAMS, f"未知工具：{name}")
        return handler(arguments)

    # -- 工具实现 -------------------------------------------------------
    def _tool_ask(self, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "").strip()
        if not question:
            return _text("question 不能为空", is_error=True)
        try:
            answer, _report = ask(question, deps=self.deps, use_cache=True)
        except Exception as exc:  # 工具失败要让模型看见，而不是把连接打崩
            logger.exception("ask_data failed")
            return _text(f"查询失败：{type(exc).__name__}: {exc}", is_error=True)

        if answer.status != "ok":
            return _text(f"未能回答：{answer.message or answer.status}", is_error=True)

        lines = [answer.insight.headline if answer.insight else "（无结论）", ""]
        lines.append(f"SQL：{answer.sql}")
        lines.append("")
        lines.append(_table_markdown(list(answer.columns), [list(r) for r in answer.rows]))
        if answer.insight and answer.insight.footnotes:
            lines.append("")
            lines.append("口径说明：" + "；".join(answer.insight.footnotes))
        return _text("\n".join(lines))

    def _tool_list_tables(self, args: dict[str, Any]) -> dict[str, Any]:
        bundle = self.deps.bundle
        lines = [f"共 {len(bundle.tables)} 张表：", ""]
        for card in bundle.tables:
            lines.append(f"### {card.name}（{card.grain}）")
            if card.meaning:
                lines.append(card.meaning)
            lines.append("字段：" + "；".join(card.columns))
            lines.append("")
        return _text("\n".join(lines))

    def _tool_describe_metric(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        if not name:
            return _text("name 不能为空", is_error=True)
        bundle = self.deps.bundle
        lowered = name.lower()
        for metric in bundle.metrics:
            haystack = [metric.name, metric.key, *metric.aliases]
            if any(lowered in str(item).lower() for item in haystack if item):
                lines = [f"{metric.name}（{metric.key}）"]
                if metric.unit:
                    lines.append(f"单位：{metric.unit}")
                if metric.definition:
                    lines.append(f"定义：{metric.definition}")
                if metric.formula:
                    lines.append(f"公式：{metric.formula}")
                for caveat in metric.caveats:
                    lines.append(f"- 注意：{caveat}")
                return _text("\n".join(lines))
        names = "、".join(m.name for m in bundle.metrics)
        return _text(f"没找到「{name}」。可用指标：{names}", is_error=True)

    # -- 请求分发 -------------------------------------------------------
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条 JSON-RPC 消息；通知类返回 None（不响应）。"""
        method = message.get("method")
        msg_id = message.get("id")

        # 通知：没有 id，按协议不得返回任何内容
        if msg_id is None and isinstance(method, str) and method.startswith("notifications/"):
            return None

        if not isinstance(method, str):
            return _error(msg_id, INVALID_REQUEST, "缺少 method")

        handler = self._handlers.get(method)
        if handler is None:
            return _error(msg_id, METHOD_NOT_FOUND, f"不支持的方法：{method}")

        try:
            result = handler(message.get("params") or {})
        except _RpcError as exc:
            return _error(msg_id, exc.code, exc.message)
        except Exception as exc:
            logger.exception("handler failed: %s", method)
            return _error(msg_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    # -- 传输 -----------------------------------------------------------
    def serve(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        """按行读 JSON-RPC。stdout 只写协议消息，日志全部改道 stderr。"""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout

        # 关键：把 root logger 指向 stderr，否则业务日志会插进协议流里
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)

        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                _write(stdout, _error(None, PARSE_ERROR, f"JSON 解析失败：{exc}"))
                continue

            if isinstance(message, list):  # 批量请求
                responses = [r for r in (self.handle(m) for m in message) if r is not None]
                if responses:
                    _write(stdout, responses)
                continue

            response = self.handle(message)
            if response is not None:
                _write(stdout, response)


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _write(stdout: TextIO, payload: Any) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stdout.flush()
