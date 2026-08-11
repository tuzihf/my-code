"""迷你版上下文压缩:消息太长时,把旧历史浓缩成摘要。

对应原版 minicode/context_compactor.py 的核心概念:
- estimate_tokens  : 估算一段文本占多少 token
- should_compact   : 判断当前消息是否超过阈值
- compact          : 把最旧的几轮浓缩成一条摘要
"""
from __future__ import annotations

from typing import Any


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数(中文约 1 token/字,英文约 4 字符/token)。

    这不是精确计算,只是给"该不该压缩"一个成本依据。
    """
    if not text:
        return 0
    # 简单估算:中文字符按 1.5 token,其他按 4 字符 1 token
    cjk = sum(1 for ch in text if '一' <= ch <= '鿿')
    other = len(text) - cjk
    return int(cjk * 1.5 + other / 4)


def _messages_token_count(messages: list[dict[str, Any]]) -> int:
    """计算一整组消息占多少 token(近似)。"""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            total += estimate_tokens(content)
        # 如果 content 是列表(OpenAI 的 tool_calls 格式),粗略按名字算
        elif isinstance(content, list):
            for part in content:
                total += estimate_tokens(str(part))
    return total


def should_compact(messages: list[dict[str, Any]], *, max_tokens: int = 2000) -> bool:
    """判断当前消息是否超过阈值,需要压缩。"""
    return _messages_token_count(messages) > max_tokens


def compact(messages: list[dict[str, Any]], *, keep_recent: int = 4, max_tokens: int = 2000) -> tuple[list[dict[str, Any]], bool]:
    """把最旧的几轮浓缩成摘要,返回 (压缩后的消息, 是否真的压缩了)。

    策略:
      1. 如果总 token 没超阈值 → 不动
      2. 保留最近 keep_recent 条消息(通常是工具结果 + 最近问答)
      3. 更旧的消息:分两类处理
         - 用户的真实提问:保留原文(不浓缩),因为那是对话的核心
         - 工具结果/旧回答:浓缩成摘要,只留开头
      4. 压缩摘要只保留一份,不无限嵌套
    """
    if not should_compact(messages, max_tokens=max_tokens):
        return messages, False
    if len(messages) <= keep_recent:
        return messages, False

    old = messages[:-keep_recent]
    recent = messages[-keep_recent:]

    # 老摘要:如果之前压缩过,找到那条摘要,新摘要替换它(避免嵌套)
    old_without_prev = [
        m for m in old
        if not (str(m.get("content") or "").startswith("前情摘要:"))
    ]

    # 用户的真实提问:完整保留
    user_questions = [
        {"role": "user", "content": str(m["content"])}
        for m in old_without_prev
        if m.get("role") == "user" and not str(m.get("content") or "").startswith("[系统]")
    ]

    # 其余(工具结果、助手文本、系统注入):浓缩成摘要,只留开头
    summary_parts = []
    for m in old_without_prev:
        role = m.get("role", "?")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(str(p.get("text", p)) for p in content)
        text = str(content).strip()
        if not text or text.startswith("[系统]") or text.startswith("前情摘要:"):
            continue
        # 用户问题已单独保留,跳过
        if role == "user":
            continue
        summary_parts.append(f"[{role}] {text[:60]}")
    summary = ""
    if summary_parts:
        summary = "前情摘要:" + "|".join(summary_parts)[:1200]

    # 组装:老摘要(替换版) + 保留的用户问题 + 最近消息
    compacted: list[dict[str, Any]] = []
    if summary:
        compacted.append({"role": "user", "content": summary})
    compacted.extend(user_questions)
    compacted.extend(recent)
    return compacted, True
