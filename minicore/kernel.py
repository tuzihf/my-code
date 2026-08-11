"""迷你版运行时控制:phase 状态机 + verification 门禁。

对应原版 minicode/turn_kernel.py 的核心概念:
- derive_turn_step_policy:根据步数推导当前 phase
- decide_assistant_turn:判断模型这一步该不该结束/继续
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PhasePolicy:
    """当前阶段的行为策略。"""
    phase: str              # explore / execute / verify / done
    should_continue: bool   # 该不该继续逼模型干活
    note: str = ""


def derive_phase(step: int, max_steps: int) -> PhasePolicy:
    """根据步数,判断当前处于哪个阶段。"""
    if step <= max_steps * 0.3:
        return PhasePolicy(phase="explore", should_continue=True,
                           note="前期:先探索、读文件,别急着下结论")
    if step <= max_steps * 0.6:
        return PhasePolicy(phase="execute", should_continue=True,
                           note="中期:动手干活,调用工具,保持推进")
    return PhasePolicy(phase="verify", should_continue=True,
                       note="后期:收尾,验证结果,准备给最终答案")


def decide_assistant_turn(*, content: str, phase: str, saw_tool_result: bool) -> tuple[bool, str]:
    """模型给了文本回答。判断:该结束? 还是该继续?

    返回 (should_finish, reason):
      - should_finish=True  : 模型确实做完了,结束
      - should_finish=False : 还没做完,塞一条 nudge 逼它继续
    """
    # ① 空回答 → 肯定没做完,催它
    if not content or not content.strip():
        return False, "你的回复是空的。请继续干活:调用工具、改代码,或给出明确的最终答案。"

    # ② 在 explore 阶段就急着"总结" → 太早了,催它先看代码
    if phase == "explore" and _looks_like_conclusion(content):
        return False, "你还在探索阶段,不要急着下结论。先读相关文件、grep 关键代码,搞清楚再行动。"

    # ③ 在 execute 阶段但连工具都没用过,且内容像是"兜底提示文本" → 空转,催它
    if phase == "execute" and not saw_tool_result and _looks_like_fallback_hint(content):
        return False, "你还没用过任何工具,只是在空转。请真正去调用工具干活:read_file / list_files / run_command。"

    # ④ 其他情况:模型给了像样的回答 → 允许结束
    return True, ""


def _looks_like_conclusion(text: str) -> bool:
    """粗略判断这段文本像不像"结论"(结束语)。"""
    markers = ("综上所述", "总结", "以上就是", "我的建议是", "the answer is",
               "in conclusion", "i think", "我认为")
    return any(m in text.lower() for m in markers)


def _looks_like_fallback_hint(text: str) -> bool:
    """判断这段文本像不像模型"兜底提示"(没认出来时机械回复的通用文本)。"""
    markers = ("you can try", "minicode", "/ls", "/grep", "/read",
               "这是一个", "可用工具", "支持的命令")
    return any(m in text.lower() for m in markers)