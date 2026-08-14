"""迷你版 read_dedup:避免重复读取同一个文件片段,省上下文空间。

对应原版 minicode/context_compactor.py 里的 ReadDedup 机制:
- 第一次 read_file:记录 (文件路径, offset, limit) + 内容哈希
- 第二次读同一文件同一片段:如果内容没变,返回"你之前读过"占位
"""
from __future__ import annotations

import hashlib


class ReadDedup:
    """记录读过的文件片段,重复读就返回占位。"""

    def __init__(self) -> None:
        # (path, offset, limit) -> content_hash
        self._index: dict[tuple[str, int, int], str] = {}

    @staticmethod
    def _key(file_path: str, offset: int, limit: int) -> tuple[str, int, int]:
        return (file_path, offset, limit)

    def should_dedup(self, file_path: str, content: str, offset: int = 1, limit: int = 2000) -> bool:
        """判断这个文件片段是否应该被去重(之前读过且内容没变)。"""
        current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return self._index.get(self._key(file_path, offset, limit)) == current_hash

    def get_stub(self, file_path: str, offset: int = 1, limit: int = 2000) -> str:
        """返回"你之前读过"的占位文本。"""
        return (f"[read_dedup] {file_path} 的第 {offset}-{offset + limit - 1} 行"
                f"你之前读过,没有变化,不再重复展示。")

    def register_read(self, file_path: str, content: str, offset: int = 1, limit: int = 2000) -> None:
        """第一次读:记录片段和内容哈希。"""
        current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._index[self._key(file_path, offset, limit)] = current_hash

    def clear(self) -> None:
        self._index.clear()
