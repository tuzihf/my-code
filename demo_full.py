"""完整能力演示:真实 DeepSeek 跑一个任务,展示权限/记忆/压缩/门禁/fallback 配合。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path

from minicore.tools import create_default_tools, ToolContext
from minicore.model import DeepSeekModel, set_tools
from minicore.agent_loop import run_agent_turn
from minicore.session import create_new_session
from minicore.memory import MemoryStore
from minicore.permissions import PermissionManager

cwd = "D:/Pycharm/my-agent"
tools = create_default_tools()
set_tools(tools)

# 用 DeepSeek 真模型
try:
    model = DeepSeekModel()
    print("模型: DeepSeek ✅")
except ValueError as e:
    print("无法初始化 DeepSeek:", e)
    sys.exit(1)

# 权限:自动允许(避免交互卡住,专注演示其它能力)
permissions = PermissionManager(cwd=cwd, prompt=lambda req: {"decision": "allow_once"})

# 记忆:从磁盘加载(如果之前存过会带进上下文)
memory = MemoryStore(Path(cwd) / ".my-agent-memory.json")
if memory.all():
    print("记忆(从磁盘加载):")
    for e in memory.all():
        print(f"  - {e.content}")

# 会话
session = create_new_session(cwd)
print("会话 id:", session.session_id)

# 系统提示词:注入记忆
system_prompt = "你是一个终端里的编程助手。用中文回答,需要时调用工具。"
mem_text = memory.render_for_prompt()
if mem_text:
    system_prompt += "\n\n" + mem_text

messages = [{"role": "system", "content": system_prompt}]

# 一个会触发"读文件"的任务
user_input = "先看这个项目的 agent_loop.py,然后用一句话总结它是干什么的"
print("\n用户:", user_input)

messages.append({"role": "user", "content": user_input})
result = run_agent_turn(
    model=model, tools=tools, messages=messages, cwd=cwd,
    max_steps=8, session=session, memory=memory, permissions=permissions,
)

# 保存会话
from session import save_session
save_session(session)

# 打印最终回复
print("\n=== 最终回复 ===")
for m in reversed(result):
    if m.get("role") == "assistant" and m.get("content"):
        print(m["content"][:800])
        break
