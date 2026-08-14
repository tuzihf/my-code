"""dotenv / fsutil / cleanup 工具测试。"""
import os
import pytest
from pathlib import Path

from minicore.dotenv import load_dotenv
from minicore.fsutil import atomic_write_text, backup_corrupt
from minicore import cleanup_sessions
from minicore import session as session_mod


class TestDotenv:
    def test_load_basic(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("# comment\nKEY_A=val_a\nKEY_B=\"quoted b\"\nKEY_C=\n", encoding="utf-8")
        for k in ("KEY_A", "KEY_B", "KEY_C"):
            monkeypatch.delenv(k, raising=False)
        assert load_dotenv(env) is True
        assert os.environ["KEY_A"] == "val_a"
        assert os.environ["KEY_B"] == "quoted b"
        assert os.environ["KEY_C"] == ""

    def test_no_override_existing(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("KEY_A=new\n", encoding="utf-8")
        monkeypatch.setenv("KEY_A", "orig")
        load_dotenv(env)
        assert os.environ["KEY_A"] == "orig"

    def test_missing_file(self, tmp_path):
        assert load_dotenv(tmp_path / ".env") is False


class TestFsutil:
    def test_atomic_write(self, tmp_path):
        p = tmp_path / "f.txt"
        atomic_write_text(p, "hello")
        assert p.read_text(encoding="utf-8") == "hello"
        assert list(tmp_path.glob("*.tmp")) == []  # 无临时残留

    def test_backup_corrupt(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_text("x", encoding="utf-8")
        assert backup_corrupt(p) is True
        assert (tmp_path / "f.txt.corrupt").exists()
        assert not p.exists()


class TestCleanup:
    @pytest.fixture
    def iso_sessions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_mod, "sessions_dir", lambda: tmp_path)
        return tmp_path

    def test_plan_cleanup_empty(self, iso_sessions):
        keep, remove = cleanup_sessions.plan_cleanup()
        assert keep == []
        assert remove == []

    def test_plan_cleanup_removes_empty_session(self, iso_sessions):
        s = session_mod.create_new_session("proj")
        s.messages = []  # 无可读对话
        session_mod.save_session(s)
        keep, remove = cleanup_sessions.plan_cleanup()
        assert len(remove) == 1
        assert len(keep) == 0
