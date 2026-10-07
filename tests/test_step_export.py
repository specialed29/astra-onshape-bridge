import json
import io
import os
import zipfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from starlette.testclient import TestClient
import app
import approvals
import step_export

D, W, E, T, F = (c * 24 for c in "abcde")
STEP = b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"


def translation(state="DONE"):
    return {"id": T, "documentId": D, "name": "Test part", "requestState": state,
            "resultExternalDataIds": [F] if state == "DONE" else []}


class StepTests(unittest.IsolatedAsyncioTestCase):
    def test_descriptors_and_filename(self):
        self.assertEqual(step_export.descriptors(translation("ACTIVE")), [])
        self.assertEqual(step_export.descriptors(translation("FAILED")), [])
        d = step_export.descriptors(translation())[0]
        self.assertEqual(d["download_path"], f"/api/exports/{T}/0")
        self.assertEqual(d["filename"], "Test-part.step")
        self.assertNotIn("/", step_export.filename("../../bad\r\nname"))

    def test_step_envelope_and_bad_payloads(self):
        self.assertEqual(step_export.validate_step(STEP)["bytes"], len(STEP))
        for data in [b"<html>Login</html>", b'{"error":"bad"}', b"PK", STEP[:-25]]:
            with self.assertRaises(ValueError):
                step_export.validate_step(data)

    def test_single_step_zip_and_rejection_cases(self):
        def zipped(files):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in files:
                    archive.writestr(name, data)
            return buffer.getvalue()
        blob = zipped([("Part Studio 1.step", STEP)])
        self.assertEqual(step_export.unpack_step(blob), STEP)
        # Names never become filesystem paths; member bytes are handled in memory.
        self.assertEqual(step_export.unpack_step(zipped([("../part.step", STEP)])), STEP)
        for files in [[], [("a.step", STEP), ("b.step", STEP)], [("part.html", STEP)],
                      [("a.step", b"not STEP")]]:
            with self.assertRaises(ValueError):
                step_export.unpack_step(zipped(files))
        with patch.object(step_export, "MAX_BYTES", 8):
            with self.assertRaises(ValueError):
                step_export.unpack_step(blob)
        with self.assertRaises(ValueError):
            step_export.unpack_step(b"PKbad archive")

    def test_redirect_allowlist(self):
        base = "https://cad.onshape.com"
        self.assertTrue(step_export.credential_target(base + "/api/file?q=x", base))
        self.assertFalse(step_export.credential_target("https://bucket.s3.amazonaws.com/f?X-Amz-Signature=abc", base))
        for url in ["http://cad.onshape.com/f", "https://evil.example/f",
                    "https://cad.onshape.com.evil.example/f", "https://user@cad.onshape.com/f",
                    "https://cad.onshape.com:8443/f", "https://bucket.s3.amazonaws.com/f",
                    "https://cad.onshape.com/f#x"]:
            with self.assertRaises(ValueError):
                step_export.credential_target(url, base)

    async def download_with_mock(self, handler, mode="hmac"):
        original = httpx.AsyncClient
        def factory(**kwargs):
            return original(**kwargs, transport=httpx.MockTransport(handler))
        with patch.object(step_export.httpx, "AsyncClient", side_effect=factory):
            return await step_export.download("https://cad.onshape.com", "/api/download",
                                              "access", "secret", mode, app.sign_onshape_request)

    async def test_redirect_resigns_and_strips_storage_auth(self):
        requests = []
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(307, headers={"location": "/api/file?fresh=yes"})
            if len(requests) == 2:
                return httpx.Response(302, headers={"location": "https://bucket.s3.amazonaws.com/f?X-Amz-Signature=abc"})
            return httpx.Response(200, content=STEP)
        self.assertEqual(await self.download_with_mock(handler), STEP)
        self.assertTrue(requests[0].headers["authorization"].startswith("On "))
        self.assertNotEqual(requests[0].headers["authorization"], requests[1].headers["authorization"])
        for key in ["authorization", "on-nonce", "date"]:
            self.assertNotIn(key, requests[2].headers)

    async def test_basic_auth_stays_off_storage(self):
        requests = []
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={"location": "https://bucket.s3.amazonaws.com/f?X-Amz-Signature=abc"})
            return httpx.Response(200, content=STEP)
        await self.download_with_mock(handler, mode="basic")
        self.assertTrue(requests[0].headers["authorization"].startswith("Basic "))
        self.assertNotIn("authorization", requests[1].headers)

    async def test_blocks_redirect_and_oversize(self):
        count = 0
        def bad(request):
            nonlocal count
            count += 1
            return httpx.Response(307, headers={"location": "https://evil.example/f"})
        with self.assertRaises(ValueError):
            await self.download_with_mock(bad)
        self.assertEqual(count, 1)
        with patch.object(step_export, "MAX_BYTES", 8):
            with self.assertRaises(ValueError):
                await self.download_with_mock(lambda r: httpx.Response(200, content=STEP))

    async def test_export_arguments_and_store_guard(self):
        with patch.dict(os.environ, {"ENABLE_CAD_WRITES": "true"}), \
             patch.object(app, "onshape_request", new_callable=AsyncMock) as provider:
            provider.return_value = translation("ACTIVE")
            await app.export_partstudio_step(D, W, E)
            self.assertEqual(provider.call_args.args, ("POST", f"/api/v10/partstudios/d/{D}/w/{W}/e/{E}/export/step"))
            self.assertEqual(provider.call_args.kwargs["body"], {"storeInDocument": False, "stepUnit": "MILLIMETER"})
            provider.reset_mock()
            with self.assertRaises(ValueError):
                await app.export_partstudio_step(D, W, E, True)
            provider.assert_not_called()


class DownloadRouteTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"CHAT_BEARER_TOKEN": "offline-chat",
                                          "ONSHAPE_ACCESS_KEY": "access", "ONSHAPE_SECRET_KEY": "secret",
                                          "OPENAI_API_KEY": "offline", "ENABLE_CAD_WRITES": "true",
                                          "PUBLIC_BASE_URL": "https://bridge.example.test"})
        self.env.start()
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.headers = {"Authorization": "Bearer offline-chat"}
        self.path = f"/api/exports/{T}/0"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        approvals.PENDING.clear()

    def test_requires_auth_before_provider(self):
        with patch.object(app, "get_translation", new_callable=AsyncMock) as provider:
            self.assertEqual(self.client.get(self.path).status_code, 401)
            provider.assert_not_called()

    def test_download_complete_file_and_cache_headers(self):
        with patch.object(app, "get_translation", new_callable=AsyncMock, return_value=translation()), \
             patch.object(step_export, "download", new_callable=AsyncMock, return_value=STEP):
            r = self.client.get(self.path, headers=self.headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content, STEP)
            self.assertEqual(r.headers["cache-control"], "no-store")
            self.assertEqual(r.headers["x-content-sha256"], step_export.validate_step(STEP)["sha256"])
            self.assertIn('filename="Test-part.step"', r.headers["content-disposition"])

    def test_incomplete_failed_missing_and_invalid(self):
        with patch.object(app, "get_translation", new_callable=AsyncMock) as provider, \
             patch.object(step_export, "download", new_callable=AsyncMock) as downloader:
            for state in ["ACTIVE", "FAILED"]:
                provider.return_value = translation(state)
                self.assertEqual(self.client.get(self.path, headers=self.headers).status_code, 409)
            provider.return_value = translation()
            self.assertEqual(self.client.get(f"/api/exports/{T}/5", headers=self.headers).status_code, 404)
            self.assertEqual(self.client.get(f"/api/exports/{T}/-1", headers=self.headers).status_code, 422)
            self.assertEqual(self.client.get("/api/exports/bad/0", headers=self.headers).status_code, 422)
            downloader.assert_not_called()

    def test_chat_export_approval_and_download_metadata(self):
        calls = []
        class FakeOpenAI:
            def __init__(self, **kwargs): self.responses = self
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def create(self, **kwargs):
                calls.append(kwargs)
                if isinstance(kwargs["input"], str):
                    return SimpleNamespace(id="resp_review", output_text="Review export.", output=[
                        SimpleNamespace(type="mcp_approval_request", id="mcpr_export", name="export_partstudio_step",
                                        server_label="onshape_cad",
                                        arguments=json.dumps({"document_id": D, "workspace_id": W, "element_id": E}))])
                return SimpleNamespace(id="resp_done", output_text="Download ready.", output=[
                    SimpleNamespace(type="mcp_call", name="get_translation", status="completed", error=None,
                                    output=json.dumps(translation()))])
        with patch.object(app, "AsyncOpenAI", FakeOpenAI):
            j = self.client.post("/api/chat", headers=self.headers, json={"message": "Export test part."}).json()
            self.assertFalse(j["tool_calls"])
            self.assertFalse(j["approval_requests"][0]["effective_arguments"]["store_in_document"])
            j = self.client.post("/api/chat", headers=self.headers,
                                 json={"approval_token": j["approval_token"], "approve": True}).json()
            self.assertEqual(j["downloads"][0]["download_path"], self.path)
            self.assertEqual(calls[1]["previous_response_id"], "resp_review")
