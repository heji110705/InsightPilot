"""MCP（Model Context Protocol）接入层。

把问数能力暴露成标准工具，让外部 Agent（Claude Desktop、IDE 插件、
其他编排框架）能直接调用，而不必理解本项目的内部约定。
"""
from app.mcp.server import MCPServer, TOOLS

__all__ = ["MCPServer", "TOOLS"]
