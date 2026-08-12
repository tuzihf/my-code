"""迷你版工具注册表:3 个工具 + 一个注册表。"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable
from pathlib import Path

from minicore.path_safety import resolve_tool_path


@dataclass
class ToolResult:
    ok: bool
    output: str
    awaitUser: bool = False


@dataclass
class ToolContext:
    cwd: str
    permissions: Any | None = None
    session: Any | None = None
    memory: Any | None = None


Runner = Callable[[dict[str, Any], ToolContext], ToolResult]


@dataclass
class ToolDefinition:
    name: str
    description: str
    run: Runner
    # 可选的输入校验函数:返回 (ok, error_msg)。None 表示不做额外校验。
    validate: Callable[[dict[str, Any]], tuple[bool, str]] | None = None


# ---------- 工具实现 ----------

def _list_files(input_data: dict, context: ToolContext) -> ToolResult:
    try:
        safe = resolve_tool_path(context, input_data.get("path", "."), mode="list")
    except PermissionError as e:
        return ToolResult(ok=False, output=str(e))
    except RuntimeError as e:
        return ToolResult(ok=False, output=str(e))
    try:
        entries = sorted(safe.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except FileNotFoundError:
        return ToolResult(ok=False, output=f"Path not found: {safe}")
    lines = [f"{'📁' if e.is_dir() else '📄'} {e.name}" for e in entries]
    return ToolResult(ok=True, output="\n".join(lines) or "(empty directory)")


def _read_file(input_data: dict, context: ToolContext) -> ToolResult:
    try:
        safe = resolve_tool_path(context, input_data.get("path", "."), mode="read")
    except PermissionError as e:
        return ToolResult(ok=False, output=str(e))
    except RuntimeError as e:
        return ToolResult(ok=False, output=str(e))
    if not safe.exists():
        return ToolResult(ok=False, output=f"Path not found: {safe}")
    if safe.is_dir():
        return ToolResult(ok=False, output=f"Is a directory: {safe}")
    try:
        text = safe.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return ToolResult(ok=False, output=f"Could not read: {e}")
    return ToolResult(ok=True, output=text)


def _command_has_path_escape(command: str, cwd: str) -> bool:
    """检查命令是否试图访问项目目录外的路径。"""
    import os
    cwd_abs = os.path.normcase(os.path.abspath(cwd))
    lowered = command.lower()
    # 绝对路径访问(如 C:\、/etc/、/usr/、/home/)
    abs_patterns = [
        r"^\s*cd\s+[a-z]:[\\/]",        # cd C:\
        r"^\s*cd\s+/",                  # cd /
        r"[a-z]:[\\/]windows",          # C:\windows
        r"/etc/", r"/usr/", r"/home/",  # 类 Unix 系统目录
    ]
    import re
    for pat in abs_patterns:
        if re.search(pat, lowered):
            return True
    return False


def _run_command(input_data: dict, context: ToolContext) -> ToolResult:
    command = input_data.get("command", "")
    timeout = float(input_data.get("timeout", 30))
    if not command.strip():
        return ToolResult(ok=False, output="No command given")
    # 路径沙箱:拒绝访问项目外路径的命令
    if _command_has_path_escape(command, context.cwd):
        return ToolResult(
            ok=False,
            output=f"[安全] 命令试图访问项目目录外: {command[:80]}. agent 只能在项目内执行命令。",
        )
    try:
        # Windows 下强制子进程用 UTF-8 输出,避免 GBK 编解码错乱
        import os as _os
        env = dict(_os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=context.cwd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(ok=False, output=f"Command timed out after {timeout}s")
    except Exception as e:
        return ToolResult(ok=False, output=f"Failed to run: {e}")
    output = proc.stdout
    if proc.returncode != 0:
        output += f"\n[exit code {proc.returncode}]\n{proc.stderr}"
    return ToolResult(ok=True, output=output or "(no output)")


def _write_file(input_data: dict, context: ToolContext) -> ToolResult:
    """写文件:写之前先打 checkpoint(如果会话存在),这样能回退。"""
    path = str(input_data.get("path", ""))
    content = input_data.get("content", "")
    if not path.strip():
        return ToolResult(ok=False, output="No path given")
    try:
        safe = resolve_tool_path(context, path, mode="write")
    except PermissionError as e:
        return ToolResult(ok=False, output=str(e))
    except RuntimeError as e:
        return ToolResult(ok=False, output=str(e))

    # 写之前:记录旧内容(checkpoint + diff 用)
    old_content = ""
    if safe.exists():
        try:
            old_content = safe.read_text(encoding="utf-8")
        except Exception:
            old_content = ""
    if context.session is not None:
        try:
            from minicore.session import create_file_checkpoint
            create_file_checkpoint(
                context.session,
                file_path=str(safe),
                existed=bool(old_content) or safe.exists(),
                previous_content=old_content,
            )
        except ImportError:
            pass  # 没有 session 模块时静默跳过

    try:
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text(content, encoding="utf-8")
    except Exception as e:
        return ToolResult(ok=False, output=f"Write failed: {e}")
    # 生成 diff 展示修改详情
    try:
        from minicore.diff import format_diff_text
        diff_text = format_diff_text(old_content, content)
    except Exception:
        diff_text = ""
    msg = f"Wrote {safe} ({len(content)} chars)"
    if diff_text:
        msg += "\n\n" + diff_text
    return ToolResult(ok=True, output=msg)


def _remember(input_data: dict, context: ToolContext) -> ToolResult:
    """把一条知识存进记忆,跨会话保留。"""
    content = input_data.get("content", "")
    if not content.strip():
        return ToolResult(ok=False, output="No content given")
    if context.memory is None:
        return ToolResult(ok=False, output="Memory store not available in this context")
    entry = context.memory.add(content)
    return ToolResult(ok=True, output=f"已记住: {content}")


def _edit_file(input_data: dict, context: ToolContext) -> ToolResult:
    """精确编辑:在文件里找到 old_str,替换成 new_str。

    必须恰好出现 1 次才替换,避免误改。改之前打 checkpoint,支持回退。
    """
    path = str(input_data.get("path", ""))
    old_str = input_data.get("old_str", "")
    new_str = input_data.get("new_str", "")
    if not path or not old_str:
        return ToolResult(ok=False, output="path 和 old_str 必填")
    try:
        safe = resolve_tool_path(context, path, mode="write")
    except PermissionError as e:
        return ToolResult(ok=False, output=str(e))
    except RuntimeError as e:
        return ToolResult(ok=False, output=str(e))
    if not safe.exists():
        return ToolResult(ok=False, output=f"Path not found: {safe}")

    # 改之前打 checkpoint(和 write_file 一致,支持回退)
    if context.session is not None:
        try:
            from minicore.session import create_file_checkpoint
            create_file_checkpoint(context.session, file_path=str(safe),
                                   existed=True,
                                   previous_content=safe.read_text(encoding="utf-8"))
        except ImportError:
            pass

    try:
        content = safe.read_text(encoding="utf-8")
    except Exception as e:
        return ToolResult(ok=False, output=f"Could not read: {e}")

    # 精确替换:必须恰好出现 1 次
    count = content.count(old_str)
    if count == 0:
        return ToolResult(ok=False, output=f"old_str 没找到: {old_str!r}")
    if count > 1:
        return ToolResult(ok=False, output=f"old_str 出现 {count} 次,不唯一,拒绝模糊替换。请提供更长的上下文。")

    new_content = content.replace(old_str, new_str, 1)
    try:
        safe.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return ToolResult(ok=False, output=f"Write failed: {e}")
    # 生成 diff 展示修改详情
    try:
        from minicore.diff import format_diff_text
        diff_text = format_diff_text(content, new_content)
    except Exception:
        diff_text = ""
    msg = f"已替换 1 处: {old_str!r} → {new_str!r}"
    if diff_text:
        msg += "\n\n" + diff_text
    return ToolResult(ok=True, output=msg)


# 子代理模型(由 server 启动时设置)
_SUB_AGENT_MODEL = None


def _delegate(input_data: dict, context: ToolContext) -> ToolResult:
    """子代理:派一个只读子循环去执行子任务。

    子代理有自己的独立上下文(不含主对话历史),用只读工具集,
    跑完返回结果文字。适合"调研/分析"类子任务。
    """
    task = str(input_data.get("task", "")).strip()
    max_steps = int(input_data.get("max_steps", 10))
    if not task:
        return ToolResult(ok=False, output="task 必填")
    try:
        # 延迟 import,避免循环依赖(tools → agent_loop → tools)
        from minicore.agent_loop import run_agent_turn
        from minicore.model import set_tools
        from minicore.tools import create_default_tools

        # 子代理用全局模型(由 server 启动时设置)
        from minicore.tools import _SUB_AGENT_MODEL
        if _SUB_AGENT_MODEL is None:
            return ToolResult(ok=False, output="子代理模型未配置")

        # 子代理只读工具集
        sub_tools = create_default_tools()
        set_tools(sub_tools)

        # 独立上下文:子代理只看到自己的任务
        sub_messages = [
            {"role": "system", "content": f"你是一个子代理,负责完成以下子任务。使用只读工具调研,最后给出结论。"},
            {"role": "user", "content": task},
        ]
        result = run_agent_turn(
            model=_SUB_AGENT_MODEL,
            tools=sub_tools,
            messages=sub_messages,
            cwd=context.cwd,
            max_steps=max_steps,
        )
        # 取最后一条 assistant 内容作为结论
        for m in reversed(result):
            if m.get("role") == "assistant" and m.get("content"):
                return ToolResult(ok=True, output=f"[子代理结果]\n{m['content']}")
        return ToolResult(ok=False, output="子代理未产生结论")
    except Exception as e:
        return ToolResult(ok=False, output=f"子代理执行失败: {e}")


# ---------- 注册表 ----------

class ToolRegistry:
    def __init__(self, tools: list[ToolDefinition]) -> None:
        self._tools = tools

    def find(self, name: str) -> ToolDefinition | None:
        for t in self._tools:
            if t.name == name:
                return t
        return None

    def list_all(self) -> list[str]:
        return [t.name for t in self._tools]

    def execute(self, name: str, input_data: dict, context: ToolContext) -> ToolResult:
        tool = self.find(name)
        if tool is None:
            return ToolResult(ok=False, output=f"Unknown tool: {name}")
        try:
            # 第一层:input 必须是 dict(模型可能传错类型)
            if not isinstance(input_data, dict):
                return ToolResult(
                    ok=False,
                    output=f"Invalid input for {name}: expected a dict, got {type(input_data).__name__}",
                )
            # 第二层:工具的专属校验(必填字段、类型)
            if tool.validate is not None:
                ok, err = tool.validate(input_data)
                if not ok:
                    return ToolResult(ok=False, output=f"Validation error in {name}: {err}")
            return tool.run(input_data, context)
        except Exception as e:
            return ToolResult(ok=False, output=f"[{type(e).__name__}] Tool crashed: {e}")


# ---------- MCP 工具集成 ----------

def create_mcp_tools(server_name: str, command: list[str]) -> tuple[list[ToolDefinition], Any]:
    """连接一个 MCP 服务端,把它暴露的工具包装成 ToolDefinition。

    返回 (tools, client)。调用方负责在退出时 client.close()。
    连接失败时返回空列表(不阻塞 agent 启动)。
    """
    try:
        from minicore.mcp import StdioMcpClient
        client = StdioMcpClient(server_name, command)
        mcp_tools = client.list_tools()
    except Exception:
        return [], None

    def _make_run(mcp_tool):
        def _run(input_data: dict, context: ToolContext) -> ToolResult:
            try:
                output = mcp_tool.call(input_data)
                return ToolResult(ok=True, output=output)
            except Exception as e:
                return ToolResult(ok=False, output=f"MCP tool error: {e}")
        return _run

    defs = []
    for t in mcp_tools:
        defs.append(ToolDefinition(
            name=f"{server_name}__{t.name}",
            description=f"[MCP:{server_name}] {t.description}",
            run=_make_run(t),
        ))
    return defs, client


# ---------- 输入校验器 ----------

def _validate_path(input_data: dict) -> tuple[bool, str]:
    """通用校验:path 必须是字符串。"""
    path = input_data.get("path", ".")
    if not isinstance(path, str):
        return False, f"path 必须是字符串,得到 {type(path).__name__}"
    return True, ""


def _validate_command(input_data: dict) -> tuple[bool, str]:
    """run_command 校验:command 必须是字符串且非空。"""
    command = input_data.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return False, "command 必须是字符串且非空"
    return True, ""


def _validate_write(input_data: dict) -> tuple[bool, str]:
    """write_file 校验:path 和 content 必须存在且类型对。"""
    path = input_data.get("path")
    content = input_data.get("content")
    if not isinstance(path, str) or not path:
        return False, "path 必填且必须是字符串"
    if not isinstance(content, str):
        return False, f"content 必须是字符串,得到 {type(content).__name__}"
    return True, ""


def _validate_edit(input_data: dict) -> tuple[bool, str]:
    """edit_file 校验:path/old_str/new_str 必须存在。"""
    for field in ("path", "old_str", "new_str"):
        val = input_data.get(field)
        if not isinstance(val, str) or not val:
            return False, f"{field} 必填且必须是字符串"
    return True, ""


def _validate_remember(input_data: dict) -> tuple[bool, str]:
    """remember 校验:content 必须非空字符串。"""
    content = input_data.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return False, "content 必填且必须是字符串"
    return True, ""


def create_default_tools() -> ToolRegistry:
    core_tools = [
        ToolDefinition(
            name="list_files",
            description="List files in a directory. Input: {\"path\": \"<dir>\"}",
            run=_list_files,
            validate=_validate_path,
        ),
        ToolDefinition(
            name="read_file",
            description="Read a text file. Input: {\"path\": \"<file>\"}",
            run=_read_file,
            validate=_validate_path,
        ),
        ToolDefinition(
            name="run_command",
            description="Run a shell command. Input: {\"command\": \"<cmd>\", \"timeout\": 30}",
            run=_run_command,
            validate=_validate_command,
        ),
        ToolDefinition(
            name="write_file",
            description="Write content to a file (overwrites). Input: {\"path\": \"<file>\", \"content\": \"<text>\"}",
            run=_write_file,
            validate=_validate_write,
        ),
        ToolDefinition(
            name="remember",
            description="Remember a project fact or preference for future sessions. Input: {\"content\": \"<fact to remember>\"}",
            run=_remember,
            validate=_validate_remember,
        ),
        ToolDefinition(
            name="edit_file",
            description="Precisely replace a unique old_str with new_str in a file. Input: {\"path\": \"<file>\", \"old_str\": \"<exact text>\", \"new_str\": \"<replacement>\"}",
            run=_edit_file,
            validate=_validate_edit,
        ),
        ToolDefinition(
            name="delegate",
            description="Delegate a subtask to a read-only sub-agent. Use for research/analysis. Input: {\"task\": \"<subtask description>\", \"max_steps\": 10}",
            run=_delegate,
        ),
    ]

    # 加载 MCP 工具(连上假服务端,暴露 add 工具)。失败时静默跳过。
    mcp_tools, mcp_client = create_mcp_tools("fake-add", ["python", "minicore/fake_mcp_server.py"])
    # 存到模块级,供关闭时用
    global _MCP_CLIENT
    _MCP_CLIENT = mcp_client
    return ToolRegistry(core_tools + mcp_tools)


# MCP 客户端句柄(供外部关闭)
_MCP_CLIENT = None


def close_mcp() -> None:
    """关闭 MCP 客户端连接。"""
    global _MCP_CLIENT
    if _MCP_CLIENT is not None:
        try:
            _MCP_CLIENT.close()
        except Exception:
            pass
        _MCP_CLIENT = None