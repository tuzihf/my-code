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


def test_name_roundtrip():
    s = session_mod.create_new_session("proj")
    s.name = "我的会话"
    d = s.to_dict()
    assert d["name"] == "我的会话"
    loaded = session_mod.SessionData.from_dict(d)
    assert loaded.name == "我的会话"


def test_name_legacy_default():
    # 旧数据没有 name 字段时,应默认空字符串
    loaded = session_mod.SessionData.from_dict({"session_id": "x", "created_at": 0, "workspace": "."})
    assert loaded.name == ""


def test_load_corrupt_returns_none(iso_sessions):
    bad = iso_sessions / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    assert session_mod.load_session("bad") is None
    assert (iso_sessions / "bad.json.corrupt").exists()  # 损坏文件被备份


def test_rewind_group_restores_all(tmp_path):
    """同一 group 的多个文件整体回退(一个 turn 事务)。"""
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("a1", encoding="utf-8")
    b.write_text("b1", encoding="utf-8")
    s = session_mod.create_new_session(str(tmp_path))
    session_mod.create_file_checkpoint(s, file_path=str(a), existed=True, previous_content="a1", group_id="g1")
    session_mod.create_file_checkpoint(s, file_path=str(b), existed=True, previous_content="b1", group_id="g1")
    a.write_text("a2", encoding="utf-8")
    b.write_text("b2", encoding="utf-8")
    restored = session_mod.rewind_group(s, group_id="g1")
    assert len(restored) == 2
    assert a.read_text(encoding="utf-8") == "a1"
    assert b.read_text(encoding="utf-8") == "b1"


def test_rewind_group_empty_or_missing(tmp_path):
    s = session_mod.create_new_session(str(tmp_path))
    assert session_mod.rewind_group(s, group_id="") == []
    assert session_mod.rewind_group(s, group_id="nonexistent") == []


def test_checkpoint_group_id_roundtrip(tmp_path):
    """group_id 随 checkpoint 序列化/反序列化保留。"""
    s = session_mod.create_new_session(str(tmp_path))
    session_mod.create_file_checkpoint(
        s, file_path=str(tmp_path / "x.txt"), existed=False, previous_content="", group_id="g9")
    d = s.to_dict()
    assert d["checkpoints"][0]["group_id"] == "g9"
    loaded = session_mod.SessionData.from_dict(d)
    assert loaded.checkpoints[0].group_id == "g9"


def test_history_roundtrip():
    """完整可读历史 history 序列化/反序列化保留,不受 messages 影响。"""
    s = session_mod.create_new_session("proj")
    s.history = [
        {"role": "user", "content": "问题1"},
        {"role": "assistant", "content": "回答1"},
    ]
    d = s.to_dict()
    assert d["history"] == s.history
    loaded = session_mod.SessionData.from_dict(d)
    assert loaded.history == s.history
    # 旧数据无 history 字段时默认空
    legacy = session_mod.SessionData.from_dict(
        {"session_id": "x", "created_at": 0, "workspace": "."})
    assert legacy.history == []
