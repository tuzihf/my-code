"""网页端后端:FastAPI 壳,复用 minicore/ 的 agent 逻辑。

设计:
- 会话状态存在服务端内存(sessions dict)
- POST /chat 每轮跑一次 run_agent_turn(复用 main.py 的交互循环模式)
- SSE 流式推送 on_assistant_chunk 增量
- 权限:网页端默认放行(无终端 input 弹窗)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from minicore.tools import create_default_tools, close_mcp
from minicore.model import DeepSeekModel, MockModel, set_tools
from minicore.agent_loop import run_agent_turn
from minicore.session import (
    create_new_session, save_session, load_session, list_sessions,
)
from minicore.memory import MemoryStore
from minicore.permissions import PermissionManager
from minicore.settings import get_model_config, set_model_config
from minicore.dotenv import load_dotenv

# 加载项目根目录的 .env(如需,可配置 DEEPSEEK_API_KEY / MY_AGENT_MOCK 等)
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("my-agent")

# ---------- 全局状态 ----------

CWD = str(Path.cwd())
_TOOLS = create_default_tools()
set_tools(_TOOLS)

# 记忆库按 workspace 缓存:不同项目目录各自独立的记忆文件
_MEMORIES: dict[str, MemoryStore] = {}


def _get_memory(cwd: str) -> MemoryStore:
    """返回指定工作目录对应的记忆库(按需创建并缓存)。"""
    key = str(Path(cwd).resolve())
    if key not in _MEMORIES:
        _MEMORIES[key] = MemoryStore(Path(key) / ".my-agent-memory.json")
    return _MEMORIES[key]

# 权限:网页端默认放行敏感工具(无终端 input)
_PERMISSIONS = PermissionManager(CWD, prompt=lambda req: {"decision": "allow_once"})

# 会话内存表: session_id -> {"messages": [...], "stream": queue}
_SESSIONS: dict[str, dict[str, Any]] = {}


def _persist_session(state: dict[str, Any]) -> None:
    """把会话内存态保存到磁盘(session.py)。"""
    try:
        from minicore.session import SessionData
        # 从 state 构造 SessionData 并保存
        sd = SessionData(
            session_id=state["_sid"],
            created_at=state.get("_created_at", 0),
            workspace=state.get("cwd", CWD),
            messages=state.get("messages", []),
            checkpoints=list(state.get("checkpoints", [])),
            name=state.get("name", ""),
            history=state.get("history", []),
        )
        save_session(sd)
    except Exception as e:
        logger.warning("保存会话失败: %s", e)


def _load_all_sessions() -> None:
    """启动时从磁盘恢复所有会话。"""
    for sid in list_sessions():
        sd = load_session(sid)
        if sd is None:
            continue
        _SESSIONS[sid] = {
            "messages": list(sd.messages),
            "cwd": sd.workspace,
            "name": sd.name or f"会话 {sid[:8]}",
            "_sid": sid,
            "_created_at": sd.created_at,
            "checkpoints": list(sd.checkpoints),
            "history": list(sd.history),
            "stream": None,
        }


# 启动时恢复
_load_all_sessions()


def _get_model(config: dict | None = None):
    """根据配置创建模型。config 结构来自 settings.json 的 model 字段。"""
    from minicore.model import OpenAICompatModel, DeepSeekModel, MockModel

    if os.environ.get("MY_AGENT_MOCK"):
        return MockModel()
    config = config or get_model_config()
    mtype = config.get("type", "deepseek")
    model_name = config.get("model", "")
    try:
        if mtype == "mock":
            return MockModel()
        if mtype == "deepseek":
            return DeepSeekModel()
        # openai_compat:通用 OpenAI 协议(含 Ollama/LM Studio 本地)
        return OpenAICompatModel(
            model=model_name,
            api_key=config.get("api_key"),
            base_url=config.get("base_url"),
        )
    except Exception as e:
        logger.warning("模型初始化失败(%s),回退 Mock", e)
        return MockModel()


_MODEL = _get_model()

# 设置子代理模型(tools.delegate 用)
from minicore.tools import _SUB_AGENT_MODEL as _sub_model_ref
import minicore.tools as _tools_mod
_tools_mod._SUB_AGENT_MODEL = _MODEL


def _system_prompt(cwd: str = CWD) -> str:
    prompt = (
        "你是一个终端里的编程助手,用中文回答,必要时调用工具。\n\n"
        "工作方式要求:\n"
        "1. 动手前先想清楚要读哪些关键文件(如 README、入口文件、核心模块),避免反复列目录浪费时间。\n"
        "2. 一次只做最有价值的一步,不要重复调同一个工具。\n"
        "3. 信息够了就及时总结回答,不要无止境地探索。\n\n"
        "输出格式要求:\n"
        "1. 能用分点就分点,用 - 列表,不要一大段文字堆在一起。\n"
        "2. 有对比/多字段信息时,用 Markdown 表格(| 列1 | 列2 |)。\n"
        "3. 代码或命令用 Markdown 代码块(```)。\n"
        "4. 标题用 # ## 等。\n"
        "5. 保持简洁,不要啰嗦。"
    )
    mem_text = _get_memory(cwd).render_for_prompt()
    if mem_text:
        prompt += "\n\n" + mem_text
    return prompt


def _ensure_session(session_id: str | None, cwd: str | None = None) -> dict[str, Any]:
    """取或建会话。返回 {"messages": [...], "cwd": str, ...}。"""
    if session_id and session_id in _SESSIONS:
        return _SESSIONS[session_id]
    sid = session_id or create_new_session(cwd or CWD).session_id
    import time as _t
    state = {
        "messages": [{"role": "system", "content": _system_prompt(cwd or CWD)}],
        "cwd": cwd or CWD,
        "name": f"会话 {sid[:8]}",
        "_sid": sid,
        "_created_at": _t.time(),
        "checkpoints": [],
        "history": [],
        "stream": None,
    }
    _SESSIONS[sid] = state
    return state


# ---------- 请求模型 ----------

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class NewSessionRequest(BaseModel):
    pass


class WorkspaceRequest(BaseModel):
    session_id: str
    path: str


class ModelConfigRequest(BaseModel):
    type: str                # openai_compat / deepseek / mock
    model: str = ""          # 模型名,如 deepseek-chat / llama3
    base_url: str = ""       # OpenAI 兼容 base_url
    api_key: str = ""        # API key(可为空,本地模型不需要)


# ---------- API ----------

app = FastAPI(title="my-agent web")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "model": getattr(_MODEL, "model_id", "unknown")}


@app.get("/usage")
def usage():
    """返回累计 token 用量与成本估算。"""
    from minicore.model import get_usage
    u = get_usage()
    # 成本估算(默认 DeepSeek 大致单价:元/百万 token;input 1 元 / output 2 元)
    price_in = float(os.environ.get("MY_AGENT_PRICE_IN", "1.0"))
    price_out = float(os.environ.get("MY_AGENT_PRICE_OUT", "2.0"))
    cost = (u["prompt_tokens"] * price_in + u["completion_tokens"] * price_out) / 1_000_000
    return {**u, "cost": round(cost, 4)}


@app.post("/usage/reset")
def usage_reset():
    """清零累计用量。"""
    from minicore.model import reset_usage
    reset_usage()
    return {"ok": True}


@app.get("/settings/model")
def get_model_settings():
    """返回当前模型配置(不含 api_key 明文,只返回是否已设置)。"""
    config = get_model_config()
    display = dict(config)
    if display.get("api_key"):
        display["api_key_set"] = True
        display.pop("api_key", None)
    else:
        display["api_key_set"] = False
    return display


@app.post("/settings/model")
def set_model_settings(req: ModelConfigRequest):
    """设置模型配置并重建模型实例。"""
    global _MODEL
    config = {"type": req.type, "model": req.model, "base_url": req.base_url, "api_key": req.api_key}
    # 测试能否初始化(失败则不保存)
    try:
        new_model = _get_model(config)
    except Exception as e:
        return {"error": f"模型配置无效: {e}"}, 400
    set_model_config(config)
    _MODEL = new_model
    import minicore.tools as _tools_mod
    _tools_mod._SUB_AGENT_MODEL = _MODEL  # 同步子代理模型
    return {"ok": True, "model": getattr(_MODEL, "model_id", req.model)}


# ---------- 记忆管理 ----------

class MemoryRequest(BaseModel):
    content: str = ""
    index: int = -1
    session_id: str | None = None


def _memory_for(session_id: str | None) -> MemoryStore:
    """根据会话定位其 workspace 对应的记忆库;无会话则用默认 CWD。"""
    if session_id and session_id in _SESSIONS:
        return _get_memory(_SESSIONS[session_id].get("cwd", CWD))
    return _get_memory(CWD)


# 是否开启"编辑前确认"(diff approve):环境变量 MY_AGENT_CONFIRM_EDIT=1 开启
_CONFIRM_EDIT = bool(os.environ.get("MY_AGENT_CONFIRM_EDIT"))


def _last_assistant_text(messages: list[dict[str, Any]]) -> str:
    """从 messages 末尾取最后一条 assistant 文本回答(非工具调用)。"""
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content"):
            return str(m["content"])
    return ""


def _make_confirm_edit(state: dict[str, Any]) -> Any:
    """返回一个 confirm_edit 回调:阻塞等待前端 approve/reject(最多 10 分钟)。"""
    def confirm_edit(tool_name: str, diff_preview: str) -> bool:
        state["_pending_edit"] = {"tool": tool_name, "diff": diff_preview}
        state["_edit_decision"] = False
        state["_edit_event"] = threading.Event()
        state["_edit_event"].wait(timeout=600)
        state["_pending_edit"] = None
        return state.get("_edit_decision", False)
    return confirm_edit


@app.get("/memory")
def get_memory(session_id: str | None = None):
    """返回所有记忆。"""
    entries = []
    for i, e in enumerate(_memory_for(session_id).all()):
        import time as _t
        entries.append({"index": i, "content": e.content, "created_at": e.created_at})
    return {"memories": entries}


@app.post("/memory")
def add_memory(req: MemoryRequest):
    """添加一条记忆。"""
    content = req.content.strip()
    if not content:
        return {"error": "内容不能为空"}, 400
    _memory_for(req.session_id).add(content)
    return {"ok": True}


@app.delete("/memory")
def delete_memory(req: MemoryRequest):
    """删除指定索引的记忆。"""
    if _memory_for(req.session_id).delete(req.index):
        return {"ok": True}
    return {"error": "记忆不存在"}, 404


@app.post("/sessions")
def new_session():
    sid = create_new_session(CWD).session_id
    import time as _t
    _SESSIONS[sid] = {
        "messages": [{"role": "system", "content": _system_prompt(CWD)}],
        "cwd": CWD,
        "name": f"会话 {sid[:8]}",
        "_sid": sid,
        "_created_at": _t.time(),
        "checkpoints": [],
        "history": [],
        "stream": None,
    }
    _persist_session(_SESSIONS[sid])
    return {"session_id": sid}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    """删除会话(内存 + 磁盘)。"""
    if session_id not in _SESSIONS:
        return {"error": "session not found"}, 404
    del _SESSIONS[session_id]
    # 删除磁盘文件
    try:
        from minicore.session import sessions_dir
        p = sessions_dir() / f"{session_id}.json"
        if p.exists():
            p.unlink()
    except Exception:
        pass
    return {"ok": True}


class RenameRequest(BaseModel):
    name: str


@app.patch("/sessions/{session_id}")
def rename_session(session_id: str, req: RenameRequest):
    """重命名会话。"""
    state = _SESSIONS.get(session_id)
    if state is None:
        return {"error": "session not found"}, 404
    state["name"] = req.name.strip() or state.get("name", session_id[:8])
    _persist_session(state)
    return {"ok": True, "name": state["name"]}


@app.post("/sessions/{session_id}/rewind")
def rewind_session(session_id: str, group_id: str = ""):
    """回退文件编辑:默认回退最近一次;传 group_id 则整体回退一组(turn 事务)。"""
    state = _SESSIONS.get(session_id)
    if state is None:
        return {"error": "session not found"}, 404
    from minicore.session import SessionData, rewind_session_data, rewind_group
    sd = SessionData(
        session_id=session_id,
        created_at=state.get("_created_at", 0),
        workspace=state.get("cwd", CWD),
        messages=state.get("messages", []),
        checkpoints=list(state.get("checkpoints", [])),
    )
    if group_id:
        restored = rewind_group(sd, group_id=group_id)
    else:
        restored = rewind_session_data(sd, steps=1)
    if not restored:
        return {"ok": False, "message": "没有可回退的 checkpoint"}
    state["checkpoints"] = list(sd.checkpoints)
    _persist_session(state)
    return {"ok": True, "restored": [c.file_path for c in restored]}


@app.post("/sessions/{session_id}/stop")
def stop_session(session_id: str):
    """请求停止该会话当前正在生成的 agent(协作式取消)。"""
    state = _SESSIONS.get(session_id)
    if state is None:
        return {"error": "session not found"}, 404
    state["_stop_requested"] = True
    state["done"] = True
    return {"ok": True}


@app.get("/sessions/{session_id}/pending-edit")
def pending_edit(session_id: str):
    """查询当前待确认的文件编辑 diff(有则返回,无则 pending=None)。"""
    state = _SESSIONS.get(session_id)
    if state is None:
        return {"error": "session not found"}, 404
    return {"pending": state.get("_pending_edit")}


@app.post("/sessions/{session_id}/edit-decision")
def edit_decision(session_id: str, approve: bool = False):
    """对当前待确认的文件编辑做出决定(approve=true 应用,false 拒绝)。"""
    state = _SESSIONS.get(session_id)
    if state is None:
        return {"error": "session not found"}, 404
    state["_edit_decision"] = bool(approve)
    evt = state.get("_edit_event")
    if evt is not None:
        evt.set()
    return {"ok": True}


@app.get("/sessions/{session_id}/workspace")
def session_workspace(session_id: str):
    """查询会话当前的工作目录。"""
    state = _SESSIONS.get(session_id)
    if state is None:
        return {"error": "session not found"}, 404
    return {"session_id": session_id, "cwd": state.get("cwd", CWD)}


@app.get("/browse")
def browse(path: str = ""):
    """列出指定目录下的子目录(用于磁盘浏览器)。

    路径为空时返回盘符列表(Windows),或根目录(Unix)。
    只返回子目录,过滤隐藏/缓存目录。
    """
    import os

    # 空路径 → 盘符/根目录
    if not path:
        drives = []
        # Windows 盘符
        for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if os.path.exists(f"{d}:\\"):
                drives.append(f"{d}:")
        if drives:
            return {"path": "", "parent": None, "dirs": drives, "is_root": True}
        return {"path": "/", "parent": None, "dirs": [], "is_root": True}

    p = Path(path).expanduser()
    if not p.is_dir():
        return {"error": f"目录不存在: {path}"}, 400
    try:
        subdirs = sorted(
            d.name for d in p.iterdir()
            if d.is_dir() and not d.name.startswith((".", "node_modules", "__pycache__", "venv"))
        )
        files = sorted(
            d.name for d in p.iterdir()
            if d.is_file() and not d.name.startswith(".")
        )
    except PermissionError:
        return {"error": "无权限访问该目录"}, 403
    # parent:盘符本身 → 上级是盘符根(空);否则是上一级目录
    parent = None
    drive_letter = str(p)[:2]
    is_drive_root = (len(drive_letter) == 2 and drive_letter[1] == ":") and len(str(p)) == 2
    if is_drive_root:
        parent = ""
    elif p.parent != p:
        parent = str(p.parent)
    return {"path": str(p), "parent": parent, "dirs": subdirs, "files": files, "is_root": False}


@app.get("/sessions")
def list_session():
    items = []
    for sid, state in _SESSIONS.items():
        # 只列出有用户输入内容的会话(排除仅 system 的空会话)
        has_user_msg = any(
            m.get("role") == "user" and m.get("content")
            for m in state.get("messages", [])
        )
        if not has_user_msg:
            continue
        items.append({"id": sid, "name": state.get("name", sid[:8])})
    return {"sessions": items}


@app.get("/sessions/{session_id}/history")
def session_history(session_id: str):
    state = _SESSIONS.get(session_id)
    if state is None:
        return {"error": "session not found"}, 404
    # 优先返回完整可读历史(不压缩);旧会话无 history 时退回 messages
    history = state.get("history") or []
    return {"messages": history if history else state["messages"]}


@app.post("/workspace/pick")
def workspace_pick(req: WorkspaceRequest):
    """用系统原生对话框(此电脑)选择项目目录,返回真实路径。

    通过 tkinter 弹出 Windows 原生文件夹选择器。
    """
    state = _SESSIONS.get(req.session_id)
    if state is None:
        return {"error": "session not found"}, 404
    try:
        import tkinter
        from tkinter import filedialog

        # Windows 高 DPI 屏幕:让 tkinter 感知系统缩放,避免对话框模糊
        if os.name == "nt":
            try:
                import ctypes
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 系统 DPI 感知
                except Exception:
                    ctypes.windll.user32.SetProcessDPIAware()  # 旧版 API
            except Exception:
                pass

        root = tkinter.Tk()
        root.withdraw()  # 隐藏主窗口,只显示选择器
        root.attributes('-topmost', True)  # 置顶
        path = filedialog.askdirectory(title="选择项目目录")
        root.destroy()
    except Exception as e:
        return {"error": f"无法打开系统对话框: {e}"}, 500

    if not path:
        return {"ok": False, "cancelled": True}

    state["cwd"] = str(Path(path).resolve())
    state["messages"].append({
        "role": "user",
        "content": f"[系统] 工作目录已切换到: {state['cwd']}",
        "system_injected": True,
    })
    _persist_session(state)
    return {"ok": True, "cwd": state["cwd"]}


@app.post("/workspace/open")
def workspace_open(req: WorkspaceRequest):
    """把会话的工作目录切换到指定项目路径。"""
    state = _SESSIONS.get(req.session_id)
    if state is None:
        return {"error": "session not found"}, 404
    path = Path(req.path).expanduser()
    if not path.is_dir():
        return {"error": f"目录不存在: {req.path}"}, 400
    state["cwd"] = str(path.resolve())
    # 保留对话,但加一条切换提示,让会话不空、上下文不串
    state["messages"].append({
        "role": "user",
        "content": f"[系统] 工作目录已切换到: {state['cwd']}",
        "system_injected": True,
    })
    _persist_session(state)
    return {"session_id": req.session_id, "cwd": state["cwd"]}


@app.post("/chat")
def chat(req: ChatRequest):
    """接收用户消息,后台跑一轮 agent,返回 session_id。

    流式增量通过 GET /sessions/{id}/stream 获取。
    同一会话的请求通过每会话锁串行执行,避免并发竞争。
    """
    state = _ensure_session(req.session_id)
    sid = next((k for k, v in _SESSIONS.items() if v is state), None) or "new"

    # 每会话一把锁:同一会话的"追加消息 → 跑 agent → 写回"原子串行
    lock = state.setdefault("lock", threading.Lock())

    # 在启动后台线程前,先分配流版本号并初始化事件队列(轻量,加锁保护)。
    # 这样前端连接 /stream 时能读到正确的 _stream_id,不会因时序竞争而立即断开。
    with lock:
        stream_id = state.get("_stream_id", 0) + 1
        state["_stream_id"] = stream_id
        state["events"] = []
        state["chunks"] = []
        state["tool_calls"] = []
        state["tool_diffs"] = []
        state["done"] = False
        state["_stop_requested"] = False

    # 后台线程跑 agent,增量实时写回 state["events"] 供 SSE 流式读取
    def _run():
        def emit(evt: dict) -> None:
            state["events"].append(evt)   # 实时写回,避免 agent 跑完才一次性可见

        def on_chunk(text: str) -> None:
            emit({"type": "delta", "delta": text})

        def on_revoke() -> None:
            emit({"type": "revoke"})

        def on_tool_call(tool_name: str, tool_input: dict, result=None) -> None:
            summary = tool_name
            if "path" in tool_input:
                summary += f" {tool_input['path']}"
            elif "command" in tool_input:
                summary += f" {str(tool_input['command'])[:40]}"
            state.setdefault("tool_calls", []).append(summary)
            emit({"type": "tool_call", "tool": summary})
            # 修改类工具:收集 diff 供前端展示
            if tool_name in ("edit_file", "write_file") and result is not None and result.ok:
                output = result.output
                if "修改详情" in output or "---" in output:
                    emit({"type": "tool_diff", "tool": summary, "diff": output})
                    state.setdefault("tool_diffs", []).append({
                        "tool": summary,
                        "diff": output,
                    })

        with lock:
            session_cwd = state.get("cwd", CWD)
            # 追加用户消息(同时归档到用户可见历史,不参与上下文压缩)
            state["messages"].append({"role": "user", "content": req.message})
            state.setdefault("history", []).append({"role": "user", "content": req.message})

            try:
                from minicore.session import SessionData
                # 网页端也构造 SessionData,使 write_file/edit_file 能创建 checkpoint(支持回退)
                sd = SessionData(
                    session_id=state["_sid"],
                    created_at=state.get("_created_at", 0),
                    workspace=session_cwd,
                    messages=state["messages"],
                    checkpoints=list(state.get("checkpoints", [])),
                )
                messages = run_agent_turn(
                    model=_MODEL,
                    tools=_TOOLS,
                    messages=state["messages"],
                    cwd=session_cwd,
                    max_steps=30,
                    session=sd,
                    memory=_get_memory(session_cwd),
                    permissions=_PERMISSIONS,
                    on_assistant_chunk=on_chunk,
                    on_tool_call=on_tool_call,
                    should_stop=lambda: state.get("_stop_requested", False),
                    confirm_edit=_make_confirm_edit(state) if _CONFIRM_EDIT else None,
                    on_revoke=on_revoke,
                )
                # 只在还是当前 stream 时写回(防止旧线程覆盖新线程数据)
                if state.get("_stream_id") == stream_id:
                    state["messages"] = messages
                    state["checkpoints"] = list(sd.checkpoints)
                    # 归档最终回答到历史(完整保留,不受上下文压缩影响)
                    ans = _last_assistant_text(messages)
                    if ans:
                        state.setdefault("history", []).append({"role": "assistant", "content": ans})
                    state["done"] = True
                    logger.info("agent 完成, %d 条消息", len(messages))
                    _persist_session(state)  # 持久化到磁盘
            except Exception as e:
                import traceback
                traceback.print_exc()
                emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
                state["done"] = True
                state["error"] = str(e)
                _persist_session(state)  # 异常也保存

    threading.Thread(target=_run, daemon=True).start()
    return {"session_id": sid}


@app.get("/sessions/{session_id}/stream")
async def session_stream(session_id: str):
    """SSE:推送流式增量。生成器,每 0.1s 检查新 chunk。"""
    state = _SESSIONS.get(session_id)
    if state is None:
        return {"error": "session not found"}, 404

    async def gen():
        seen = 0
        # 绑定当前流版本,只推本请求的增量
        my_stream = state.get("_stream_id", 0)
        while True:
            # 如果新请求开始了,旧流立即结束,不推旧数据
            if state.get("_stream_id", 0) != my_stream:
                break
            # 按真实执行顺序推事件(边输出边修改,delta 以模型生成速度自然流出)
            events = state.get("events", [])
            while seen < len(events):
                evt = events[seen]
                if evt["type"] == "delta":
                    yield f"data: {json.dumps({'delta': evt['delta']})}\n\n"
                    await asyncio.sleep(0.015)   # 打字机节奏:避免多个 delta 一次性刷出
                elif evt["type"] == "revoke":
                    yield f"data: {json.dumps({'type': 'revoke'})}\n\n"
                elif evt["type"] == "error":
                    yield f"data: {json.dumps({'type': 'error', 'message': evt['message']})}\n\n"
                elif evt["type"] == "tool_call":
                    yield f"data: {json.dumps({'type': 'tool_call', 'tool': evt['tool']})}\n\n"
                elif evt["type"] == "tool_diff":
                    yield f"data: {json.dumps({'type': 'tool_diff', 'tool': evt['tool'], 'diff': evt['diff']})}\n\n"
                seen += 1
            if state.get("done"):
                yield f"data: {json.dumps({'delta': '', 'done': True})}\n\n"
                break
            await asyncio.sleep(0.02)   # 20ms 轮询,接近实时

    return StreamingResponse(gen(), media_type="text/event-stream")


# 静态前端
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
