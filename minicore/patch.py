"""apply_patch:解析并应用 unified diff(单个文件,可多个 hunk)。

支持标准 unified diff 格式:
    @@ -old_start,old_count +new_start,new_count @@
     上下文行
    -删除行
    +新增行

匹配策略:先用 hunk 头行号定位做精确匹配;失败则全局搜索(fuzz 偏移),
允许 hunk 头的行号与实际文件不完全一致(模型生成的 diff 常有 ± 几行的偏差)。
"""
from __future__ import annotations


def _norm(line: str) -> str:
    return line.rstrip("\n")


def _parse_hunks(patch_text: str) -> list[dict]:
    """解析 unified diff,返回 hunk 列表(不含 ---/+++ 文件头)。"""
    hunks: list[dict] = []
    current: dict | None = None
    for raw in patch_text.splitlines():
        if raw.startswith("@@ "):
            parts = raw.split()
            old = parts[1].lstrip("-")
            new = parts[2].lstrip("+")
            old_start, _, old_count = old.partition(",")
            current = {
                "old_start": int(old_start or 1),
                "old_count": int(old_count or 1),
                "lines": [],
            }
            hunks.append(current)
        elif current is not None:
            if raw.startswith(" "):
                current["lines"].append((" ", raw[1:]))
            elif raw.startswith("+"):
                current["lines"].append(("+", raw[1:]))
            elif raw.startswith("-"):
                current["lines"].append(("-", raw[1:]))
            elif raw.startswith("\\"):
                continue  # "\ No newline at end of file"
            # 其他行(如 diff 头)忽略
    return hunks


def _find_subsequence(lines: list[str], sub: list[str], start: int) -> int | None:
    """在 lines 中从 start 起找 sub 的连续匹配(按行内容,忽略换行),返回起始索引。"""
    n = len(sub)
    if n == 0:
        return start
    for i in range(start, len(lines) - n + 1):
        if all(_norm(lines[i + j]) == sub[j] for j in range(n)):
            return i
    return None


def _apply_hunk(lines: list[str], hunk: dict) -> tuple[list[str], str]:
    old_lines = [text for op, text in hunk["lines"] if op in (" ", "-")]
    new_lines = [text for op, text in hunk["lines"] if op in (" ", "+")]

    start = max(0, hunk["old_start"] - 1)   # 0-based,由 hunk 头定位
    idx = _find_subsequence(lines, old_lines, start)
    if idx is None:
        # fuzz:行号不准时全局搜索
        idx = _find_subsequence(lines, old_lines, 0)
    if idx is None:
        return lines, f"hunk 匹配失败(@@ -{hunk['old_start']} 附近的上下文行未找到)"

    # 新行加回换行符(尽量继承被替换首行的换行风格)
    newline = "\n"
    if idx + len(old_lines) - 1 < len(lines):
        newline = "\r\n" if lines[idx + len(old_lines) - 1].endswith("\r\n") else "\n"
    new_with_nl = [t + newline for t in new_lines]

    result = lines[:idx] + new_with_nl + lines[idx + len(old_lines):]
    return result, ""


def apply_unified_diff(content: str, patch_text: str) -> tuple[str, str]:
    """对单个文件内容应用 unified diff,返回 (新内容, 错误)。

    错误为空字符串表示成功;失败时返回原始 content 和错误信息。
    """
    hunks = _parse_hunks(patch_text)
    if not hunks:
        return content, "未解析到任何 @@ hunk"

    lines = content.splitlines(keepends=True)
    for hunk in hunks:
        lines, err = _apply_hunk(lines, hunk)
        if err:
            return content, err
    return "".join(lines), ""
