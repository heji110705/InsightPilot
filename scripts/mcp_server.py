"""MCP Server 入口：从命令行启动，走 stdio 传输。

用法
----
在 MCP 客户端（Claude Desktop / Cursor / 其他）的配置里加：

    {
      "mcpServers": {
        "insightpilot": {
          "command": "python",
          "args": ["scripts/mcp_server.py"],
          "cwd": "<项目根目录>"
        }
      }
    }

手动调试（一行一个请求）：

    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python scripts/mcp_server.py

注意：这个进程的 stdout 是协议通道，任何调试输出都要走 stderr。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.mcp.server import MCPServer  # noqa: E402


def main() -> int:
    MCPServer().serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
