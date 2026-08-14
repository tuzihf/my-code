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
    group_id: str = ""   # 同一批编辑共享的组号,用于整体回退


Runner = Callable[[dict[str, Any], ToolContext], ToolResult]


@dataclass
class ToolDefinition:
    name: str
    description: str
    run: Runner
    # 可选的输入校验函数:返回 (ok, error_msg)。None 表示不做额外校验。
    validate: Callable[[dict[str, Any]], tuple[bool, str]] | None = None
    # 可选:给模型看的参数 JSON Schema(OpenAI function calling 的 parameters 字段)
    schema: dict[str, Any] | None = None


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
    """按行读文件,支持 offset/limit 分页,输出带行号。"""
    path = str(input_data.get("path", ".") or ".")
    offset = int(input_data.get("offset", 1) or 1)
    limit = int(input_data.get("limit", 2000) or 2000)

    try:
        safe = resolve_tool_path(context, path, mode="read")
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

    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return ToolResult(ok=True, output=f"{safe}  (空文件)")

    if offset < 1:
        offset = 1
    if offset > total:
        return ToolResult(ok=False, output=f"offset {offset} 超出文件总行数 {total}")
    end = min(offset + limit - 1, total)

    numbered = [f"{i:>6}  {line}" for i, line in enumerate(lines[offset - 1:end], start=offset)]
    body = "\n".join(numbered)
    if end < total:
        body += f"\n... 还有 {total - end} 行未显示,用 offset={end + 1} 继续读取"
    return ToolResult(ok=True, output=f"{safe}  (第 {offset}-{end} 行 / 共 {total} 行)\n{body}")


def _glob_files(input_data: dict, context: ToolContext) -> ToolResult:
    """按 glob 模式匹配项目内文件,返回相对路径列表。"""
    pattern = str(input_data.get("pattern", "") or "").strip()
    if not pattern:
        return ToolResult(ok=False, output="pattern 必填")

    raw = Path(pattern)
    if raw.is_absolute():
        return ToolResult(ok=False, output="pattern 必须是相对项目根的路径")

    base = Path(context.cwd).resolve()
    try:
        matches = sorted(base.glob(pattern))
    except Exception as e:
        return ToolResult(ok=False, output=f"glob 失败: {e}")

    _SKIP_DIRS = {".git", ".agent_cache", "__pycache__", ".pytest_cache",
                  "node_modules", "venv", ".venv", ".idea", "dist", "build"}
    out = []
    for m in matches:
        if not m.is_file():
            continue
        try:
            rel = m.resolve().relative_to(base)   # 防 .. / 绝对路径逃逸
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        out.append(str(rel))
        if len(out) >= 200:
            break
    return ToolResult(ok=True, output="\n".join(out) or "(无匹配)")


def _grep_files(input_data: dict, context: ToolContext) -> ToolResult:
    """在项目内按正则搜索文件内容,返回 "路径:行号: 内容" 列表。"""
    pattern = str(input_data.get("pattern", "") or "")
    if not pattern.strip():
        return ToolResult(ok=False, output="pattern 必填")
    try:
        safe = resolve_tool_path(context, input_data.get("path", "."), mode="list")
    except PermissionError as e:
        return ToolResult(ok=False, output=str(e))
    except RuntimeError as e:
        return ToolResult(ok=False, output=str(e))

    import re
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return ToolResult(ok=False, output=f"无效的正则表达式: {e}")

    if safe.is_file():
        targets = [safe]
    elif safe.is_dir():
        targets = safe.rglob("*")
    else:
        return ToolResult(ok=False, output=f"Path not found: {safe}")

    _SKIP_DIRS = {".git", ".agent_cache", "__pycache__", ".pytest_cache",
                  "node_modules", "venv", ".venv", ".idea", "dist", "build"}
    MAX_RESULTS = 200
    # 收集每个文件的匹配(文件路径 -> [(行号, 文本)])
    matches: dict[str, list[tuple[int, str]]] = {}
    for f in targets:
        if not f.is_file():
            continue
        if any(part in _SKIP_DIRS for part in f.parts):
            continue
        try:
            if f.stat().st_size > 1_000_000:   # 跳过 >1MB 的文件
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        hits = [(lineno, line.strip()[:200]) for lineno, line in enumerate(text.splitlines(), 1)
                if regex.search(line)]
        if hits:
            matches[str(f)] = hits

    if not matches:
        return ToolResult(ok=True, output="(无匹配)")

    # 相关度排序:命中行数越多的文件越靠前;文件内按行号升序
    results: list[str] = []
    for file_path, hits in sorted(matches.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        for lineno, text in hits:
            results.append(f"{file_path}:{lineno}: {text}")
            if len(results) >= MAX_RESULTS:
                results.append(f"... 已达 {MAX_RESULTS} 条上限,结果被截断 ...")
                return ToolResult(ok=True, output="\n".join(results))
    return ToolResult(ok=True, output="\n".join(results))


def _find_symbol(input_data: dict, context: ToolContext) -> ToolResult:
    """查找符号定义(函数/类/赋值/导入)。"""
    from minicore.code_index import build_index, _format_refs
    name = str(input_data.get("name", "") or "").strip()
    if not name:
        return ToolResult(ok=False, output="name 必填")
    index = build_index(context.cwd)
    refs = index.get(name, [])
    defs = [r for r in refs if r.kind in ("function", "class", "assign", "import")]
    return ToolResult(ok=True, output=_format_refs(defs or refs, name))


def _find_references(input_data: dict, context: ToolContext) -> ToolResult:
    """查找符号的所有引用(定义 + 使用 + 调用)。"""
    from minicore.code_index import build_index, _format_refs
    name = str(input_data.get("name", "") or "").strip()
    if not name:
        return ToolResult(ok=False, output="name 必填")
    index = build_index(context.cwd)
    return ToolResult(ok=True, output=_format_refs(index.get(name, []), name))


def _command_has_path_escape(command: str) -> bool:
    """检查命令是否试图访问项目目录外的路径。

    采用"可疑即拒绝"策略(误报优先于漏报),覆盖:
    Windows 盘符、Unix 系统根目录、`..` 穿越、cd 到根/上级、
    常见外联工具、命令替换向量(嵌套 shell)。
    """
    import re
    lowered = command.lower()
    patterns = [
        # Windows 盘符绝对路径(C:\ 或 C:/),但避开 http:// 这类 URL
        r"(^|[\s'\"&|;])[a-z]:[\\/]",
        # Unix 系统根目录直接访问
        r"(^|[\s'\"])/(etc|usr|home|var|tmp|root|opt|proc|sys|windows|system32)(/|[\s'\"])",
        # cd 到根目录或上级目录
        r"\bcd\s+[/\\]",
        r"\bcd\s+\.\.",
        # `..` 路径穿越
        r"\.\.",
        # Windows 环境变量逃逸
        r"%userprofile%|%homedrive%|%systemroot%|%appdata%|%temp%",
        # 外联工具(可能把项目数据外传)
        r"\b(curl|wget|nc|netcat|ncat|ssh|scp|ftp|telnet)\b",
        # 命令替换:嵌套 shell 可绕过路径检测执行任意命令
        r"\b(powershell|pwsh|cmd)\s+[-/][cC]\b",
    ]
    for pat in patterns:
        if re.search(pat, lowered):
            return True
    return False


# 白名单:shell=False 模式下允许直接执行的命令名(已去 .exe 后缀)
_COMMAND_WHITELIST = frozenset({
    "python", "python3", "py", "pytest", "pip", "pip3",
    "git", "node", "npm", "npx",
    "ls", "cat", "grep", "find", "echo", "pwd", "head", "tail", "wc",
    "sort", "uniq", "mkdir", "touch", "cp", "mv", "rm", "which", "where",
})

# shell=False 下无意义且危险的元字符(管道/重定向/命令链/变量展开/命令替换)
_SHELL_METACHARS = set("|&;<>$`")


def _run_command(input_data: dict, context: ToolContext) -> ToolResult:
    """执行命令:shell=False + 白名单,消除 shell 注入与命令逃逸风险。

    约束(逐层):
    1. 路径沙箱:拒绝访问项目外路径
    2. 拒绝 shell 元字符(不支持管道/重定向/命令链)
    3. shlex 分词
    4. 命令名必须在白名单内
    """
    import os as _os
    import shlex

    command = input_data.get("command", "")
    timeout = float(input_data.get("timeout", 30))
    if not command.strip():
        return ToolResult(ok=False, output="No command given")

    # 第一层:路径沙箱
    if _command_has_path_escape(command):
        return ToolResult(
            ok=False,
            output=f"[安全] 命令试图访问项目目录外: {command[:80]}. agent 只能在项目内执行命令。",
        )

    # 第二层:拒绝 shell 元字符
    metachars = sorted(set(command) & _SHELL_METACHARS)
    if metachars:
        return ToolResult(
            ok=False,
            output=f"[安全] 命令含 shell 元字符 {''.join(metachars)},已拒绝。"
                   f"shell=False 模式不支持管道/重定向/命令链。",
        )

    # 第三层:分词
    try:
        parts = shlex.split(command, posix=True)
    except ValueError as e:
        return ToolResult(ok=False, output=f"命令解析失败: {e}")
    if not parts:
        return ToolResult(ok=False, output="No command given")

    # 第四层:白名单
    cmd_name = _os.path.basename(parts[0]).lower()
    if cmd_name.endswith(".exe"):
        cmd_name = cmd_name[:-4]
    if cmd_name not in _COMMAND_WHITELIST:
        return ToolResult(
            ok=False,
            output=f"[安全] 命令 '{parts[0]}' 不在白名单内,已拒绝。"
                   f"允许的命令: {', '.join(sorted(_COMMAND_WHITELIST))}",
        )

    try:
        # Windows 下强制子进程用 UTF-8 输出,避免 GBK 编解码错乱
        env = dict(_os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            parts,
            shell=False,
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
    except FileNotFoundError:
        return ToolResult(ok=False, output=f"命令不存在: {parts[0]}")
    except Exception as e:
        return ToolResult(ok=False, output=f"Failed to run: {e}")
    output = proc.stdout
    if proc.returncode != 0:
        output += f"\n[exit code {proc.returncode}]\n{proc.stderr}"
    return ToolResult(ok=True, output=output or "(no output)")


def _verify(input_data: dict, context: ToolContext) -> ToolResult:
    """运行测试验证代码修改,默认跑 pytest。复用 run_command 的白名单收敛逻辑。"""
    command = str(input_data.get("command", "python -m pytest") or "").strip()
    timeout = float(input_data.get("timeout", 120) or 120)
    return _run_command({"command": command, "timeout": timeout}, context)


def _write_file(input_data: dict, context: ToolContext) -> ToolResult:
    """写文件:写之前先打 checkpoint(如果会话存在),这样能回退。

    dry_run=True 时只生成 diff 预览,不真正写文件、不打 checkpoint。
    """
    path = str(input_data.get("path", ""))
    content = input_data.get("content", "")
    dry_run = bool(input_data.get("dry_run", False))
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

    # dry_run:只预览 diff,不写文件、不打 checkpoint
    if dry_run:
        try:
            from minicore.diff import format_diff_text
            diff_text = format_diff_text(old_content, content)
        except Exception:
            diff_text = ""
        msg = f"[dry_run] 将写入 {safe} ({len(content)} chars)"
        if diff_text:
            msg += "\n\n" + diff_text
        return ToolResult(ok=True, output=msg)

    if context.session is not None:
        try:
            from minicore.session import create_file_checkpoint
            create_file_checkpoint(
                context.session,
                file_path=str(safe),
                existed=bool(old_content) or safe.exists(),
                previous_content=old_content,
                group_id=context.group_id,
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


def _replace_unique(content: str, old: str, new: str) -> tuple[str | None, int]:
    """精确替换:old 恰好出现 1 次才替换。返回 (新内容或 None, 出现次数)。"""
    n = content.count(old)
    if n == 1:
        return content.replace(old, new, 1), 1
    return None, n


def _replace_linewise(content: str, old: str, new: str) -> tuple[str | None, int]:
    """行级匹配:忽略每行尾随空白,old 必须恰好匹配一段连续行。"""
    content_lines = content.splitlines(keepends=True)
    stripped = [l.rstrip("\n").rstrip() for l in content_lines]
    old_lines = [l.rstrip() for l in old.splitlines()] or [""]
    n = len(old_lines)
    starts = [i for i in range(len(stripped) - n + 1) if stripped[i:i + n] == old_lines]
    if len(starts) != 1:
        return None, len(starts)
    start = starts[0]
    new_lines = new.splitlines(keepends=True) or [""]
    return "".join(content_lines[:start] + new_lines + content_lines[start + n:]), 1


def _closest_hint(content: str, old: str) -> str:
    """用 difflib 找最接近 old 的位置,返回该处上下文帮助模型修正。"""
    import difflib
    sm = difflib.SequenceMatcher(None, content, old, autojunk=False)
    m = sm.find_longest_match(0, len(content), 0, len(old))
    if m.size < 5:
        return ""
    line_no = content[:m.a].count("\n") + 1
    lines = content.splitlines()
    ctx = "\n".join(f"{i:>6}  {l}" for i, l in enumerate(lines[line_no - 1:line_no + 2], start=line_no))
    return f"最接近的位置在第 {line_no} 行附近:\n{ctx}"


def _edit_file(input_data: dict, context: ToolContext) -> ToolResult:
    """精确编辑:在文件里找到 old_str,替换成 new_str。

    匹配策略:精确唯一 → 行级去尾空白唯一 → 报错并给"最接近"提示。
    改之前打 checkpoint,支持回退。dry_run=True 时只预览 diff,不写、不打 checkpoint。
    """
    path = str(input_data.get("path", ""))
    old_str = input_data.get("old_str", "")
    new_str = input_data.get("new_str", "")
    dry_run = bool(input_data.get("dry_run", False))
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

    try:
        content = safe.read_text(encoding="utf-8")
    except Exception as e:
        return ToolResult(ok=False, output=f"Could not read: {e}")

    # 替换:① 精确唯一 → ② 行级去尾空白唯一 → ③ 报错并给"最接近"提示
    new_content, n_exact = _replace_unique(content, old_str, new_str)
    if new_content is None:
        new_content, n_line = _replace_linewise(content, old_str, new_str)
        if new_content is None:
            hint = _closest_hint(content, old_str)
            if n_exact == 0 and n_line == 0:
                head = "old_str 没找到(精确 0 次,行级去尾空白后 0 次)"
            else:
                head = f"old_str 不唯一(精确 {n_exact} 次,行级去尾空白后 {n_line} 次)"
            return ToolResult(ok=False, output=(
                f"{head}。\n{hint}\n请提供更精确的上下文,或用 write_file 整写。"))

    # 生成 diff(预览/展示用)
    try:
        from minicore.diff import format_diff_text
        diff_text = format_diff_text(content, new_content)
    except Exception:
        diff_text = ""

    # dry_run:只预览 diff,不写文件、不打 checkpoint
    if dry_run:
        msg = f"[dry_run] 将替换 1 处: {old_str!r} → {new_str!r}"
        if diff_text:
            msg += "\n\n" + diff_text
        return ToolResult(ok=True, output=msg)

    # 改之前打 checkpoint(和 write_file 一致,支持回退)
    if context.session is not None:
        try:
            from minicore.session import create_file_checkpoint
            create_file_checkpoint(context.session, file_path=str(safe),
                                   existed=True,
                                   previous_content=content,
                                   group_id=context.group_id)
        except ImportError:
            pass

    try:
        safe.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return ToolResult(ok=False, output=f"Write failed: {e}")
    msg = f"已替换 1 处: {old_str!r} → {new_str!r}"
    if diff_text:
        msg += "\n\n" + diff_text
    return ToolResult(ok=True, output=msg)


def _apply_patch(input_data: dict, context: ToolContext) -> ToolResult:
    """应用 unified diff 修改文件(可一次多处)。改之前打 checkpoint。dry_run=True 时只预览。"""
    from minicore.patch import apply_unified_diff
    path = str(input_data.get("path", "") or "")
    patch_text = input_data.get("patch", "")
    dry_run = bool(input_data.get("dry_run", False))
    if not path.strip() or not patch_text:
        return ToolResult(ok=False, output="path 和 patch 必填")
    try:
        safe = resolve_tool_path(context, path, mode="write")
    except PermissionError as e:
        return ToolResult(ok=False, output=str(e))
    except RuntimeError as e:
        return ToolResult(ok=False, output=str(e))
    if not safe.exists():
        return ToolResult(ok=False, output=f"Path not found: {safe}")

    try:
        content = safe.read_text(encoding="utf-8")
    except Exception as e:
        return ToolResult(ok=False, output=f"Could not read: {e}")

    new_content, err = apply_unified_diff(content, patch_text)
    if err:
        return ToolResult(ok=False, output=f"apply_patch 失败: {err}")

    # 生成 diff(预览/展示用)
    try:
        from minicore.diff import format_diff_text
        diff_text = format_diff_text(content, new_content)
    except Exception:
        diff_text = ""

    # dry_run:只预览 diff,不写文件、不打 checkpoint
    if dry_run:
        msg = f"[dry_run] 将应用 patch 到 {safe}"
        if diff_text:
            msg += "\n\n" + diff_text
        return ToolResult(ok=True, output=msg)

    # 改之前打 checkpoint(和 write_file/edit_file 一致,支持回退)
    if context.session is not None:
        try:
            from minicore.session import create_file_checkpoint
            create_file_checkpoint(context.session, file_path=str(safe), existed=True,
                                   previous_content=content,
                                   group_id=context.group_id)
        except ImportError:
            pass

    try:
        safe.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return ToolResult(ok=False, output=f"Write failed: {e}")

    msg = f"apply_patch 已应用到 {safe}"
    if diff_text:
        msg += "\n\n" + diff_text
    return ToolResult(ok=True, output=msg)


def _finish(input_data: dict, context: ToolContext) -> ToolResult:
    """结构化结束信号:由 agent_loop 拦截处理,正常不会走到这里。"""
    return ToolResult(ok=True, output="[finish] 应被 agent_loop 拦截")


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
        import minicore.model as model_mod
        from minicore.tools import create_readonly_tools

        # 子代理用全局模型(由 server 启动时设置)
        from minicore.tools import _SUB_AGENT_MODEL
        if _SUB_AGENT_MODEL is None:
            return ToolResult(ok=False, output="子代理模型未配置")

        # 子代理只读工具集(read_file / list_files / grep_files)
        sub_tools = create_readonly_tools()
        # 保存并恢复全局工具集,避免子代理的只读工具集污染主循环
        prev_tools = model_mod._TOOLS
        set_tools(sub_tools)
        try:
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
        finally:
            set_tools(prev_tools)
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


def _validate_read(input_data: dict) -> tuple[bool, str]:
    """read_file 校验:path 必填字符串,offset/limit 可选且为 >=1 整数。"""
    path = input_data.get("path")
    if not isinstance(path, str) or not path:
        return False, "path 必填且必须是字符串"
    for field in ("offset", "limit"):
        v = input_data.get(field)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            return False, f"{field} 必须是 >=1 的整数"
    return True, ""


def _validate_glob(input_data: dict) -> tuple[bool, str]:
    """glob_files 校验:pattern 必须是非空字符串。"""
    pattern = input_data.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return False, "pattern 必填且必须是字符串"
    return True, ""


def _validate_symbol(input_data: dict) -> tuple[bool, str]:
    """find_symbol / find_references 校验:name 必须是非空字符串。"""
    name = input_data.get("name")
    if not isinstance(name, str) or not name.strip():
        return False, "name 必填且必须是字符串"
    return True, ""


def _validate_grep(input_data: dict) -> tuple[bool, str]:
    """grep_files 校验:pattern 必须是非空字符串。"""
    pattern = input_data.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return False, "pattern 必填且必须是字符串"
    return True, ""


def _validate_command(input_data: dict) -> tuple[bool, str]:
    """run_command 校验:command 必须是字符串且非空。"""
    command = input_data.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return False, "command 必须是字符串且非空"
    return True, ""


def _validate_verify(input_data: dict) -> tuple[bool, str]:
    """verify 校验:command 可选字符串。"""
    command = input_data.get("command", "python -m pytest")
    if not isinstance(command, str):
        return False, "command 必须是字符串"
    return True, ""


def _validate_finish(input_data: dict) -> tuple[bool, str]:
    """finish 校验:summary 可选字符串。"""
    summary = input_data.get("summary", "")
    if not isinstance(summary, str):
        return False, "summary 必须是字符串"
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


def _validate_apply_patch(input_data: dict) -> tuple[bool, str]:
    """apply_patch 校验:path/patch 必须存在。"""
    for field in ("path", "patch"):
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


# 只读工具名(无副作用,可并发执行,也是 delegate 子代理可用的工具集)
READONLY_TOOL_NAMES = frozenset({"read_file", "list_files", "grep_files", "glob_files",
                                 "find_symbol", "find_references"})


def _core_tools() -> list[ToolDefinition]:
    """内置核心工具(不含 MCP)。"""
    return [
        ToolDefinition(
            name="list_files",
            description="List files in a directory. Input: {\"path\": \"<dir>\"}",
            run=_list_files,
            validate=_validate_path,
            schema={"type": "object",
                   "properties": {"path": {"type": "string", "description": "目录路径,默认当前目录"}}},
        ),
        ToolDefinition(
            name="read_file",
            description="Read a text file with line numbers, supports paging. Input: {\"path\": \"<file>\", \"offset\": 1, \"limit\": 2000}",
            run=_read_file,
            validate=_validate_read,
            schema={"type": "object",
                   "properties": {
                       "path": {"type": "string", "description": "文件路径"},
                       "offset": {"type": "integer", "description": "起始行号(1-based),默认 1"},
                       "limit": {"type": "integer", "description": "最多返回行数,默认 2000"},
                   },
                   "required": ["path"]},
        ),
        ToolDefinition(
            name="grep_files",
            description="Search file contents in the project by regex. Input: {\"pattern\": \"<regex>\", \"path\": \"<dir or file, default .>\"}",
            run=_grep_files,
            validate=_validate_grep,
            schema={"type": "object",
                   "properties": {
                       "pattern": {"type": "string", "description": "正则表达式"},
                       "path": {"type": "string", "description": "目录或文件路径,默认当前目录"},
                   },
                   "required": ["pattern"]},
        ),
        ToolDefinition(
            name="glob_files",
            description="Find files matching a glob pattern. Input: {\"pattern\": \"<glob, e.g. **/*.py>\"}",
            run=_glob_files,
            validate=_validate_glob,
            schema={"type": "object",
                   "properties": {"pattern": {"type": "string", "description": "glob 模式,如 **/*.py 或 minicore/*.py"}},
                   "required": ["pattern"]},
        ),
        ToolDefinition(
            name="find_symbol",
            description="Find where a symbol (function/class/variable) is defined. Input: {\"name\": \"<symbol name>\"}",
            run=_find_symbol,
            validate=_validate_symbol,
            schema={"type": "object",
                   "properties": {"name": {"type": "string", "description": "符号名,如函数名/类名"}},
                   "required": ["name"]},
        ),
        ToolDefinition(
            name="find_references",
            description="Find all references (definitions, uses, calls) of a symbol. Input: {\"name\": \"<symbol name>\"}",
            run=_find_references,
            validate=_validate_symbol,
            schema={"type": "object",
                   "properties": {"name": {"type": "string", "description": "符号名"}},
                   "required": ["name"]},
        ),
        ToolDefinition(
            name="run_command",
            description=("Run a whitelisted command (shell=False, no pipes/redirects). "
                         "Allowed: python/pytest/pip/git/node/npm/npx and common file tools. "
                         "Input: {\"command\": \"<cmd>\", \"timeout\": 30}"),
            run=_run_command,
            validate=_validate_command,
            schema={"type": "object",
                   "properties": {
                       "command": {"type": "string", "description": "要执行的命令(白名单内,不支持管道/重定向)"},
                       "timeout": {"type": "number", "description": "超时秒数,默认 30"},
                   },
                   "required": ["command"]},
        ),
        ToolDefinition(
            name="verify",
            description="Run tests to verify code changes (default: python -m pytest). Input: {\"command\": \"<optional test cmd>\", \"timeout\": 120}",
            run=_verify,
            validate=_validate_verify,
            schema={"type": "object",
                   "properties": {
                       "command": {"type": "string", "description": "验证命令,默认 python -m pytest"},
                       "timeout": {"type": "number", "description": "超时秒数,默认 120"},
                   }},
        ),
        ToolDefinition(
            name="finish",
            description=("Signal completion. Call this when done; summary MUST be the direct, "
                         "concise answer to the user's CURRENT question — not a project overview "
                         "or a recap of prior topics. Input: {\"summary\": \"<final answer>\"}"),
            run=_finish,
            validate=_validate_finish,
            schema={"type": "object",
                   "properties": {"summary": {"type": "string", "description": "针对用户当前问题的简短最终回答"}}},
        ),
        ToolDefinition(
            name="write_file",
            description="Write content to a file (overwrites). Input: {\"path\": \"<file>\", \"content\": \"<text>\"}",
            run=_write_file,
            validate=_validate_write,
            schema={"type": "object",
                   "properties": {
                       "path": {"type": "string", "description": "文件路径"},
                       "content": {"type": "string", "description": "写入内容"},
                   },
                   "required": ["path", "content"]},
        ),
        ToolDefinition(
            name="remember",
            description="Remember a project fact or preference for future sessions. Input: {\"content\": \"<fact to remember>\"}",
            run=_remember,
            validate=_validate_remember,
            schema={"type": "object",
                   "properties": {"content": {"type": "string", "description": "要记住的事实或偏好"}},
                   "required": ["content"]},
        ),
        ToolDefinition(
            name="edit_file",
            description="Precisely replace a unique old_str with new_str in a file. Input: {\"path\": \"<file>\", \"old_str\": \"<exact text>\", \"new_str\": \"<replacement>\"}",
            run=_edit_file,
            validate=_validate_edit,
            schema={"type": "object",
                   "properties": {
                       "path": {"type": "string", "description": "文件路径"},
                       "old_str": {"type": "string", "description": "要替换的原文(必须唯一出现)"},
                       "new_str": {"type": "string", "description": "替换后的内容"},
                   },
                   "required": ["path", "old_str", "new_str"]},
        ),
        ToolDefinition(
            name="apply_patch",
            description="Apply a unified diff to a file (multiple hunks at once). Input: {\"path\": \"<file>\", \"patch\": \"<unified diff with @@ hunks>\"}",
            run=_apply_patch,
            validate=_validate_apply_patch,
            schema={"type": "object",
                   "properties": {
                       "path": {"type": "string", "description": "文件路径"},
                       "patch": {"type": "string", "description": "unified diff(含 @@ hunk 头)"},
                   },
                   "required": ["path", "patch"]},
        ),
        ToolDefinition(
            name="delegate",
            description="Delegate a subtask to a read-only sub-agent. Use for research/analysis. Input: {\"task\": \"<subtask description>\", \"max_steps\": 10}",
            run=_delegate,
            schema={"type": "object",
                   "properties": {
                       "task": {"type": "string", "description": "子任务描述"},
                       "max_steps": {"type": "integer", "description": "最大步数,默认 10"},
                   },
                   "required": ["task"]},
        ),
    ]


# MCP 客户端句柄(供外部关闭)
_MCP_CLIENTS: list[Any] = []


def _load_mcp_config() -> list[tuple[str, list[str]]]:
    """从环境变量读取 MCP 服务器配置,返回 [(name, command), ...]。

    MY_AGENT_MCP_SERVERS 格式(JSON 数组):
    [{"name": "filesystem", "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem"]}]
    """
    import json
    import os
    raw = os.environ.get("MY_AGENT_MCP_SERVERS", "").strip()
    if not raw:
        return []
    try:
        servers = json.loads(raw)
    except json.JSONDecodeError:
        return []
    result = []
    for s in servers:
        name = s.get("name")
        cmd = s.get("command")
        if isinstance(name, str) and name and isinstance(cmd, list) and cmd:
            result.append((name, cmd))
    return result


def create_default_tools() -> ToolRegistry:
    # MCP 服务器:优先读环境变量 MY_AGENT_MCP_SERVERS,否则连内置假服务端(测试/演示用)
    servers = _load_mcp_config() or [("fake-add", ["python", "minicore/fake_mcp_server.py"])]
    mcp_tools: list[ToolDefinition] = []
    for name, command in servers:
        tools, client = create_mcp_tools(name, command)
        mcp_tools.extend(tools)
        if client is not None:
            _MCP_CLIENTS.append(client)
    return ToolRegistry(_core_tools() + mcp_tools)


def create_readonly_tools() -> ToolRegistry:
    """只含只读工具(read_file / list_files / grep_files)的注册表,供 delegate 子代理使用。"""
    return ToolRegistry([t for t in _core_tools() if t.name in READONLY_TOOL_NAMES])


def close_mcp() -> None:
    """关闭所有 MCP 客户端连接。"""
    global _MCP_CLIENTS
    for client in _MCP_CLIENTS:
        try:
            client.close()
        except Exception:
            pass
    _MCP_CLIENTS = []