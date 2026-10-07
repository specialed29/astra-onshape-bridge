import os
import unittest
from unittest.mock import patch
from starlette.testclient import TestClient
from offline import OfflineGate


class OfflineTests(unittest.TestCase):
    def test_offline_never_dispatches_http(self):
        async def forbidden(scope, receive, send):
            raise AssertionError("Paused bridge must not execute application code.")
        with patch.dict(os.environ, {"BRIDGE_OFFLINE": "true"}):
            client = TestClient(OfflineGate(forbidden))
            for method, path in [("GET", "/"), ("POST", "/api/chat"), ("POST", "/mcp"),
                                 ("GET", "/mcp"), ("DELETE", "/mcp"), ("GET", "/mcp-info"),
                                 ("GET", "/api/exports/" + "a"*24 + "/0")]:
                response = client.request(method, path)
                self.assertEqual(response.status_code, 503)
                self.assertTrue(response.json()["offline"])
            response = client.get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["app"])
