"""迷你版 MCP 客户端:通过 stdio 连接 MCP 服务端,能列工具、调工具。

对应原版 minicode/mcp.py 的 StdioMcpClient 核心:
- 用 subprocess 起一个服务端子进程
- 用"换行分隔 JSON"在 stdin/stdout 上通信(JSON-RPC 风格)
- tools/list 列工具, tools/call 调工具

健壮性:
- stderr 丢弃(避免管道缓冲写满导致死锁)
- 后台读线程 + 队列实现请求超时
- 响应解析失败返回错误字典,不抛异常
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
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

    def __init__(self, server_name: str, command: list[str], request_timeout: float = 10.0) -> None:
        self.server_name = server_name
        self.request_timeout = request_timeout
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,   # 丢弃 stderr,避免管道缓冲写满导致死锁
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._next_id = 1
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        # 后台线程持续读 stdout,主线程通过队列带超时取行
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        except Exception:
            pass
        finally:
            self._lines.put(None)   # EOF 标记

    def _read_line(self) -> str | None:
        """读一行,带超时。超时或 EOF 返回 None。"""
        try:
            line = self._lines.get(timeout=self.request_timeout)
        except queue.Empty:
            return None
        return line

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发一个 JSON-RPC 请求,读响应。任何失败都返回带 error 的字典,不抛异常。"""
        msg = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        self._next_id += 1

        # 写到 stdin
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            return {"error": f"MCP 写入失败: {e}"}

        line = self._read_line()
        if line is None:
            return {"error": f"MCP 服务端在 {self.request_timeout}s 内无响应"}
        if not line.strip():
            return {"error": "MCP 服务端返回空响应"}
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            return {"error": f"MCP 响应解析失败: {e}"}

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
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:
                pass
