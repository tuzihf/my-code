"""迷你版模型 fallback:主模型失败时,自动切换备用模型。

对应原版 minicode/model_switcher.py 的核心概念:
- ModelSwitcher:记录候选模型,失败时切换重试
- switch_to:切换到下一个可用模型
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class SwitchResult:
    success: bool
    new_model: str = ""
    reason: str = ""


class ModelSwitcher:
    """管理一组模型候选,主模型失败时切换到备用。"""

    def __init__(self, models: list[Any], model_names: list[str]) -> None:
        """models: 模型适配器列表; model_names: 对应名称。"""
        self.models = models
        self.model_names = model_names
        self._current_index = 0
        self._switch_count = 0

    @property
    def current_model(self) -> Any:
        return self.models[self._current_index]

    @property
    def current_name(self) -> str:
        return self.model_names[self._current_index]

    def has_next(self) -> bool:
        return self._current_index + 1 < len(self.models)

    def switch_to_next(self, reason: str) -> SwitchResult:
        """切换到下一个模型。返回是否成功。"""
        if not self.has_next():
            return SwitchResult(success=False, reason="没有更多备用模型")
        self._current_index += 1
        self._switch_count += 1
        return SwitchResult(success=True, new_model=self.current_name, reason=reason)

    def get_switch_count(self) -> int:
        return self._switch_count