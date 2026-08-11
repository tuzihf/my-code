"""路径沙箱:统一解析路径,锁死在项目目录内。

对应原版 minicode/workspace.py 的 resolve_tool_path 机制:
- 解析相对路径 → 绝对路径
- 检查结果必须在允许的根目录(cwd)内,否则拒绝
- 防 `..` 穿越、绝对路径逃逸
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_tool_path(context, user_path: str, mode: str = "read") -> Path:
    """解析工具传入的路径,确保它在项目目录内。

    Args:
        context: ToolContext,含 cwd
        user_path: 用户/模型传入的路径
        mode: 操作类型(read/write/list)

    Returns:
        解析后的绝对路径(已在 cwd 内)

    Raises:
        PermissionError: 路径逃逸出项目目录
        RuntimeError: 路径无效
    """
    cwd = Path(context.cwd).resolve()
    user_path = str(user_path or "").strip()

    # 空路径 → 默认当前目录
    if not user_path:
        user_path = "."

    # 解析为绝对路径
    raw = Path(user_path)
    if not raw.is_absolute():
        candidate = (cwd / raw).resolve()
    else:
        candidate = raw.resolve()

    # 关键:检查是否在 cwd 内(防 .. 穿越和绝对路径逃逸)
    try:
        candidate.relative_to(cwd)
    except ValueError:
        raise PermissionError(
            f"路径 {user_path} 逃逸出项目目录 {cwd}。agent 只能在项目内操作文件。"
        )

    return candidate
