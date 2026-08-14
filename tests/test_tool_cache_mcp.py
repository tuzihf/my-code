"""tool_cache / mcp 测试。"""
import pytest
from pathlib import Path

from minicore.tool_cache import should_persist, persist_tool_result, cache_dir
from minicore.mcp import StdioMcpClient


class TestToolCache:
    def test_threshold(self):
        assert should_persist("short") is False
        assert should_persist("x" * 5000) is True

    def test_persist_and_read_back(self, tmp_path):
        ref = persist_tool_result(str(tmp_path), "c1", "内容" * 1000)
        assert "tool_c1.txt" in ref
        files = list(cache_dir(str(tmp_path)).glob("*.txt"))
        assert len(files) == 1
        assert "内容" in files[0].read_text(encoding="utf-8")

    def test_persist_creates_dir(self, tmp_path):
        # 即使目录不存在也会创建(cache_dir 会连目录和 .agent_cache 一起建)
        nested = tmp_path / "sub"
        persist_tool_result(str(nested), "c2", "abc")
        cache = cache_dir(str(nested))
        assert cache.exists()
        assert (cache / "tool_c2.txt").exists()


class TestMcpClient:
    """用假服务端测 MCP 客户端。"""

    @pytest.fixture
    def client(self):
        c = StdioMcpClient("fake", ["python", "minicore/fake_mcp_server.py"])
        yield c
        c.close()

    def test_list_tools(self, client):
        tools = client.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "add"

    def test_call_tool(self, client):
        result = client.call_tool("add", {"a": 3, "b": 4})
        assert "7" in result

    def test_call_tool_again(self, client):
        # 多次调用,确保状态不残留
        assert "30" in client.call_tool("add", {"a": 10, "b": 20})
        assert "5" in client.call_tool("add", {"a": 2, "b": 3})

    def test_request_timeout(self):
        """不响应的服务端应快速返回错误,而不是永久挂起。"""
        import sys
        import time
        c = StdioMcpClient("slow", [sys.executable, "-c", "import time; time.sleep(30)"],
                           request_timeout=0.5)
        try:
            t0 = time.time()
            resp = c._request("tools/list")
            elapsed = time.time() - t0
            assert "error" in resp
            assert elapsed < 3, f"超时应在 3s 内返回,实际 {elapsed:.2f}s"
        finally:
            c.close()
