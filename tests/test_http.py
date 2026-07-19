import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import app


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "http.db")
        self.backup_dir = os.path.join(self.temp.name, "backups")
        self.attachments_dir = os.path.join(self.temp.name, "attachments")
        self.patches = [mock.patch.object(app, "DB_PATH", self.db_path), mock.patch.object(app, "BACKUP_DIR", self.backup_dir), mock.patch.object(app, "ATTACHMENTS_DIR", self.attachments_dir)]
        for patch in self.patches:
            patch.start()
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def raw_request(self, method, path, body=b"", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        content = response.read()
        result = (response.status, dict(response.getheaders()), content)
        connection.close()
        return result

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        content = response.read()
        result = (response.status, dict(response.getheaders()), content)
        connection.close()
        return result

    def test_board_snapshot(self):
        status, headers, body = self.request("GET", "/api/board")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["schema_version"], 4)
        self.assertEqual(len(data["columns"]), 3)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_invalid_json_shape_returns_structured_error(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", "/api/cards", b"[]", {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(data["error"]["code"], "INVALID_JSON")

    def test_create_card_and_conflict(self):
        _, _, board_body = self.request("GET", "/api/board")
        board = json.loads(board_body)
        payload = {"column_id": board["columns"][0]["id"], "title": "HTTP 卡片", "description": "", "labels": "", "due_date": "", "priority": "medium", "expected_board_revision": board["revision"]}
        status, _, body = self.request("POST", "/api/cards", payload)
        self.assertEqual(status, 201)
        created = json.loads(body)
        payload["expected_board_revision"] = board["revision"]
        status, _, body = self.request("POST", "/api/cards", payload)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"]["code"], "BOARD_REVISION_CONFLICT")
        self.assertEqual(created["card"]["title"], "HTTP 卡片")

    def test_attachment_http_flow(self):
        _, _, board_body = self.request("GET", "/api/board")
        board = json.loads(board_body)
        column_id = board["columns"][0]["id"]
        status, _, body = self.request("POST", "/api/cards/drafts", {"column_id": column_id, "expected_board_revision": board["revision"]})
        self.assertEqual(status, 201)
        draft = json.loads(body)["card"]
        payload = "附件内容".encode("utf-8")
        status, _, body = self.raw_request("POST", f"/api/cards/{draft['id']}/attachments", payload, {"Content-Type": "application/octet-stream", "X-File-Name": "%E9%9C%80%E6%B1%82.txt", "X-File-Type": "text/plain"})
        self.assertEqual(status, 201)
        attachment = json.loads(body)
        status, headers, body = self.raw_request("GET", f"/api/attachments/{attachment['id']}/download")
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])
        status, _, body = self.raw_request("DELETE", f"/api/attachments/{attachment['id']}", headers={"X-Attachment-Version": str(attachment["version"])})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_backup_is_zip(self):
        status, headers, body = self.raw_request("GET", "/api/backup")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertTrue(body.startswith(b"PK"))


if __name__ == "__main__":
    unittest.main()
