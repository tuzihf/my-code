"""会话系统测试:checkpoint/rewind 边界。"""
import pytest
from pathlib import Path

import minicore.session as session_mod


@pytest.fixture
def iso_sessions(tmp_path, monkeypatch):
    """把会话目录隔离到临时目录,不污染真实 ~/.my-agent。"""
    monkeypatch.setattr(session_mod, "sessions_dir", lambda: tmp_path)
    return tmp_path


def test_create_and_save(iso_sessions):
    s = session_mod.create_new_session("proj")
    s.messages = [{"role": "user", "content": "hi"}]
    session_mod.save_session(s)
    loaded = session_mod.load_session(s.session_id)
    assert loaded is not None
    assert len(loaded.messages) == 1


def test_load_missing(iso_sessions):
    assert session_mod.load_session("nonexistent") is None


def test_save_load_roundtrip_checkpoints(iso_sessions, tmp_path):
    work = tmp_path / "f.txt"
    work.write_text("v1\n", encoding="utf-8")
    s = session_mod.create_new_session(str(tmp_path))
    session_mod.create_file_checkpoint(s, file_path=str(work), existed=True, previous_content="v1\n")
    session_mod.save_session(s)
    loaded = session_mod.load_session(s.session_id)
    assert len(loaded.checkpoints) == 1
    assert loaded.checkpoints[0].previous_content == "v1\n"


class TestRewind:
    def test_rewind_none(self, iso_sessions, tmp_path):
        work = tmp_path / "f.txt"
        work.write_text("v1\n", encoding="utf-8")
        s = session_mod.create_new_session(str(tmp_path))
        # 没有 checkpoint → 回退空
        assert session_mod.rewind_session_data(s, steps=1) == []

    def test_rewind_restores(self, iso_sessions, tmp_path):
        work = tmp_path / "f.txt"
        work.write_text("v1\n", encoding="utf-8")
        s = session_mod.create_new_session(str(tmp_path))
        session_mod.create_file_checkpoint(s, file_path=str(work), existed=True, previous_content="v1\n")
        work.write_text("v2\n", encoding="utf-8")
        restored = session_mod.rewind_session_data(s, steps=1)
        assert len(restored) == 1
        assert work.read_text(encoding="utf-8") == "v1\n"

    def test_multi_rewind_sequence(self, iso_sessions, tmp_path):
        """回归:多步回退应依次还原,不跳档。"""
        work = tmp_path / "f.txt"
        s = session_mod.create_new_session(str(tmp_path))
        work.write_text("v1\n", encoding="utf-8")
        session_mod.create_file_checkpoint(s, file_path=str(work), existed=True, previous_content="v1\n")
        work.write_text("v2\n", encoding="utf-8")
        session_mod.create_file_checkpoint(s, file_path=str(work), existed=True, previous_content="v2\n")
        work.write_text("v3\n", encoding="utf-8")

        session_mod.rewind_session_data(s, steps=1)
        assert work.read_text(encoding="utf-8") == "v2\n"
        session_mod.rewind_session_data(s, steps=1)
        assert work.read_text(encoding="utf-8") == "v1\n"


class TestReadableConversation:
    def test_filters_system_injected(self):
        messages = [
            {"role": "user", "content": "真实问题"},
            {"role": "user", "content": "[系统] 压缩提示", "system_injected": True},
            {"role": "tool", "content": "工具输出"},
            {"role": "assistant", "content": None, "tool_calls": [{}]},
        ]
        assert session_mod.readable_conversation_count(messages) == 1

    def test_filters_summary(self):
        messages = [
            {"role": "user", "content": "前情摘要:[assistant] ..."},
            {"role": "user", "content": "真实问题"},
        ]
        assert session_mod.readable_conversation_count(messages) == 1
