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
    """把大段输出写到磁盘,返回引用文本。"""
    target = cache_dir(cwd) / f"tool_{tool_id}.txt"
    target.write_text(output, encoding="utf-8")
    return f"[工具结果已存到 {target.name},共 {len(output)} 字符;需要时用 read_file 读它]"


def should_persist(output: str) -> bool:
    """判断这个输出是否超过阈值,需要持久化。"""
    return len(output) > PERSIST_THRESHOLD_CHARS