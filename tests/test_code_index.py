"""符号级代码索引(code_index)测试。"""
from minicore.code_index import build_index, SymbolRef, _format_refs


def test_build_index_finds_function_and_call(tmp_path):
    (tmp_path / "m.py").write_text("def foo(x):\n    return x\n\nfoo(1)\n", encoding="utf-8")
    index = build_index(str(tmp_path))
    kinds = {r.kind for r in index.get("foo", [])}
    assert "function" in kinds
    assert "call" in kinds


def test_build_index_finds_class(tmp_path):
    (tmp_path / "m.py").write_text("class Bar:\n    pass\n", encoding="utf-8")
    index = build_index(str(tmp_path))
    assert any(r.kind == "class" for r in index.get("Bar", []))


def test_build_index_finds_import(tmp_path):
    (tmp_path / "m.py").write_text("import os\nfrom pathlib import Path\n", encoding="utf-8")
    index = build_index(str(tmp_path))
    assert any(r.kind == "import" for r in index.get("Path", []))


def test_build_index_skips_syntax_error(tmp_path):
    (tmp_path / "bad.py").write_text("def (broken\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("def good():\n    pass\n", encoding="utf-8")
    index = build_index(str(tmp_path))
    assert "good" in index  # 语法错误文件被跳过,不影响其他文件


def test_format_refs_sorted_by_density():
    refs = [
        SymbolRef("b.py", 1, "foo", "call", ""),
        SymbolRef("a.py", 1, "foo", "call", ""),
        SymbolRef("a.py", 2, "foo", "call", ""),
    ]
    lines = _format_refs(refs, "foo").splitlines()
    assert "a.py" in lines[0]  # 引用多的文件排前面
