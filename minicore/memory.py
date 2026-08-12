"""迷你版记忆系统:把"项目知识"存到磁盘,启动时注入上下文。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MemoryEntry:
    """一条记忆:内容 + 时间戳。"""
    content: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEntry":
        return cls(content=data["content"], created_at=data["created_at"])


class MemoryStore:
    """存到磁盘的简单记忆库。"""
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[MemoryEntry] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [MemoryEntry.from_dict(e) for e in data]
        else:
            self.entries = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([e.to_dict() for e in self.entries], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, content: str) -> MemoryEntry:
        import time
        entry = MemoryEntry(content=content, created_at=time.time())
        self.entries.append(entry)
        self._save()
        return entry

    def all(self) -> list[MemoryEntry]:
        return list(self.entries)

    def delete(self, index: int) -> bool:
        """删除指定索引的记忆。返回是否成功。"""
        if 0 <= index < len(self.entries):
            self.entries.pop(index)
            self._save()
            return True
        return False

    def clear(self) -> None:
        """清空所有记忆。"""
        self.entries = []
        self._save()

    def render_for_prompt(self) -> str:
        """把记忆拼成一段文本,注入系统提示词。"""
        if not self.entries:
            return ""
        lines = ["## 你记得的项目知识"]
        for e in self.entries:
            lines.append(f"- {e.content}")
        return "\n".join(lines)