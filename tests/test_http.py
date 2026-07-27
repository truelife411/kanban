import http.client
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.parse import quote

import kanban as app


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "http.db")
        self.backup_dir = os.path.join(self.temp.name, "backups")
        self.attachments_dir = os.path.join(self.temp.name, "attachments")
        self.patches = [
            mock.patch.object(app, "DB_PATH", self.db_path),
            mock.patch.object(app, "BACKUP_DIR", self.backup_dir),
            mock.patch.object(app, "ATTACHMENTS_DIR", self.attachments_dir),
        ]
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

    def wire_request(self, request_bytes, shutdown_write=False):
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
        try:
            sock.sendall(request_bytes)
            if shutdown_write:
                sock.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(sock)
            response.begin()
            content = response.read()
            return response.status, dict(response.getheaders()), content
        finally:
            sock.close()

    def get_board(self):
        status, _, body = self.request("GET", "/api/board")
        self.assertEqual(status, 200)
        return json.loads(body)

    def card_payload(self, board, title="HTTP 卡片"):
        return {
            "column_id": board["columns"][0]["id"],
            "title": title,
            "description": "",
            "labels": "",
            "due_date": "",
            "priority": "medium",
            "expected_board_revision": board["revision"],
        }

    def create_card(self, title="HTTP 卡片"):
        board = self.get_board()
        status, _, body = self.request("POST", "/api/cards", self.card_payload(board, title))
        self.assertEqual(status, 201)
        return json.loads(body)

    def test_search_sort_query_and_invalid_sort(self):
        self.create_card("较早")
        self.create_card("较新")
        status, _, body = self.request("GET", "/api/search?all=1&sort=created_asc")
        self.assertEqual(status, 200)
        items = json.loads(body)["items"]
        self.assertEqual([item["title"] for item in items], ["较早", "较新"])
        status, _, body = self.request("GET", "/api/search?sort=invalid")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "INVALID_SORT")

    def create_draft(self):
        board = self.get_board()
        status, _, body = self.request(
            "POST",
            "/api/cards/drafts",
            {"column_id": board["columns"][0]["id"], "expected_board_revision": board["revision"]},
        )
        self.assertEqual(status, 201)
        return json.loads(body)["card"]

    def upload(self, card_id, file_name, payload, extra_headers=None):
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
            "X-File-Name": quote(file_name),
            "X-File-Type": "text/plain",
        }
        headers.update(extra_headers or {})
        return self.raw_request("POST", f"/api/cards/{card_id}/attachments", payload, headers)

    def test_board_snapshot(self):
        status, headers, body = self.request("GET", "/api/board")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["schema_version"], 6)
        self.assertEqual(len(data["columns"]), 3)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_description_html_round_trips_through_create_read_and_update(self):
        board = self.get_board()
        create_payload = self.card_payload(board, "换行往返")
        create_payload["description"] = "<div>第一段</div><div><br></div><div><strong>第三段</strong></div>"
        status, _, body = self.request("POST", "/api/cards", create_payload)
        self.assertEqual(status, 201)
        created = json.loads(body)
        card = created["card"]
        self.assertEqual(card["description"], "<p>第一段</p><p><br></p><p><strong>第三段</strong></p>")

        status, _, body = self.request("GET", f"/api/cards/{card['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["description"], card["description"])

        update_payload = self.card_payload({"columns": board["columns"], "revision": created["revision"]}, "换行往返更新")
        update_payload.update({
            "description": '第一行\n\n第三行<ul><li>列表<ol><li><span class="rt-fg-purple rt-bg-yellow"><em>嵌套</em></span></li></ol></li></ul>',
            "expected_version": card["version"],
        })
        status, _, body = self.request("PUT", f"/api/cards/{card['id']}", update_payload)
        self.assertEqual(status, 200)
        updated = json.loads(body)["card"]
        self.assertEqual(updated["description"], update_payload["description"])

        status, _, body = self.request("GET", "/api/board")
        self.assertEqual(status, 200)
        stored = next(item for item in json.loads(body)["cards"] if item["id"] == card["id"])
        self.assertEqual(stored["description"], update_payload["description"])

    def test_invalid_json_shape_returns_structured_error(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", "/api/cards", b"[]", {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(data["error"]["code"], "INVALID_JSON")

    def test_interrupted_json_body_is_rejected(self):
        board = self.get_board()
        payload = json.dumps(self.card_payload(board), ensure_ascii=False).encode("utf-8")
        request = (
            b"POST /api/cards HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(payload) + 5}\r\nConnection: close\r\n\r\n".encode("ascii")
            + payload
        )
        status, _, body = self.wire_request(request, shutdown_write=True)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "UPLOAD_INTERRUPTED")
        self.assertEqual(self.get_board()["cards"], [])

    def test_normal_writes_use_revision_and_version_and_stale_revision_conflicts(self):
        board = self.get_board()
        payload = self.card_payload(board)
        status, _, body = self.request("POST", "/api/cards", payload)
        self.assertEqual(status, 201)
        created = json.loads(body)
        card = created["card"]

        update = {
            **self.card_payload({"columns": board["columns"], "revision": created["revision"]}, "已更新"),
            "expected_version": card["version"],
        }
        status, _, body = self.request("PUT", f"/api/cards/{card['id']}", update)
        self.assertEqual(status, 200)
        updated = json.loads(body)
        self.assertEqual(updated["card"]["title"], "已更新")

        delete = {
            "expected_version": updated["card"]["version"],
            "expected_board_revision": updated["revision"],
        }
        status, _, body = self.request("DELETE", f"/api/cards/{card['id']}", delete)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        status, _, body = self.request("POST", "/api/cards", payload)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"]["code"], "BOARD_REVISION_CONFLICT")

    def test_missing_write_tokens_and_empty_delete_body_return_400(self):
        board = self.get_board()
        missing_revision = self.card_payload(board)
        missing_revision.pop("expected_board_revision")
        status, _, body = self.request("POST", "/api/cards", missing_revision)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "INVALID_BOARD_REVISION")

        created = self.create_card()
        card = created["card"]
        current_board = self.get_board()
        base_update = self.card_payload(current_board, "更新请求")

        status, _, body = self.request("PUT", f"/api/cards/{card['id']}", base_update)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "INVALID_VERSION")

        missing_update_revision = {**base_update, "expected_version": card["version"]}
        missing_update_revision.pop("expected_board_revision")
        status, _, body = self.request("PUT", f"/api/cards/{card['id']}", missing_update_revision)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "INVALID_BOARD_REVISION")

        status, _, body = self.raw_request(
            "DELETE",
            f"/api/cards/{card['id']}",
            b"",
            {"Content-Type": "application/json", "Content-Length": "0"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "INVALID_JSON")

    def test_attachment_http_flow_and_explicit_zero_length_upload(self):
        draft = self.create_draft()
        payload = "附件内容".encode("utf-8")
        status, _, body = self.upload(draft["id"], "需求.txt", payload)
        self.assertEqual(status, 201)
        attachment = json.loads(body)

        status, headers, body = self.raw_request("GET", f"/api/attachments/{attachment['id']}/download")
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])

        status, _, body = self.upload(draft["id"], "empty.bin", b"")
        self.assertEqual(status, 201)
        empty_attachment = json.loads(body)
        self.assertEqual(empty_attachment["size"], 0)
        self.assertTrue(os.path.isfile(app.attachment_path(draft["id"], "empty.bin")))

        status, _, body = self.raw_request(
            "DELETE",
            f"/api/attachments/{attachment['id']}",
            headers={"X-Attachment-Version": str(attachment["version"])},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_attachment_framing_rejections_and_interrupted_upload_cleanup(self):
        draft = self.create_draft()
        path = f"/api/cards/{draft['id']}/attachments"
        common = f"Host: 127.0.0.1\r\nX-File-Name: test.bin\r\nConnection: close\r\n"

        status, _, body = self.wire_request(
            f"POST {path} HTTP/1.1\r\n{common}\r\n".encode("ascii")
        )
        self.assertEqual(status, 411)
        self.assertEqual(json.loads(body)["error"]["code"], "CONTENT_LENGTH_REQUIRED")

        status, _, body = self.wire_request(
            f"POST {path} HTTP/1.1\r\n{common}Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n".encode("ascii")
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "TRANSFER_ENCODING_NOT_ALLOWED")

        status, _, body = self.wire_request(
            f"POST {path} HTTP/1.1\r\n{common}Content-Length: invalid\r\n\r\n".encode("ascii")
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "INVALID_CONTENT_LENGTH")

        interrupted_name = "interrupted.bin"
        interrupted_request = (
            f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"X-File-Name: {interrupted_name}\r\nContent-Length: 10\r\nConnection: close\r\n\r\nabc"
        ).encode("ascii")
        status, _, body = self.wire_request(interrupted_request, shutdown_write=True)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "UPLOAD_INTERRUPTED")

        status, _, body = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])
        files = [os.path.join(root, name) for root, _, names in os.walk(self.attachments_dir) for name in names]
        self.assertEqual(files, [])

    def test_permanent_delete_active_and_archived_cards_with_conflicts_and_attachments(self):
        first = self.create_card("第一张")["card"]
        target_result = self.create_card("永久删除")
        target = target_result["card"]
        self.create_card("第三张")
        status, _, body = self.upload(target["id"], "gone.txt", b"gone")
        self.assertEqual(status, 201)
        stale = {"expected_version": target["version"], "expected_board_revision": target_result["revision"] - 1}
        status, _, body = self.request("DELETE", f"/api/cards/{target['id']}/permanent", stale)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"]["code"], "BOARD_REVISION_CONFLICT")
        current = self.get_board()
        current_target = next(card for card in current["cards"] if card["id"] == target["id"])
        status, _, body = self.request("DELETE", f"/api/cards/{target['id']}/permanent", {
            "expected_version": current_target["version"], "expected_board_revision": current["revision"],
        })
        self.assertEqual(status, 200)
        deleted = json.loads(body)
        self.assertTrue(deleted["ok"])
        board = self.get_board()
        self.assertEqual([(card["id"], card["position"]) for card in board["cards"]], [(first["id"], 0), (board["cards"][1]["id"], 1)])
        self.assertFalse(os.path.exists(app.attachment_directory(target["id"])))

        archived_result = self.create_card("归档后永久删除")
        archived = archived_result["card"]
        status, _, body = self.request("DELETE", f"/api/cards/{archived['id']}", {
            "expected_version": archived["version"], "expected_board_revision": archived_result["revision"],
        })
        self.assertEqual(status, 200)
        archive_response = json.loads(body)
        status, _, body = self.request("DELETE", f"/api/cards/{archived['id']}/permanent", {
            "expected_version": archive_response["version"], "expected_board_revision": archive_response["revision"],
        })
        self.assertEqual(status, 200)
        status, _, body = self.request("GET", f"/api/cards/{archived['id']}")
        self.assertEqual(status, 404)

    def test_permanent_delete_requires_both_tokens(self):
        card = self.create_card()["card"]
        board = self.get_board()
        for payload, code in (({"expected_version": card["version"]}, "INVALID_BOARD_REVISION"), ({"expected_board_revision": board["revision"]}, "INVALID_VERSION")):
            with self.subTest(code=code):
                status, _, body = self.request("DELETE", f"/api/cards/{card['id']}/permanent", payload)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], code)

    def test_plan_card_http_add_reorder_remove_and_board_snapshot(self):
        first_result = self.create_card("今日 A")
        second_result = self.create_card("今日 B")
        first, second = first_result["card"], second_result["card"]
        board = self.get_board()

        status, _, body = self.request("PUT", f"/api/cards/{first['id']}/plan", {
            "planned_date": "2026-07-26",
            "expected_version": first["version"],
            "expected_board_revision": board["revision"],
        })
        self.assertEqual(status, 200)
        first_planned = json.loads(body)
        self.assertEqual(first_planned["card"]["planned_date"], "2026-07-26")
        self.assertEqual(first_planned["card"]["planned_position"], 0)

        status, _, body = self.request("PUT", f"/api/cards/{second['id']}/plan", {
            "planned_date": "2026-07-26",
            "position": 0,
            "expected_version": second["version"],
            "expected_board_revision": first_planned["revision"],
        })
        self.assertEqual(status, 200)
        second_planned = json.loads(body)
        board = self.get_board()
        planned = sorted(
            (card for card in board["cards"] if card["planned_date"] == "2026-07-26"),
            key=lambda card: card["planned_position"],
        )
        self.assertEqual(
            [(card["title"], card["planned_position"]) for card in planned],
            [("今日 B", 0), ("今日 A", 1)],
        )

        current_first = next(card for card in board["cards"] if card["id"] == first["id"])
        status, _, body = self.request("PUT", f"/api/cards/{first['id']}/plan", {
            "planned_date": "",
            "expected_version": current_first["version"],
            "expected_board_revision": second_planned["revision"],
        })
        self.assertEqual(status, 200)
        removed = json.loads(body)["card"]
        self.assertEqual(removed["planned_date"], "")
        self.assertIsNone(removed["planned_position"])

    def test_create_card_with_planned_date_supports_today_quick_add(self):
        board = self.get_board()
        payload = self.card_payload(board, "快速今日")
        payload["due_date"] = "2026-07-26"
        payload["planned_date"] = "2026-07-26"
        status, _, body = self.request("POST", "/api/cards", payload)
        self.assertEqual(status, 201)
        card = json.loads(body)["card"]
        self.assertEqual(card["due_date"], "2026-07-26")
        self.assertEqual(card["planned_date"], "2026-07-26")
        self.assertEqual(card["planned_position"], 0)

    def test_plan_card_http_rejects_stale_version(self):
        created = self.create_card("冲突今日")
        card = created["card"]
        board = self.get_board()
        status, _, body = self.request("PUT", f"/api/cards/{card['id']}/plan", {
            "planned_date": "2026-07-26",
            "expected_version": card["version"] + 1,
            "expected_board_revision": board["revision"],
        })
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"]["code"], "CARD_VERSION_CONFLICT")

    def test_static_assets_require_revalidation(self):
        for path in (
            "/", "/index.html", "/static/style.css", "/static/theme-init.js",
            "/static/kanban.js", "/static/js/state.js",
        ):
            with self.subTest(path=path):
                status, headers, _ = self.raw_request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-cache, max-age=0, must-revalidate")

    def test_options_has_empty_204_allow_and_security_headers(self):
        status, headers, body = self.raw_request("OPTIONS", "/api/cards/1")
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertEqual(headers["Content-Length"], "0")
        self.assertEqual(headers["Allow"], "GET, PUT, DELETE, OPTIONS")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        csp = headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)

        status, headers, body = self.raw_request("OPTIONS", "/api/cards/1/plan")
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertEqual(headers["Allow"], "PUT, OPTIONS")

        status, headers, body = self.raw_request("OPTIONS", "/api/cards/1/permanent")
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertEqual(headers["Allow"], "DELETE, OPTIONS")

    def test_backup_with_missing_attachment_returns_incomplete_backup_json(self):
        created = self.create_card("缺失附件备份")
        card = created["card"]
        status, _, body = self.upload(card["id"], "missing.txt", b"content")
        self.assertEqual(status, 201)
        attachment = json.loads(body)
        os.remove(app.attachment_path(card["id"], attachment["file_name"]))

        status, headers, body = self.raw_request("GET", "/api/backup/check")
        self.assertEqual(status, 422)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        check_error = json.loads(body)["error"]
        self.assertEqual(check_error["code"], "INCOMPLETE_BACKUP")
        self.assertEqual(check_error["details"]["missing_attachments"], [f"attachments/{card['id']}/missing.txt"])

        status, headers, body = self.raw_request("GET", "/api/backup")
        self.assertEqual(status, 422)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "INCOMPLETE_BACKUP")
        self.assertEqual(error["details"]["missing_attachments"], [f"attachments/{card['id']}/missing.txt"])

    def test_backup_is_valid_zip_with_download_headers(self):
        status, headers, body = self.raw_request("GET", "/api/backup")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertRegex(headers["Content-Disposition"], r'^attachment; filename="kanban-backup-\d{8}-\d{6}\.zip"$')
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(body)))
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertIn("kanban.db", archive.namelist())
            self.assertIn("manifest.json", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["format"], "kanban-full-backup")


if __name__ == "__main__":
    unittest.main()
