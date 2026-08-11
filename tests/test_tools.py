"""工具系统测试:正常路径 + 边界输入。"""
import pytest
from pathlib import Path

from minicore.tools import create_default_tools, ToolContext


@pytest.fixture
def tools():
    return create_default_tools()


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path))


class TestListFiles:
    def test_lists_files(self, tools, ctx, tmp_path):
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        r = tools.execute("list_files", {"path": "."}, ctx)
        assert r.ok is True
        assert "a.txt" in r.output

    def test_empty_dir(self, tools, ctx):
        r = tools.execute("list_files", {"path": "."}, ctx)
        assert r.ok is True

    def test_nonexistent_path(self, tools, ctx):
        r = tools.execute("list_files", {"path": "/definitely/not/here"}, ctx)
        assert r.ok is False
        assert "not found" in r.output.lower() or "找不到" in r.output


class TestInputValidation:
    """重点:边界输入,这是 verify 脚本没覆盖的。"""

    def test_input_not_dict(self, tools, ctx):
        r = tools.execute("read_file", "not-a-dict", ctx)
        assert r.ok is False
        assert "expected a dict" in r.output

    def test_input_none(self, tools, ctx):
        r = tools.execute("read_file", None, ctx)
        assert r.ok is False

    def test_path_not_string(self, tools, ctx):
        r = tools.execute("read_file", {"path": 123}, ctx)
        assert r.ok is False

    def test_empty_path(self, tools, ctx, tmp_path):
        # 空路径应失败或安全处理,不能崩
        r = tools.execute("read_file", {"path": ""}, ctx)
        assert r.ok is False

    def test_path_with_special_chars(self, tools, ctx, tmp_path):
        # 带特殊字符的路径应被安全处理(不崩,不逃逸)
        r = tools.execute("read_file", {"path": ".."}, ctx)
        # 结果不重要,但不能抛异常
        assert r.ok in (True, False)

    def test_unknown_tool(self, tools, ctx):
        r = tools.execute("no_such_tool", {}, ctx)
        assert r.ok is False
        assert "Unknown tool" in r.output


class TestEditFile:
    def test_edit_missing_old_str(self, tools, ctx, tmp_path):
        (tmp_path / "c.py").write_text("a\nb\n", encoding="utf-8")
        r = tools.execute("edit_file", {"path": "c.py", "new_str": "z"}, ctx)
        assert r.ok is False
        assert "old_str" in r.output

    def test_edit_old_str_not_found(self, tools, ctx, tmp_path):
        (tmp_path / "c.py").write_text("hello\n", encoding="utf-8")
        r = tools.execute("edit_file", {"path": "c.py", "old_str": "xxx", "new_str": "y"}, ctx)
        assert r.ok is False
        assert "没找到" in r.output

    def test_edit_unique_replace(self, tools, ctx, tmp_path):
        f = tmp_path / "c.py"
        f.write_text("DEBUG = True\n", encoding="utf-8")
        r = tools.execute("edit_file", {"path": "c.py", "old_str": "DEBUG = True", "new_str": "DEBUG = False"}, ctx)
        assert r.ok is True
        assert f.read_text(encoding="utf-8") == "DEBUG = False\n"


class TestRunCommand:
    def test_empty_command(self, tools, ctx):
        r = tools.execute("run_command", {"command": "   "}, ctx)
        assert r.ok is False

    def test_missing_command(self, tools, ctx):
        r = tools.execute("run_command", {}, ctx)
        assert r.ok is False

    def test_basic_command(self, tools, ctx):
        r = tools.execute("run_command", {"command": "python -c \"print('hi')\""}, ctx)
        assert r.ok is True
        assert "hi" in r.output
