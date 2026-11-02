"""A minimal MCP server over stdio, for tests: tools echo and add."""
import json
import sys

TOOLS = [
    {"name": "echo", "description": "Echo text back", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two numbers", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
    {"name": "fail", "description": "Always fails", "inputSchema": {"type": "object", "properties": {}}},
]


def reply(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    msg["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        m = json.loads(line)
    except json.JSONDecodeError:
        continue
    method, rid = m.get("method"), m.get("id")
    if rid is None:
        continue  # notification
    if method == "initialize":
        reply(rid, {"protocolVersion": m["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "echo-test", "version": "1"}})
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = m["params"]["name"], m["params"].get("arguments") or {}
        if name == "echo":
            reply(rid, {"content": [{"type": "text", "text": "echo: " + str(args.get("text"))}]})
        elif name == "add":
            reply(rid, {"content": [], "structuredContent": {"sum": args.get("a", 0) + args.get("b", 0)}})
        else:
            reply(rid, {"content": [{"type": "text", "text": "boom"}], "isError": True})
    else:
        reply(rid, error={"code": -32601, "message": "unknown method"})
