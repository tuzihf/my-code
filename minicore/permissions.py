"""迷你版权限系统:敏感工具执行前先问用户。

对应原版 minicode/permissions.py 的核心概念:
- PermissionManager:管理"这个工具该不该问用户"
- allow / allow_once / deny / deny_once:四种决定
"""
from __future__ import annotations

from typing import Any, Callable


class PermissionManager:
    """管理工具权限:读安全,写和命令要问用户。

    prompt: 一个函数,收到请求时返回用户的决定。可注入 mock,方便测试。
    """

    def __init__(
        self,
        cwd: str,
        prompt: Callable[[dict[str, Any]], dict[str, str]] | None = None,
    ) -> None:
        self.cwd = cwd
        self._prompt = prompt or self._default_prompt
        self.decisions: dict[str, str] = {}   # 记录每个工具的最后决定

    # 一个工具该不该问用户?
    def _needs_confirmation(self, tool_name: str) -> bool:
        # 写文件、跑命令、记住东西 → 敏感,要问
        return tool_name in {"write_file", "run_command", "remember"}

    # 真正执行前,检查是否允许
    def check_permission(self, tool_name: str, input_data: dict[str, Any]) -> tuple[bool, str]:
        """返回 (allowed, reason)。allowed=False 表示被拒绝。"""
        if not self._needs_confirmation(tool_name):
            return True, ""   # 只读工具,直接放行

        request = {
            "tool": tool_name,
            "input": input_data,
            "summary": f"{tool_name} 需要你的批准",
        }
        decision = self._prompt(request)
        self.decisions[tool_name] = decision.get("decision", "deny")
        allowed = decision.get("decision") in ("allow", "allow_once")
        reason = "用户批准" if allowed else "用户拒绝"
        return allowed, reason

    # 默认的询问逻辑(真实交互时用 input 问用户)
    def _default_prompt(self, request: dict[str, Any]) -> dict[str, str]:
        answer = input(f"\n⚠️  {request['summary']}\n  {request['tool']} 参数: {request['input']}\n允许吗? [y/n] ").strip().lower()
        return {"decision": "allow_once" if answer in ("y", "yes") else "deny_once"}