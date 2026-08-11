"""路径沙箱测试:锁死 agent 在项目目录内。"""
import pytest
from pathlib import Path

from minicore.tools import create_default_tools, ToolContext


@pytest.fixture
def tools():
    return create_default_tools()


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path))


class TestReadEscape:
    def test_dotdot_escape_blocked(self, tools, ctx, tmp_path):
        (tmp_path / "file.txt").write_text("x", encoding="utf-8")
        # .. 穿越到项目外
        r = tools.execute("read_file", {"path": "../file.txt"}, ctx)
        assert r.ok is False
        assert "逃逸" in r.output or "项目目录" in r.output

    def test_absolute_path_escape_blocked(self, tools, ctx):
        # 绝对路径(系统目录)
        r = tools.execute("read_file", {"path": "C:/Windows/win.ini"}, ctx)
        assert r.ok is False

    def test_deep_escape_blocked(self, tools, ctx):
        # 多层 .. 穿越
        r = tools.execute("read_file", {"path": "../../../etc/passwd"}, ctx)
        assert r.ok is False


class TestWriteEscape:
    def test_write_dotdot_blocked(self, tools, ctx):
        r = tools.execute("write_file", {"path": "../evil.txt", "content": "x"}, ctx)
        assert r.ok is False

    def test_write_absolute_blocked(self, tools, ctx):
        r = tools.execute("write_file", {"path": "C:/evil.txt", "content": "x"}, ctx)
        assert r.ok is False


class TestCommandEscape:
    def test_cd_absolute_blocked(self, tools, ctx):
        r = tools.execute("run_command", {"command": "cd C:/ && dir"}, ctx)
        assert r.ok is False
        assert "安全" in r.output

    def test_cd_slash_blocked(self, tools, ctx):
        r = tools.execute("run_command", {"command": "cd / && ls"}, ctx)
        assert r.ok is False


class TestNormalOperation:
    def test_read_inside_project_ok(self, tools, ctx, tmp_path):
        (tmp_path / "ok.txt").write_text("hi", encoding="utf-8")
        r = tools.execute("read_file", {"path": "ok.txt"}, ctx)
        assert r.ok is True
        assert "hi" in r.output

    def test_list_inside_ok(self, tools, ctx, tmp_path):
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        r = tools.execute("list_files", {"path": "."}, ctx)
        assert r.ok is True
        assert "a.txt" in r.output
