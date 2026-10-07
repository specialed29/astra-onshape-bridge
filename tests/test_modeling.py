import asyncio
from copy import deepcopy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
import app
import approvals
import cad

DID, WID, EID = "a" * 24, "b" * 24, "c" * 24


class BuilderTests(unittest.TestCase):
    def test_rectangle_meters_and_editable_dimensions(self):
        f = cad.rectangle("Plate outline", "Top", 40, 30)
        self.assertEqual(len(f["entities"]), 4)
        self.assertAlmostEqual(f["entities"][0]["endParam"], .04)
        self.assertAlmostEqual(f["entities"][1]["endParam"], .03)
        self.assertIn('makeId("Top")', f["parameters"][0]["queries"][0]["queryString"])
        f["featureId"] = "sketch_1"
        edited = cad.edit_dimension(f, "width", 50)
        self.assertEqual(edited["featureId"], "sketch_1")
        self.assertEqual(edited["entities"], f["entities"])
        width = next(c for c in edited["constraints"] if c["entityId"] == "astra_width")
        self.assertEqual(next(p["expression"] for p in width["parameters"] if p["parameterId"] == "length"), "50 mm")
        self.assertNotEqual(f, edited)

    def test_circle_and_extrude(self):
        f = cad.circle("Round", "Front", 20, 5, -5)
        g = f["entities"][0]["geometry"]
        self.assertEqual(g["radius"], .01)
        self.assertEqual(g["xCenter"], .005)
        self.assertEqual(g["yCenter"], -.005)
        extrude = cad.extrude("Cylinder", "sketch_1", 12)
        edited = cad.edit_dimension(extrude, "depth", 16)
        self.assertEqual(next(p["expression"] for p in edited["parameters"] if p["parameterId"] == "depth"), "16 mm")
        self.assertEqual(next(p["value"] for p in edited["parameters"] if p["parameterId"] == "operationType"), "NEW")

    def test_bad_dimensions_and_ids(self):
        for value in [0, -1, 10001, float("nan"), float("inf"), True]:
            with self.assertRaises(ValueError):
                cad.circle("Circle", "Top", value)
        for identifier in ["../documents", "z"*24, "a"*23]:
            with self.assertRaises(ValueError):
                cad.cad_id(identifier)
        with self.assertRaises(ValueError):
            cad.extrude("Part", 'bad");evil', 10)
        with self.assertRaises(ValueError):
            cad.rectangle("Part", "Any plane", 10, 10)

    def test_microversion_guard(self):
        f = cad.circle("Circle", "Top", 20)
        with self.assertRaises(ValueError):
            cad.feature_payload(f, {})
        payload = cad.feature_payload(f, {"sourceMicroversion": "snapshot", "libraryVersion": 2232})
        self.assertTrue(payload["rejectMicroversionSkew"])
        self.assertEqual(payload["sourceMicroversion"], "snapshot")
        self.assertEqual(payload["libraryVersion"], 2232)

    def test_dont_edit_foreign_or_subtractive_features(self):
        with self.assertRaises(ValueError):
            cad.edit_dimension({"featureType": "newSketch", "constraints": []}, "width", 20)
        f = cad.extrude("Part", "sketch_1", 10)
        next(p for p in f["parameters"] if p["parameterId"] == "operationType")["value"] = "REMOVE"
        with self.assertRaises(ValueError):
            cad.edit_dimension(f, "depth", 15)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        approvals.PENDING.clear()
        self.env = patch.dict(os.environ, {
            "OPENAI_API_KEY": "offline-key", "ONSHAPE_ACCESS_KEY": "offline-access",
            "ONSHAPE_SECRET_KEY": "offline-secret", "CHAT_BEARER_TOKEN": "offline-chat",
            "PUBLIC_BASE_URL": "https://bridge.example.test", "ENABLE_CAD_WRITES": "true",
        })
        self.env.start()
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.headers = {"Authorization": "Bearer offline-chat"}
        self.requests = []
        outer = self

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.responses = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def create(self, **kwargs):
                outer.requests.append(deepcopy(kwargs))
                if isinstance(kwargs["input"], str):
                    return SimpleNamespace(
                        id="resp_pending", output_text="Review creation.",
                        output=[SimpleNamespace(type="mcp_approval_request", id="mcpr_one",
                                                name="create_document", arguments='{"name":"Test part"}',
                                                server_label="onshape_cad")],
                    )
                return SimpleNamespace(id="resp_decision", output_text="Decision processed.", output=[])

        self.openai = patch.object(app, "AsyncOpenAI", FakeOpenAI)
        self.openai.start()

    def tearDown(self):
        self.openai.stop()
        self.client.__exit__(None, None, None)
        self.env.stop()
        approvals.PENDING.clear()

    def pending(self):
        r = self.client.post("/api/chat", headers=self.headers, json={"message": "Create a test document."})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_write_requires_native_approval_and_shows_defaults(self):
        response = self.pending()
        self.assertTrue(response["approval_token"])
        tool = self.requests[0]["tools"][0]
        self.assertEqual(tool["require_approval"], {"never": {"tool_names": app.READ_TOOLS}})
        self.assertEqual(tool["allowed_tools"], app.READ_TOOLS + app.MODELING_WRITE_TOOLS)
        self.assertNotIn("add_feature_raw", tool["allowed_tools"])
        self.assertNotIn("export_partstudio_step", tool["allowed_tools"])
        self.assertEqual(response["approval_requests"][0]["effective_arguments"],
                         {"name": "Test part", "is_public": False})
        self.assertEqual(len(self.requests), 1)  # No continuation, therefore no execution.

    def test_approve_uses_server_record_and_is_single_use(self):
        token = self.pending()["approval_token"]
        r = self.client.post("/api/chat", headers=self.headers, json={"approval_token": token, "approve": True})
        self.assertEqual(r.status_code, 200, r.text)
        continuation = self.requests[1]
        self.assertEqual(continuation["previous_response_id"], "resp_pending")
        self.assertEqual(continuation["input"], [
            {"type": "mcp_approval_response", "approval_request_id": "mcpr_one", "approve": True},
        ])
        self.assertTrue(continuation["tools"][0]["authorization"])
        r = self.client.post("/api/chat", headers=self.headers, json={"approval_token": token, "approve": True})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.requests), 2)

    def test_deny(self):
        token = self.pending()["approval_token"]
        r = self.client.post("/api/chat", headers=self.headers, json={"approval_token": token, "approve": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.requests[1]["input"][0]["approve"])

    def test_cannot_forge_or_override_approval(self):
        for body, code in [
            ({"approval_token": "forged", "approve": True}, 409),
            ({"approval_token": "forged", "approve": "true"}, 400),
            ({"approval_token": "forged", "approve": True, "previous_response_id": "resp_other"}, 400),
            ({"approval_token": "forged", "approve": True, "message": "change dimensions"}, 400),
        ]:
            self.assertEqual(self.client.post("/api/chat", headers=self.headers, json=body).status_code, code)
        self.assertFalse(self.requests)

    def test_expiry_and_disabled_switch(self):
        token = self.pending()["approval_token"]
        approvals.PENDING[token]["expires"] = 0
        self.assertEqual(self.client.post("/api/chat", headers=self.headers,
                                         json={"approval_token": token, "approve": True}).status_code, 409)
        token = self.pending()["approval_token"]
        with patch.dict(os.environ, {"ENABLE_CAD_WRITES": "false"}):
            self.assertEqual(self.client.post("/api/chat", headers=self.headers,
                                             json={"approval_token": token, "approve": True}).status_code, 403)


class MutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rectangle_write_payload_and_feature_state(self):
        calls = []

        async def fake(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET":
                return {"sourceMicroversion": "test-microversion", "features": [], "libraryVersion": 2232}
            return {"feature": {"featureId": "new_sketch"}, "featureState": {"featureStatus": "OK"}}

        with patch.dict(os.environ, {"ENABLE_CAD_WRITES": "true"}), patch.object(app, "onshape_request", fake):
            result = await app.create_rectangle_sketch(DID, WID, EID, 40, 30)
        self.assertTrue(result["ok"])
        self.assertEqual([c[0] for c in calls], ["GET", "POST"])
        self.assertEqual(calls[1][2]["body"]["feature"]["featureType"], "newSketch")
        self.assertTrue(calls[1][2]["body"]["rejectMicroversionSkew"])

    async def test_regeneration_error_not_reported_as_success(self):
        async def fake(*args, **kwargs):
            return {"feature": {"featureId": "persisted"}, "featureState": {"featureStatus": "ERROR"}}
        with patch.dict(os.environ, {"ENABLE_CAD_WRITES": "true"}), patch.object(app, "onshape_request", fake):
            result = await app.write_feature(DID, WID, EID, cad.circle("C", "Top", 10),
                                             {"sourceMicroversion": "m"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["feature_id"], "persisted")

    async def test_disabled_typed_tools_do_not_reach_network(self):
        async def fail(*args, **kwargs):
            self.fail("Unexpected network request.")
        with patch.dict(os.environ, {"ENABLE_CAD_WRITES": "false"}), patch.object(app, "onshape_request", fail):
            with self.assertRaises(PermissionError):
                await app.create_circle_sketch(DID, WID, EID, 20)

    async def test_raw_write_disabled_even_with_modeling_enabled(self):
        with patch.dict(os.environ, {"ENABLE_CAD_WRITES": "true", "ENABLE_RAW_FEATURE_WRITES": "false"}):
            with self.assertRaises(PermissionError):
                await app.add_feature_raw(DID, WID, EID, '{"feature":{}}')


if __name__ == "__main__":
    unittest.main()
