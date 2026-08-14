"""集成测试:真实 API、并行性能、MCP 子进程、编码。

这些需要真实 DEEPSEEK_API_KEY 或涉及耗时,用 @pytest.mark.integration 标记。
默认不跑,加 -m integration 才跑:
    python -m pytest tests/ -m integration
"""
import pytest
from pathlib import Path

pytestmark = pytest.mark.integration

from minicore.tools import create_default_tools, ToolContext, ToolRegistry, ToolDefinition, ToolResult
from minicore.model import DeepSeekModel, set_tools
from minicore.agent_loop import run_agent_turn


def _have_api_key():
    import os
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


@pytest.mark.skipif(not _have_api_key(), reason="需要 DEEPSEEK_API_KEY")
class TestDeepSeekReal:
    def test_multi_turn_no_400(self):
        """回归:多轮对话不应因 reasoning_content 报 400。"""
        from pathlib import Path
        cwd = str(Path(".").resolve())
        tools = create_default_tools()
        set_tools(tools)
        model = DeepSeekModel()
        messages = [{"role": "user", "content": "列出当前目录文件"}]
        messages = run_agent_turn(model=model, tools=tools, messages=messages, cwd=cwd, max_steps=3)
        # 第二轮不应 400
        messages.append({"role": "user", "content": "继续"})
        run_agent_turn(model=model, tools=tools, messages=messages, cwd=cwd, max_steps=3)

    def test_streaming_chunks(self):
        """流式输出:on_chunk 应收到多个 chunk。"""
        from pathlib import Path
        cwd = str(Path(".").resolve())
        tools = create_default_tools()
        set_tools(tools)
        model = DeepSeekModel()
        chunks = []
        model.next([{"role": "user", "content": "你好"}], on_chunk=chunks.append)
        assert len(chunks) > 1


class TestParallelPerf:
    def test_parallel_speedup(self):
        """3 个慢工具并行,总耗时约等于 1 个,而不是 3 个。"""
        import time

        def _slow(input_data, context):
            time.sleep(0.3)
            return ToolResult(ok=True, output="ok")

        slow_tools = ToolRegistry([
            ToolDefinition(name="list_files", description="slow", run=_slow),
        ])

        class ParallelModel:
            def next(self, messages, *, on_chunk=None, tools=None):
                from minicore.model import AgentStep
                return AgentStep(type="tool_calls", calls=[
                    {"id": f"c{i}", "toolName": "list_files", "input": {"path": str(i)}} for i in range(3)
                ])

        t0 = time.time()
        run_agent_turn(model=ParallelModel(), tools=slow_tools,
                       messages=[{"role": "user", "content": "x"}],
                       cwd=str(Path(".").resolve()), max_steps=2)
        elapsed = time.time() - t0
        assert elapsed < 0.9, f"并行应 <0.9s,实际 {elapsed:.2f}s(可能串行了)"


class TestEncoding:
    def test_chinese_output(self):
        """Windows 下 run_command 中文输出不崩不乱码。"""
        tools = create_default_tools()
        ctx = ToolContext(cwd=str(Path(".").resolve()))
        r = tools.execute("run_command", {"command": "python -c \"print('你好')\""}, ctx)
        assert r.ok is True
        assert "你好" in r.output
