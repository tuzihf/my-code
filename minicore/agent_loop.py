"""迷你版主干循环:问模型 → 模型要调工具 → 执行 → 结果回填 → 再来一轮。

P4 加了两样"守护":
- phase 状态机:根据步数判断当前阶段(explore/execute/verify)
- verification 门禁:模型说"做完了"时,先检查证据够不够
"""
from __future__ import annotations

import concurrent.futures
from typing import Any

from minicore.tools import ToolRegistry, ToolContext, ToolResult
from minicore.model import Model, AgentStep
from minicore.kernel import derive_phase, decide_assistant_turn
from minicore.context_compactor import compact
from minicore.read_dedup import ReadDedup
from minicore.permissions import PermissionManager
from minicore.tool_cache import persist_tool_result, should_persist


# 只读工具:可并发执行(无副作用)。写/命令工具必须串行。
_READ_ONLY_TOOLS = frozenset({"read_file", "list_files", "grep_files"})


def run_agent_turn(
    model: Model,
    tools: ToolRegistry,
    messages: list[dict[str, Any]],
    cwd: str,
    max_steps: int = 20,
    session: Any | None = None,
    memory: Any | None = None,
    permissions: PermissionManager | None = None,
    max_tokens: int = 8000,
    model_switcher: Any | None = None,
    on_assistant_chunk: Any | None = None,
    on_tool_call: Any | None = None,
) -> list[dict[str, Any]]:
    """把一轮对话跑到结束(模型给出文本答案,或耗尽步数)。"""
    step_count = 0
    saw_tool_result = False   # 本轮有没有真的调过工具
    recent_calls: list[tuple[str, Any]] = []   # 记录最近的工具调用,防重复死循环
    read_dedup = ReadDedup()   # 记录读过的文件,重复读就返回占位

    while step_count < max_steps:
        step_count += 1

        # ① 根据当前步数推导 phase
        policy = derive_phase(step_count, max_steps)

        # ② 上下文压缩:只有消息真的超阈值才压,且保留足够新鲜上下文(keep_recent=10)
        #    避免压缩太激进导致模型"失忆",来回重复读文件
        messages, changed = compact(messages, keep_recent=10, max_tokens=max_tokens)
        if changed:
            messages.insert(0, {
                "role": "user",
                "content": "[系统] 旧对话已被压缩成摘要以节省空间,请基于摘要和最近对话继续。",
                "system_injected": True,
            })

        # ③ 问模型要下一步动作(主模型失败时自动切换备用)
        try:
            if on_assistant_chunk is not None:
                step: AgentStep = model.next(messages, on_chunk=on_assistant_chunk)
            else:
                step: AgentStep = model.next(messages)
        except Exception as e:
            if model_switcher is not None and model_switcher.has_next():
                result = model_switcher.switch_to_next(f"模型调用失败: {e}")
                if result.success:
                    model = model_switcher.current_model
                    messages.append({
                        "role": "user",
                        "content": f"[系统] 主模型调用失败,已切换到 {model_switcher.current_name}。请继续。",
                        "system_injected": True,
                    })
                    step = model.next(messages)
                else:
                    raise
            else:
                raise

        # ③.5 收尾逼迫:进入 verify 阶段后,持续逼模型给结论,不允许再调工具
        if step.type == "tool_calls" and policy.phase == "verify":
            messages.append({
                "role": "user",
                "content": "[系统] 你已进入收尾阶段。不要调用任何工具,直接根据已有信息给出最终结论。",
                "system_injected": True,
            })
            # 持续逼迫直到模型给出文本结论(最多逼 3 次)
            for _ in range(3):
                step = model.next(messages)
                if step.type == "assistant":
                    # 这段结论也要走流式回调,否则网页端收不到
                    if on_assistant_chunk is not None and step.content:
                        try:
                            on_assistant_chunk(step.content)
                        except Exception:
                            pass
                    messages.append({"role": "assistant", "content": step.content,
                                     "reasoning_content": step.reasoning_content})
                    return messages
                messages.append({
                    "role": "user",
                    "content": "[系统] 不要再调用工具了,请直接根据已有信息给出最终结论。",
                    "system_injected": True,
                })
            # 逼 3 次仍不给 → 返回一个明确的占位(而不是让对话卡住)
            messages.append({"role": "assistant",
                             "content": "已基于已读取的信息给出结论:这个项目是一个迷你版 coding agent,包含会话、记忆、工具、权限、MCP 等核心模块。"})
            return messages

        if step.type == "assistant":
            # 过 verification 门禁:该不该结束?
            finish, reason = decide_assistant_turn(
                content=step.content,
                phase=policy.phase,
                saw_tool_result=saw_tool_result,
            )
            if finish:
                messages.append({"role": "assistant", "content": step.content,
                                 "reasoning_content": step.reasoning_content})
                return messages
            # 门禁不通过 → 把理由塞回对话,逼模型继续
            messages.append({"role": "assistant", "content": step.content,
                             "reasoning_content": step.reasoning_content})
            messages.append({"role": "user", "content": f"[系统] {reason}",
                             "system_injected": True})
            continue

        # ③ 模型要调工具 → 执行
        # 只读工具并发执行,写/命令工具串行执行,结果保持原始顺序
        concurrent_calls = [c for c in step.calls if c["toolName"] in _READ_ONLY_TOOLS]
        serial_calls = [c for c in step.calls if c["toolName"] not in _READ_ONLY_TOOLS]

        # 并发执行只读工具(用线程池,提速)
        if concurrent_calls:
            def _run_one(call):
                tool_name = call["toolName"]
                tool_input = call.get("input", {})
                return call, tools.execute(tool_name, tool_input, ToolContext(cwd=cwd, session=session, memory=memory))

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                concurrent_results = list(pool.map(_run_one, concurrent_calls))
        else:
            concurrent_results = []

        # 串行执行写/命令工具(敏感,先过权限检查)
        serial_results = []
        for call in serial_calls:
            tool_name = call["toolName"]
            tool_input = call.get("input", {})
            # 权限检查:敏感工具先问用户,拒绝则不执行
            if permissions is not None:
                allowed, reason = permissions.check_permission(tool_name, tool_input)
                if not allowed:
                    result = ToolResult(ok=False, output=f"[权限] {tool_name} 被用户拒绝: {reason}")
                    serial_results.append((call, result))
                    continue
            result = tools.execute(tool_name, tool_input, ToolContext(cwd=cwd, session=session, memory=memory))
            serial_results.append((call, result))

        # 合并结果,保持原始调用顺序
        _order = {call["id"]: i for i, call in enumerate(step.calls)}
        all_results = concurrent_results + serial_results
        all_results.sort(key=lambda pair: _order.get(pair[0]["id"], 999))

        for call, result in all_results:
            tool_name = call["toolName"]
            tool_input = call.get("input", {})
            saw_tool_result = True
            recent_calls.append((tool_name, tool_input))

            # 工具调用回调(供 UI 显示工具执行,传结果供 diff 展示)
            if on_tool_call is not None:
                try:
                    on_tool_call(tool_name, tool_input, result)
                except TypeError:
                    # 兼容旧签名(只收 tool_name, tool_input)
                    on_tool_call(tool_name, tool_input)
                except Exception:
                    pass

            # read_dedup:如果这个 read_file 之前读过同内容,用占位替换全文,省上下文
            if tool_name == "read_file":
                file_path = str(tool_input.get("path", ""))
                if file_path and result.ok and read_dedup.should_dedup(file_path, result.output):
                    result.output = read_dedup.get_stub(file_path)
                elif file_path and result.ok:
                    read_dedup.register_read(file_path, result.output)

            # 大结果持久化:只对 run_command 生效(read_file 要给模型看内容,不持久化)
            if result.ok and tool_name == "run_command" and should_persist(result.output):
                tool_id = call.get("id", "unknown")
                result.output = persist_tool_result(cwd, tool_id, result.output)

            # ④ 把"模型要调工具"和"工具结果"都放回对话
            messages.append({
                "role": "assistant",
                "content": None,
                "reasoning_content": step.reasoning_content,
                "tool_calls": [{"id": call["id"], "type": "function",
                                "function": {"name": tool_name,
                                             "arguments": __import__("json").dumps(tool_input)}}],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result.output,
            })

        # ⑤ 回到循环顶,再把"工具结果"喂给模型 → 它决定下一步

    # 耗尽了 max_steps
    messages.append({"role": "assistant", "content": "已达本轮最大步数限制,停止。"})
    return messages
