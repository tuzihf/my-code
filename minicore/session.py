"""迷你版会话持久化 + checkpoint/rewind。

对应原版 minicode/session.py 的核心概念:
- SessionData  : 一个会话的全部状态(对话 + checkpoint)
- FileCheckpoint: 文件被改前的快照
- save/load    : 存到磁盘 / 从磁盘恢复
- rewind       : 用快照还原文件
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FileCheckpoint:
    """一次文件编辑前的"照片"。kind="edit" 是编辑记录,kind="rewind" 是回退产生的反向记录。"""
    checkpoint_id: str
    created_at: float
    file_path: str
    existed: bool
    previous_content: str
    kind: str = "edit"


@dataclass
class SessionData:
    """一个会话:对话消息 + 检查点列表。"""
    session_id: str
    created_at: float
    workspace: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[FileCheckpoint] = field(default_factory=list)

    def add_message(self, msg: dict[str, Any]) -> None:
        self.messages.append(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "workspace": self.workspace,
            "messages": self.messages,
            "checkpoints": [c.__dict__ for c in self.checkpoints],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionData":
        s = cls(
            session_id=data["session_id"],
            created_at=data["created_at"],
            workspace=data["workspace"],
            messages=data.get("messages", []),
        )
        s.checkpoints = [
            FileCheckpoint(**c) for c in data.get("checkpoints", [])
        ]
        return s


# ---------- 存盘 / 读取 ----------

def sessions_dir() -> Path:
    d = Path.home() / ".my-agent" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(session: SessionData) -> None:
    path = sessions_dir() / f"{session.session_id}.json"
    path.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(session_id: str) -> SessionData | None:
    path = sessions_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    return SessionData.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_sessions() -> list[str]:
    return [p.stem for p in sessions_dir().glob("*.json")]


def create_new_session(workspace: str) -> SessionData:
    return SessionData(
        session_id=uuid.uuid4().hex[:12],
        created_at=time.time(),
        workspace=workspace,
    )


def _is_readable_conversation_message(m: dict) -> bool:
    """判断这条消息是不是"可读的对话"(真实用户提问或助手回答)。

    跳过:system 角色、系统注入(system_injected 字段或 [系统] 前缀)、
    压缩摘要、工具调用/结果。
    """
    if m.get("system_injected"):
        return False
    role = m.get("role")
    if role == "system":
        return False
    if role == "tool":
        return False
    content = m.get("content")
    if not content:
        return False
    text = str(content)
    if text.startswith("[系统]"):
        return False
    if text.startswith("前情摘要:") or "前情摘要:" in text[:30]:
        return False
    return True


def readable_conversation_count(messages: list[dict]) -> int:
    """统计一个会话里有几条"可读对话消息"。"""
    return sum(1 for m in messages if _is_readable_conversation_message(m))


def format_session_list() -> str:
    """把磁盘上的会话列表格式化成可读文字。"""
    ids = list_sessions()
    if not ids:
        return "(没有已保存的会话)"
    # 按创建时间倒序排(最新的在前)
    sessions = []
    for sid in ids:
        s = load_session(sid)
        if s is None:
            continue
        sessions.append(s)
    sessions.sort(key=lambda s: s.created_at, reverse=True)

    lines = [f"共 {len(sessions)} 个会话:"]
    for s in sessions:
        total = len(s.messages)
        readable = readable_conversation_count(s.messages)
        cp_count = len([c for c in s.checkpoints if c.kind == "edit"])
        # 找第一条可读的对话作为摘要
        first_msg = ""
        for m in s.messages:
            if _is_readable_conversation_message(m):
                first_msg = str(m.get("content", ""))[:40]
                break
        tag = "💬" if readable >= 2 else ("📄" if total > 0 else "·")
        lines.append(
            f"  {tag} {s.session_id[:12]}  | 对话:{readable}/{total}  checkpoint:{cp_count}  | {first_msg}"
        )
    return "\n".join(lines)


# ---------- checkpoint + rewind ----------

def create_file_checkpoint(session: SessionData, *, file_path: str, existed: bool, previous_content: str) -> FileCheckpoint:
    """在改文件前,先给"旧内容"拍照存档。"""
    cp = FileCheckpoint(
        checkpoint_id=uuid.uuid4().hex[:12],
        created_at=time.time(),
        file_path=file_path,
        existed=existed,
        previous_content=previous_content,
    )
    session.checkpoints.append(cp)
    return cp


def rewind_session_data(session: SessionData, *, steps: int = 1) -> list[FileCheckpoint]:
    """回退:用 checkpoint 里的旧内容还原文件,并保留"回退"记录以便再撤销。"""
    # 只回退"编辑"记录;回退产生的反向记录(kind="rewind")不应被再次回退
    edit_checkpoints = [c for c in session.checkpoints if c.kind == "edit"]
    if not edit_checkpoints or steps <= 0:
        return []
    selected = edit_checkpoints[-steps:]   # 取最后 steps 个编辑记录
    selected_ids = {c.checkpoint_id for c in selected}
    restored = []

    # 先对每个要还原的文件,记录"当前内容"作为反向 checkpoint(kind="rewind")
    for cp in selected:
        target = Path(cp.file_path)
        existed_now = target.exists()
        content_now = target.read_text(encoding="utf-8") if existed_now else ""
        session.checkpoints.append(FileCheckpoint(
            checkpoint_id=uuid.uuid4().hex[:12],
            created_at=time.time(),
            file_path=cp.file_path,
            existed=existed_now,
            previous_content=content_now,
            kind="rewind",
        ))

    # 真正还原:把文件写回旧内容,或删除
    for cp in selected:
        target = Path(cp.file_path)
        if cp.existed:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(cp.previous_content, encoding="utf-8")
        elif target.exists():
            target.unlink()
        restored.append(cp)

    # 从 checkpoint 列表里移除刚被回退的编辑记录
    session.checkpoints = [c for c in session.checkpoints if c.checkpoint_id not in selected_ids]
    return restored


def format_rewind_preview(session: SessionData, *, steps: int = 1) -> str:
    """展示"回退会还原什么"(不真的改文件)。"""
    if not session.checkpoints:
        return "No checkpoints available."
    selected = session.checkpoints[-steps:]
    lines = [f"将回退 {len(selected)} 个 checkpoint:"]
    for cp in selected:
        lines.append(f"  - [{cp.checkpoint_id[:8]}] {cp.file_path}  existed={cp.existed}")
    return "\n".join(lines)