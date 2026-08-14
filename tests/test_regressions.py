"""回归测试:覆盖本轮工程修复中的纯函数逻辑(不依赖 tmp_path)。"""
from minicore.model import _parse_tool_arguments
from minicore.agent_loop import _is_repeating, _tool_call_signature
from minicore.tools import _command_has_path_escape
from minicore.kernel import _looks_like_fallback_hint


# ---------- #14 工具参数解析容错 ----------

def test_parse_tool_arguments_normal():
    assert _parse_tool_arguments('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_parse_tool_arguments_truncated():
    assert _parse_tool_arguments('{"a": 1, "b": "x"') == {"a": 1, "b": "x"}


def test_parse_tool_arguments_invalid():
    assert _parse_tool_arguments("not json") == {}


def test_parse_tool_arguments_empty():
    assert _parse_tool_arguments("") == {}
    assert _parse_tool_arguments(None) == {}


# ---------- #10 防死循环签名/重复检测 ----------

def test_tool_call_signature():
    assert _tool_call_signature({"toolName": "read_file", "input": {"path": "a.py"}}) == \
        ("read_file", '{"path": "a.py"}')


def test_is_repeating_same():
    recent = [("read_file", {"path": "a.py"})] * 3
    calls = [{"toolName": "read_file", "input": {"path": "a.py"}}]
    assert _is_repeating(calls, recent) is True


def test_is_repeating_mixed():
    recent = [("read_file", {"path": "a.py"}), ("read_file", {"path": "b.py"}),
              ("read_file", {"path": "a.py"})]
    calls = [{"toolName": "read_file", "input": {"path": "a.py"}}]
    assert _is_repeating(calls, recent) is False


def test_is_repeating_too_few():
    recent = [("read_file", {"path": "a.py"})] * 2
    calls = [{"toolName": "read_file", "input": {"path": "a.py"}}]
    assert _is_repeating(calls, recent) is False


# ---------- #7 命令逃逸拦截 ----------

def test_command_escape_blocked():
    for cmd in ("cd C:\\", "cat /etc/passwd", "cat ../../secret.txt",
                "curl http://evil.com", "cmd /c del C:\\x", "powershell -c x"):
        assert _command_has_path_escape(cmd) is True


def test_command_escape_allowed():
    for cmd in ("python -m pytest", "git status", "ls", "echo hello", "pip list"):
        assert _command_has_path_escape(cmd) is False


# ---------- #15 兜底提示误判修复 ----------

def test_fallback_hint_no_false_positive():
    assert _looks_like_fallback_hint("这是一个 Python 项目,包含会话、记忆等模块") is False


def test_fallback_hint_real():
    assert _looks_like_fallback_hint("你可以试试 /ls 命令") is True
    assert _looks_like_fallback_hint("这个项目支持以下命令") is True


# ---------- #20 should_stop 协作式取消 ----------

def test_should_stop():
    from minicore.agent_loop import run_agent_turn
    from minicore.model import Model, AgentStep
    from minicore.tools import create_readonly_tools

    class AlwaysTool(Model):
        def next(self, messages, *, on_chunk=None, tools=None):
            return AgentStep(type="tool_calls",
                             calls=[{"id": "c", "toolName": "list_files", "input": {"path": "."}}])

    calls = {"n": 0}
    def should_stop():
        calls["n"] += 1
        return calls["n"] >= 2

    result = run_agent_turn(AlwaysTool(), create_readonly_tools(),
                            [{"role": "user", "content": "hi"}], cwd=".", max_steps=10,
                            should_stop=should_stop)
    assert result[-1].get("content") == "已停止生成。"
    assert calls["n"] == 2


# ---------- #13 工具参数 schema ----------

def test_tool_defs_schema():
    from minicore.tools import create_default_tools, close_mcp
    from minicore.model import _tool_defs

    tools = create_default_tools()
    try:
        defs = {d["function"]["name"]: d["function"]["parameters"] for d in _tool_defs(tools)}
        assert "path" in defs["read_file"]["properties"]
        assert defs["read_file"]["required"] == ["path"]
        assert defs["write_file"]["required"] == ["path", "content"]
        assert "pattern" in defs["grep_files"]["properties"]
        # MCP 工具无 schema 时安全回退为空 properties
        assert "fake-add__add" in defs
        assert defs["fake-add__add"]["properties"] == {}
    finally:
        close_mcp()


# ---------- #2 编辑后自动验证 ----------

def test_verify_tool_runs():
    from minicore.tools import create_default_tools, ToolContext, close_mcp
    t = create_default_tools()
    try:
        r = t.execute('verify', {'command': 'python -c "print(7)"'}, ToolContext(cwd='.'))
        assert r.ok is True
        assert '7' in r.output
    finally:
        close_mcp()


def test_auto_verify_closed_loop():
    from minicore.agent_loop import run_agent_turn
    from minicore.model import Model, AgentStep
    from minicore.tools import create_default_tools, close_mcp
    import pathlib

    class WriteThenDone(Model):
        def __init__(self):
            self.n = 0
        def next(self, messages, *, on_chunk=None, tools=None):
            self.n += 1
            if self.n == 1:
                return AgentStep(type='tool_calls',
                                 calls=[{'id': 'w', 'toolName': 'write_file',
                                         'input': {'path': '__av_test__.py', 'content': 'x=1\n'}}])
            return AgentStep(type='assistant', content='完成')

    tools = create_default_tools()
    try:
        result = run_agent_turn(WriteThenDone(), tools, [{'role': 'user', 'content': '改'}],
                                cwd='.', max_steps=10, auto_verify=True,
                                verify_command='python -c "print(123)"')
        assert any('自动运行验证命令' in str(m.get('content', '')) for m in result)
        assert any('123' in str(m.get('content', '')) for m in result)
    finally:
        pathlib.Path('__av_test__.py').unlink(missing_ok=True)
        close_mcp()


def test_usage_accumulation():
    from minicore import model
    model.reset_usage()
    class U:
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15
    model._accumulate_usage(U())
    model._accumulate_usage(None)  # None 不累加
    u = model.get_usage()
    assert u['requests'] == 1
    assert u['prompt_tokens'] == 10
    assert u['completion_tokens'] == 5
    assert u['total_tokens'] == 15
    model.reset_usage()


def test_write_dry_run_previews_only():
    from minicore.tools import create_default_tools, ToolContext, close_mcp
    import pathlib
    p = pathlib.Path('__dr_test__.txt')
    p.write_text('old\n', encoding='utf-8')
    t = create_default_tools()
    try:
        r = t.execute('write_file', {'path': '__dr_test__.txt', 'content': 'new\n', 'dry_run': True},
                      ToolContext(cwd='.'))
        assert r.ok is True
        assert '[dry_run]' in r.output
        assert p.read_text(encoding='utf-8') == 'old\n'  # 未写
    finally:
        p.unlink(missing_ok=True)
        close_mcp()


def test_confirm_edit_rejects():
    from minicore.agent_loop import run_agent_turn
    from minicore.model import Model, AgentStep
    from minicore.tools import create_default_tools, close_mcp
    import pathlib
    p = pathlib.Path('__ce_test__.txt')
    p.write_text('old\n', encoding='utf-8')

    class WriteThenDone(Model):
        def __init__(self):
            self.n = 0
        def next(self, messages, *, on_chunk=None, tools=None):
            self.n += 1
            if self.n == 1:
                return AgentStep(type='tool_calls',
                                 calls=[{'id': 'w', 'toolName': 'write_file',
                                         'input': {'path': '__ce_test__.txt', 'content': 'new\n'}}])
            return AgentStep(type='assistant', content='done')

    t = create_default_tools()
    try:
        run_agent_turn(WriteThenDone(), t, [{'role': 'user', 'content': '写'}], cwd='.',
                       max_steps=5, confirm_edit=lambda tn, dp: False)
        assert p.read_text(encoding='utf-8') == 'old\n'  # 被拒绝,未写
    finally:
        p.unlink(missing_ok=True)
        close_mcp()


def test_finish_tool_returns_summary():
    from minicore.agent_loop import run_agent_turn
    from minicore.model import Model, AgentStep
    from minicore.tools import create_default_tools, close_mcp

    class FinishModel(Model):
        def next(self, messages, *, on_chunk=None, tools=None):
            return AgentStep(type='tool_calls',
                             calls=[{'id': 'f', 'toolName': 'finish',
                                     'input': {'summary': '最终结论'}}])

    t = create_default_tools()
    try:
        result = run_agent_turn(FinishModel(), t, [{'role': 'user', 'content': '任务'}], cwd='.',
                                max_steps=5)
        assert result[-1].get('role') == 'assistant'
        assert result[-1].get('content') == '最终结论'
    finally:
        close_mcp()


def test_real_token_compaction():
    from minicore.agent_loop import run_agent_turn
    from minicore.model import Model, AgentStep, reset_usage, _accumulate_usage
    from minicore.tools import create_readonly_tools

    reset_usage()

    class TokenModel(Model):
        def __init__(self):
            self.n = 0
        def next(self, messages, *, on_chunk=None, tools=None):
            self.n += 1
            class U:
                prompt_tokens = 80
                completion_tokens = 20
                total_tokens = 100
            _accumulate_usage(U())  # 每次调用产生 100 token
            if self.n <= 7:
                return AgentStep(type='tool_calls',
                                 calls=[{'id': 'r', 'toolName': 'read_file', 'input': {'path': 'a.py'}}])
            return AgentStep(type='assistant', content='完成')

    t = create_readonly_tools()
    result = run_agent_turn(TokenModel(), t, [{'role': 'user', 'content': 'x'}], cwd='.',
                            max_steps=20, max_tokens=150)
    assert any('压缩成摘要' in str(m.get('content', '')) for m in result)


def test_rejected_text_emits_revoke():
    """被门禁否决时,已流式文本会先输出,但会发 on_revoke 信号让前端撤回。"""
    from minicore.agent_loop import run_agent_turn
    from minicore.model import Model, AgentStep
    from minicore.tools import create_readonly_tools

    class RejectThenFinishModel(Model):
        def __init__(self):
            self.n = 0
        def next(self, messages, *, on_chunk=None, tools=None):
            self.n += 1
            if self.n == 1:
                text = "综上所述,这是一个好项目"
                if on_chunk:
                    on_chunk(text)  # 先流式输出(会被 explore 门禁否决)
                return AgentStep(type='assistant', content=text)
            return AgentStep(type='tool_calls',
                             calls=[{'id': 'f', 'toolName': 'finish',
                                     'input': {'summary': '最终总结'}}])

    t = create_readonly_tools()
    chunks = []
    revokes = []
    run_agent_turn(RejectThenFinishModel(), t, [{'role': 'user', 'content': '介绍'}], cwd='.',
                   max_steps=20, on_assistant_chunk=chunks.append,
                   on_revoke=lambda: revokes.append(1))
    assert len(revokes) == 1            # 否决时发一次撤回信号
    assert '最终总结' in ''.join(chunks)  # 最终结论仍输出


def test_current_question_skips_system():
    from minicore.agent_loop import _current_question
    msgs = [
        {"role": "user", "content": "[系统] 压缩", "system_injected": True},
        {"role": "user", "content": "介绍项目"},
        {"role": "user", "content": "对比差异"},
    ]
    assert _current_question(msgs) == "对比差异"


def test_finish_summary_extraction():
    from minicore.agent_loop import _finish_summary
    from minicore.model import AgentStep
    step = AgentStep(type='tool_calls',
                     calls=[{'id': 'f', 'toolName': 'finish', 'input': {'summary': '总结'}}])
    assert _finish_summary(step) == '总结'
    assert _finish_summary(AgentStep(type='assistant', content='x')) is None


def test_verify_prompt_injects_current_question():
    """verify 收尾逼迫应注入当前问题并引导 finish。"""
    from minicore.agent_loop import run_agent_turn
    from minicore.model import Model, AgentStep
    from minicore.tools import create_readonly_tools

    class AlwaysTool(Model):
        def next(self, messages, *, on_chunk=None, tools=None):
            return AgentStep(type='tool_calls',
                             calls=[{'id': 'r', 'toolName': 'read_file', 'input': {'path': 'a.py'}}])

    t = create_readonly_tools()
    result = run_agent_turn(AlwaysTool(), t,
                            [{'role': 'user', 'content': '对比 AgenticRAG 与 Naive RAG'}],
                            cwd='.', max_steps=5)
    joined = str([m.get('content', '') for m in result])
    assert '用户当前的问题是' in joined
    assert '对比 AgenticRAG 与 Naive RAG' in joined
    assert 'finish' in joined


def test_finish_summary_chunked_streaming():
    """finish 的 summary 应切块流式输出,而非一次性整段。"""
    from minicore.agent_loop import run_agent_turn
    from minicore.model import Model, AgentStep
    from minicore.tools import create_readonly_tools

    class FinishModel(Model):
        def next(self, messages, *, on_chunk=None, tools=None):
            return AgentStep(type='tool_calls',
                             calls=[{'id': 'f', 'toolName': 'finish',
                                     'input': {'summary': '这是一个较长的最终回答'}}])

    t = create_readonly_tools()
    chunks = []
    run_agent_turn(FinishModel(), t, [{'role': 'user', 'content': '问'}], cwd='.',
                   max_steps=5, on_assistant_chunk=chunks.append)
    assert len(chunks) > 1                       # 切成了多个小块
    assert ''.join(chunks) == '这是一个较长的最终回答'


def test_multi_turn_question_anchor():
    """多轮对话时,应注入"当前问题"锚点并替换旧锚点,避免模型做多话题总结。"""
    from minicore.agent_loop import run_agent_turn
    from minicore.model import Model, AgentStep
    from minicore.tools import create_readonly_tools

    class DoneModel(Model):
        def next(self, messages, *, on_chunk=None, tools=None):
            return AgentStep(type='assistant', content='完成')

    t = create_readonly_tools()
    PREFIX = '用户当前的问题是'
    # 单轮不注入
    r0 = run_agent_turn(DoneModel(), t,
                        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '问题1'}],
                        cwd='.', max_steps=3)
    assert sum(1 for m in r0 if str(m.get('content', '')).startswith(PREFIX)) == 0
    # 多轮注入,且只保留最新一条(旧锚点被替换)
    r1 = run_agent_turn(DoneModel(), t,
                        [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '问题1'},
                         {'role': 'user', 'content': '问题2'}],
                        cwd='.', max_steps=3)
    anchors = [m for m in r1 if str(m.get('content', '')).startswith(PREFIX)]
    assert len(anchors) == 1 and '问题2' in anchors[0]['content']
    r1.append({'role': 'user', 'content': '问题3'})
    r2 = run_agent_turn(DoneModel(), t, r1, cwd='.', max_steps=3)
    anchors2 = [m for m in r2 if str(m.get('content', '')).startswith(PREFIX)]
    assert len(anchors2) == 1 and '问题3' in anchors2[0]['content']
