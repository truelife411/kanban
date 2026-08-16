# -*- coding: utf-8 -*-
"""看板系统入口:HTTP 服务器、路由与启动逻辑。

业务逻辑按层拆分:config/errors/utils/sync/security/db/attachments/board/backup。
本文件保持 `python kanban.py` 可运行,并 re-export 各层公开名字以兼容 `import kanban`。
"""

import json
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

import backup
import board
import config
import db
import security
from attachments import (save_attachment, delete_attachment, list_attachments, get_attachment)
from board import (create_column, update_column, delete_column, reorder_columns,
                   create_card_draft, finalize_card_draft, delete_card_draft,
                   create_card, update_card, plan_card, move_card, archive_card,
                   permanently_delete_card, restore_card, copy_card,
                   batch_archive_cards, batch_restore_cards, batch_permanently_delete_cards,
                   search_cards_page, export_data, import_preview, import_replace)
from backup import (backup_readiness, create_full_backup, inspect_full_backup,
                    stage_full_backup, restore_full_backup)
from db import (init_db, get_conn, board_revision, list_columns, list_cards, get_card, get_board)
from errors import ApiError, fail, require_string
from security import attachment_path
from sync import MAINTENANCE_GATE
from utils import now_iso


class Handler(BaseHTTPRequestHandler):
    server_version = "KanbanServer/2.0"

    def log_message(self, fmt, *args): sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))
    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8"); self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self._security_headers(); self.end_headers(); self.wfile.write(body)

    def _send_error(self, error):
        payload = {"error": {"code": error.code, "message": error.message}}
        if error.details is not None: payload["error"]["details"] = error.details
        self._send_json(payload, error.status)

    def _send_text(self, data, status=200, content_type="text/plain; charset=utf-8", cache_control=None):
        body = data.encode("utf-8") if isinstance(data, str) else data; self.send_response(status)
        self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body)))
        if cache_control: self.send_header("Cache-Control", cache_control)
        self._security_headers(); self.end_headers(); self.wfile.write(body)

    def _content_length(self, maximum=None):
        if self.headers.get_all("Transfer-Encoding"):
            raise ApiError("不支持 Transfer-Encoding，请使用 Content-Length", 400, "TRANSFER_ENCODING_NOT_ALLOWED")
        values = self.headers.get_all("Content-Length") or []
        if not values:
            raise ApiError("请求必须提供 Content-Length", 411, "CONTENT_LENGTH_REQUIRED")
        if len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]):
            raise ApiError("Content-Length 无效", 400, "INVALID_CONTENT_LENGTH")
        length = int(values[0])
        if maximum is not None and length > maximum:
            raise ApiError("请求体过大", 413, "PAYLOAD_TOO_LARGE")
        return length

    def _read_json_body(self, maximum=config.MAX_JSON_BODY):
        length = self._content_length(maximum)
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json": raise ApiError("请求必须使用 application/json", 415, "UNSUPPORTED_MEDIA_TYPE")
        if length == 0: raise ApiError("请求体不能为空", 400, "INVALID_JSON")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ApiError("请求内容中断", 400, "UPLOAD_INTERRUPTED")
            value = json.loads(raw.decode("utf-8"))
        except ApiError:
            raise
        except (ValueError, UnicodeDecodeError):
            raise ApiError("请求体不是有效 JSON", 400, "INVALID_JSON")
        return value

    def _serve_static(self, rel_path):
        rel_path = rel_path.replace("\\", "/").lstrip("/"); file_path = os.path.join(config.STATIC_DIR, "index.html") if rel_path in ("", "index.html") else os.path.normpath(os.path.join(config.STATIC_DIR, rel_path))
        if os.path.commonpath((os.path.abspath(config.STATIC_DIR), os.path.abspath(file_path))) != os.path.abspath(config.STATIC_DIR): return self._send_text("Forbidden", 403)
        if not os.path.isfile(file_path): return self._send_text("Not Found", 404)
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml"}.get(os.path.splitext(file_path)[1].lower(), "application/octet-stream")
        with open(file_path, "rb") as handle: self._send_text(handle.read(), content_type=ctype, cache_control="no-cache, max-age=0, must-revalidate")

    def _send_attachment(self, row):
        path = attachment_path(row["card_id"], row["file_name"])
        if not os.path.isfile(path):
            raise ApiError("附件文件已不存在", 404, "ATTACHMENT_FILE_MISSING")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(row["file_name"]))
        self._security_headers(); self.end_headers()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk: break
                self.wfile.write(chunk)

    def _send_backup(self):
        zip_path = create_full_backup("download")
        try:
            filename = "kanban-backup-%s.zip" % datetime.now().strftime("%Y%m%d-%H%M%S")
            self.send_response(200); self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(os.path.getsize(zip_path))); self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
            self._security_headers(); self.end_headers()
            with open(zip_path, "rb") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk: break
                    self.wfile.write(chunk)
        finally:
            try: os.remove(zip_path)
            except OSError: pass

    def _route(self, method):
        parsed = urlparse(self.path); path, query = parsed.path, parse_qs(parsed.query)
        if method == "GET" and (path in ("/", "/index.html") or path.startswith("/static/")): return self._serve_static(path[len("/static/"):] if path.startswith("/static/") else path)
        if not path.startswith("/api/"): return self._send_text("Not Found", 404)
        try: self._route_api(method, path, query)
        except ApiError as error: self._send_error(error)
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower(): self._send_error(ApiError("数据库繁忙，请稍后重试", 409, "DATABASE_BUSY"))
            else: self.log_error("database error: %s", error); self._send_error(ApiError("服务器内部错误", 500, "INTERNAL_ERROR"))
        except Exception as error: self.log_error("internal error: %r", error); self._send_error(ApiError("服务器内部错误", 500, "INTERNAL_ERROR"))

    def _route_api(self, method, path, query):
        mutation = method in ("POST", "PUT", "DELETE")
        maintenance = path in ("/api/import", "/api/import/zip", "/api/backup")
        gate = MAINTENANCE_GATE.exclusive() if maintenance else MAINTENANCE_GATE.shared()
        with gate:
            conn = get_conn()
            try:
                self._dispatch_api(conn, method, path, query, mutation)
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _dispatch_api(self, conn, method, path, query, mutation=False):
        if path == "/api/board" and method == "GET": return self._send_json(get_board(conn))
        if path == "/api/columns" and method == "GET": return self._send_json(list_columns(conn))
        if path == "/api/columns" and method == "POST": return self._send_json(create_column(conn, self._read_json_body()), 201)
        if path == "/api/columns/reorder" and method == "POST": return self._send_json(reorder_columns(conn, self._read_json_body()))
        match = re.fullmatch(r"/api/columns/(\d+)", path)
        if match:
            cid = int(match.group(1))
            if method == "PUT": return self._send_json(update_column(conn, cid, self._read_json_body()))
            if method == "DELETE": return self._send_json(delete_column(conn, cid, self._read_json_body()))
        if path == "/api/cards" and method == "GET":
            raw = query.get("column_id", [None])[0]
            if raw is not None and not raw.isdigit(): fail("column_id 无效", field="column_id")
            return self._send_json(list_cards(conn, int(raw) if raw is not None else None))
        if path == "/api/cards" and method == "POST": return self._send_json(create_card(conn, self._read_json_body()), 201)
        if path == "/api/cards/drafts" and method == "POST": return self._send_json(create_card_draft(conn, self._read_json_body()), 201)
        if path == "/api/cards/batch/archive" and method == "POST": return self._send_json(batch_archive_cards(conn, self._read_json_body()))
        if path == "/api/cards/batch/restore" and method == "POST": return self._send_json(batch_restore_cards(conn, self._read_json_body()))
        if path == "/api/cards/batch/permanent-delete" and method == "POST": return self._send_json(batch_permanently_delete_cards(conn, self._read_json_body()))
        match = re.fullmatch(r"/api/cards/(\d+)", path)
        if match:
            cid = int(match.group(1))
            if method == "GET": return self._send_json(get_card(conn, cid))
            if method == "PUT": return self._send_json(update_card(conn, cid, self._read_json_body()))
            if method == "DELETE": return self._send_json(archive_card(conn, cid, self._read_json_body()))
        match = re.fullmatch(r"/api/cards/(\d+)/permanent", path)
        if match and method == "DELETE": return self._send_json(permanently_delete_card(conn, int(match.group(1)), self._read_json_body()))
        for suffix, action, verb in (("move", move_card, "PUT"), ("plan", plan_card, "PUT"), ("restore", restore_card, "POST"), ("copy", copy_card, "POST"), ("finalize", finalize_card_draft, "PUT")):
            match = re.fullmatch(r"/api/cards/(\d+)/%s" % suffix, path)
            if match and method == verb: return self._send_json(action(conn, int(match.group(1)), self._read_json_body()))
        match = re.fullmatch(r"/api/cards/(\d+)/draft", path)
        if match and method == "DELETE":
            header = self.headers.get("X-Card-Version")
            if not header or not header.isdigit(): raise ApiError("草稿版本无效", 400, "INVALID_VERSION")
            return self._send_json(delete_card_draft(conn, int(match.group(1)), int(header)))
        match = re.fullmatch(r"/api/cards/(\d+)/attachments", path)
        if match:
            card_id = int(match.group(1))
            if method == "GET": return self._send_json(list_attachments(conn, card_id))
            if method == "POST":
                length = self._content_length(config.MAX_ATTACHMENT_BODY)
                encoded_name = self.headers.get("X-File-Name", "")
                try: file_name = unquote(encoded_name, encoding="utf-8", errors="strict")
                except UnicodeDecodeError: raise ApiError("文件名编码无效", 400, "INVALID_FILE_NAME")
                replace = query.get("replace", ["0"])[0] in ("1", "true")
                version_header = self.headers.get("X-Attachment-Version")
                if replace and (not version_header or not version_header.isdigit()): raise ApiError("覆盖附件需要有效版本", 400, "INVALID_VERSION")
                expected_version = int(version_header) if version_header and version_header.isdigit() else None
                attachment = save_attachment(conn, card_id, file_name, self.headers.get("X-File-Type", ""), self.rfile, length, replace, expected_version)
                return self._send_json(attachment, 200 if replace else 201)
        match = re.fullmatch(r"/api/attachments/(\d+)(?:/(download))?", path)
        if match:
            attachment_id = int(match.group(1))
            if method == "GET" and match.group(2) == "download": return self._send_attachment(get_attachment(conn, attachment_id))
            if method == "DELETE":
                version_header = self.headers.get("X-Attachment-Version")
                if not version_header or not version_header.isdigit(): raise ApiError("附件版本无效", 400, "INVALID_VERSION")
                return self._send_json(delete_attachment(conn, attachment_id, int(version_header)))
        if path == "/api/search" and method == "GET":
            return self._send_json(search_cards_page(conn, query.get("q", [""])[0], query.get("from", [None])[0], query.get("to", [None])[0], query.get("priority", [None])[0], query.get("all", ["0"])[0].lower() in ("1", "true"), query.get("cursor", [None])[0], query.get("limit", [50])[0], query.get("sort", ["archived_desc"])[0], query.get("created_from", [None])[0], query.get("created_to", [None])[0], query.get("updated_from", [None])[0], query.get("updated_to", [None])[0]))
        if path == "/api/export" and method == "GET":
            body = json.dumps(export_data(conn), ensure_ascii=False, indent=2).encode("utf-8"); filename = "kanban-export-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename); self._security_headers(); self.end_headers(); self.wfile.write(body); return
        if path == "/api/backup/check" and method == "GET": return self._send_json(backup_readiness(conn))
        if path == "/api/backup" and method == "GET": return self._send_backup()
        if path == "/api/import/zip/preview" and method == "POST":
            length = self._content_length(config.MAX_ZIP_BODY)
            if length == 0: raise ApiError("ZIP 文件不能为空", 400, "INVALID_BACKUP")
            return self._send_json(stage_full_backup(self.rfile, length))
        if path == "/api/import/zip" and method == "POST":
            request = self._read_json_body()
            return self._send_json(restore_full_backup(conn, require_string(request.get("token"), "token", 1, 100), request.get("expected_board_revision")))
        if path == "/api/import/preview" and method == "POST": return self._send_json(import_preview(self._read_json_body(config.MAX_IMPORT_BODY)))
        if path == "/api/import" and method == "POST": return self._send_json(import_replace(conn, self._read_json_body(config.MAX_IMPORT_BODY)))
        raise ApiError("未知的 API 路径", 404, "NOT_FOUND")

    def _allowed_methods(self, path):
        if path in ("/api/board", "/api/search", "/api/export", "/api/backup", "/api/backup/check"):
            return "GET, OPTIONS"
        if path == "/api/columns":
            return "GET, POST, OPTIONS"
        if path == "/api/columns/reorder":
            return "POST, OPTIONS"
        if re.fullmatch(r"/api/columns/\d+", path):
            return "PUT, DELETE, OPTIONS"
        if path == "/api/cards":
            return "GET, POST, OPTIONS"
        if path == "/api/cards/drafts":
            return "POST, OPTIONS"
        if path in ("/api/cards/batch/archive", "/api/cards/batch/restore", "/api/cards/batch/permanent-delete"):
            return "POST, OPTIONS"
        if re.fullmatch(r"/api/cards/\d+", path):
            return "GET, PUT, DELETE, OPTIONS"
        if re.fullmatch(r"/api/cards/\d+/permanent", path):
            return "DELETE, OPTIONS"
        if re.fullmatch(r"/api/cards/\d+/(?:restore|copy)", path):
            return "POST, OPTIONS"
        if re.fullmatch(r"/api/cards/\d+/(?:move|plan|finalize)", path):
            return "PUT, OPTIONS"
        if re.fullmatch(r"/api/cards/\d+/draft", path):
            return "DELETE, OPTIONS"
        if re.fullmatch(r"/api/cards/\d+/attachments", path):
            return "GET, POST, OPTIONS"
        if re.fullmatch(r"/api/attachments/\d+", path):
            return "DELETE, OPTIONS"
        if re.fullmatch(r"/api/attachments/\d+/download", path):
            return "GET, OPTIONS"
        if path in ("/api/import/zip/preview", "/api/import/zip", "/api/import/preview", "/api/import"):
            return "POST, OPTIONS"
        return "OPTIONS"

    def do_GET(self): self._route("GET")
    def do_POST(self): self._route("POST")
    def do_PUT(self): self._route("PUT")
    def do_DELETE(self): self._route("DELETE")
    def do_OPTIONS(self):
        path = urlparse(self.path).path
        self.send_response(204)
        self.send_header("Allow", self._allowed_methods(path))
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()


AUTO_ARCHIVE_STOP = threading.Event()


def run_auto_archive_once(now=None):
    with MAINTENANCE_GATE.shared():
        conn = get_conn()
        try:
            return db.auto_archive_completed_cards(conn, now)
        finally:
            conn.close()


def auto_archive_worker(interval=3600):
    while not AUTO_ARCHIVE_STOP.wait(interval):
        try:
            run_auto_archive_once()
        except Exception as error:
            print("自动归档检查失败：%s" % error, file=sys.stderr)


def start_auto_archive_worker(interval=3600):
    AUTO_ARCHIVE_STOP.clear()
    run_auto_archive_once()
    thread = threading.Thread(target=auto_archive_worker, args=(interval,), daemon=True, name="kanban-auto-archive")
    thread.start()
    return thread


def main():
    init_db(); start_auto_archive_worker(); server = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print("=" * 60); print("  看板系统已启动（仅限本机访问）"); print("  访问地址: http://127.0.0.1:%d" % config.PORT)
    print("  数据库: %s" % config.DB_PATH); print("  按 Ctrl+C 停止"); print("=" * 60)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n正在停止...")
    finally:
        AUTO_ARCHIVE_STOP.set()
        server.shutdown()
        server.server_close()


# ---- 兼容 re-export:保持 `import kanban as app` 可访问各层公开名字 ----
from config import *  # noqa: E402,F401,F403
from errors import *  # noqa: E402,F401,F403
from utils import *  # noqa: E402,F401,F403
from security import *  # noqa: E402,F401,F403
from sync import *  # noqa: E402,F401,F403
from db import *  # noqa: E402,F401,F403
from attachments import *  # noqa: E402,F401,F403
from board import *  # noqa: E402,F401,F403
from backup import *  # noqa: E402,F401,F403


if __name__ == "__main__":
    main()
