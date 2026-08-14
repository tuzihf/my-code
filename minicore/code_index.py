"""符号级代码索引:用标准库 ast 做轻量静态分析。

提供函数/类定义、赋值、导入、名字引用、函数调用点的索引,
供 find_symbol / find_references 工具做跨文件代码理解。

设计:
- 只覆盖 Python(其他语言后续可用 tree-sitter 替换 _Visitor 而不动工具层)
- 按目录最新 mtime 做简单缓存失效,编辑文件后下次搜索自动重建
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

_SKIP = {".git", "__pycache__", ".agent_cache", ".pytest_cache", ".test_tmp",
         ".test_tmp2", ".pytest_tmp", "node_modules", "venv", ".venv", ".idea",
         "dist", "build"}


@dataclass
class SymbolRef:
    """一次符号出现:文件、行号、名字、类别、补充说明。"""
    file: str      # 相对项目根的路径
    line: int      # 1-based
    name: str
    kind: str      # function | class | assign | import | name_use | call
    detail: str = ""


def _sig(node: ast.FunctionDef) -> str:
    args = [a.arg for a in node.args.args]
    return f"def {node.name}({', '.join(args)})"


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.refs: list[SymbolRef] = []

    def visit_FunctionDef(self, node):
        self.refs.append(SymbolRef(self.rel, node.lineno, node.name, "function", _sig(node)))
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.refs.append(SymbolRef(self.rel, node.lineno, node.name, "class", ""))
        self.generic_visit(node)

    def visit_Assign(self, node):
        for t in node.targets:
            if isinstance(t, ast.Name):
                self.refs.append(SymbolRef(self.rel, node.lineno, t.id, "assign", ""))
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name):
            self.refs.append(SymbolRef(self.rel, node.lineno, node.target.id, "assign", ""))
        self.generic_visit(node)

    def visit_Import(self, node):
        for a in node.names:
            self.refs.append(SymbolRef(self.rel, node.lineno, a.asname or a.name, "import", a.name))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for a in node.names:
            self.refs.append(SymbolRef(self.rel, node.lineno, a.asname or a.name, "import",
                                       f"{node.module}.{a.name}"))
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Load, ast.Store)):
            self.refs.append(SymbolRef(self.rel, node.lineno, node.id, "name_use", ""))
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            self.refs.append(SymbolRef(self.rel, node.lineno, node.func.id, "call", ""))
        self.generic_visit(node)


# 目录绝对路径 -> (最新 mtime, {符号名 -> [SymbolRef]})
_cache: dict[str, tuple[float, dict[str, list[SymbolRef]]]] = {}


def build_index(cwd: str) -> dict[str, list[SymbolRef]]:
    """构建 cwd 下所有 .py 文件的符号索引,按符号名聚合。"""
    base = Path(cwd).resolve()

    newest = 0.0
    try:
        for p in base.rglob("*.py"):
            newest = max(newest, p.stat().st_mtime)
    except OSError:
        pass

    cached = _cache.get(str(base))
    if cached is not None and cached[0] >= newest:
        return cached[1]

    index: dict[str, list[SymbolRef]] = {}
    for p in sorted(base.rglob("*.py")):
        rel_parts = p.relative_to(base).parts
        if any(part in _SKIP for part in rel_parts):
            continue
        try:
            if p.stat().st_size > 1_000_000:
                continue
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        v = _Visitor(str(p.relative_to(base)))
        v.visit(tree)
        for r in v.refs:
            index.setdefault(r.name, []).append(r)

    _cache[str(base)] = (newest, index)
    return index


def _format_refs(refs: list[SymbolRef], name: str) -> str:
    if not refs:
        return f"(未找到符号: {name})"
    # 相关度排序:引用次数越多的文件越靠前(热文件优先)
    from collections import Counter
    counts = Counter(r.file for r in refs)
    ordered = sorted(refs, key=lambda r: (-counts[r.file], r.file, r.line))
    lines = []
    for r in ordered[:200]:
        suffix = f"  {r.detail}" if r.detail else ""
        lines.append(f"{r.kind:<10} {r.name:<20} {r.file}:{r.line}{suffix}")
    if len(ordered) > 200:
        lines.append(f"... 还有 {len(ordered) - 200} 条未显示")
    return "\n".join(lines)
