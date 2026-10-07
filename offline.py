"""Reversible, fail-closed shutdown gate for a paused bridge."""
import os
from starlette.responses import JSONResponse


class OfflineGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if os.getenv("BRIDGE_OFFLINE", "false").lower() == "true":
            if scope["type"] == "http":
                health = scope["path"] == "/health" and scope["method"] in {"GET", "HEAD"}
                response = JSONResponse(
                    {"app": False, "offline": True, "message": "Development paused. Bridge is offline."},
                    status_code=200 if health else 503,
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)
