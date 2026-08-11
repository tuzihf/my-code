"""诊断:为什么真模型跑任务会"耗尽步数没回答"。

逐轮打印:模型返回什么、门禁怎么判、压缩是否触发。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path

from minicore.tools import create_default_tools
from minicore.model import DeepSeekModel, set_tools
import minicore.agent_loop as agent_loop
from minicore.context_compactor import estimate_tokens

tools = create_default_tools()
set_tools(tools)
model = DeepSeekModel()

# 包裹 model.next,打印模型每一步返回什么
orig_next = model.next
def logged_next(messages):
    step = orig_next(messages)
    if step.type == "tool_calls":
        print(f"  [模型] 想调工具: {[c['toolName'] for c in step.calls]}")
    else:
        print(f"  [模型] 给文本: {step.content[:70]!r}")
    return step
model.next = logged_next

# 包裹门禁,打印它怎么判
orig_gate = agent_loop.decide_assistant_turn
def logged_gate(**kw):
    finish, reason = orig_gate(**kw)
    print(f"  [门禁] phase={kw['phase']} saw_tool={kw['saw_tool_result']} content={kw['content'][:40]!r} → finish={finish}")
    return finish, reason
agent_loop.decide_assistant_turn = logged_gate

# 包裹压缩,打印是否触发
orig_compact = agent_loop.compact
def logged_compact(messages, **kw):
    result, changed = orig_compact(messages, **kw)
    if changed:
        print(f"  [压缩] 触发! 消息数 {len(messages)} → {len(result)}")
    return result, changed
agent_loop.compact = logged_compact

cwd = str(Path("D:/Pycharm/my-agent").resolve())
messages = [{"role": "user", "content": "解释这个项目的架构,并给出优化建议"}]
print("=== 开始跑, max_steps=20 ===")
result = agent_loop.run_agent_turn(model=model, tools=tools, messages=messages, cwd=cwd, max_steps=20)

print("\n=== 结束时消息数:", len(result), " 总token:", sum(estimate_tokens(m.get("content") or "") for m in result))
print("=== 最后5条消息角色 ===")
for m in result[-5:]:
    print(f"  [{m['role']}] {str(m.get('content'))[:80]!r}")
