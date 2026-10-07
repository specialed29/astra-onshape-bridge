from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from openai import OpenAI
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

load_dotenv()
ROOT = Path(__file__).resolve().parent
MODEL = os.getenv("OPENAI_MODEL", "gpt-6-astra")
BASE = os.getenv("ONSHAPE_BASE_URL", "https://cad.onshape.com").rstrip("/")


def configured() -> bool:
    return bool(
        os.getenv("OPENAI_API_KEY")
        and os.getenv("ONSHAPE_ACCESS_KEY")
        and os.getenv("ONSHAPE_SECRET_KEY")
    )


def onshape_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: Any | None = None,
) -> Any:
    access = os.environ["ONSHAPE_ACCESS_KEY"]
    secret = os.environ["ONSHAPE_SECRET_KEY"]
    headers = {
        "Accept": "application/json;charset=UTF-8; qs=0.09",
        "Content-Type": "application/json;charset=UTF-8; qs=0.09",
    }
    with httpx.Client(
        base_url=BASE,
        auth=httpx.BasicAuth(access, secret),
        headers=headers,
        timeout=45,
        follow_redirects=True,
    ) as client:
        r = client.request(method, path, params=params, json=body)

    if r.status_code >= 400:
        raise RuntimeError(
            f"Onshape {method} {path} failed with HTTP {r.status_code}: {r.text[:3000]}"
        )
    if not r.content:
        return {"ok": True, "status_code": r.status_code}
    if "json" in r.headers.get("content-type", ""):
        return r.json()
    return {
        "status_code": r.status_code,
        "content_type": r.headers.get("content-type"),
        "text": r.text[:3000],
    }


mcp = FastMCP(
    "Astra Onshape Bridge",
    instructions=(
        "Use these tools to inspect and operate the configured Onshape account. "
        "Do not perform writes unless the user's current request explicitly asks for them. "
        "Keep document/workspace/element IDs explicit in write operations."
    ),
)


@mcp.tool
def onshape_health() -> dict[str, Any]:
    """Verify Onshape credentials with a minimal read."""
    if not configured():
        return {"ok": False, "reason": "Required secrets are not configured."}
    return {"ok": True, "sample": onshape_request("GET", "/api/v10/documents", params={"limit": 1})}


@mcp.tool
def search_documents(query: str = "", limit: int = 20) -> Any:
    """Search documents visible to the configured Onshape account. READ ONLY."""
    return onshape_request(
        "GET",
        "/api/v10/documents",
        params={"q": query, "offset": 0, "limit": max(1, min(limit, 100))},
    )


@mcp.tool
def get_document(document_id: str) -> Any:
    """Get one Onshape document by ID. READ ONLY."""
    return onshape_request("GET", f"/api/v10/documents/{document_id}")


@mcp.tool
def list_elements(document_id: str, workspace_id: str) -> Any:
    """List the tabs/elements in a workspace. READ ONLY."""
    return onshape_request(
        "GET",
        f"/api/v10/documents/d/{document_id}/w/{workspace_id}/elements",
    )


@mcp.tool
def get_partstudio_features(document_id: str, workspace_id: str, element_id: str) -> Any:
    """Inspect the complete feature list in a Part Studio. READ ONLY."""
    return onshape_request(
        "GET",
        f"/api/v10/partstudios/d/{document_id}/w/{workspace_id}/e/{element_id}/features",
        params={"rollbackBarIndex": -1, "includeGeometryIds": "true"},
    )


@mcp.tool
def create_document(name: str) -> Any:
    """Create a new Onshape document. WRITE."""
    return onshape_request("POST", "/api/v10/documents", body={"name": name})


@mcp.tool
def add_feature_raw(
    document_id: str,
    workspace_id: str,
    element_id: str,
    feature_definition_json: str,
) -> Any:
    """Advanced: add one raw Onshape feature payload. WRITE. Inspect feature JSON first when possible."""
    payload = json.loads(feature_definition_json)
    if not isinstance(payload, dict) or "feature" not in payload:
        raise ValueError("Expected a JSON object with a top-level 'feature' key.")
    return onshape_request(
        "POST",
        f"/api/v10/partstudios/d/{document_id}/w/{workspace_id}/e/{element_id}/features",
        body=payload,
    )


@mcp.tool
def export_partstudio_step(
    document_id: str,
    workspace_id: str,
    element_id: str,
    store_in_document: bool = True,
) -> Any:
    """Start an asynchronous STEP export of a Part Studio."""
    return onshape_request(
        "POST",
        f"/api/v10/partstudios/d/{document_id}/w/{workspace_id}/e/{element_id}/export/step",
        body={"storeInDocument": store_in_document, "stepUnit": "MILLIMETER"},
    )


@mcp.tool
def get_translation(translation_id: str) -> Any:
    """Check the state of an Onshape asynchronous export/translation. READ ONLY."""
    return onshape_request("GET", f"/api/v10/translations/{translation_id}")


def mcp_token() -> str:
    explicit = os.getenv("MCP_BEARER_TOKEN")
    if explicit:
        return explicit
    secret = os.getenv("ONSHAPE_SECRET_KEY", "unconfigured")
    return hmac.new(
        secret.encode(), b"astra-onshape-mcp-v1", hashlib.sha256
    ).hexdigest()


def public_base(request: Request) -> str | None:
    explicit = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if explicit:
        return explicit
    render = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if render:
        return render
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if host and "localhost" not in host and "127.0.0.1" not in host:
        return f"{proto}://{host}".rstrip("/")
    return None


async def index(request: Request):
    return HTMLResponse((ROOT / "static" / "index.html").read_text(encoding="utf-8"))


async def health(request: Request):
    return JSONResponse(
        {
            "app": True,
            "configured": configured(),
            "model": MODEL,
            "public_base_url": public_base(request),
        }
    )


async def chat(request: Request):
    if not configured():
        return JSONResponse(
            {"error": "Add OPENAI_API_KEY, ONSHAPE_ACCESS_KEY, and ONSHAPE_SECRET_KEY in Render."},
            status_code=503,
        )
    data = await request.json()
    message = str(data.get("message", "")).strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    base = public_base(request)
    if not base:
        return JSONResponse({"error": "Could not determine public service URL."}, status_code=500)

    tool = {
        "type": "mcp",
        "server_label": "onshape_cad",
        "server_description": "Inspect Onshape documents, modify Part Studio features, and export STEP.",
        "server_url": f"{base}/mcp",
        "authorization": mcp_token(),
        "require_approval": "never",
    }
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "instructions": (
            "You are a mechanical CAD copilot connected to Onshape. "
            "Never modify CAD unless the current user message explicitly asks for a write. "
            "Prefer inspection before raw feature writes. Keep units and IDs explicit."
        ),
        "input": message,
        "tools": [tool],
    }
    if data.get("previous_response_id"):
        kwargs["previous_response_id"] = data["previous_response_id"]

    try:
        r = OpenAI(api_key=os.environ["OPENAI_API_KEY"]).responses.create(**kwargs)
        return JSONResponse({"text": r.output_text, "response_id": r.id})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


class MCPBearerMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            auth = headers.get("authorization", "")
            token = mcp_token()
            if auth not in {token, f"Bearer {token}"}:
                response = PlainTextResponse("Unauthorized", status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app = mcp.http_app(stateless_http=True)
app.router.routes.extend(
    [
        Route("/", index, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/api/chat", chat, methods=["POST"]),
    ]
)
app.add_middleware(MCPBearerMiddleware)
