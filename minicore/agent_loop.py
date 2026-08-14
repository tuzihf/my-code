"""迷你版主干循环:问模型 → 模型要调工具 → 执行 → 结果回填 → 再来一轮。

P4 加了两样"守护":
- phase 状态机:根据步数判断当前阶段(explore/execute/verify)
- verification 门禁:模型说"做完了"时,先检查证据够不够
"""
from __future__ import annotations

import concurrent.futures
from typing import Any

from minicore.tools import ToolRegistry, ToolContext, ToolResult, READONLY_TOOL_NAMES
from minicore.model import Model, AgentStep
from minicore.kernel import derive_phase, decide_assistant_turn
from minicore.context_compactor import compact
from minicore.read_dedup import ReadDedup
from minicore.permissions import PermissionManager
from minicore.tool_cache import persist_tool_result, should_persist


# 只读工具:可并发执行(无副作用)。写/命令工具必须串行。
_READ_ONLY_TOOLS = READONLY_TOOL_NAMES


def _tool_call_signature(call: dict[str, Any]) -> tuple[str, str]:
    """把一次工具调用规范成可哈希的 (工具名, 参数) 签名。"""
    import json
    tool_name = call.get("toolName", "")
    tool_input = call.get("input", {})
    try:
        key = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    except Exception:
        key = str(tool_input)
    return (tool_name, key)


def _is_repeating(calls: list[dict[str, Any]], recent_calls: list[tuple[str, Any]], threshold: int = 3) -> bool:
    """判断本轮调用是否与最近 threshold 次调用完全重复(死循环信号)。"""
    if len(recent_calls) < threshold:
        return False
    recent_sigs = [_tool_call_signature({"toolName": n, "input": i}) for n, i in recent_calls[-threshold:]]
    # 最近 threshold 次必须是同一次调用
    if len(set(recent_sigs)) != 1:
        return False
    # 且本轮的所有调用也都等于该签名
    return all(_tool_call_signature(c) == recent_sigs[0] for c in calls)


def _current_question(messages: list[dict[str, Any]]) -> str:
    """取最后一条真实用户提问(跳过系统注入),作为"当前问题"锚点。"""
    for m in reversed(messages):
        if m.get("role") == "user" and not m.get("system_injected"):
            c = str(m.get("content", "")).strip()
            if c:
                return c
    return ""


def _finish_summary(step: AgentStep) -> str | None:
    """如果 step 是 finish 工具调用,返回其 summary;否则返回 None。"""
    if step.type != "tool_calls":
        return None
    for c in step.calls:
        if c.get("toolName") == "finish":
            s = str(c.get("input", {}).get("summary", "") or "").strip()
            return s or step.content or "完成。"
    return None


def _emit_chunks(on_chunk: Any, text: str, chunk_size: int = 3) -> None:
    """把整段文本切成小块逐个回调,模拟流式打字机(供一次性来源的文本使用)。"""
    if on_chunk is None or not text:
        return
    for i in range(0, len(text), chunk_size):
        try:
            on_chunk(text[i:i + chunk_size])
        except Exception:
            pass


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
    should_stop: Any | None = None,
    auto_verify: bool = False,
    verify_command: str = "python -m pytest",
    confirm_edit: Any | None = None,
    on_revoke: Any | None = None,
) -> list[dict[str, Any]]:
    """把一轮对话跑到结束(模型给出文本答案,或耗尽步数)。

    should_stop: 可选的零参回调,返回 True 时在每轮循环开头协作式停止。
    auto_verify: 为 True 时,本轮改过文件后、模型要结束前,自动跑 verify_command
                 并把结果回喂给模型(最多 2 次),形成"改→验→修"闭环。
    """
    step_count = 0
    saw_tool_result = False   # 本轮有没有真的调过工具
    saw_write = False         # 本轮是否真的改过文件(write_file/edit_file)
    verify_count = 0          # 本轮已自动验证的次数
    recent_calls: list[tuple[str, Any]] = []   # 记录最近的工具调用,防重复死循环
    read_dedup = ReadDedup()   # 记录读过的文件,重复读就返回占位
    import uuid
    group_id = f"turn-{uuid.uuid4().hex[:12]}"   # 本 turn 所有文件编辑共享的组号(整体回退用)

    # 真实 token 累计(用模型 usage 差值,替换纯启发式估算触发压缩)
    from minicore.model import get_usage
    real_tokens = 0
    prev_usage_total = get_usage()["total_tokens"]

    def _call_model(msgs: list[dict[str, Any]], on_chunk: Any | None = None) -> AgentStep:
        """调用模型并累计真实 token。"""
        nonlocal real_tokens, prev_usage_total
        if on_chunk is not None:
            s = model.next(msgs, on_chunk=on_chunk, tools=tools)
        else:
            s = model.next(msgs, tools=tools)
        cur = get_usage()["total_tokens"]
        real_tokens += cur - prev_usage_total
        prev_usage_total = cur
        return s

    # 多轮对话聚焦:维护"当前问题"锚点(原地替换,只保留最新一条)。
    # 否则模型看到上下文里多个并列的历史问题,会倾向做"多话题全面总结"而非只答当前问题。
    _anchor_prefix = "用户当前的问题是"
    for _i in range(len(messages) - 1, -1, -1):
        _m = messages[_i]
        if _m.get("system_injected") and str(_m.get("content", "")).startswith(_anchor_prefix):
            del messages[_i]
    _user_qs = [m for m in messages if m.get("role") == "user" and not m.get("system_injected")]
    if len(_user_qs) > 1:
        _q = _current_question(messages)
        messages.append({
            "role": "user",
            "content": (f"用户当前的问题是:「{_q}」。"
                        f"之前的历史问题已经回答过,请只针对当前问题回答,不要重复或总结它们。"),
            "system_injected": True,
        })

    while step_count < max_steps:
        # 协作式取消:外部请求停止时,尽快结束
        if should_stop is not None and should_stop():
            messages.append({"role": "assistant", "content": "已停止生成。", "reasoning_content": ""})
            return messages
        step_count += 1

        # ① 根据当前步数推导 phase
        policy = derive_phase(step_count, max_steps)

        # ② 上下文压缩:真实累计 token 超阈值才压,且保留足够新鲜上下文(keep_recent=10)
        #    避免压缩太激进导致模型"失忆",来回重复读文件
        if real_tokens > max_tokens:
            messages, changed = compact(messages, keep_recent=10, max_tokens=max_tokens, force=True)
            if changed:
                messages.insert(0, {
                    "role": "user",
                    "content": "[系统] 旧对话已被压缩成摘要以节省空间,请基于摘要和最近对话继续。",
                    "system_injected": True,
                })
                real_tokens = 0   # 压缩后重置累计(近似,下轮重新累计)

        # ③ 问模型要下一步动作(真正流式:边生成边输出;若被门禁否决,由 on_revoke 撤回)
        try:
            if on_assistant_chunk is not None:
                step: AgentStep = _call_model(messages, on_chunk=on_assistant_chunk)
            else:
                step: AgentStep = _call_model(messages)
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
                    step = _call_model(messages)
                else:
                    raise
            else:
                raise

        # ③.0 finish 工具:结构化结束信号(模型调用 finish 表达"完成")
        summary = _finish_summary(step)
        if summary is not None:
            _emit_chunks(on_assistant_chunk, summary)   # 切块输出,保留打字机效果
            messages.append({"role": "assistant", "content": summary,
                             "reasoning_content": step.reasoning_content})
            return messages

        # ③.5 收尾逼迫:进入 verify 阶段后,持续逼模型针对当前问题给结论
        if step.type == "tool_calls" and policy.phase == "verify":
            q = _current_question(messages)
            messages.append({
                "role": "user",
                "content": (f"[系统] 你已进入收尾阶段。用户当前的问题是:「{q}」。"
                            f"请只针对它给出简短、直接的答案,不要总结历史对话,"
                            f"也不要介绍项目的其他模块。完成后请调用 finish 工具(summary 为最终答案)。"),
                "system_injected": True,
            })
            # 持续逼迫直到模型给出文本结论或 finish(最多逼 3 次)
            for _ in range(3):
                step = _call_model(messages, on_chunk=on_assistant_chunk)   # 流式
                if step.type == "assistant":
                    # 已通过 on_chunk 流式输出,这里只记录消息
                    messages.append({"role": "assistant", "content": step.content,
                                     "reasoning_content": step.reasoning_content})
                    return messages
                # 模型可能在这里调用 finish 结束
                s = _finish_summary(step)
                if s is not None:
                    _emit_chunks(on_assistant_chunk, s)   # 切块输出
                    messages.append({"role": "assistant", "content": s,
                                     "reasoning_content": step.reasoning_content})
                    return messages
                messages.append({
                    "role": "user",
                    "content": (f"[系统] 不要再调用工具了。直接回答用户当前的问题「{q}」,"
                                f"或调用 finish 工具给出结论。"),
                    "system_injected": True,
                })
            # 逼 3 次仍不给 → 返回一个诚实的占位(而不是让对话卡住,也不编造结论)
            fallback = ("我已在收尾阶段多次尝试给出结论,但仍无法基于已获取的信息形成可靠回答。"
                        "请补充更具体的要求,或允许我继续调用工具查证后重试。")
            _emit_chunks(on_assistant_chunk, fallback)
            messages.append({"role": "assistant", "content": fallback, "reasoning_content": ""})
            return messages

        if step.type == "assistant":
            # 过 verification 门禁:该不该结束?
            finish, reason = decide_assistant_turn(
                content=step.content,
                phase=policy.phase,
                saw_tool_result=saw_tool_result,
                question=_current_question(messages),
            )
            if finish:
                # 编辑后自动验证:改过文件且开启 auto_verify 时,先跑测试回喂给模型
                if auto_verify and saw_write and verify_count < 2:
                    verify_count += 1
                    messages.append({"role": "assistant", "content": step.content,
                                     "reasoning_content": step.reasoning_content})
                    verify_result = tools.execute(
                        "run_command",
                        {"command": verify_command, "timeout": 120},
                        ToolContext(cwd=cwd, session=session, memory=memory, group_id=group_id),
                    )
                    messages.append({
                        "role": "user",
                        "content": (f"[系统] 你修改了文件,自动运行验证命令 `{verify_command}` 的结果如下:\n"
                                    f"{verify_result.output}\n"
                                    f"如果验证失败,请修复后重新验证;如果通过,请给出最终结论。"),
                        "system_injected": True,
                    })
                    saw_write = False   # 重置,等待下一轮写操作
                    continue
                messages.append({"role": "assistant", "content": step.content,
                                 "reasoning_content": step.reasoning_content})
                return messages
            # 门禁不通过 → 通知前端撤回已流式输出的文本,并逼模型继续
            if on_revoke is not None:
                try:
                    on_revoke()
                except Exception:
                    pass
            messages.append({"role": "assistant", "content": step.content,
                             "reasoning_content": step.reasoning_content})
            messages.append({"role": "user", "content": f"[系统] {reason}",
                             "system_injected": True})
            continue

        # ③ 模型要调工具 → 执行
        # 防死循环:连续 3 次调用同一工具+相同参数 → 注入提示打断
        if _is_repeating(step.calls, recent_calls):
            messages.append({
                "role": "user",
                "content": "[系统] 你已连续多次调用同一个工具(相同参数)却没有进展。请换一种方式:读不同的文件、grep 关键词,或直接基于已有信息给出结论。",
                "system_injected": True,
            })
            continue

        # 只读工具并发执行,写/命令工具串行执行,结果保持原始顺序
        concurrent_calls = [c for c in step.calls if c["toolName"] in _READ_ONLY_TOOLS]
        serial_calls = [c for c in step.calls if c["toolName"] not in _READ_ONLY_TOOLS]

        # 并发执行只读工具(用线程池,提速)
        if concurrent_calls:
            def _run_one(call):
                tool_name = call["toolName"]
                tool_input = call.get("input", {})
                return call, tools.execute(tool_name, tool_input, ToolContext(cwd=cwd, session=session, memory=memory, group_id=group_id))

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
            # 编辑确认:confirm_edit 提供时,先 dry_run 预览 diff,经批准后才真正写
            if confirm_edit is not None and tool_name in ("write_file", "edit_file", "apply_patch"):
                ctx = ToolContext(cwd=cwd, session=session, memory=memory, group_id=group_id)
                preview = tools.execute(tool_name, {**tool_input, "dry_run": True}, ctx)
                try:
                    approved = bool(confirm_edit(tool_name, preview.output))
                except Exception:
                    approved = True   # 回调异常时放行,避免卡死
                if not approved:
                    result = ToolResult(ok=False, output=f"[已拒绝] {tool_name} 的修改被用户拒绝")
                    serial_results.append((call, result))
                    continue
            result = tools.execute(tool_name, tool_input, ToolContext(cwd=cwd, session=session, memory=memory, group_id=group_id))
            serial_results.append((call, result))

        # 合并结果,保持原始调用顺序
        _order = {call["id"]: i for i, call in enumerate(step.calls)}
        all_results = concurrent_results + serial_results
        all_results.sort(key=lambda pair: _order.get(pair[0]["id"], 999))

        for call, result in all_results:
            tool_name = call["toolName"]
            tool_input = call.get("input", {})
            saw_tool_result = True
            if tool_name in ("write_file", "edit_file") and result.ok:
                saw_write = True
            recent_calls.append((tool_name, tool_input))
            # 只保留最近 6 次调用,避免无限增长
            if len(recent_calls) > 6:
                recent_calls.pop(0)

            # 工具调用回调(供 UI 显示工具执行,传结果供 diff 展示)
            if on_tool_call is not None:
                try:
                    on_tool_call(tool_name, tool_input, result)
                except TypeError:
                    # 兼容旧签名(只收 tool_name, tool_input)
                    on_tool_call(tool_name, tool_input)
                except Exception:
                    pass

            # read_dedup:如果这个 read_file 片段之前读过同内容,用占位替换全文,省上下文
            if tool_name == "read_file":
                file_path = str(tool_input.get("path", ""))
                offset = int(tool_input.get("offset", 1) or 1)
                limit = int(tool_input.get("limit", 2000) or 2000)
                if file_path and result.ok and read_dedup.should_dedup(file_path, result.output, offset, limit):
                    result.output = read_dedup.get_stub(file_path, offset, limit)
                elif file_path and result.ok:
                    read_dedup.register_read(file_path, result.output, offset, limit)

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
    messages.append({"role": "assistant", "content": "已达本轮最大步数限制,停止。", "reasoning_content": ""})
    return messages
