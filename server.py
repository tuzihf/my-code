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

# ---------- 全局状态 ----------

CWD = str(Path.cwd())
_TOOLS = create_default_tools()
set_tools(_TOOLS)
_MEMORY = MemoryStore(Path(CWD) / ".my-agent-memory.json")

# 权限:网页端默认放行敏感工具(无终端 input)
_PERMISSIONS = PermissionManager(CWD, prompt=lambda req: {"decision": "allow_once"})

# 会话内存表: session_id -> {"messages": [...], "stream": queue}
_SESSIONS: dict[str, dict[str, Any]] = {}


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
        print(f"[模型] 初始化失败({e}),回退 Mock")
        return MockModel()


_MODEL = _get_model()


def _system_prompt() -> str:
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
    mem_text = _MEMORY.render_for_prompt()
    if mem_text:
        prompt += "\n\n" + mem_text
    return prompt


def _ensure_session(session_id: str | None, cwd: str | None = None) -> dict[str, Any]:
    """取或建会话。返回 {"messages": [...], "cwd": str, ...}。"""
    if session_id and session_id in _SESSIONS:
        return _SESSIONS[session_id]
    sid = session_id or create_new_session(cwd or CWD).session_id
    state = {
        "messages": [{"role": "system", "content": _system_prompt()}],
        "cwd": cwd or CWD,
        "name": f"会话 {sid[:8]}",
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
    return {"ok": True, "model": getattr(_MODEL, "model_id", req.model)}


@app.post("/sessions")
def new_session():
    sid = create_new_session(CWD).session_id
    _SESSIONS[sid] = {
        "messages": [{"role": "system", "content": _system_prompt()}],
        "cwd": CWD,
        "name": f"会话 {sid[:8]}",
        "stream": None,
    }
    return {"session_id": sid}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    """删除会话。"""
    if session_id not in _SESSIONS:
        return {"error": "session not found"}, 404
    del _SESSIONS[session_id]
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
    return {"ok": True, "name": state["name"]}


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
    return {"path": str(p), "parent": parent, "dirs": subdirs, "is_root": False}


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
    return {"messages": state["messages"]}


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
    })
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
    })
    return {"session_id": req.session_id, "cwd": state["cwd"]}


@app.post("/chat")
def chat(req: ChatRequest):
    """接收用户消息,后台跑一轮 agent,返回 session_id。

    流式增量通过 GET /sessions/{id}/stream 获取。
    """
    state = _ensure_session(req.session_id)
    sid = next((k for k, v in _SESSIONS.items() if v is state), None) or "new"
    session_cwd = state.get("cwd", CWD)

    # 追加用户消息
    state["messages"].append({"role": "user", "content": req.message})

    # 给这次 chat 分配流版本号,隔离并发线程的旧数据
    stream_id = state.get("_stream_id", 0) + 1
    state["_stream_id"] = stream_id
    state["chunks"] = []
    state["tool_calls"] = []
    state["done"] = False

    # 后台线程跑 agent,结果写回 state(chunks 列表由 GIL 保证线程安全)
    def _run():
        chunks: list[str] = []

        def on_chunk(text: str) -> None:
            chunks.append(text)

        def on_tool_call(tool_name: str, tool_input: dict) -> None:
            # 实时追加到 state,SSE 端点能立即读到
            summary = tool_name
            if "path" in tool_input:
                summary += f" {tool_input['path']}"
            elif "command" in tool_input:
                summary += f" {str(tool_input['command'])[:40]}"
            state.setdefault("tool_calls", []).append(summary)

        try:
            messages = run_agent_turn(
                model=_MODEL,
                tools=_TOOLS,
                messages=state["messages"],
                cwd=session_cwd,
                max_steps=30,
                memory=_MEMORY,
                permissions=_PERMISSIONS,
                on_assistant_chunk=on_chunk,
                on_tool_call=on_tool_call,
            )
            # 只在还是当前 stream 时写回(防止旧线程覆盖新线程数据)
            if state.get("_stream_id") == stream_id:
                state["messages"] = messages
                state["chunks"] = chunks
                state["done"] = True
                print(f"[agent] 完成, {len(messages)} 条消息")
        except Exception as e:
            import traceback
            traceback.print_exc()
            state["chunks"] = chunks
            state["done"] = True
            state["error"] = str(e)

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
        seen_tools = 0
        # 绑定当前流版本,只推本请求的增量
        my_stream = state.get("_stream_id", 0)
        while True:
            # 如果新请求开始了,旧流立即结束,不推旧数据
            if state.get("_stream_id", 0) != my_stream:
                break
            # 先推工具调用(语义上:模型先调工具,再给文本)
            tool_calls = state.get("tool_calls", [])
            while seen_tools < len(tool_calls):
                yield f"data: {json.dumps({'type': 'tool_call', 'tool': tool_calls[seen_tools]})}\n\n"
                seen_tools += 1
            # 再推文本增量(放慢流速,每次推一个 chunk 后停顿)
            chunks = state.get("chunks", [])
            while seen < len(chunks):
                yield f"data: {json.dumps({'delta': chunks[seen]})}\n\n"
                seen += 1
                await asyncio.sleep(0.02)  # 每字符间隔,制造"打字机"节奏
            if state.get("done"):
                yield f"data: {json.dumps({'delta': '', 'done': True})}\n\n"
                break
            await asyncio.sleep(0.1)

    return StreamingResponse(gen(), media_type="text/event-stream")


# 静态前端
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
