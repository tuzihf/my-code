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
        # 绝对路径 /definitely/not/here 会被沙箱拦截(或返回 not found),都合理
        r = tools.execute("list_files", {"path": "/definitely/not/here"}, ctx)
        assert r.ok is False
        assert ("not found" in r.output.lower() or "找不到" in r.output
                or "逃逸" in r.output or "项目目录" in r.output)


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

    def test_edit_linewise_match(self, tools, ctx, tmp_path):
        """尾随空白不一致时,行级去空白仍能唯一匹配。"""
        f = tmp_path / "c.py"
        f.write_text("X = 1  \n", encoding="utf-8")  # 行尾多了空格
        r = tools.execute("edit_file", {"path": "c.py", "old_str": "X = 1", "new_str": "X = 2"}, ctx)
        assert r.ok is True
        assert f.read_text(encoding="utf-8").strip() == "X = 2"

    def test_edit_ambiguous_rejected(self, tools, ctx, tmp_path):
        f = tmp_path / "c.py"
        f.write_text("foo\nfoo\n", encoding="utf-8")
        r = tools.execute("edit_file", {"path": "c.py", "old_str": "foo", "new_str": "bar"}, ctx)
        assert r.ok is False
        assert "不唯一" in r.output


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

    def test_shell_metachar_rejected(self, tools, ctx):
        r = tools.execute("run_command", {"command": "ls | grep x"}, ctx)
        assert r.ok is False
        assert "元字符" in r.output

    def test_non_whitelist_rejected(self, tools, ctx):
        r = tools.execute("run_command", {"command": "whoami"}, ctx)
        assert r.ok is False
        assert "白名单" in r.output


class TestGrepFiles:
    def test_grep_finds_matches(self, tools, ctx, tmp_path):
        (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
        r = tools.execute("grep_files", {"pattern": "def foo", "path": "."}, ctx)
        assert r.ok is True
        assert "def foo" in r.output

    def test_grep_no_match(self, tools, ctx, tmp_path):
        (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
        r = tools.execute("grep_files", {"pattern": "zzz_no_such", "path": "."}, ctx)
        assert r.ok is True
        assert "无匹配" in r.output

    def test_grep_invalid_regex(self, tools, ctx):
        r = tools.execute("grep_files", {"pattern": "[unclosed", "path": "."}, ctx)
        assert r.ok is False

    def test_grep_sorted_by_density(self, tools, ctx, tmp_path):
        (tmp_path / "many.py").write_text("foo\nfoo\nfoo\n", encoding="utf-8")
        (tmp_path / "few.py").write_text("foo\n", encoding="utf-8")
        r = tools.execute("grep_files", {"pattern": "foo", "path": "."}, ctx)
        lines = r.output.splitlines()
        assert "many.py" in lines[0]  # 命中多的文件排前面


def test_create_readonly_tools():
    from minicore.tools import create_readonly_tools, READONLY_TOOL_NAMES
    ro = create_readonly_tools()
    names = ro.list_all()
    assert set(names) == set(READONLY_TOOL_NAMES)
    assert "write_file" not in names
    assert "run_command" not in names
    assert "delegate" not in names


class TestGlobFiles:
    def test_glob_matches(self, tools, ctx, tmp_path):
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.py").write_text("y", encoding="utf-8")
        r = tools.execute("glob_files", {"pattern": "**/*.py"}, ctx)
        assert r.ok is True
        assert "a.py" in r.output
        assert "b.py" in r.output

    def test_glob_no_match(self, tools, ctx):
        r = tools.execute("glob_files", {"pattern": "*.nonexistent"}, ctx)
        assert r.ok is True
        assert "无匹配" in r.output

    def test_glob_empty_pattern(self, tools, ctx):
        r = tools.execute("glob_files", {"pattern": ""}, ctx)
        assert r.ok is False


class TestReadFilePaging:
    def test_read_with_offset_limit(self, tools, ctx, tmp_path):
        f = tmp_path / "f.py"
        f.write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
        r = tools.execute("read_file", {"path": "f.py", "offset": 2, "limit": 2}, ctx)
        assert r.ok is True
        assert "l2" in r.output
        assert "l3" in r.output
        assert "l1" not in r.output  # offset=2 起,不含第 1 行

    def test_read_offset_beyond_eof(self, tools, ctx, tmp_path):
        (tmp_path / "f.py").write_text("l1\n", encoding="utf-8")
        r = tools.execute("read_file", {"path": "f.py", "offset": 100}, ctx)
        assert r.ok is False
