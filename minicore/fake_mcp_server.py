"""假 MCP 服务端:读一行 JSON,处理,回一行。

用于测试 StdioMcpClient。模拟一个"加法计算器"服务:
- tools/list → 返回一个 add 工具
- tools/call → 执行 add,返回两数之和
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")


def handle_request(msg: dict) -> dict:
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "add",
                        "description": "Add two numbers. Input: {\"a\": <int>, \"b\": <int>}",
                    }
                ]
            },
        }
    if method == "tools/call":
        name = msg.get("params", {}).get("name")
        args = msg.get("params", {}).get("arguments", {})
        if name == "add":
            total = int(args.get("a", 0)) + int(args.get("b", 0))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"add({args.get('a')}, {args.get('b')}) = {total}"}]
                },
            }
        return {"jsonrpc": "2.0", "id": req_id, "error": {"message": f"unknown tool {name}"}}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"message": f"unknown method {method}"}}


def main() -> None:
    # 持续读 stdin 的每一行,处理,写到 stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(msg)
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
