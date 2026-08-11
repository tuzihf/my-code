"""迷你版 read_dedup:避免重复读取同一个文件,省上下文空间。

对应原版 minicode/context_compactor.py 里的 ReadDedup 机制:
- 第一次 read_file:记录文件路径 + 内容哈希
- 第二次读同一个文件:如果内容没变,返回"你之前读过"占位,不返回全文
"""
from __future__ import annotations

import hashlib


class ReadDedup:
    """记录读过的文件,重复读就返回占位。"""

    def __init__(self) -> None:
        # path -> (content_hash, stub)
        self._index: dict[str, tuple[str, str]] = {}

    def should_dedup(self, file_path: str, content: str) -> bool:
        """判断这个文件是否应该被去重(之前读过且内容没变)。"""
        current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        record = self._index.get(file_path)
        if record is None:
            return False
        old_hash, _ = record
        return old_hash == current_hash

    def get_stub(self, file_path: str) -> str:
        """返回"你之前读过"的占位文本。"""
        return f"[read_dedup] {file_path} 的内容你之前读过,没有变化,不再重复展示。"

    def register_read(self, file_path: str, content: str) -> None:
        """第一次读:记录路径和内容哈希。"""
        current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._index[file_path] = (current_hash, "")

    def clear(self) -> None:
        self._index.clear()