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
    def next(self, messages: list[dict[str, Any]], *, on_chunk=None, tools=None) -> AgentStep:
        raise NotImplementedError


# ---------- DeepSeek 真实实现 ----------
class OpenAICompatModel(Model):
    """OpenAI 兼容模型:接受任意 base_url/api_key/model。

    覆盖 DeepSeek、OpenAI、Ollama、LM Studio 等所有 OpenAI 协议服务。
    """
    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None) -> None:
        self.model_id = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "") or "not-needed"
        self.base_url = base_url or "https://api.openai.com/v1"
        self.client = openai.OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
        )

    def next(self, messages: list[dict[str, Any]], *, on_chunk=None, tools=None) -> AgentStep:
        """调用模型。on_chunk 传入时流式生成(每段回调),否则一次性返回。"""
        # 把工具的"名字+描述"发给模型,让它知道能调什么
        # 优先用显式传入的 tools,回退到全局注册表(向后兼容)
        tool_defs = _tool_defs(tools) if tools is not None else messages_get_tool_defs()

        def _create():
            kwargs = {
                "model": self.model_id,
                "messages": messages,
                "tools": tool_defs,
                "tool_choice": "auto",
                "stream": on_chunk is not None,
            }
            if on_chunk is not None:
                kwargs["stream_options"] = {"include_usage": True}
            return self.client.chat.completions.create(**kwargs)

        # 用 with_retry 包住 API 调用:超时/限流/5xx 自动重试(指数退避)
        response = with_retry(_create, max_retries=3, base_delay=1.0)

        # 流式模式:逐 chunk 累积,每段回调
        if on_chunk is not None:
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls_accum: dict[int, dict] = {}
            usage = None
            for chunk in response:
                # 最后一个 chunk 可能带 usage(stream_options.include_usage)
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                # 文本增量
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                    on_chunk(delta.content)
                # 思考增量(DeepSeek 特有,兼容标准属性 + model_extra)
                rc = _get_reasoning_content(delta)
                if rc:
                    reasoning_parts.append(rc)
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
            _accumulate_usage(usage)
            # 有工具调用增量 → 返回 tool_calls
            if tool_calls_accum:
                calls = []
                for idx in sorted(tool_calls_accum):
                    acc = tool_calls_accum[idx]
                    calls.append({
                        "id": acc["id"] or f"call_{idx}",
                        "toolName": acc["name"],
                        "input": _parse_tool_arguments(acc["arguments"]),
                    })
                return AgentStep(type="tool_calls", calls=calls, reasoning_content=reasoning)
            return AgentStep(type="assistant", content="".join(content_parts), reasoning_content=reasoning)

        # 非流式:原逻辑
        _accumulate_usage(getattr(response, "usage", None))
        choice = response.choices[0]
        reasoning = _get_reasoning_content(choice.message)
        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            calls = []
            for tc in choice.message.tool_calls:
                calls.append({
                    "id": tc.id,
                    "toolName": tc.function.name,
                    "input": _parse_tool_arguments(tc.function.arguments),
                })
            return AgentStep(type="tool_calls", calls=calls, reasoning_content=reasoning)
        return AgentStep(type="assistant", content=choice.message.content or "", reasoning_content=reasoning)


class DeepSeekModel(OpenAICompatModel):
    """DeepSeek API 预设(OpenAI 兼容)。"""
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise ValueError("需要 DEEPSEEK_API_KEY 环境变量或传 api_key")
        super().__init__(
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            api_key=key,
            base_url="https://api.deepseek.com",
        )


# ---------- Mock 假实现 ----------
class MockModel(Model):
    """不联网:靠规则演戏,方便离线测试整条链路。"""
    def next(self, messages: list[dict[str, Any]], *, on_chunk=None, tools=None) -> AgentStep:
        # Mock 不流式,忽略 on_chunk 和 tools(保持接口兼容)
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


def _get_reasoning_content(obj: Any) -> str:
    """从 openai 响应对象取 DeepSeek 的 reasoning_content(思考模式)。

    DeepSeek 的 reasoning_content 是 OpenAI 协议之外的字段,openai SDK 可能
    把它放在 model_extra 里而非标准属性,这里两种都兼容。
    """
    rc = getattr(obj, "reasoning_content", None)
    if rc:
        return rc
    extra = getattr(obj, "model_extra", None) or {}
    return extra.get("reasoning_content", "") or ""


# ---------- 工具参数解析 ----------
def _parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    """解析工具调用参数,容错处理截断/非法 JSON。

    流式输出时 arguments 可能被截断(缺尾部括号),或模型输出非法 JSON。
    失败时降级为空 dict,由工具层的输入校验兜底,而不是让整个请求崩溃。
    """
    if not raw:
        return {}
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试补齐缺失的右括号(常见于流式截断)
    if text.startswith("{"):
        candidate = text
        for _ in range(50):
            candidate += "}"
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return {}


# ---------- 工具定义生成(给模型看的) ----------
def _tool_defs(tools) -> list[dict[str, Any]]:
    """把注册表里的工具,转成 OpenAI 认识的格式。"""
    out = []
    for name in tools.list_all():
        t = tools.find(name)
        schema = getattr(t, "schema", None)
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.description,
                "parameters": schema or {"type": "object", "properties": {}},
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


# ---------- 用量追踪 ----------

# 模块级累计用量(进程内所有模型调用的 token 统计)
_USAGE: dict[str, int] = {
    "requests": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}


def get_usage() -> dict[str, int]:
    """返回累计用量快照。"""
    return dict(_USAGE)


def reset_usage() -> None:
    """清零累计用量。"""
    for k in _USAGE:
        _USAGE[k] = 0


def _accumulate_usage(usage: Any) -> None:
    """累加一次 API 响应的 usage(可能为 None)。"""
    if usage is None:
        return
    _USAGE["requests"] += 1
    _USAGE["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
    _USAGE["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
    _USAGE["total_tokens"] += getattr(usage, "total_tokens", 0) or 0