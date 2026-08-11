"""kernel / 压缩器 / 记忆 测试。"""
import pytest
from pathlib import Path

from minicore.kernel import derive_phase, decide_assistant_turn
from minicore.context_compactor import compact, estimate_tokens, should_compact
from minicore.read_dedup import ReadDedup
from minicore.memory import MemoryStore


class TestKernel:
    def test_phase_progression(self):
        assert derive_phase(1, 20).phase == "explore"
        assert derive_phase(10, 20).phase == "execute"
        assert derive_phase(18, 20).phase == "verify"

    def test_gate_blocks_empty(self):
        finish, _ = decide_assistant_turn(content="", phase="execute", saw_tool_result=True)
        assert finish is False

    def test_gate_blocks_early_conclusion(self):
        finish, _ = decide_assistant_turn(content="综上所述,这是个好项目", phase="explore", saw_tool_result=True)
        assert finish is False

    def test_gate_allows_reasonable(self):
        finish, _ = decide_assistant_turn(content="完成,文件已修改", phase="verify", saw_tool_result=True)
        assert finish is True


class TestCompactor:
    def test_estimate_tokens(self):
        assert estimate_tokens("hello") > 0
        assert estimate_tokens("") == 0

    def test_no_compact_when_short(self):
        msgs = [{"role": "user", "content": "短"}]
        result, changed = compact(msgs, max_tokens=100000)
        assert changed is False
        assert result == msgs

    def test_preserves_user_questions(self):
        """回归:压缩不能吞掉用户真实提问。"""
        msgs = [
            {"role": "user", "content": "帮我看看模块1"},
            {"role": "tool", "content": "x" * 500},
            {"role": "assistant", "content": "分析结果" * 50},
            {"role": "user", "content": "帮我看看模块2"},
            {"role": "tool", "content": "y" * 500},
        ]
        result, changed = compact(msgs, keep_recent=2, max_tokens=500)
        assert changed is True
        joined = str(result)
        assert "帮我看看模块1" in joined
        assert "帮我看看模块2" in joined

    def test_no_nested_summary(self):
        """回归:二次压缩不叠加前情摘要。"""
        msgs = [{"role": "user", "content": "问题"}, {"role": "tool", "content": "x" * 1000}]
        r1, _ = compact(msgs, keep_recent=1, max_tokens=500)
        r2, _ = compact(r1, keep_recent=1, max_tokens=500)
        count = sum(1 for m in r2 if str(m.get("content", "")).startswith("前情摘要:"))
        assert count <= 1


class TestReadDedup:
    def test_second_read_dedup(self):
        d = ReadDedup()
        path = "a.py"
        content = "hello world" * 10
        d.register_read(path, content)
        assert d.should_dedup(path, content) is True

    def test_changed_content_not_dedup(self):
        d = ReadDedup()
        path = "a.py"
        d.register_read(path, "old")
        assert d.should_dedup(path, "new") is False


class TestMemory:
    def test_remember_and_reload(self, tmp_path):
        store_path = tmp_path / "mem.json"
        m1 = MemoryStore(store_path)
        m1.add("测试记忆内容")
        # 模拟重启
        m2 = MemoryStore(store_path)
        assert len(m2.all()) == 1
        assert m2.all()[0].content == "测试记忆内容"
