"""轻量 diff 生成:对比文件修改前后内容,生成行级 diff。

用 Python 标准库 difflib,返回便于前端渲染的 diff 行列表。
"""
from __future__ import annotations

import difflib
from typing import Any


def generate_diff(old_content: str, new_content: str, context_lines: int = 2) -> list[dict[str, str]]:
    """对比新旧内容,返回 diff 行列表(只保留改动行及相邻 context_lines 行)。

    每项: {"op": "+"|"-"|" ", "text": "行内容"}
    "+" = 新增, "-" = 删除, " " = 未变
    """
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    differ = difflib.SequenceMatcher(None, old_lines, new_lines)
    # 完整 diff
    full: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in differ.get_opcodes():
        if tag == "equal":
            for line in old_lines[i1:i2]:
                full.append({"op": " ", "text": line})
        elif tag == "delete":
            for line in old_lines[i1:i2]:
                full.append({"op": "-", "text": line})
        elif tag == "insert":
            for line in new_lines[j1:j2]:
                full.append({"op": "+", "text": line})
        elif tag == "replace":
            for line in old_lines[i1:i2]:
                full.append({"op": "-", "text": line})
            for line in new_lines[j1:j2]:
                full.append({"op": "+", "text": line})

    # 找出所有改动行的索引
    change_idx = [i for i, l in enumerate(full) if l["op"] in ("+", "-")]
    if not change_idx:
        return full

    # 保留改动行 + 相邻 context_lines 行
    keep = set()
    for idx in change_idx:
        for j in range(idx - context_lines, idx + context_lines + 1):
            if 0 <= j < len(full):
                keep.add(j)

    # 添加"跳过"标记(连续跳过时用 ... 表示)
    result: list[dict[str, str]] = []
    prev_kept = None
    for i in range(len(full)):
        if i in keep:
            if prev_kept is not None and i - prev_kept > 1:
                result.append({"op": "...", "text": ""})
            result.append(full[i])
            prev_kept = i
        else:
            # 非改动行被跳过了,标记后续会有 ...
            pass
    return result


def format_diff_text(old_content: str, new_content: str) -> str:
    """把 diff 格式化成文本(供工具结果展示)。"""
    lines = generate_diff(old_content, new_content)
    if not lines:
        return ""
    # 统计改动行
    add = sum(1 for l in lines if l["op"] == "+")
    rem = sum(1 for l in lines if l["op"] == "-")
    parts = [f"--- 修改详情 ({add} 增 / {rem} 删) ---"]
    for l in lines:
        if l["op"] == "...":
            parts.append("...")
        else:
            parts.append(f"{l['op']} {l['text']}")
    return "\n".join(parts)
