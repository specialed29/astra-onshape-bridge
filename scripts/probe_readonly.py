"""Run in the service environment. Outputs no tokens and performs no CAD mutations."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from openai import AsyncOpenAI
import app


def redact(text):
    for key in ("OPENAI_API_KEY", "ONSHAPE_ACCESS_KEY", "ONSHAPE_SECRET_KEY",
                "MCP_BEARER_TOKEN", "CHAT_BEARER_TOKEN"):
        secret = os.getenv(key)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text.replace(app.mcp_token(), "[REDACTED]")


def decode_rpc(response):
    if "text/event-stream" not in response.headers.get("content-type", ""):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            item = json.loads(line[5:])
            if "result" in item or "error" in item:
                return item
    raise RuntimeError("No JSON-RPC result in SSE body.")


async def run(args):
    base = args.base_url.rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("A public HTTPS --base-url is required.")
    headers = {
        "Authorization": "Bearer " + app.mcp_token(),
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=90, follow_redirects=False) as c:
        for path in ["/", "/health", "/mcp-info"]:
            r = await c.get(base + path)
            print(f"GET {path}: HTTP {r.status_code}")
            r.raise_for_status()
            if path != "/":
                print(redact(r.text))
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "astra-readonly-probe", "version": "1.0"},
            }},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        for message in messages:
            r = await c.post(base + "/mcp", headers=headers, json=message)
            print(f"{message['method']}: HTTP {r.status_code}, Content-Type={r.headers.get('content-type')}")
            if not r.is_success:
                print(redact(r.text[:6000]))
                r.raise_for_status()
            session_id = r.headers.get("mcp-session-id")
            if session_id:
                headers["Mcp-Session-Id"] = session_id
                print("MCP session ID received and retained (not printed).")
            if message["method"] == "initialize":
                result = decode_rpc(r)
                if "error" in result:
                    raise RuntimeError(redact(json.dumps(result["error"])))
                headers["MCP-Protocol-Version"] = result["result"]["protocolVersion"]
                print(json.dumps(result["result"]["serverInfo"]))
            elif message["method"] == "tools/list":
                result = decode_rpc(r)
                if "error" in result:
                    raise RuntimeError(redact(json.dumps(result["error"])))
                names = [tool["name"] for tool in result["result"]["tools"]]
                print("TOOLS:", ", ".join(names))
                missing = set(app.READ_TOOLS + app.WRITE_TOOLS) - set(names)
                if missing:
                    raise RuntimeError("Missing tools: " + ", ".join(sorted(missing)))
        if args.openai:
            # Approval always: even a misbehaving model cannot execute a tool here.
            async with AsyncOpenAI(timeout=90, max_retries=0) as client:
                response = await client.responses.create(
                    model=app.MODEL, store=False,
                    input="Report only the names of the available Onshape tools. Do not call tools or modify anything.",
                    tools=[{"type": "mcp", "server_label": "onshape_cad",
                            "server_url": base + "/mcp", "authorization": app.mcp_token(),
                            "require_approval": "always"}],
                )
            imports = [item for item in response.output if item.type == "mcp_list_tools"]
            if not imports:
                raise RuntimeError("No mcp_list_tools output item; discovery not proven.")
            for item in imports:
                if getattr(item, "error", None):
                    raise RuntimeError(redact(str(item.error)))
                print("OPENAI IMPORTED:", ", ".join(t.name for t in item.tools))
            print("OPENAI RESPONSE:", response.id)
        if args.onshape:
            result = await app.onshape_request("GET", "/api/v10/documents", params={"limit": 1, "offset": 0})
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise RuntimeError("Unexpected Onshape document-list schema.")
            print(f"ONSHAPE DIRECT: HTTP success; {len(result['items'])} sample document(s). Names/IDs withheld.")
        if args.chat:
            r = await c.post(base + "/api/chat", headers={
                "Authorization": "Bearer " + (os.getenv("CHAT_BEARER_TOKEN") or app.mcp_token()),
            }, json={"message": "List my Onshape documents. Do not modify anything."})
            print("CHAT:", r.status_code)
            if not r.is_success:
                print(redact(r.text[:6000]))
                r.raise_for_status()
            result = r.json()
            calls = result.get("tool_calls", [])
            if not any(t["name"] == "search_documents" and t.get("status") == "completed"
                       and not t.get("error") for t in calls):
                raise RuntimeError("Chat did not report a successful search_documents call.")
            print("CHAT: verified completed search_documents call; private document output withheld.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL"))
    parser.add_argument("--openai", action="store_true")
    parser.add_argument("--onshape", action="store_true")
    parser.add_argument("--chat", action="store_true")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("--base-url or PUBLIC_BASE_URL / RENDER_EXTERNAL_URL is required")
    try:
        asyncio.run(run(args))
    except Exception as error:
        print(redact(f"FAILED: {type(error).__name__}: {error}"), file=sys.stderr)
        sys.exit(1)
