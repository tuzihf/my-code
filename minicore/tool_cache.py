"""迷你版工具结果持久化:大段工具输出挪到磁盘,对话只留引用。

对应原版 context_compactor 的 tool_results 持久化机制:
- 输出超过阈值 → 写到磁盘缓存文件
- 对话里只留一行引用,省上下文
"""
from __future__ import annotations

from pathlib import Path


# 超过这个字符数的工具结果 → 持久化到磁盘
PERSIST_THRESHOLD_CHARS = 2000


def cache_dir(cwd: str) -> Path:
    d = Path(cwd) / ".agent_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def persist_tool_result(cwd: str, tool_id: str, output: str) -> str:
    """把大段输出写到磁盘,返回引用文本。

    引用包含完整相对路径(模型能 read_file 读回)+ 首尾预览(模型能看到内容片段)。
    """
    target = cache_dir(cwd) / f"tool_{tool_id}.txt"
    target.write_text(output, encoding="utf-8")
    # 完整相对路径 + 首尾预览
    rel_path = f".agent_cache/{target.name}"
    lines = output.splitlines()
    head = "\n".join(lines[:5])
    tail = "\n".join(lines[-3:]) if len(lines) > 10 else ""
    preview = f"{head}"
    if tail:
        preview += f"\n...({len(lines) - 8} 行省略)...\n{tail}"
    return (
        f"[工具结果已存到 {rel_path},共 {len(output)} 字符]\n"
        f"--- 预览 ---\n{preview}\n"
        f"(完整内容用 read_file 读 {rel_path})"
    )


def should_persist(output: str) -> bool:
    """判断这个输出是否超过阈值,需要持久化。"""
    return len(output) > PERSIST_THRESHOLD_CHARS