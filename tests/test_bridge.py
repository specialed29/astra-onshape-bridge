"""Offline regression tests. No external calls, production credentials, or CAD writes."""
import asyncio
import base64
import hashlib
import hmac
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

import app


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "OPENAI_API_KEY": "offline-test-key",
            "ONSHAPE_ACCESS_KEY": "offline-access",
            "ONSHAPE_SECRET_KEY": "offline-secret",
            "CHAT_BEARER_TOKEN": "offline-chat-token",
            "PUBLIC_BASE_URL": "https://bridge.example.test",
            "ENABLE_CAD_WRITES": "false",
        })
        # Verifier captures the token when app.py is imported, before the env patch.
        self.mcp_token = next(iter(app.auth.tokens))
        self.env.start()
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.headers = {
            "Authorization": "Bearer " + self.mcp_token,
            "Accept": "application/json, text/event-stream",
        }

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()

    def rpc(self, method, params=None, id=1):
        body = {"jsonrpc": "2.0", "method": method}
        if id is not None:
            body["id"] = id
        if params is not None:
            body["params"] = params
        return self.client.post("/mcp", headers=self.headers, json=body)

    def test_public_routes(self):
        for path in ["/", "/health", "/mcp-info"]:
            self.assertEqual(self.client.get(path).status_code, 200)
        health = self.client.get("/health").json()
        self.assertTrue(health["configured"])
        self.assertFalse(health["writes_enabled"])
        self.assertEqual(health["fastmcp_version"], "4.0.11")
        self.assertEqual(self.client.get("/mcp-info").json()["endpoint"], "/mcp")

    def test_streamable_http_sequence(self):
        initialized = self.rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "offline-test", "version": "1.0"},
        })
        self.assertEqual(initialized.status_code, 200)
        self.assertIn("application/json", initialized.headers["content-type"])
        self.assertNotIn("mcp-session-id", initialized.headers)
        self.assertEqual(initialized.json()["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(self.rpc("notifications/initialized", id=None).status_code, 202)
        listed = self.rpc("tools/list")
        self.assertEqual(listed.status_code, 200)
        tools = listed.json()["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, set(app.READ_TOOLS + app.WRITE_TOOLS))
        self.assertTrue(next(t for t in tools if t["name"] == "search_documents")["annotations"]["readOnlyHint"])
        self.assertNotIn("location", listed.headers)

    def test_mcp_auth_fails_closed(self):
        for token in ["", "Bearer invalid", self.mcp_token]:
            r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                 headers={"Authorization": token, "Accept": "application/json, text/event-stream"})
            self.assertEqual(r.status_code, 401)

    def test_mcp_headers_and_trailing_slash(self):
        r = self.client.post("/mcp", headers={"Authorization": "Bearer " + self.mcp_token, "Accept": "text/plain"},
                             json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        self.assertEqual(r.status_code, 406)
        r = self.client.post("/mcp/", headers=self.headers, json={}, follow_redirects=False)
        self.assertEqual(r.status_code, 307)
        self.assertTrue(r.headers["location"].endswith("/mcp"))

    def test_chat_requires_auth_and_valid_input(self):
        self.assertEqual(self.client.post("/api/chat", json={"message": "hello"}).status_code, 401)
        headers = {"Authorization": "Bearer offline-chat-token"}
        for body in [[], {}, {"message": 12}, {"message": ""}, {"message": "x", "previous_response_id": []}]:
            self.assertEqual(self.client.post("/api/chat", headers=headers, json=body).status_code, 400)
        self.assertEqual(self.client.post("/api/chat", headers=headers, content="{").status_code, 400)
        self.assertEqual(self.client.post("/api/chat", headers=headers, content="x"*33000).status_code, 413)

    def test_direct_diagnostic_requires_auth(self):
        self.assertEqual(self.client.get("/api/diagnostics/onshape").status_code, 401)

    def test_no_host_header_credential_exfiltration(self):
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": "", "RENDER_EXTERNAL_URL": ""}):
            r = self.client.get("/health", headers={"Host": "attacker.example", "X-Forwarded-Host": "attacker.example"})
            self.assertIsNone(r.json()["public_base_url"])
        for base in ["http://example.test", "https://user:password@example.test", "https://example.test/mcp"]:
            with patch.dict(os.environ, {"PUBLIC_BASE_URL": base}):
                self.assertIsNone(self.client.get("/health").json()["public_base_url"])

    def test_write_tools_fail_before_network(self):
        for name, arguments in [
            ("create_document", {"name": "must-not-exist"}),
            ("add_feature_raw", {"document_id": "d", "workspace_id": "w", "element_id": "e",
                                 "feature_definition_json": '{"feature":{}}'}),
            ("export_partstudio_step", {"document_id": "d", "workspace_id": "w", "element_id": "e"}),
        ]:
            result = self.rpc("tools/call", {"name": name, "arguments": arguments}).json()
            self.assertTrue(result["result"]["isError"])
            self.assertIn("disabled", result["result"]["content"][0]["text"])

    def test_chat_allows_mcp_callback_while_waiting(self):
        """Regression for single-worker deadlock: OpenAI calls back before it returns."""
        headers = self.headers
        outer = self

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.responses = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def create(self, **kwargs):
                outer.assertEqual(kwargs["tools"][0]["allowed_tools"], app.READ_TOOLS)
                outer.assertEqual(kwargs["tools"][0]["server_url"], "https://bridge.example.test/mcp")
                outer.assertFalse(kwargs["tools"][0]["authorization"].startswith("Bearer "))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                             base_url="http://testserver") as c:
                    r = await asyncio.wait_for(c.post("/mcp", headers=headers, json={
                        "jsonrpc": "2.0", "method": "tools/list", "id": 99,
                    }), timeout=2)
                outer.assertEqual(r.status_code, 200)
                return SimpleNamespace(id="resp_offline", output_text="Read-only callback passed.", output=[])

        with patch.object(app, "AsyncOpenAI", FakeOpenAI):
            r = self.client.post("/api/chat", headers={"Authorization": "Bearer offline-chat-token"},
                                 json={"message": "List tool names only."})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["response_id"], "resp_offline")

    def test_hmac_signature_uses_exact_path_query(self):
        request = httpx.Request("GET", "https://cad.onshape.com/api/v10/documents?q=A%20B&limit=1",
                                headers={"Content-Type": "application/json"})
        with patch.object(app.secrets, "token_hex", return_value="a"*32), \
             patch.object(app, "formatdate", return_value="Wed, 07 Oct 2026 22:00:00 GMT"):
            app.sign_onshape_request(request, "test-access", "test-secret")
        canonical = "get\n" + "a"*32 + "\nwed, 07 oct 2026 22:00:00 gmt\napplication/json\n/api/v10/documents\nq=a%20b&limit=1\n"
        signature = base64.b64encode(hmac.new(b"test-secret", canonical.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(request.headers["Authorization"], f"On test-access:HmacSHA256:{signature}")


if __name__ == "__main__":
    unittest.main()
