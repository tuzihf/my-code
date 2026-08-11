"""清理会话。

规则(默认):
- 可读对话数 >= 1 的会话 → 保留(可能有真实对话)
- 可读对话数 == 0 的会话 → 删除(纯工具日志/空会话/系统注入)

加 --healthy 额外删除"对话占比过低"的旧噪音会话(修复前的产物):
- 有对话但占比 < 30% 且总数 > 5 → 删除

执行前先预览,加 --delete 才真正删除。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import session as session_mod


def main() -> None:
    do_delete = "--delete" in sys.argv
    healthy_only = "--healthy" in sys.argv
    ids = session_mod.list_sessions()
    keep = []
    remove = []
    for sid in ids:
        s = session_mod.load_session(sid)
        if s is None:
            remove.append((sid, "(无法加载)"))
            continue
        readable = session_mod.readable_conversation_count(s.messages)
        total = len(s.messages)
        if readable >= 1:
            ratio = readable / total if total else 0
            if healthy_only and total > 5 and ratio < 0.3:
                remove.append((sid, f"旧噪音会话 对话:{readable}/{total} ({ratio:.0%})"))
            else:
                keep.append((sid, readable, total, ratio))
        else:
            remove.append((sid, f"可读对话:0/{total}"))

    print(f"共 {len(ids)} 个会话")
    print(f"保留 {len(keep)} 个:")
    for sid, r, t, ratio in keep:
        print(f"  💬 {sid} | 对话:{r}/{t} ({ratio:.0%})")
    print(f"\n删除 {len(remove)} 个:")
    for sid, reason in remove:
        print(f"  🗑  {sid} | {reason}")

    if not do_delete:
        print("\n这是预览。加 --delete 才真正删除。")
        return

    for sid, _ in remove:
        import pathlib
        p = pathlib.Path(session_mod.sessions_dir()) / f"{sid}.json"
        if p.exists():
            p.unlink()
            print(f"  已删除 {sid}")
    print(f"\n完成,删除 {len(remove)} 个。")


if __name__ == "__main__":
    main()
