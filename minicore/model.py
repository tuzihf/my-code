"""迷你版模型层:接口 + DeepSeek 真实实现 + Mock 假实现。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import openai

from minicore.api_retry import with_retry


# ---------- 消息类型 ----------
@dataclass
class AgentStep:
    """模型的一步输出:要么是普通文本,要么是工具调用。

    reasoning_content: DeepSeek 思考模式的思考过程。回传时必须带回去,
    否则 DeepSeek 会 400 报错。
    """
    type: str                     # "assistant"(文本) 或 "tool_calls"(调工具)
    content: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning_content: str = ""


# ---------- 抽象接口 ----------
class Model:
    """所有模型的接口:一个 next() 方法。"""
    def next(self, messages: list[dict[str, Any]]) -> AgentStep:
        raise NotImplementedError


# ---------- DeepSeek 真实实现 ----------
class DeepSeekModel(Model):
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise ValueError("需要 DEEPSEEK_API_KEY 环境变量或传 api_key")
        self.client = openai.OpenAI(
            base_url="https://api.deepseek.com",
            api_key=self.api_key,
        )

    def next(self, messages: list[dict[str, Any]], *, on_chunk=None) -> AgentStep:
        """调用模型。on_chunk 传入时流式生成(每段回调),否则一次性返回。"""
        # 把工具的"名字+描述"发给模型,让它知道能调什么
        tool_defs = messages_get_tool_defs()

        def _create():
            return self.client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=messages,
                tools=tool_defs,
                tool_choice="auto",
                stream=on_chunk is not None,
            )

        # 用 with_retry 包住 API 调用:超时/限流/5xx 自动重试(指数退避)
        response = with_retry(_create, max_retries=3, base_delay=1.0)

        # 流式模式:逐 chunk 累积,每段回调
        if on_chunk is not None:
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls_accum: dict[int, dict] = {}
            finish_reason = None
            for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason
                # 文本增量
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                    on_chunk(delta.content)
                # 思考增量
                if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                    reasoning_parts.append(delta.reasoning_content)
                # 工具调用增量(OpenAI 流式工具调用是分段的)
                if getattr(delta, "tool_calls", None):
                    for tc in delta.tool_calls:
                        idx = tc.index
                        acc = tool_calls_accum.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function and tc.function.name:
                            acc["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            acc["arguments"] += tc.function.arguments

            reasoning = "".join(reasoning_parts)
            # 有工具调用增量 → 返回 tool_calls
            if tool_calls_accum:
                calls = []
                for idx in sorted(tool_calls_accum):
                    acc = tool_calls_accum[idx]
                    calls.append({
                        "id": acc["id"] or f"call_{idx}",
                        "toolName": acc["name"],
                        "input": json.loads(acc["arguments"] or "{}"),
                    })
                return AgentStep(type="tool_calls", calls=calls, reasoning_content=reasoning)
            return AgentStep(type="assistant", content="".join(content_parts), reasoning_content=reasoning)

        # 非流式:原逻辑
        choice = response.choices[0]
        reasoning = choice.message.reasoning_content if hasattr(choice.message, "reasoning_content") else ""
        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            calls = []
            for tc in choice.message.tool_calls:
                calls.append({
                    "id": tc.id,
                    "toolName": tc.function.name,
                    "input": json.loads(tc.function.arguments or "{}"),
                })
            return AgentStep(type="tool_calls", calls=calls, reasoning_content=reasoning)
        return AgentStep(type="assistant", content=choice.message.content or "", reasoning_content=reasoning)


# ---------- Mock 假实现 ----------
class MockModel(Model):
    """不联网:靠规则演戏,方便离线测试整条链路。"""
    def next(self, messages: list[dict[str, Any]]) -> AgentStep:
        # 如果最近有工具结果 → 基于结果给文本回答(不再无脑调工具)
        for m in reversed(messages):
            if m.get("role") == "tool":
                return AgentStep(
                    type="assistant",
                    content=f"工具结果: {str(m.get('content', ''))[:200]}",
                )
        user_text = _last_user_text(messages)
        if user_text == "/tools":
            return AgentStep(type="assistant", content="list_files, read_file, run_command, write_file, remember")
        if user_text.startswith("/ls"):
            return AgentStep(
                type="tool_calls",
                calls=[{"id": "mock-1", "toolName": "list_files",
                        "input": {"path": user_text[3:].strip() or "."}}],
            )
        return AgentStep(type="assistant", content="mock 模型收到的消息:\n" + str(messages)[:200])


# ---------- 工具定义生成(给模型看的) ----------
def _tool_defs(tools) -> list[dict[str, Any]]:
    """把注册表里的工具,转成 OpenAI 认识的格式。"""
    out = []
    for name in tools.list_all():
        t = tools.find(name)
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.description,
                "parameters": {"type": "object", "properties": {}},
            },
        })
    return out


# 全局变量:保存工具注册表,供 DeepSeekModel.next 使用
_TOOLS = None

def set_tools(tools) -> None:
    global _TOOLS
    _TOOLS = tools

def messages_get_tool_defs() -> list[dict[str, Any]]:
    return _tool_defs(_TOOLS)


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m["role"] == "user":
            return str(m["content"])
    return ""