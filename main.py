"""迷你版入口:组装工具和模型,起一个交互式循环。"""
from __future__ import annotations

import os
import sys

from minicore.tools import create_default_tools, close_mcp, ToolContext
from minicore.model import Model, DeepSeekModel, MockModel, set_tools
from minicore.agent_loop import run_agent_turn
from minicore.session import (
    create_new_session, save_session, load_session, list_sessions,
    format_session_list, format_rewind_preview, rewind_session_data,
)
from minicore.memory import MemoryStore
from minicore.permissions import PermissionManager
from minicore.dotenv import load_dotenv
from pathlib import Path

# 加载项目根目录的 .env(如需,可配置 DEEPSEEK_API_KEY / MY_AGENT_MOCK 等)
load_dotenv()

# Windows 终端中文显示兼容
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def build_model() -> Model:
    """按环境变量决定用真模型还是 Mock。"""
    if os.environ.get("MY_AGENT_MOCK"):
        return MockModel()
    try:
        return DeepSeekModel()   # 用环境变量 DEEPSEEK_API_KEY
    except ValueError as e:
        print(f"[警告] {e}\n将回退到 Mock 模型。")
        return MockModel()


def _print_conversation(messages: list) -> None:
    """像 Claude Code 的 conversationHistory 一样,只打印"对话"。
    过滤:工具调用/结果、系统注入、压缩摘要。显示:用户提问 + 助手回答。
    """
    print("── 历史对话 ──")
    shown_any = False
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        # 只保留 user / assistant 文本对话
        if role not in ("user", "assistant"):
            continue
        # 跳过系统注入标记
        if m.get("system_injected"):
            continue
        # 跳过 assistant 的工具调用消息(没有文本内容)
        if not content:
            continue
        text = str(content)
        # 跳过系统注入(前缀兜底)
        if text.startswith("[系统]"):
            continue
        # 跳过压缩摘要
        if text.startswith("前情摘要:") or "前情摘要:" in text[:30]:
            continue
        # 显示完整对话(不截断)
        label = "用户" if role == "user" else "助手"
        print(f"  [{label}] {text}")
        shown_any = True
    if not shown_any:
        print("  (这个会话没有可读的对话记录)")


def main() -> None:
    cwd = os.getcwd()
    tools = create_default_tools()
    set_tools(tools)   # 让模型层能生成工具清单

    model = build_model()
    print("可用工具:", ", ".join(tools.list_all()))

    # 支持 --resume <session_id> 启动时恢复会话
    resume_id = ""
    if "--resume" in sys.argv:
        idx = sys.argv.index("--resume")
        if idx + 1 < len(sys.argv):
            resume_id = sys.argv[idx + 1]

    # 创建记忆库,把项目记忆注入系统提示词
    memory = MemoryStore(Path(cwd) / ".my-agent-memory.json")
    system_prompt = "你是一个终端里的编程助手。用中文回答,需要时调用工具。"
    memory_text = memory.render_for_prompt()
    if memory_text:
        system_prompt += "\n\n" + memory_text

    # 创建或恢复会话
    if resume_id:
        session = load_session(resume_id)
        if session is None:
            print(f"[警告] 未找到会话 {resume_id},新建一个")
            session = create_new_session(cwd)
        else:
            print(f"已恢复会话: {session.session_id[:12]}")
    else:
        session = create_new_session(cwd)
    print("会话 id:", session.session_id)

    messages = list(session.messages)
    if not messages or messages[0]["role"] != "system":
        messages.insert(0, {"role": "system", "content": system_prompt})

    # 创建权限管理器(敏感工具会弹窗问用户)
    permissions = PermissionManager(cwd=cwd)

    # 简单交互循环
    print("输入任务(或 /exit 退出):")
    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            break
        if user_input == "/tools":
            print(", ".join(tools.list_all()))
            continue
        if user_input == "/rewind-preview":
            print(format_rewind_preview(session))
            continue
        if user_input == "/rewind":
            restored = rewind_session_data(session, steps=1)
            if restored:
                print(f"已回退 {len(restored)} 个 checkpoint")
            else:
                print("没有可回退的 checkpoint")
            continue
        if user_input == "/sessions":
            print(format_session_list())
            continue
        if user_input == "/cleanup":
            from minicore.cleanup_sessions import plan_cleanup
            keep, remove = plan_cleanup()
            print(f"将保留 {len(keep)} 个,删除 {len(remove)} 个会话:")
            for sid, reason in remove:
                print(f"  - {sid}: {reason}")
            print("真正删除请运行: python -m minicore.cleanup_sessions --delete")
            continue
        if user_input.startswith("/resume"):
            parts = user_input.split(maxsplit=1)
            resume_id = parts[1] if len(parts) > 1 else ""
            loaded = load_session(resume_id) if resume_id else None
            if loaded is None:
                print(f"未找到会话: {resume_id or '(缺 id)'},可用 /sessions 查看")
                continue
            session = loaded
            messages = list(loaded.messages)
            # 确保有 system 消息
            if not messages or messages[0]["role"] != "system":
                messages.insert(0, {"role": "system", "content": system_prompt})
            print(f"已恢复会话 {session.session_id[:12]},消息 {len(messages)} 条")
            _print_conversation(messages)
            continue

        if user_input == "/history":
            _print_conversation(messages)
            continue

        messages.append({"role": "user", "content": user_input})

        # 流式输出回调:打印助手回复(打字机效果)。用列表收集,避免和后面 print 冲突
        streamed_parts: list[str] = []
        def on_chunk(text: str) -> None:
            streamed_parts.append(text)
            sys.stdout.write(text)
            sys.stdout.flush()

        messages = run_agent_turn(
            model=model,
            tools=tools,
            messages=messages,
            cwd=cwd,
            max_steps=20,
            session=session,
            memory=memory,
            permissions=permissions,
            on_assistant_chunk=on_chunk,
        )
        # 流式模式下手动补一个换行
        if streamed_parts:
            print()
        # 关键:把 run_agent_turn 返回的新消息同步回 session,这样 save 才存得下
        session.messages = list(messages)
        # 每轮后自动把会话存到磁盘
        save_session(session)


if __name__ == "__main__":
    try:
        main()
    finally:
        # 退出时关闭 MCP 客户端连接,避免子进程残留
        close_mcp()