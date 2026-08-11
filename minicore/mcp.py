"""迷你版 MCP 客户端:通过 stdio 连接 MCP 服务端,能列工具、调工具。

对应原版 minicode/mcp.py 的 StdioMcpClient 核心:
- 用 subprocess 起一个服务端子进程
- 用"换行分隔 JSON"在 stdin/stdout 上通信(JSON-RPC 风格)
- tools/list 列工具, tools/call 调工具
"""
from __future__ import annotations

import json
import subprocess
from typing import Any


class McpTool:
    """MCP 服务端暴露的一个工具。"""
    def __init__(self, name: str, description: str, client: "StdioMcpClient") -> None:
        self.name = name
        self.description = description
        self._client = client

    def call(self, arguments: dict[str, Any]) -> str:
        return self._client.call_tool(self.name, arguments)


class StdioMcpClient:
    """通过 stdio 连接一个 MCP 服务端子进程。"""

    def __init__(self, server_name: str, command: list[str]) -> None:
        self.server_name = server_name
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._next_id = 1

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发一个 JSON-RPC 请求,读响应。"""
        msg = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        self._next_id += 1

        # 写到 stdin,读 stdout 一行
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            return {"error": "MCP 服务端无响应"}
        return json.loads(line)

    def list_tools(self) -> list[McpTool]:
        """列出服务端提供的工具。"""
        resp = self._request("tools/list")
        tools = resp.get("result", {}).get("tools", [])
        return [McpTool(t["name"], t.get("description", ""), self) for t in tools]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用一个工具,返回结果文本。"""
        resp = self._request("tools/call", {"name": name, "arguments": arguments})
        result = resp.get("result", {})
        # MCP 结果通常是 content 列表
        content = result.get("content", [])
        if isinstance(content, list):
            return "\n".join(str(item.get("text", item)) for item in content)
        return str(result)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.terminate()