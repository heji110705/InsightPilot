"""MCP Server 的协议级测试。

MCP 的失败模式很特殊：协议错一处，客户端不是报错而是**静默不工作**。所以这里
不测"业务对不对"（那是 pipeline 的事），只测协议行为本身：
- 握手回显版本、通知不响应、未知方法给 -32601；
- 工具参数缺失时用 isError 而不是协议错误；
- stdout 里除了 JSON-RPC 不能有任何别的东西。

最后一条单独用子进程测：日志一旦写进 stdout，客户端就再也读不懂这个进程了。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.mcp.server import MCPServer, TOOLS


class FakeDeps:
    """只提供工具读到的字段，不碰数据库和模型。"""

    def __init__(self) -> None:
        card = SimpleNamespace(
            name="dim_book",
            grain="一行一本书",
            meaning="图书维表",
            columns=["book_id", "title", "price"],
            caveats=[],
        )
        metric = SimpleNamespace(
            name="好评率",
            key="good_rating_rate",
            aliases=["好评"],
            unit="%",
            definition="score >= 8 的占比",
            formula="SUM(...) * 100.0 / COUNT(*)",
            caveats=["score 是 0-10 分"],
        )
        self.bundle = SimpleNamespace(tables=[card], metrics=[metric])


@pytest.fixture()
def server() -> MCPServer:
    return MCPServer(deps=FakeDeps())


def rpc(server: MCPServer, method: str, params: dict | None = None, msg_id: int = 1):
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


# ---------------------------------------------------------------------------
# 握手
# ---------------------------------------------------------------------------
def test_initialize_echoes_client_version(server: MCPServer) -> None:
    """回显客户端版本，老客户端才不会在握手阶段被踢掉。"""
    result = rpc(server, "initialize", {"protocolVersion": "2024-11-05"})["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "insightpilot"
    assert "tools" in result["capabilities"]


def test_initialize_without_version_falls_back(server: MCPServer) -> None:
    result = rpc(server, "initialize")["result"]
    assert result["protocolVersion"] == "2025-06-18"


def test_ping_returns_empty_object(server: MCPServer) -> None:
    assert rpc(server, "ping")["result"] == {}


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
def test_notification_gets_no_response(server: MCPServer) -> None:
    """通知必须没有响应；回了空对象会被判协议违规。"""
    response = server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response is None


def test_unknown_notification_also_silent(server: MCPServer) -> None:
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None


# ---------------------------------------------------------------------------
# 工具发现
# ---------------------------------------------------------------------------
def test_tools_list_shape(server: MCPServer) -> None:
    tools = rpc(server, "tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == ["ask_data", "list_tables", "describe_metric"]
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_required_arguments_declared() -> None:
    """inputSchema 的 required 必须和实现里的校验一致，否则模型会少传参数。"""
    schema = next(t for t in TOOLS if t["name"] == "ask_data")["inputSchema"]
    assert schema["required"] == ["question"]


# ---------------------------------------------------------------------------
# 错误分层
# ---------------------------------------------------------------------------
def test_unknown_method_is_protocol_error(server: MCPServer) -> None:
    response = rpc(server, "no/such/method")
    assert response["error"]["code"] == -32601


def test_unknown_tool_is_invalid_params(server: MCPServer) -> None:
    response = rpc(server, "tools/call", {"name": "drop_table", "arguments": {}})
    assert response["error"]["code"] == -32602


def test_missing_question_is_tool_error_not_protocol_error(server: MCPServer) -> None:
    """业务参数问题走 isError：这是给模型看的，不是给客户端看的。"""
    result = rpc(server, "tools/call", {"name": "ask_data", "arguments": {}})["result"]
    assert result["isError"] is True
    assert "question" in result["content"][0]["text"]


def test_list_tables_renders_cards(server: MCPServer) -> None:
    result = rpc(server, "tools/call", {"name": "list_tables", "arguments": {}})["result"]
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "dim_book" in text
    assert "一行一本书" in text


def test_describe_metric_hit(server: MCPServer) -> None:
    result = rpc(server, "tools/call",
                 {"name": "describe_metric", "arguments": {"name": "好评"}})["result"]
    text = result["content"][0]["text"]
    assert "good_rating_rate" in text
    assert "0-10 分" in text


def test_describe_metric_miss_lists_alternatives(server: MCPServer) -> None:
    result = rpc(server, "tools/call",
                 {"name": "describe_metric", "arguments": {"name": "转化率"}})["result"]
    assert result["isError"] is True
    assert "好评率" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent


def test_stdout_carries_only_jsonrpc() -> None:
    """子进程级验证：stdout 每一行都必须是合法 JSON-RPC。

    这条测试挡的是「某天有人在工具函数里加了个 print」——那会让整个服务
    对客户端静默失效，而在单元测试里完全看不出来。
    """
    payload = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ])
    proc = subprocess.run(
        [sys.executable, "scripts/mcp_server.py"],
        input=payload, capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 2, f"通知不应产生输出，实际 {len(lines)} 行"
    for line in lines:
        parsed = json.loads(line)  # 任何非 JSON 噪音都会在这里炸
        assert parsed["jsonrpc"] == "2.0"


def test_parse_error_returns_minus_32700() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/mcp_server.py"],
        input="{not json}\n", capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    parsed = json.loads(proc.stdout.strip())
    assert parsed["error"]["code"] == -32700
