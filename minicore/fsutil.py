"""文件系统小工具:原子写入 + 损坏文件备份。

用于会话/记忆等持久化文件,避免写一半崩溃导致文件损坏,
以及加载损坏文件时直接抛异常导致启动失败。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """原子写入:先写同目录临时文件,再 os.replace 覆盖目标。

    同目录 + os.replace 保证写入过程要么完整要么不生效,不会留下半截文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_corrupt(path: Path) -> bool:
    """把损坏文件改名备份(加 .corrupt 后缀),返回是否成功。"""
    try:
        path.rename(path.with_suffix(path.suffix + ".corrupt"))
        return True
    except OSError:
        return False
