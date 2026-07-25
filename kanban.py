# -*- coding: utf-8 -*-
"""看板系统后端：Python 标准库 + SQLite。"""

import base64
import errno
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from html import escape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "kanban.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
ATTACHMENTS_DIR = os.path.join(BASE_DIR, "attachments")
HOST = "127.0.0.1"
PORT = int(os.environ.get("KANBAN_PORT", "8000"))
SCHEMA_VERSION = 5
EXPORT_VERSION = 2
MAX_JSON_BODY = 1024 * 1024
MAX_IMPORT_BODY = 10 * 1024 * 1024
MAX_ATTACHMENT_BODY = int(os.environ.get("KANBAN_MAX_ATTACHMENT_BODY", str(100 * 1024 * 1024)))
MAX_ZIP_BODY = int(os.environ.get("KANBAN_MAX_ZIP_BODY", str(1024 * 1024 * 1024)))
VALID_PRIORITY = ("high", "medium", "low")
MIN_FREE_BYTES = int(os.environ.get("KANBAN_MIN_FREE_BYTES", str(512 * 1024 * 1024)))
MAX_ZIP_ENTRIES = int(os.environ.get("KANBAN_MAX_ZIP_ENTRIES", "100000"))
MAX_ZIP_RATIO = int(os.environ.get("KANBAN_MAX_ZIP_RATIO", "200"))
MAX_ZIP_EXPANDED_BYTES = int(os.environ.get("KANBAN_MAX_ZIP_EXPANDED_BYTES", "0"))
RESTORE_TOKENS = {}
RESTORE_TOKEN_LOCK = threading.Lock()
RESTORE_TOKEN_TTL = 60 * 60
DRAFT_TTL = 24 * 60 * 60
CLEANUP_AGE = 24 * 60 * 60
MAX_DATABASE_BACKUPS = 10
DATABASE_BACKUP_NAME = re.compile(r"^kanban-[A-Za-z0-9_-]+-(\d{8}-\d{6}-\d{6})\.db$")
MAINTENANCE_REPORT = {"missing": [], "orphans": [], "size_mismatch": [], "cleanup": []}


class MaintenanceGate:
    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.readers = 0
        self.writer = None
        self.writer_depth = 0
        self.waiting_writers = 0
        self.local = threading.local()

    @contextmanager
    def shared(self):
        ident = threading.get_ident()
        if self.writer == ident:
            yield
            return
        depth = getattr(self.local, "shared_depth", 0)
        with self.condition:
            if depth == 0:
                while self.writer is not None or self.waiting_writers:
                    self.condition.wait()
                self.readers += 1
            self.local.shared_depth = depth + 1
        try:
            yield
        finally:
            with self.condition:
                depth = self.local.shared_depth - 1
                self.local.shared_depth = depth
                if depth == 0:
                    self.readers -= 1
                    self.condition.notify_all()

    @contextmanager
    def exclusive(self):
        ident = threading.get_ident()
        with self.condition:
            if self.writer == ident:
                self.writer_depth += 1
            else:
                if getattr(self.local, "shared_depth", 0):
                    raise RuntimeError("不能从共享维护门禁升级为独占门禁")
                self.waiting_writers += 1
                try:
                    while self.writer is not None or self.readers:
                        self.condition.wait()
                    self.writer = ident
                    self.writer_depth = 1
                finally:
                    self.waiting_writers -= 1
        try:
            yield
        finally:
            with self.condition:
                self.writer_depth -= 1
                if self.writer_depth == 0:
                    self.writer = None
                    self.condition.notify_all()


MAINTENANCE_GATE = MaintenanceGate()
DB_MAINTENANCE_LOCK = threading.RLock()


def ensure_free_space(path, incoming=0):
    root = path if os.path.isdir(path) else os.path.dirname(path) or BASE_DIR
    os.makedirs(root, exist_ok=True)
    if shutil.disk_usage(root).free - incoming < MIN_FREE_BYTES:
        raise ApiError("磁盘剩余空间不足，请清理空间后重试", 507, "INSUFFICIENT_DISK_SPACE")


def stream_copy_limited(source, output, expected=None, disk_path=None, hasher=None):
    written = 0
    remaining = expected
    while remaining is None or remaining > 0:
        chunk = source.read(65536 if remaining is None else min(65536, remaining))
        if not chunk:
            if remaining:
                raise ApiError("上传内容中断", 400, "UPLOAD_INTERRUPTED")
            break
        if disk_path:
            ensure_free_space(disk_path, len(chunk))
        output.write(chunk)
        if hasher:
            hasher.update(chunk)
        written += len(chunk)
        if remaining is not None:
            remaining -= len(chunk)
    return written


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.create_function("column_name_key", 1, column_name_key)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


class ApiError(Exception):
    def __init__(self, message, status=400, code="BAD_REQUEST", details=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details


def fail(message, status=422, code="VALIDATION_ERROR", field=None):
    raise ApiError(message, status, code, {"field": field} if field else None)


def require_object(value):
    if not isinstance(value, dict):
        fail("JSON 请求体必须是对象", 400, "INVALID_JSON")
    return value


def require_int(value, field, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        fail("%s 必须是大于等于 %s 的整数" % (field, minimum), field=field)
    return value


def require_string(value, field, minimum=0, maximum=1000, trim=True):
    if not isinstance(value, str):
        fail("%s 必须是字符串" % field, field=field)
    value = value.strip() if trim else value
    if len(value) < minimum:
        fail("%s 不能为空" % field, field=field)
    if len(value) > maximum:
        fail("%s 不能超过 %s 个字符" % (field, maximum), field=field)
    return value


def validate_priority(value):
    if value not in VALID_PRIORITY:
        fail("优先级必须是 high、medium 或 low", field="priority")
    return value


def validate_due_date(value):
    value = require_string(value, "due_date", 0, 16)
    if not value:
        return ""
    if len(value) not in (10, 16):
        fail("截止日期格式必须是 YYYY-MM-DD 或 YYYY-MM-DD HH:MM", field="due_date")
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M" if len(value) == 16 else "%Y-%m-%d")
    except ValueError:
        fail("截止日期无效", field="due_date")
    return value


class SafeHtmlParser(HTMLParser):
    allowed = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u", "s", "span"}
    blocked_tags = {"script", "style", "iframe", "object", "svg"}
    text_color_classes = {"rt-fg-default", "rt-fg-red", "rt-fg-yellow", "rt-fg-green", "rt-fg-blue", "rt-fg-purple"}
    highlight_classes = {"rt-bg-red", "rt-bg-yellow", "rt-bg-green", "rt-bg-blue", "rt-bg-purple"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = []
        self.stack = [(None, (), self.root)]
        self.blocked = 0

    def _span_classes(self, attrs):
        values = dict(attrs).get("class", "").split()
        foreground = next((value for value in values if value in self.text_color_classes), None)
        highlight = next((value for value in values if value in self.highlight_classes), None)
        return tuple(value for value in (foreground, highlight) if value)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.blocked:
            if tag in self.blocked_tags:
                self.blocked += 1
            return
        if tag in self.blocked_tags:
            self.blocked = 1
            return
        if tag == "br":
            self.stack[-1][2].append(("br", (), []))
        elif tag in self.allowed or tag == "div":
            classes = self._span_classes(attrs) if tag == "span" else ()
            node = (tag, classes, [])
            self.stack[-1][2].append(node)
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() != "br":
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.blocked:
            if tag in self.blocked_tags:
                self.blocked = max(0, self.blocked - 1)
            return
        if tag == "br" or (tag not in self.allowed and tag != "div"):
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.blocked:
            self.stack[-1][2].append(data)

    @staticmethod
    def _has_content(nodes):
        for node in nodes:
            if isinstance(node, str):
                if node.strip():
                    return True
            elif node[0] == "br" or SafeHtmlParser._has_content(node[2]):
                return True
        return False

    def _canonical_nodes(self, nodes):
        result = []
        for node in nodes:
            if isinstance(node, str):
                result.append(node)
                continue
            tag, classes, children = node
            children = self._canonical_nodes(children)
            if tag == "span":
                if not self._has_content(children):
                    continue
                if not classes:
                    result.extend(children)
                    continue
                flattened = []
                for child in children:
                    if not isinstance(child, str) and child[0] == "span" and child[1] == classes:
                        flattened.extend(child[2])
                    else:
                        flattened.append(child)
                children = flattened
            current = (tag, classes, children)
            if tag == "span" and result and not isinstance(result[-1], str) and result[-1][0] == "span" and result[-1][1] == classes:
                previous = result[-1]
                result[-1] = ("span", classes, previous[2] + children)
            else:
                result.append(current)
        return result

    def _render(self, nodes, parent=None):
        output = []
        for node in nodes:
            if isinstance(node, str):
                output.append(escape(node, quote=False))
                continue
            tag, classes, children = node
            if tag == "br":
                output.append("<br>")
                continue
            rendered = self._render(children, tag)
            if tag == "div" and parent is not None:
                output.append(rendered)
                continue
            if tag == "span":
                output.append("<span class=\"%s\">%s</span>" % (" ".join(classes), rendered))
                continue
            normalized_tag = "p" if tag == "div" else tag
            if normalized_tag == "p" and not self._has_content(children):
                rendered = "<br>"
            output.append("<%s>%s</%s>" % (normalized_tag, rendered, normalized_tag))
        return "".join(output)

    @property
    def output(self):
        return self._render(self._canonical_nodes(self.root))


def sanitize_description(value):
    parser = SafeHtmlParser()
    parser.feed(require_string(value, "description", 0, 100000, trim=False))
    parser.close()
    return parser.output.strip()


def validate_attachment_name(value):
    if not isinstance(value, str) or not value or len(value) > 255:
        fail("文件名不能为空且不能超过 255 个字符", field="file_name")
    name = value
    if name in (".", "..") or any(char in name for char in '/\\:*?"<>|') or name.endswith((" ", ".")):
        fail("文件名包含当前系统不支持的字符，请重命名文件后再上传", field="file_name")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        fail("文件名包含当前系统不支持的字符，请重命名文件后再上传", field="file_name")
    if len(name.encode("utf-8")) > 255:
        raise ApiError("文件名过长，请缩短后再上传", 422, "ATTACHMENT_NAME_TOO_LONG", {"field": "file_name"})
    stem = name.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL"} | {"COM%s" % i for i in range(1, 10)} | {"LPT%s" % i for i in range(1, 10)}
    if stem in reserved:
        fail("文件名是系统保留名称，请重命名后再上传", field="file_name")
    return name


def column_name_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def attachment_name_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def attachment_directory(card_id):
    return os.path.join(ATTACHMENTS_DIR, str(card_id))


def attachment_path(card_id, file_name):
    root = os.path.abspath(ATTACHMENTS_DIR)
    path = os.path.abspath(os.path.join(attachment_directory(card_id), file_name))
    if os.path.commonpath((root, path)) != root:
        raise ApiError("附件路径无效", 400, "INVALID_ATTACHMENT_PATH")
    return path


def row_to_attachment(row):
    result = {key: row[key] for key in ("id", "card_id", "file_name", "content_type", "size", "created_at", "updated_at", "version")}
    result["file_missing"] = not os.path.isfile(attachment_path(row["card_id"], row["file_name"]))
    return result


def list_attachments(conn, card_id):
    if conn.execute("SELECT 1 FROM cards WHERE id=?", (card_id,)).fetchone() is None:
        raise ApiError("卡片不存在", 404, "CARD_NOT_FOUND")
    return [row_to_attachment(row) for row in conn.execute("SELECT * FROM attachments WHERE card_id=? ORDER BY id", (card_id,))]


def get_attachment(conn, attachment_id):
    row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if row is None:
        raise ApiError("附件不存在", 404, "ATTACHMENT_NOT_FOUND")
    return row


def _attachment_conflict(existing):
    raise ApiError("同名附件已存在，是否覆盖？", 409, "ATTACHMENT_EXISTS", {"attachment": row_to_attachment(existing)})


def _file_in_use_error(error):
    if isinstance(error, PermissionError):
        return ApiError("文件正在被其他程序使用，请关闭相关程序后重试", 409, "ATTACHMENT_FILE_IN_USE")
    return error


def save_attachment(conn, card_id, file_name, content_type, input_stream, content_length, replace=False, expected_version=None):
    file_name = validate_attachment_name(file_name)
    if content_length < 0:
        raise ApiError("Content-Length 无效", 400, "INVALID_CONTENT_LENGTH")
    directory = attachment_directory(card_id)
    os.makedirs(directory, exist_ok=True)
    ensure_free_space(directory, min(content_length, 65536))
    fd, temp_path = tempfile.mkstemp(prefix=".upload-", dir=directory)
    os.close(fd)
    rollback_path = None
    target_path = attachment_path(card_id, file_name)
    installed = False
    committed = False
    existing = None
    try:
        with open(temp_path, "wb") as output:
            written = stream_copy_limited(input_stream, output, content_length, directory)
        with MAINTENANCE_GATE.shared(), DB_MAINTENANCE_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            try:
                card = conn.execute("SELECT id,archived,is_draft FROM cards WHERE id=?", (card_id,)).fetchone()
                if card is None:
                    raise ApiError("卡片不存在", 404, "CARD_NOT_FOUND")
                if card["archived"]:
                    raise ApiError("归档卡片不能修改附件，请先恢复", 409, "CARD_ARCHIVED")
                existing = conn.execute("SELECT * FROM attachments WHERE card_id=? AND name_key=?", (card_id, attachment_name_key(file_name))).fetchone()
                if existing is not None and not replace:
                    _attachment_conflict(existing)
                if replace and existing is None:
                    raise ApiError("要覆盖的附件已不存在，请重新上传", 409, "ATTACHMENT_VERSION_CONFLICT")
                if existing is not None and (expected_version is None or existing["version"] != expected_version):
                    raise ApiError("附件已在其他页面被更新，请重新确认后再覆盖", 409, "ATTACHMENT_VERSION_CONFLICT")
                if existing is not None:
                    old_path = attachment_path(card_id, existing["file_name"])
                    if os.path.isfile(old_path):
                        rollback_path = old_path + ".rollback-" + uuid.uuid4().hex
                        os.replace(old_path, rollback_path)
                os.replace(temp_path, target_path)
                installed = True
                ts = now_iso()
                if existing is None:
                    try:
                        cursor = conn.execute("INSERT INTO attachments (card_id,file_name,name_key,content_type,size,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (card_id, file_name, attachment_name_key(file_name), content_type or "", written, ts, ts))
                    except sqlite3.IntegrityError:
                        current = conn.execute("SELECT * FROM attachments WHERE card_id=? AND name_key=?", (card_id, attachment_name_key(file_name))).fetchone()
                        if current is not None:
                            _attachment_conflict(current)
                        raise
                    attachment_id = cursor.lastrowid
                else:
                    conn.execute("UPDATE attachments SET file_name=?,name_key=?,content_type=?,size=?,updated_at=?,version=version+1 WHERE id=?", (file_name, attachment_name_key(file_name), content_type or "", written, ts, existing["id"]))
                    attachment_id = existing["id"]
                if card["is_draft"]:
                    conn.execute("UPDATE cards SET updated_at=? WHERE id=?", (ts, card_id))
                conn.commit()
                committed = True
            except Exception:
                conn.rollback()
                if installed and os.path.isfile(target_path):
                    os.remove(target_path)
                installed = False
                if rollback_path and os.path.isfile(rollback_path) and existing is not None:
                    os.replace(rollback_path, attachment_path(card_id, existing["file_name"]))
                    rollback_path = None
                raise
            if rollback_path and os.path.isfile(rollback_path):
                try:
                    os.remove(rollback_path)
                except OSError:
                    MAINTENANCE_REPORT["cleanup"].append(rollback_path)
        return row_to_attachment(get_attachment(conn, attachment_id))
    except OSError as error:
        if error.errno in (errno.ENAMETOOLONG, errno.EINVAL):
            raise ApiError("文件名或路径过长，请缩短文件名后重试", 422, "ATTACHMENT_NAME_TOO_LONG")
        raise _file_in_use_error(error)
    except Exception as error:
        if not committed:
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
        raise _file_in_use_error(error)


def delete_attachment(conn, attachment_id, expected_version=None):
    trash = None
    path = None
    row = None
    committed = False
    try:
        with MAINTENANCE_GATE.shared(), DB_MAINTENANCE_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = get_attachment(conn, attachment_id)
                card = conn.execute("SELECT archived,is_draft FROM cards WHERE id=?", (row["card_id"],)).fetchone()
                if card and card["archived"]:
                    raise ApiError("归档卡片不能删除附件，请先恢复", 409, "CARD_ARCHIVED")
                if expected_version is None or row["version"] != expected_version:
                    raise ApiError("附件已在其他页面被更新，请刷新后重试", 409, "ATTACHMENT_VERSION_CONFLICT")
                path = attachment_path(row["card_id"], row["file_name"])
                if os.path.isfile(path):
                    trash = path + ".deleting-" + uuid.uuid4().hex
                    os.replace(path, trash)
                conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
                if card and card["is_draft"]:
                    conn.execute("UPDATE cards SET updated_at=? WHERE id=?", (now_iso(), row["card_id"]))
                conn.commit()
                committed = True
            except Exception:
                conn.rollback()
                if trash and path and os.path.isfile(trash):
                    os.replace(trash, path)
                    trash = None
                raise
            if trash and os.path.isfile(trash):
                try:
                    os.remove(trash)
                except OSError:
                    MAINTENANCE_REPORT["cleanup"].append(trash)
            try:
                os.rmdir(attachment_directory(row["card_id"]))
            except OSError:
                pass
        return {"ok": True}
    except Exception as error:
        if not committed and trash and path and os.path.isfile(trash):
            try:
                os.replace(trash, path)
            except OSError:
                pass
        raise _file_in_use_error(error)


def normalize_card_fields(data):
    require_object(data)
    return {
        "title": require_string(data.get("title"), "title", 1, 300),
        "description": sanitize_description(data.get("description", "")),
        "labels": require_string(data.get("labels", ""), "labels", 0, 2000),
        "due_date": validate_due_date(data.get("due_date", "")),
        "priority": validate_priority(data.get("priority", "medium")),
    }


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _column_exists(conn, table, name):
    return any(row["name"] == name for row in conn.execute("PRAGMA table_info(%s)" % table))


def _rotate_database_backups():
    try:
        names = []
        for name in os.listdir(BACKUP_DIR):
            match = DATABASE_BACKUP_NAME.fullmatch(name)
            if match and os.path.isfile(os.path.join(BACKUP_DIR, name)):
                names.append((match.group(1), name))
        names.sort()
    except OSError as error:
        sys.stderr.write("数据库备份清理失败：%s\n" % error)
        return
    for _, name in names[:-MAX_DATABASE_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, name))
        except OSError as error:
            sys.stderr.write("数据库备份清理失败（%s）：%s\n" % (name, error))


def create_backup(prefix="auto", required=True):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    while True:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        final_path = os.path.join(BACKUP_DIR, "kanban-%s-%s.db" % (prefix, stamp))
        if not os.path.exists(final_path):
            break
        time.sleep(0.000001)
    fd, temp_path = tempfile.mkstemp(prefix=".kanban-", suffix=".tmp", dir=BACKUP_DIR)
    os.close(fd)
    try:
        source, target = get_conn(), sqlite3.connect(temp_path)
        try:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("备份完整性检查失败")
        finally:
            target.close()
            source.close()
        os.replace(temp_path, final_path)
        if prefix != "snapshot":
            _rotate_database_backups()
        return final_path
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        if required:
            raise
        return None


def cleanup_expired_drafts(conn, now=None):
    cutoff = datetime.fromtimestamp((now or datetime.now()).timestamp() - DRAFT_TTL).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("SELECT id FROM cards WHERE is_draft=1 AND updated_at<?", (cutoff,)).fetchall()
    cleaned = 0
    with MAINTENANCE_GATE.shared(), DB_MAINTENANCE_LOCK:
        for row in rows:
            directory = attachment_directory(row["id"])
            rollback = directory + ".draft-expired-" + uuid.uuid4().hex
            moved = False
            try:
                if os.path.isdir(directory):
                    os.replace(directory, rollback)
                    moved = True
                with transaction(conn):
                    cursor = conn.execute("DELETE FROM cards WHERE id=? AND is_draft=1", (row["id"],))
                if cursor.rowcount:
                    cleaned += 1
                if moved:
                    shutil.rmtree(rollback, ignore_errors=True)
            except Exception:
                if moved and os.path.isdir(rollback) and not os.path.exists(directory):
                    os.replace(rollback, directory)
                raise
    return cleaned


def reconcile_attachments(conn):
    report = {"missing": [], "orphans": [], "size_mismatch": [], "cleanup": []}
    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
    for name in os.listdir(ATTACHMENTS_DIR):
        if ".permanent-delete-" not in name:
            continue
        rollback = os.path.join(ATTACHMENTS_DIR, name)
        if not os.path.isdir(rollback):
            continue
        original_name = name.split(".permanent-delete-", 1)[0]
        if not original_name.isdigit():
            continue
        original = attachment_directory(int(original_name))
        card_exists = conn.execute("SELECT 1 FROM cards WHERE id=?", (int(original_name),)).fetchone() is not None
        try:
            if card_exists and not os.path.exists(original):
                os.replace(rollback, original)
                report["cleanup"].append(original)
            elif not card_exists:
                shutil.rmtree(rollback)
                report["cleanup"].append(rollback)
        except OSError:
            pass
    known = {}
    for row in conn.execute("SELECT id,card_id,file_name,size FROM attachments"):
        path = attachment_path(row["card_id"], row["file_name"])
        known[os.path.abspath(path)] = row
        if not os.path.isfile(path):
            report["missing"].append(path)
        elif os.path.getsize(path) != row["size"]:
            report["size_mismatch"].append(path)
    cutoff = time.time() - CLEANUP_AGE
    for root, _, files in os.walk(ATTACHMENTS_DIR):
        for name in files:
            path = os.path.abspath(os.path.join(root, name))
            if ".upload-" in name and os.path.getmtime(path) < cutoff:
                try:
                    os.remove(path)
                    report["cleanup"].append(path)
                except OSError:
                    pass
                continue
            if ".rollback-" in name:
                original = path.split(".rollback-", 1)[0]
                if os.path.isfile(original):
                    try:
                        os.remove(path)
                        report["cleanup"].append(path)
                    except OSError:
                        pass
                elif original in known:
                    try:
                        os.replace(path, original)
                        report["cleanup"].append(original)
                    except OSError:
                        pass
                continue
            if ".deleting-" in name:
                original = path.split(".deleting-", 1)[0]
                if original in known and not os.path.exists(original):
                    try:
                        os.replace(path, original)
                        report["cleanup"].append(original)
                    except OSError:
                        pass
                else:
                    try:
                        os.remove(path)
                        report["cleanup"].append(path)
                    except OSError:
                        pass
                continue
            if path not in known:
                report["orphans"].append(path)
    MAINTENANCE_REPORT.clear()
    MAINTENANCE_REPORT.update(report)
    if any(report.values()):
        print("附件维护检查：缺失 %d，孤儿 %d，大小异常 %d，已清理/恢复 %d" %
              (len(report["missing"]), len(report["orphans"]), len(report["size_mismatch"]), len(report["cleanup"])))
    return report


def init_db():
    conn = get_conn()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        if not _table_exists(conn, "columns"):
            with transaction(conn):
                conn.execute("CREATE TABLE columns (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,position INTEGER NOT NULL DEFAULT 0,deleted_at TEXT,version INTEGER NOT NULL DEFAULT 1)")
                conn.execute("""CREATE TABLE cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,column_id INTEGER NOT NULL,title TEXT NOT NULL,
                    description TEXT DEFAULT '',labels TEXT DEFAULT '',due_date TEXT DEFAULT '',
                    priority TEXT NOT NULL DEFAULT 'medium',position INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    completed_at TEXT,archived_at TEXT,archive_reason TEXT,is_draft INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (column_id) REFERENCES columns(id) ON DELETE CASCADE)""")
                conn.execute("CREATE TABLE board_state (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL)")
                conn.execute("""CREATE TABLE attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,card_id INTEGER NOT NULL,file_name TEXT NOT NULL,
                    name_key TEXT NOT NULL,content_type TEXT DEFAULT '',size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE,
                    UNIQUE(card_id,name_key))""")
                conn.execute("CREATE INDEX idx_attachments_card ON attachments(card_id,id)")
                conn.execute("CREATE INDEX idx_columns_active_position ON columns(deleted_at,position,id)")
                conn.execute("CREATE INDEX idx_cards_active_position ON cards(is_draft,archived,column_id,position,id)")
                conn.execute("CREATE INDEX idx_cards_archived_updated ON cards(is_draft,archived,updated_at,id)")
                conn.execute("CREATE INDEX idx_cards_auto_archive ON cards(is_draft,archived,column_id,completed_at)")
                conn.execute("CREATE INDEX idx_cards_draft_updated ON cards(is_draft,updated_at,id)")
                conn.execute("INSERT INTO board_state VALUES (1,1,?)", (now_iso(),))
                conn.executemany("INSERT INTO columns (name,position) VALUES (?,?)", [("待办", 0), ("进行中", 1), ("已完成", 2)])
                conn.execute("PRAGMA user_version = 5")
        else:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("数据库版本高于当前程序支持版本")
            if version < SCHEMA_VERSION:
                create_backup("pre-migration")
            if version < 1:
                with transaction(conn):
                    if not _column_exists(conn, "columns", "deleted_at"):
                        conn.execute("ALTER TABLE columns ADD COLUMN deleted_at TEXT")
                    if not _column_exists(conn, "columns", "version"):
                        conn.execute("ALTER TABLE columns ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
                    if not _column_exists(conn, "cards", "version"):
                        conn.execute("ALTER TABLE cards ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_columns_active_position ON columns(deleted_at,position,id)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_active_position ON cards(archived,column_id,position,id)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_archived_updated ON cards(archived,updated_at,id)")
                    conn.execute("PRAGMA user_version = 1")
                version = 1
            if version < 2:
                with transaction(conn):
                    conn.execute("CREATE TABLE IF NOT EXISTS board_state (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL)")
                    conn.execute("INSERT OR IGNORE INTO board_state VALUES (1,1,?)", (now_iso(),))
                    conn.execute("PRAGMA user_version = 2")
                version = 2
            if version < 3:
                with transaction(conn):
                    if not _column_exists(conn, "cards", "completed_at"):
                        conn.execute("ALTER TABLE cards ADD COLUMN completed_at TEXT")
                    if not _column_exists(conn, "cards", "archived_at"):
                        conn.execute("ALTER TABLE cards ADD COLUMN archived_at TEXT")
                    if not _column_exists(conn, "cards", "archive_reason"):
                        conn.execute("ALTER TABLE cards ADD COLUMN archive_reason TEXT")
                    ts = now_iso()
                    conn.execute("""UPDATE cards SET completed_at=? WHERE archived=0 AND column_id IN
                                  (SELECT id FROM columns WHERE deleted_at IS NULL AND trim(name)='已完成')""", (ts,))
                    conn.execute("UPDATE cards SET archived_at=updated_at,archive_reason='legacy' WHERE archived=1")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_auto_archive ON cards(archived,column_id,completed_at)")
                    conn.execute("PRAGMA user_version = 3")
                version = 3
            if version < 4:
                with transaction(conn):
                    if not _column_exists(conn, "cards", "is_draft"):
                        conn.execute("ALTER TABLE cards ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0")
                    conn.execute("""CREATE TABLE IF NOT EXISTS attachments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,card_id INTEGER NOT NULL,file_name TEXT NOT NULL,
                        name_key TEXT NOT NULL,content_type TEXT DEFAULT '',size INTEGER NOT NULL,
                        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
                        FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE,
                        UNIQUE(card_id,name_key))""")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_card ON attachments(card_id,id)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_draft_updated ON cards(is_draft,updated_at,id)")
                    conn.execute("PRAGMA user_version = 4")
                version = 4
            if version < 5:
                with transaction(conn):
                    for row in conn.execute("SELECT id,description FROM cards"):
                        normalized = sanitize_description(row["description"] or "")
                        if normalized != row["description"]:
                            conn.execute("UPDATE cards SET description=? WHERE id=?", (normalized, row["id"]))
                    conn.execute("PRAGMA user_version = 5")
                version = 5
            with transaction(conn):
                if not _column_exists(conn, "cards", "is_draft"):
                    conn.execute("ALTER TABLE cards ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_columns_active_position ON columns(deleted_at,position,id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_active_position ON cards(is_draft,archived,column_id,position,id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_archived_updated ON cards(is_draft,archived,updated_at,id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_auto_archive ON cards(is_draft,archived,column_id,completed_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_draft_updated ON cards(is_draft,updated_at,id)")
        if conn.execute("SELECT COUNT(*) FROM columns WHERE deleted_at IS NULL").fetchone()[0] == 0:
            with transaction(conn):
                conn.executemany("INSERT INTO columns (name,position) VALUES (?,?)", [("待办", 0), ("进行中", 1), ("已完成", 2)])
                bump_revision(conn)
        cleanup_expired_drafts(conn)
        reconcile_attachments(conn)
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("数据库完整性检查失败")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("数据库外键检查失败")
    finally:
        conn.close()
    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)


def board_revision(conn):
    return conn.execute("SELECT revision FROM board_state WHERE id=1").fetchone()[0]


def bump_revision(conn):
    conn.execute("UPDATE board_state SET revision=revision+1,updated_at=? WHERE id=1", (now_iso(),))
    return board_revision(conn)


def check_revision(conn, expected, required=False):
    if expected is None:
        if required:
            raise ApiError("缺少看板版本，请刷新后重试", 400, "INVALID_BOARD_REVISION")
        return
    require_int(expected, "expected_board_revision", 1)
    actual = board_revision(conn)
    if expected != actual:
        raise ApiError("看板已发生变化，请刷新后重试", 409, "BOARD_REVISION_CONFLICT", {"expected": expected, "actual": actual})


def check_version(row, expected, required=False):
    if expected is None:
        if required:
            raise ApiError("缺少内容版本，请刷新后重试", 400, "INVALID_VERSION")
        return
    require_int(expected, "expected_version", 1)
    if expected != row["version"]:
        raise ApiError("内容已被其他操作修改，请刷新后重试", 409, "VERSION_CONFLICT", {"expected": expected, "actual": row["version"]})


def row_to_column(row):
    return {"id": row["id"], "name": row["name"], "position": row["position"], "deleted_at": row["deleted_at"], "version": row["version"]}


def row_to_card(row):
    result = {key: row[key] for key in ("id", "column_id", "title", "description", "labels", "due_date", "priority", "position", "archived", "created_at", "updated_at", "completed_at", "archived_at", "archive_reason", "version")}
    if "attachment_count" in row.keys():
        result["attachment_count"] = row["attachment_count"]
    if "column_name" in row.keys():
        result.update(column_name=row["column_name"], column_deleted=bool(row["column_deleted"]))
        if "restore_column_id" in row.keys():
            result["restore_column_id"] = row["restore_column_id"]
    return result


def list_columns(conn, include_deleted=False):
    sql = "SELECT * FROM columns" + ("" if include_deleted else " WHERE deleted_at IS NULL") + " ORDER BY position,id"
    return [row_to_column(r) for r in conn.execute(sql)]


def list_cards(conn, column_id=None, archived=0):
    sql, params = """SELECT cards.*,(SELECT COUNT(*) FROM attachments WHERE attachments.card_id=cards.id) AS attachment_count
                     FROM cards WHERE archived=? AND is_draft=0""", [archived]
    if column_id is not None:
        sql += " AND column_id=?"
        params.append(column_id)
    return [row_to_card(r) for r in conn.execute(sql + " ORDER BY position,id", params)]


def get_card(conn, cid, include_draft=False):
    sql = """SELECT cards.*,(SELECT COUNT(*) FROM attachments WHERE attachments.card_id=cards.id) AS attachment_count
             FROM cards WHERE id=?"""
    if not include_draft:
        sql += " AND is_draft=0"
    row = conn.execute(sql, (cid,)).fetchone()
    if row is None:
        raise ApiError("卡片不存在", 404, "CARD_NOT_FOUND")
    return row_to_card(row)


def is_completed_column(row):
    return row is not None and row["name"].strip() == "已完成" and row["deleted_at"] is None


def auto_archive_completed_cards(conn, now=None):
    now = now or datetime.now()
    cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    completed = conn.execute("SELECT * FROM columns WHERE deleted_at IS NULL AND trim(name)='已完成' LIMIT 1").fetchone()
    if completed is None:
        return 0
    rows = conn.execute("SELECT id FROM cards WHERE column_id=? AND archived=0 AND is_draft=0 AND completed_at IS NOT NULL AND completed_at<=?", (completed["id"], cutoff)).fetchall()
    if not rows:
        return 0
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    with transaction(conn):
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute("UPDATE cards SET archived=1,archived_at=?,archive_reason='auto_completed',updated_at=?,version=version+1 WHERE id IN (%s)" % placeholders, [ts, ts] + ids)
        _rewrite_positions(conn, completed["id"], _active_card_ids(conn, completed["id"]))
        bump_revision(conn)
    return len(rows)


def get_board(conn):
    auto_archive_completed_cards(conn)
    conn.execute("BEGIN")
    try:
        result = {"schema_version": SCHEMA_VERSION, "revision": board_revision(conn), "columns": list_columns(conn), "cards": list_cards(conn)}
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def active_column(conn, cid):
    row = conn.execute("SELECT * FROM columns WHERE id=? AND deleted_at IS NULL", (cid,)).fetchone()
    if row is None:
        raise ApiError("列不存在或已删除", 404, "COLUMN_NOT_FOUND")
    return row


def ensure_unique_column_name(conn, name, exclude_id=None):
    sql = "SELECT 1 FROM columns WHERE deleted_at IS NULL AND column_name_key(name) = ?"
    params = [column_name_key(name)]
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_id)
    if conn.execute(sql, params).fetchone() is not None:
        raise ApiError("列名已存在，请使用其他名称", 409, "COLUMN_NAME_EXISTS", {"field": "name"})


def create_column(conn, data):
    name = require_string(require_object(data).get("name"), "name", 1, 100)
    with transaction(conn):
        check_revision(conn, data.get("expected_board_revision"), required=True)
        ensure_unique_column_name(conn, name)
        pos = conn.execute("SELECT COALESCE(MAX(position),-1)+1 FROM columns WHERE deleted_at IS NULL").fetchone()[0]
        cur = conn.execute("INSERT INTO columns (name,position) VALUES (?,?)", (name, pos))
        revision = bump_revision(conn)
    return {"column": row_to_column(conn.execute("SELECT * FROM columns WHERE id=?", (cur.lastrowid,)).fetchone()), "revision": revision}


def update_column(conn, cid, data):
    name = require_string(require_object(data).get("name"), "name", 1, 100)
    with transaction(conn):
        row = active_column(conn, cid)
        check_version(row, data.get("expected_version"), required=True); check_revision(conn, data.get("expected_board_revision"), required=True)
        ensure_unique_column_name(conn, name, cid)
        was_completed = is_completed_column(row)
        becomes_completed = name.strip() == "已完成"
        conn.execute("UPDATE columns SET name=?,version=version+1 WHERE id=?", (name, cid))
        if becomes_completed and not was_completed:
            conn.execute("UPDATE cards SET completed_at=? WHERE column_id=? AND archived=0", (now_iso(), cid))
        elif was_completed and not becomes_completed:
            conn.execute("UPDATE cards SET completed_at=NULL WHERE column_id=? AND archived=0", (cid,))
        revision = bump_revision(conn)
    return {"column": row_to_column(conn.execute("SELECT * FROM columns WHERE id=?", (cid,)).fetchone()), "revision": revision}


def _active_card_ids(conn, column_id, exclude=None):
    return [r["id"] for r in conn.execute("SELECT id FROM cards WHERE column_id=? AND archived=0 AND is_draft=0 ORDER BY position,id", (column_id,)) if r["id"] != exclude]


def _rewrite_positions(conn, column_id, ids):
    for index, card_id in enumerate(ids):
        conn.execute("UPDATE cards SET position=? WHERE id=?", (index, card_id))


def delete_column(conn, cid, data=None):
    data = require_object(data or {})
    with transaction(conn):
        row = active_column(conn, cid)
        check_version(row, data.get("expected_version"), required=True); check_revision(conn, data.get("expected_board_revision"), required=True)
        if conn.execute("SELECT COUNT(*) FROM columns WHERE deleted_at IS NULL").fetchone()[0] <= 1:
            raise ApiError("不能删除最后一个列", 409, "LAST_ACTIVE_COLUMN")
        ts = now_iso()
        count = conn.execute("SELECT COUNT(*) FROM cards WHERE column_id=? AND archived=0 AND is_draft=0", (cid,)).fetchone()[0]
        conn.execute("UPDATE cards SET archived=1,archived_at=?,archive_reason='column_deleted',updated_at=?,version=version+1 WHERE column_id=? AND archived=0 AND is_draft=0", (ts, ts, cid))
        conn.execute("UPDATE columns SET deleted_at=?,version=version+1 WHERE id=?", (ts, cid))
        for index, col in enumerate(conn.execute("SELECT id FROM columns WHERE deleted_at IS NULL ORDER BY position,id")):
            conn.execute("UPDATE columns SET position=? WHERE id=?", (index, col["id"]))
        revision = bump_revision(conn)
    return {"ok": True, "archived_card_count": count, "revision": revision}


def reorder_columns(conn, data):
    ids = require_object(data).get("ids")
    if not isinstance(ids, list) or not ids:
        fail("ids 必须是非空数组", field="ids")
    for value in ids:
        require_int(value, "ids", 1)
    if len(ids) != len(set(ids)):
        fail("列 id 不能重复", field="ids")
    with transaction(conn):
        check_revision(conn, data.get("expected_board_revision"), required=True)
        active_ids = [r["id"] for r in conn.execute("SELECT id FROM columns WHERE deleted_at IS NULL ORDER BY position,id")]
        if set(ids) != set(active_ids) or len(ids) != len(active_ids):
            fail("必须提交全部活跃列的完整顺序", field="ids")
        for index, cid in enumerate(ids):
            conn.execute("UPDATE columns SET position=?,version=version+1 WHERE id=?", (index, cid))
        revision = bump_revision(conn)
    return {"columns": list_columns(conn), "revision": revision}


def create_card_draft(conn, data):
    data = require_object(data)
    column_id = require_int(data.get("column_id"), "column_id", 1)
    with transaction(conn):
        check_revision(conn, data.get("expected_board_revision"), required=True)
        active_column(conn, column_id)
        ts = now_iso()
        cur = conn.execute("""INSERT INTO cards (column_id,title,description,labels,due_date,priority,position,archived,created_at,updated_at,is_draft)
                            VALUES (?,'','','','','medium',0,0,?,?,1)""", (column_id, ts, ts))
    return {"card": get_card(conn, cur.lastrowid, include_draft=True), "revision": board_revision(conn)}


def finalize_card_draft(conn, cid, data):
    fields = normalize_card_fields(data)
    column_id = require_int(data.get("column_id"), "column_id", 1)
    with transaction(conn):
        row = conn.execute("SELECT * FROM cards WHERE id=? AND is_draft=1", (cid,)).fetchone()
        if row is None:
            raise ApiError("草稿卡片不存在", 404, "DRAFT_NOT_FOUND")
        check_version(row, data.get("expected_version"), required=True); check_revision(conn, data.get("expected_board_revision"), required=True)
        column = active_column(conn, column_id)
        position, ts = len(_active_card_ids(conn, column_id)), now_iso()
        completed_at = ts if is_completed_column(column) else None
        conn.execute("""UPDATE cards SET column_id=?,title=?,description=?,labels=?,due_date=?,priority=?,position=?,completed_at=?,updated_at=?,is_draft=0,version=version+1 WHERE id=?""",
                     (column_id, fields["title"], fields["description"], fields["labels"], fields["due_date"], fields["priority"], position, completed_at, ts, cid))
        revision = bump_revision(conn)
    return {"card": get_card(conn, cid), "revision": revision}


def delete_card_draft(conn, cid, expected_version=None):
    with DB_MAINTENANCE_LOCK:
        row = conn.execute("SELECT * FROM cards WHERE id=? AND is_draft=1", (cid,)).fetchone()
        if row is None:
            raise ApiError("草稿卡片不存在", 404, "DRAFT_NOT_FOUND")
        check_version(row, expected_version, required=True)
        directory = attachment_directory(cid)
        rollback = directory + ".draft-delete-" + uuid.uuid4().hex
        if os.path.isdir(directory):
            os.replace(directory, rollback)
        try:
            with transaction(conn):
                conn.execute("DELETE FROM cards WHERE id=? AND is_draft=1", (cid,))
        except Exception:
            if os.path.isdir(rollback):
                os.replace(rollback, directory)
            raise
        shutil.rmtree(rollback, ignore_errors=True)
    return {"ok": True}


def create_card(conn, data):
    fields = normalize_card_fields(data); column_id = require_int(data.get("column_id"), "column_id", 1)
    with transaction(conn):
        check_revision(conn, data.get("expected_board_revision"), required=True); column = active_column(conn, column_id)
        position, ts = len(_active_card_ids(conn, column_id)), now_iso()
        completed_at = ts if is_completed_column(column) else None
        cur = conn.execute("""INSERT INTO cards (column_id,title,description,labels,due_date,priority,position,archived,created_at,updated_at,completed_at)
                            VALUES (?,?,?,?,?,?,?,0,?,?,?)""", (column_id, fields["title"], fields["description"], fields["labels"], fields["due_date"], fields["priority"], position, ts, ts, completed_at))
        revision = bump_revision(conn)
    return {"card": get_card(conn, cur.lastrowid), "revision": revision}


def update_card(conn, cid, data):
    fields = normalize_card_fields(data)
    current = conn.execute("SELECT column_id FROM cards WHERE id=?", (cid,)).fetchone()
    if current is None:
        raise ApiError("卡片不存在或已归档", 404, "CARD_NOT_FOUND")
    target_column_id = require_int(data.get("column_id", current["column_id"]), "column_id", 1)
    requested_position = data.get("position")
    if requested_position is not None:
        requested_position = require_int(requested_position, "position", 0)
    with transaction(conn):
        row = conn.execute("SELECT * FROM cards WHERE id=? AND archived=0 AND is_draft=0", (cid,)).fetchone()
        if row is None:
            raise ApiError("卡片不存在或已归档", 404, "CARD_NOT_FOUND")
        check_version(row, data.get("expected_version"), required=True); check_revision(conn, data.get("expected_board_revision"), required=True)
        target_column = active_column(conn, target_column_id)
        source_column_id = row["column_id"]
        source_ids = _active_card_ids(conn, source_column_id, cid)
        if source_column_id == target_column_id:
            target_ids = source_ids
            default_position = min(row["position"], len(target_ids))
        else:
            target_ids = _active_card_ids(conn, target_column_id, cid)
            default_position = len(target_ids)
        position = min(requested_position if requested_position is not None else default_position, len(target_ids))
        target_ids.insert(position, cid)
        _rewrite_positions(conn, source_column_id, target_ids if source_column_id == target_column_id else source_ids)
        if source_column_id != target_column_id:
            _rewrite_positions(conn, target_column_id, target_ids)
        completed_at = row["completed_at"] if source_column_id == target_column_id else (now_iso() if is_completed_column(target_column) else None)
        ts = now_iso()
        conn.execute("""UPDATE cards SET column_id=?,position=?,title=?,description=?,labels=?,due_date=?,priority=?,completed_at=?,updated_at=?,version=version+1 WHERE id=?""",
                     (target_column_id, position, fields["title"], fields["description"], fields["labels"], fields["due_date"], fields["priority"], completed_at, ts, cid))
        revision = bump_revision(conn)
    return {"card": get_card(conn, cid), "revision": revision}


def move_card(conn, cid, data):
    data = require_object(data); target = require_int(data.get("column_id"), "column_id", 1); position = require_int(data.get("position"), "position", 0)
    with transaction(conn):
        row = conn.execute("SELECT * FROM cards WHERE id=? AND is_draft=0", (cid,)).fetchone()
        if row is None: raise ApiError("卡片不存在", 404, "CARD_NOT_FOUND")
        if row["archived"]: raise ApiError("归档卡片不能移动", 409, "CARD_ARCHIVED")
        check_version(row, data.get("expected_version"), required=True); check_revision(conn, data.get("expected_board_revision"), required=True); target_column = active_column(conn, target)
        source = row["column_id"]; source_ids = _active_card_ids(conn, source, cid)
        target_ids = source_ids if source == target else _active_card_ids(conn, target, cid)
        position = min(position, len(target_ids)); target_ids.insert(position, cid)
        _rewrite_positions(conn, source, target_ids if source == target else source_ids)
        if source != target: _rewrite_positions(conn, target, target_ids)
        if source == target:
            completed_at = row["completed_at"]
        else:
            completed_at = now_iso() if is_completed_column(target_column) else None
        conn.execute("UPDATE cards SET column_id=?,position=?,completed_at=?,updated_at=?,version=version+1 WHERE id=?", (target, position, completed_at, now_iso(), cid))
        revision = bump_revision(conn)
    return {"card": get_card(conn, cid), "revision": revision}


def archive_card(conn, cid, data=None):
    data = require_object(data or {})
    with transaction(conn):
        row = conn.execute("SELECT * FROM cards WHERE id=? AND archived=0 AND is_draft=0", (cid,)).fetchone()
        if row is None: raise ApiError("卡片不存在或已归档", 404, "CARD_NOT_FOUND")
        check_version(row, data.get("expected_version"), required=True); check_revision(conn, data.get("expected_board_revision"), required=True)
        ts = now_iso()
        conn.execute("UPDATE cards SET archived=1,archived_at=?,archive_reason='manual',updated_at=?,version=version+1 WHERE id=?", (ts, ts, cid))
        _rewrite_positions(conn, row["column_id"], _active_card_ids(conn, row["column_id"])); revision = bump_revision(conn)
    return {"ok": True, "column_id": row["column_id"], "position": row["position"], "version": row["version"] + 1, "revision": revision}


def permanently_delete_card(conn, cid, data=None):
    data = require_object(data or {})
    directory = attachment_directory(cid)
    rollback = directory + ".permanent-delete-" + uuid.uuid4().hex
    moved = False
    committed = False
    with DB_MAINTENANCE_LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM cards WHERE id=? AND is_draft=0", (cid,)).fetchone()
                if row is None:
                    raise ApiError("卡片不存在", 404, "CARD_NOT_FOUND")
                check_version(row, data.get("expected_version"), required=True)
                check_revision(conn, data.get("expected_board_revision"), required=True)
                if os.path.isdir(directory):
                    os.replace(directory, rollback)
                    moved = True
                conn.execute("DELETE FROM cards WHERE id=? AND is_draft=0", (cid,))
                if not row["archived"]:
                    _rewrite_positions(conn, row["column_id"], _active_card_ids(conn, row["column_id"]))
                revision = bump_revision(conn)
                conn.commit()
                committed = True
            except Exception:
                conn.rollback()
                if moved and os.path.isdir(rollback) and not os.path.exists(directory):
                    os.replace(rollback, directory)
                    moved = False
                raise
            if moved and os.path.isdir(rollback):
                try:
                    shutil.rmtree(rollback)
                except OSError:
                    MAINTENANCE_REPORT["cleanup"].append(rollback)
            return {"ok": True, "revision": revision}
        except Exception as error:
            if not committed and moved and os.path.isdir(rollback) and not os.path.exists(directory):
                try:
                    os.replace(rollback, directory)
                except OSError:
                    pass
            raise _file_in_use_error(error)


def restore_card(conn, cid, data=None):
    data = require_object(data or {})
    with transaction(conn):
        row = conn.execute("SELECT * FROM cards WHERE id=? AND archived=1 AND is_draft=0", (cid,)).fetchone()

        if row is None: raise ApiError("归档卡片不存在", 404, "CARD_NOT_FOUND")
        check_version(row, data.get("expected_version"), required=True); check_revision(conn, data.get("expected_board_revision"), required=True)
        target = data.get("target_column_id")
        target_row = None
        if target is not None:
            target = require_int(target, "target_column_id", 1)
            target_row = conn.execute("SELECT * FROM columns WHERE id=? AND deleted_at IS NULL", (target,)).fetchone()
            if target_row is None:
                raise ApiError("指定的恢复列不存在或已删除", 404, "COLUMN_NOT_FOUND")
        else:
            target_row = conn.execute("SELECT * FROM columns WHERE id=? AND deleted_at IS NULL", (row["column_id"],)).fetchone()
            if target_row is None:
                original = conn.execute("SELECT name FROM columns WHERE id=?", (row["column_id"],)).fetchone()
                if original is not None:
                    target_row = conn.execute(
                        "SELECT * FROM columns WHERE deleted_at IS NULL AND column_name_key(name)=? ORDER BY position,id LIMIT 1",
                        (column_name_key(original["name"]),),
                    ).fetchone()
        if target_row is None: target_row = conn.execute("SELECT * FROM columns WHERE deleted_at IS NULL ORDER BY position,id LIMIT 1").fetchone()
        if target_row is None: raise ApiError("没有可用列", 409, "NO_ACTIVE_COLUMN")
        target = target_row["id"]; ids = _active_card_ids(conn, target, cid)
        position = min(require_int(data.get("position", len(ids)), "position", 0), len(ids)); ids.insert(position, cid)
        _rewrite_positions(conn, target, ids)
        completed_at = now_iso() if is_completed_column(target_row) else None
        conn.execute("UPDATE cards SET archived=0,column_id=?,position=?,completed_at=?,archived_at=NULL,archive_reason=NULL,updated_at=?,version=version+1 WHERE id=?", (target, position, completed_at, now_iso(), cid))
        revision = bump_revision(conn)
    return {"card": get_card(conn, cid), "revision": revision}


def copy_card(conn, cid, data=None):
    data = require_object(data or {})
    with transaction(conn):
        row = conn.execute("SELECT * FROM cards WHERE id=? AND archived=0 AND is_draft=0", (cid,)).fetchone()
        if row is None: raise ApiError("卡片不存在", 404, "CARD_NOT_FOUND")
        check_version(row, data.get("expected_version"), required=True); check_revision(conn, data.get("expected_board_revision"), required=True)
        ids = _active_card_ids(conn, row["column_id"])
        source_index = ids.index(cid)
        position = source_index + 1
        ts = now_iso()
        column = active_column(conn, row["column_id"])
        completed_at = ts if is_completed_column(column) else None
        cur = conn.execute("""INSERT INTO cards (column_id,title,description,labels,due_date,priority,position,archived,created_at,updated_at,completed_at)
                            VALUES (?,?,?,?,?,?,?,0,?,?,?)""", (row["column_id"], row["title"] + "（副本）", row["description"], row["labels"], row["due_date"], row["priority"], position, ts, ts, completed_at))
        ids.insert(position, cur.lastrowid)
        _rewrite_positions(conn, row["column_id"], ids)
        revision = bump_revision(conn)
    return {"card": get_card(conn, cur.lastrowid), "revision": revision, "attachments_copied": False}


SEARCH_SORTS = {
    "archived_desc": ("archived_at", "DESC"),
    "archived_asc": ("archived_at", "ASC"),
    "updated_desc": ("updated_at", "DESC"),
    "updated_asc": ("updated_at", "ASC"),
    "created_desc": ("created_at", "DESC"),
    "created_asc": ("created_at", "ASC"),
}


def encode_search_cursor(sort, null_rank, value, card_id):
    raw = json.dumps([1, sort, null_rank, value, card_id], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_search_cursor(value, sort):
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        version, cursor_sort, null_rank, field_value, card_id = json.loads(raw.decode("utf-8"))
        if version != 1 or cursor_sort != sort or null_rank not in (0, 1):
            raise ValueError
        if not isinstance(field_value, str) or len(field_value) > 30:
            raise ValueError
        require_int(card_id, "cursor", 1)
        return null_rank, field_value, card_id
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ApiError):
        raise ApiError("搜索游标无效，请重新搜索", 400, "INVALID_CURSOR")


def search_cards_page(conn, q="", date_from=None, date_to=None, priority=None, include_active=False, cursor=None, limit=50, sort="archived_desc"):
    if date_from: validate_due_date(date_from)
    if date_to: validate_due_date(date_to)
    if priority: validate_priority(priority)
    if sort not in SEARCH_SORTS:
        raise ApiError("历史排序方式无效", 400, "INVALID_SORT")
    sort_field, direction = SEARCH_SORTS[sort]
    sql = """SELECT cards.*,columns.name AS column_name,
              (SELECT COUNT(*) FROM attachments WHERE attachments.card_id=cards.id) AS attachment_count,
              CASE WHEN columns.deleted_at IS NULL THEN 0 ELSE 1 END AS column_deleted,
              CASE WHEN columns.deleted_at IS NULL THEN columns.id ELSE
                  (SELECT matching.id FROM columns AS matching
                   WHERE matching.deleted_at IS NULL AND column_name_key(matching.name)=column_name_key(columns.name)
                   ORDER BY matching.position,matching.id LIMIT 1)
              END AS restore_column_id
              FROM cards JOIN columns ON columns.id=cards.column_id WHERE cards.is_draft=0"""

    params = []
    if not include_active: sql += " AND cards.archived=1"
    if q:
        name_query = "%%%s%%" % attachment_name_key(q)
        sql += """ AND (cards.title LIKE ? OR cards.description LIKE ? OR cards.labels LIKE ? OR EXISTS
                             (SELECT 1 FROM attachments WHERE attachments.card_id=cards.id AND attachments.name_key LIKE ?))"""
        params.extend(["%%%s%%" % q] * 3 + [name_query])
    if date_from: sql += " AND substr(cards.due_date,1,10)>=?"; params.append(date_from[:10])
    if date_to: sql += " AND substr(cards.due_date,1,10)<=?"; params.append(date_to[:10])
    if priority: sql += " AND cards.priority=?"; params.append(priority)
    decoded = decode_search_cursor(cursor, sort)
    null_rank_sql = "CASE WHEN cards.%s IS NULL THEN 1 ELSE 0 END" % sort_field
    value_sql = "COALESCE(cards.%s,'')" % sort_field
    comparison = "<" if direction == "DESC" else ">"
    if decoded:
        null_rank, field_value, card_id = decoded
        sql += " AND (%s>? OR (%s=? AND (%s%s? OR (%s=? AND cards.id%s?))))" % (
            null_rank_sql, null_rank_sql, value_sql, comparison, value_sql, comparison)
        params.extend([null_rank, null_rank, field_value, field_value, card_id])
    limit = min(max(int(limit), 1), 100)
    order_sql = " ORDER BY %s ASC,cards.%s %s,cards.id %s LIMIT ?" % (null_rank_sql, sort_field, direction, direction)
    rows = conn.execute(sql + order_sql, params + [limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [row_to_card(r) for r in rows]
    if has_more and rows:
        last = rows[-1]
        value = last[sort_field]
        next_cursor = encode_search_cursor(sort, 1 if value is None else 0, value or "", last["id"])
    else:
        next_cursor = None
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


def search_cards(conn, q="", date_from=None, date_to=None, priority=None, include_active=False, sort="archived_desc"):
    return search_cards_page(conn, q, date_from, date_to, priority, include_active, sort=sort)["items"]


def export_data(conn):
    attachments = [dict(row) for row in conn.execute("""SELECT attachments.id,attachments.card_id,attachments.file_name,attachments.content_type,
                 attachments.size,attachments.created_at,attachments.updated_at,attachments.version FROM attachments
                 JOIN cards ON cards.id=attachments.card_id WHERE cards.is_draft=0 ORDER BY attachments.id""")]
    for attachment in attachments:
        attachment["file_included"] = False
    return {"format": "kanban-export", "format_version": EXPORT_VERSION, "schema_version": SCHEMA_VERSION,
            "board_revision": board_revision(conn), "exported_at": now_iso(), "columns": list_columns(conn, True),
            "cards": list_cards(conn, archived=0) + list_cards(conn, archived=1), "attachments": attachments}


def normalize_import(data):
    data = require_object(data)
    if data.get("format") not in (None, "kanban-export") or data.get("format_version", 1) not in (1, 2):
        fail("不支持的导入格式或版本", code="UNSUPPORTED_IMPORT_VERSION")
    columns, cards = data.get("columns"), data.get("cards")
    if not isinstance(columns, list) or not isinstance(cards, list) or not columns: fail("导入文件必须包含非空 columns 和 cards 数组")
    normalized_columns, column_ids, active_name_keys = [], set(), set()
    for index, item in enumerate(columns):
        item = require_object(item); cid = require_int(item.get("id"), "columns.id", 1)
        if cid in column_ids: fail("列 id 重复")
        column_ids.add(cid); version = item.get("version", 1)
        column = {"id": cid, "name": require_string(item.get("name"), "name", 1, 100), "position": index,
                  "deleted_at": item.get("deleted_at"), "version": version if isinstance(version, int) and version > 0 else 1}
        if not column["deleted_at"]:
            key = column_name_key(column["name"])
            if key in active_name_keys:
                raise ApiError("活动列名称不能重复", 422, "DUPLICATE_COLUMN_NAME")
            active_name_keys.add(key)
        normalized_columns.append(column)
    if not any(not c["deleted_at"] for c in normalized_columns): fail("导入文件至少需要一个活跃列")
    normalized_cards, card_ids, per_column = [], set(), {}
    for item in cards:
        item = require_object(item); card_id = require_int(item.get("id"), "cards.id", 1); column_id = require_int(item.get("column_id"), "column_id", 1)
        if card_id in card_ids: fail("卡片 id 重复")
        if column_id not in column_ids: fail("卡片引用了不存在的列", code="INVALID_IMPORT_REFERENCE")
        card_ids.add(card_id); fields = normalize_card_fields(item); archived = item.get("archived", 0)
        if archived not in (0, 1, False, True): fail("archived 必须是 0 或 1")
        version = item.get("version", 1)
        card = {"id": card_id, "column_id": column_id, **fields, "archived": int(archived),
                "created_at": require_string(item.get("created_at", now_iso()), "created_at", 1, 30),
                "updated_at": require_string(item.get("updated_at", now_iso()), "updated_at", 1, 30),
                "completed_at": item.get("completed_at"), "archived_at": item.get("archived_at"),
                "archive_reason": item.get("archive_reason"),
                "version": version if isinstance(version, int) and version > 0 else 1, "source_position": item.get("position", 0)}
        normalized_cards.append(card)
        if not card["archived"]: per_column.setdefault(column_id, []).append(card)
    for values in per_column.values():
        values.sort(key=lambda c: (c["source_position"] if isinstance(c["source_position"], int) else 0, c["id"]))
        for index, card in enumerate(values): card["position"] = index
    for card in normalized_cards: card.setdefault("position", 0); card.pop("source_position", None)
    return {"columns": normalized_columns, "cards": normalized_cards}


def import_preview(data):
    normalized = normalize_import(data)
    ignored = len(data.get("attachments", [])) if isinstance(data, dict) and isinstance(data.get("attachments"), list) else 0
    return {"ok": True, "columns": len(normalized["columns"]), "cards": sum(not c["archived"] for c in normalized["cards"]),
            "archived_cards": sum(c["archived"] for c in normalized["cards"]), "attachments_ignored": ignored}


def import_replace(conn, request):
    request = require_object(request); normalized = normalize_import(request.get("data"))
    rollback_dir = ATTACHMENTS_DIR + ".import-rollback-" + uuid.uuid4().hex
    backup = None
    with DB_MAINTENANCE_LOCK:
        check_revision(conn, request.get("expected_board_revision"), required=True); backup = create_full_backup("pre-import")
        if os.path.isdir(ATTACHMENTS_DIR): os.replace(ATTACHMENTS_DIR, rollback_dir)
        os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
        try:
            with transaction(conn):
                check_revision(conn, request.get("expected_board_revision"), required=True); conn.execute("DELETE FROM cards"); conn.execute("DELETE FROM columns")
                for col in normalized["columns"]:
                    conn.execute("INSERT INTO columns (id,name,position,deleted_at,version) VALUES (?,?,?,?,?)", (col["id"], col["name"], col["position"], col["deleted_at"], col["version"]))
                for card in normalized["cards"]:
                    conn.execute("""INSERT INTO cards (id,column_id,title,description,labels,due_date,priority,position,archived,created_at,updated_at,completed_at,archived_at,archive_reason,is_draft,version)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""", (card["id"], card["column_id"], card["title"], card["description"], card["labels"], card["due_date"], card["priority"], card["position"], card["archived"], card["created_at"], card["updated_at"], card["completed_at"], card["archived_at"], card["archive_reason"], card["version"]))
                revision = bump_revision(conn)
                if conn.execute("PRAGMA foreign_key_check").fetchone() is not None: raise ApiError("导入数据外键检查失败", 422, "INVALID_IMPORT_REFERENCE")
        except Exception:
            shutil.rmtree(ATTACHMENTS_DIR, ignore_errors=True)
            if os.path.isdir(rollback_dir): os.replace(rollback_dir, ATTACHMENTS_DIR)
            raise
        shutil.rmtree(rollback_dir, ignore_errors=True)
    return {"ok": True, "imported": import_preview(request.get("data")), "backup": os.path.basename(backup), "revision": revision}


def _zip_attachment_manifest(rows):
    result = []
    missing = []
    for row in rows:
        path = attachment_path(row["card_id"], row["file_name"])
        arcname = "attachments/%s/%s" % (row["card_id"], row["file_name"])
        if not os.path.isfile(path):
            missing.append(arcname)
            continue
        result.append({"path": arcname, "size": os.path.getsize(path), "sha256": sha256_file(path)})
    return result, missing


def backup_readiness(conn):
    rows = conn.execute("""SELECT attachments.card_id,attachments.file_name,attachments.size FROM attachments
                         JOIN cards ON cards.id=attachments.card_id WHERE cards.is_draft=0 ORDER BY attachments.id""").fetchall()
    missing = []
    for row in rows:
        if not os.path.isfile(attachment_path(row["card_id"], row["file_name"])):
            missing.append("attachments/%s/%s" % (row["card_id"], row["file_name"]))
    if missing:
        raise ApiError("完整备份所需附件缺失", 422, "INCOMPLETE_BACKUP", {"missing_attachments": missing})
    attachment_size = sum(row["size"] for row in rows)
    database_size = os.path.getsize(DB_PATH) if os.path.isfile(DB_PATH) else 0
    ensure_free_space(BACKUP_DIR, database_size * 2 + attachment_size)
    return {"ok": True, "attachment_count": len(rows), "attachment_size": attachment_size}


def create_full_backup(prefix="backup"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with MAINTENANCE_GATE.exclusive(), DB_MAINTENANCE_LOCK:
        db_snapshot = create_backup("snapshot")
        final_path = os.path.join(BACKUP_DIR, "kanban-%s-%s.zip" % (prefix, datetime.now().strftime("%Y%m%d-%H%M%S-%f")))
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".kanban-full-", suffix=".tmp", dir=BACKUP_DIR)
            os.close(fd)
            snapshot = get_conn(db_snapshot)
            with transaction(snapshot):
                snapshot.execute("DELETE FROM cards WHERE is_draft=1")
            rows = snapshot.execute("""SELECT attachments.card_id,attachments.file_name,attachments.size FROM attachments
                                     JOIN cards ON cards.id=attachments.card_id WHERE cards.is_draft=0 ORDER BY attachments.id""").fetchall()
            snapshot.close()
            attachment_manifest, missing = _zip_attachment_manifest(rows)
            if missing:
                raise ApiError("完整备份所需附件缺失", 422, "INCOMPLETE_BACKUP", {"missing_attachments": missing})
            required_output = os.path.getsize(db_snapshot) + sum(item["size"] for item in attachment_manifest)
            ensure_free_space(BACKUP_DIR, required_output)
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(db_snapshot, "kanban.db")
                for item in attachment_manifest:
                    card_id, file_name = item["path"].split("/", 2)[1:]
                    archive.write(attachment_path(int(card_id), file_name), item["path"])
                manifest = {"format": "kanban-full-backup", "format_version": 2, "schema_version": SCHEMA_VERSION,
                            "created_at": now_iso(), "database": {"size": os.path.getsize(db_snapshot), "sha256": sha256_file(db_snapshot)},
                            "attachment_count": len(rows), "attachment_size": sum(row["size"] for row in rows),
                            "attachments": attachment_manifest, "missing_attachments": missing}
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            os.replace(temp_path, final_path)
            return final_path
        finally:
            for path in (db_snapshot, temp_path):
                try:
                    if path and os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass


def _safe_zip_entries(archive):
    entries = archive.infolist()
    if len(entries) > MAX_ZIP_ENTRIES:
        raise ApiError("ZIP 文件条目过多", 413, "ZIP_TOO_MANY_ENTRIES")
    expanded = sum(info.file_size for info in entries)
    if MAX_ZIP_EXPANDED_BYTES and expanded > MAX_ZIP_EXPANDED_BYTES:
        raise ApiError("ZIP 展开后内容过大", 413, "ZIP_EXPANDED_TOO_LARGE")
    seen = set()
    result = []
    for info in entries:
        name = info.filename
        if not name or name.endswith("/"):
            continue
        if info.file_size and info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_ZIP_RATIO:
            raise ApiError("ZIP 压缩率异常，无法安全解压", 422, "ZIP_COMPRESSION_RATIO")
        if "\\" in name or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
            raise ApiError("ZIP 中包含不安全路径", 422, "UNSAFE_ZIP_PATH")
        parts = name.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ApiError("ZIP 中包含不安全路径", 422, "UNSAFE_ZIP_PATH")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ApiError("ZIP 中不能包含符号链接", 422, "UNSAFE_ZIP_ENTRY")
        key = name.casefold()
        if key in seen:
            raise ApiError("ZIP 中存在大小写冲突或重复文件", 422, "ZIP_NAME_CONFLICT")
        seen.add(key)
        result.append(info)
    return result


def validate_board_invariants(conn):
    if conn.execute("SELECT 1 FROM cards WHERE is_draft=1 LIMIT 1").fetchone():
        raise ApiError("备份中不能包含未完成草稿", 422, "INVALID_BACKUP_DRAFT")
    if conn.execute("""SELECT 1 FROM cards JOIN columns ON columns.id=cards.column_id
                     WHERE cards.archived=0 AND cards.is_draft=0 AND columns.deleted_at IS NOT NULL LIMIT 1""").fetchone():
        raise ApiError("未归档卡片不能属于已删除列", 422, "INVALID_BOARD_STATE")
    names = set()
    for row in conn.execute("SELECT name FROM columns WHERE deleted_at IS NULL"):
        key = column_name_key(row["name"])
        if key in names:
            raise ApiError("活动列名称不能重复", 422, "DUPLICATE_COLUMN_NAME")
        names.add(key)
    for row in conn.execute("SELECT id,description,due_date,priority,created_at,updated_at,archived,archived_at,archive_reason FROM cards WHERE is_draft=0"):
        if sanitize_description(row["description"]) != row["description"]:
            raise ApiError("卡片描述包含不安全或非规范格式", 422, "INVALID_CARD_DESCRIPTION", {"card_id": row["id"]})
        validate_priority(row["priority"]); validate_due_date(row["due_date"])
        try:
            created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
            updated = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            raise ApiError("卡片时间格式无效", 422, "INVALID_BOARD_TIMESTAMP", {"card_id": row["id"]})
        if created > updated:
            raise ApiError("卡片更新时间不能早于创建时间", 422, "INVALID_BOARD_TIMESTAMP", {"card_id": row["id"]})
        if bool(row["archived"]) != bool(row["archived_at"]):
            raise ApiError("卡片归档状态不一致", 422, "INVALID_ARCHIVE_STATE", {"card_id": row["id"]})
        if row["archived"] and not row["archive_reason"]:
            raise ApiError("归档卡片缺少归档原因", 422, "INVALID_ARCHIVE_STATE", {"card_id": row["id"]})
    for column in conn.execute("SELECT id FROM columns"):
        positions = [row[0] for row in conn.execute("SELECT position FROM cards WHERE column_id=? AND archived=0 AND is_draft=0 ORDER BY position,id", (column["id"],))]
        if positions != list(range(len(positions))):
            raise ApiError("卡片位置不连续", 422, "INVALID_CARD_POSITIONS", {"column_id": column["id"]})
    state = conn.execute("SELECT revision FROM board_state WHERE id=1").fetchone()
    if state is None or not isinstance(state["revision"], int) or state["revision"] < 1:
        raise ApiError("看板版本状态无效", 422, "INVALID_BOARD_STATE")


def inspect_full_backup(path, extract=False):
    extract_dir = tempfile.mkdtemp(prefix="kanban-restore-", dir=BACKUP_DIR) if extract else None
    db_path = None
    try:
        try:
            archive = zipfile.ZipFile(path, "r")
        except (zipfile.BadZipFile, OSError):
            raise ApiError("所选文件不是有效的完整看板备份", 422, "INVALID_BACKUP")
        with archive:
            entries = _safe_zip_entries(archive)
            by_name = {info.filename: info for info in entries}
            if "manifest.json" not in by_name or "kanban.db" not in by_name:
                raise ApiError("ZIP 不是有效的完整看板备份", 422, "INVALID_BACKUP")
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError):
                raise ApiError("备份清单格式无效", 422, "INVALID_BACKUP")
            version = manifest.get("format_version")
            if manifest.get("format") != "kanban-full-backup" or version not in (1, 2):
                raise ApiError("不支持的完整备份格式", 422, "UNSUPPORTED_BACKUP_VERSION")
            if manifest.get("missing_attachments"):
                raise ApiError("备份包含缺失附件，不能直接恢复", 422, "INCOMPLETE_BACKUP", {"missing_attachments": manifest["missing_attachments"]})
            fd, db_path = tempfile.mkstemp(prefix=".restore-check-", suffix=".db", dir=BACKUP_DIR)
            os.close(fd)
            with archive.open("kanban.db") as source, open(db_path, "wb") as output:
                stream_copy_limited(source, output, by_name["kanban.db"].file_size, BACKUP_DIR)
            if version == 2:
                database = manifest.get("database") or {}
                if database.get("size") != os.path.getsize(db_path) or database.get("sha256") != sha256_file(db_path):
                    raise ApiError("备份数据库哈希校验失败", 422, "BACKUP_HASH_MISMATCH")
        check = get_conn(db_path)
        try:
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok" or check.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ApiError("备份数据库完整性检查失败", 422, "INVALID_BACKUP_DATABASE")
            if check.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ApiError("备份数据库版本不受支持", 422, "UNSUPPORTED_BACKUP_SCHEMA")
            validate_board_invariants(check)
            attachment_rows = check.execute("""SELECT attachments.card_id,attachments.file_name,attachments.size FROM attachments
                                             JOIN cards ON cards.id=attachments.card_id WHERE cards.is_draft=0 ORDER BY attachments.id""").fetchall()
            expected = {"manifest.json", "kanban.db"}
            manifest_items = {item.get("path"): item for item in manifest.get("attachments", [])} if version == 2 else {}
            with zipfile.ZipFile(path, "r") as content_archive:
                for row in attachment_rows:
                    validate_attachment_name(row["file_name"])
                    arcname = "attachments/%s/%s" % (row["card_id"], row["file_name"])
                    expected.add(arcname)
                    info = by_name.get(arcname)
                    if info is None or info.file_size != row["size"]:
                        raise ApiError("备份附件与数据库记录不一致", 422, "INVALID_BACKUP_ATTACHMENT")
                    digest = hashlib.sha256()
                    if extract:
                        target = os.path.abspath(os.path.join(extract_dir, *arcname.split("/")))
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with content_archive.open(info) as source, open(target, "wb") as output:
                            stream_copy_limited(source, output, info.file_size, extract_dir, digest)
                    elif version == 2:
                        with content_archive.open(info) as source:
                            while True:
                                chunk = source.read(65536)
                                if not chunk:
                                    break
                                digest.update(chunk)
                    if version == 2:
                        item = manifest_items.get(arcname)
                        if not item or item.get("size") != row["size"] or item.get("sha256") != digest.hexdigest():
                            raise ApiError("备份附件哈希校验失败", 422, "BACKUP_HASH_MISMATCH", {"path": arcname})
            extras = set(by_name) - expected
            if extras:
                raise ApiError("备份中包含未登记文件", 422, "UNEXPECTED_BACKUP_ENTRY", {"entries": sorted(extras)[:20]})
            columns = check.execute("SELECT COUNT(*) FROM columns WHERE deleted_at IS NULL").fetchone()[0]
            active = check.execute("SELECT COUNT(*) FROM cards WHERE archived=0 AND is_draft=0").fetchone()[0]
            archived = check.execute("SELECT COUNT(*) FROM cards WHERE archived=1 AND is_draft=0").fetchone()[0]
        finally:
            check.close()
        if extract:
            target_db = os.path.join(extract_dir, "kanban.db")
            os.replace(db_path, target_db)
            db_path = target_db
        preview = {"ok": True, "created_at": manifest.get("created_at"), "columns": columns, "cards": active,
                   "archived_cards": archived, "attachments": len(attachment_rows),
                   "attachment_size": sum(row["size"] for row in attachment_rows), "format_version": version,
                   "hash_verified": version == 2}
        return preview, extract_dir
    except Exception:
        if extract_dir:
            shutil.rmtree(extract_dir, ignore_errors=True)
        raise
    finally:
        if db_path and (not extract or not extract_dir or not db_path.startswith(extract_dir)):
            try:
                os.remove(db_path)
            except OSError:
                pass


def cleanup_restore_tokens():
    cutoff = datetime.now().timestamp() - RESTORE_TOKEN_TTL
    with RESTORE_TOKEN_LOCK:
        expired = [(token, item) for token, item in RESTORE_TOKENS.items() if item["created"] < cutoff and not item.get("claimed")]
        for token, _ in expired:
            RESTORE_TOKENS.pop(token, None)
    for _, item in expired:
        try:
            os.remove(item["path"])
        except OSError:
            pass


def stage_full_backup(input_stream, content_length):
    cleanup_restore_tokens()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ensure_free_space(BACKUP_DIR, min(content_length, 65536))
    fd, path = tempfile.mkstemp(prefix=".restore-upload-", suffix=".zip", dir=BACKUP_DIR)
    os.close(fd)
    try:
        with open(path, "wb") as output:
            stream_copy_limited(input_stream, output, content_length, BACKUP_DIR)
        preview, _ = inspect_full_backup(path)
        token = uuid.uuid4().hex
        with RESTORE_TOKEN_LOCK:
            RESTORE_TOKENS[token] = {"path": path, "created": datetime.now().timestamp(), "claimed": False}
        return {**preview, "token": token}
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _claim_restore_token(token):
    cleanup_restore_tokens()
    with RESTORE_TOKEN_LOCK:
        item = RESTORE_TOKENS.get(token)
        if item is None or item.get("claimed"):
            raise ApiError("恢复预览已过期或已使用，请重新选择 ZIP", 404, "RESTORE_TOKEN_EXPIRED")
        item["claimed"] = True
        return dict(item)


def _move_if_exists(source, target):
    if os.path.exists(source):
        os.replace(source, target)
        return True
    return False


def restore_full_backup(conn, token, expected_revision):
    item = _claim_restore_token(token)
    preview, extracted = inspect_full_backup(item["path"], extract=True)
    operation = uuid.uuid4().hex
    rollback_db = DB_PATH + ".restore-rollback-" + operation
    rollback_wal = rollback_db + "-wal"
    rollback_shm = rollback_db + "-shm"
    rollback_dir = ATTACHMENTS_DIR + ".restore-rollback-" + operation
    new_db = os.path.join(extracted, "kanban.db")
    new_dir = os.path.join(extracted, "attachments")
    os.makedirs(new_dir, exist_ok=True)
    backup = None
    moved = {"db": False, "wal": False, "shm": False, "attachments": False}
    try:
        with MAINTENANCE_GATE.exclusive(), DB_MAINTENANCE_LOCK:
            check_revision(conn, expected_revision, required=True)
            old_revision = board_revision(conn)
            backup = create_full_backup("pre-restore")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            moved["db"] = _move_if_exists(DB_PATH, rollback_db)
            moved["wal"] = _move_if_exists(DB_PATH + "-wal", rollback_wal)
            moved["shm"] = _move_if_exists(DB_PATH + "-shm", rollback_shm)
            moved["attachments"] = _move_if_exists(ATTACHMENTS_DIR, rollback_dir)
            try:
                os.replace(new_db, DB_PATH)
                os.replace(new_dir, ATTACHMENTS_DIR)
                restored = get_conn()
                try:
                    validate_board_invariants(restored)
                    if restored.execute("PRAGMA quick_check").fetchone()[0] != "ok" or restored.execute("PRAGMA foreign_key_check").fetchone() is not None:
                        raise ApiError("恢复后数据库完整性检查失败", 500, "RESTORE_FAILED")
                    restored_revision = board_revision(restored)
                    with transaction(restored):
                        restored.execute("UPDATE board_state SET revision=?,updated_at=? WHERE id=1", (max(old_revision, restored_revision) + 1, now_iso()))
                    new_revision = board_revision(restored)
                finally:
                    restored.close()
            except Exception:
                for path in (DB_PATH + "-wal", DB_PATH + "-shm"):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass
                shutil.rmtree(ATTACHMENTS_DIR, ignore_errors=True)
                try:
                    if os.path.isfile(DB_PATH):
                        os.remove(DB_PATH)
                except OSError:
                    pass
                if moved["db"]:
                    os.replace(rollback_db, DB_PATH)
                if moved["wal"]:
                    os.replace(rollback_wal, DB_PATH + "-wal")
                if moved["shm"]:
                    os.replace(rollback_shm, DB_PATH + "-shm")
                if moved["attachments"]:
                    os.replace(rollback_dir, ATTACHMENTS_DIR)
                verify = get_conn()
                try:
                    if verify.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("恢复失败且原数据库回滚检查失败")
                finally:
                    verify.close()
                raise
            for path in (rollback_db, rollback_wal, rollback_shm):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    MAINTENANCE_REPORT["cleanup"].append(path)
            shutil.rmtree(rollback_dir, ignore_errors=True)
        with RESTORE_TOKEN_LOCK:
            RESTORE_TOKENS.pop(token, None)
        try:
            os.remove(item["path"])
        except OSError:
            pass
        return {"ok": True, "preview": preview, "backup": os.path.basename(backup), "revision": new_revision}
    except Exception:
        with RESTORE_TOKEN_LOCK:
            current = RESTORE_TOKENS.get(token)
            if current:
                current["claimed"] = False
        raise
    finally:
        shutil.rmtree(extracted, ignore_errors=True)


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

    def _read_json_body(self, maximum=MAX_JSON_BODY):
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
        return require_object(value)

    def _serve_static(self, rel_path):
        rel_path = rel_path.replace("\\", "/").lstrip("/"); file_path = os.path.join(STATIC_DIR, "index.html") if rel_path in ("", "index.html") else os.path.normpath(os.path.join(STATIC_DIR, rel_path))
        if os.path.commonpath((os.path.abspath(STATIC_DIR), os.path.abspath(file_path))) != os.path.abspath(STATIC_DIR): return self._send_text("Forbidden", 403)
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
            match = re.fullmatch(r"/api/cards/(\d+)", path)
            if match:
                cid = int(match.group(1))
                if method == "GET": return self._send_json(get_card(conn, cid))
                if method == "PUT": return self._send_json(update_card(conn, cid, self._read_json_body()))
                if method == "DELETE": return self._send_json(archive_card(conn, cid, self._read_json_body()))
            match = re.fullmatch(r"/api/cards/(\d+)/permanent", path)
            if match and method == "DELETE": return self._send_json(permanently_delete_card(conn, int(match.group(1)), self._read_json_body()))
            for suffix, action, verb in (("move", move_card, "PUT"), ("restore", restore_card, "POST"), ("copy", copy_card, "POST"), ("finalize", finalize_card_draft, "PUT")):
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
                    length = self._content_length(MAX_ATTACHMENT_BODY)
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
                return self._send_json(search_cards_page(conn, query.get("q", [""])[0], query.get("from", [None])[0], query.get("to", [None])[0], query.get("priority", [None])[0], query.get("all", ["0"])[0].lower() in ("1", "true"), query.get("cursor", [None])[0], query.get("limit", [50])[0], query.get("sort", ["archived_desc"])[0]))
            if path == "/api/export" and method == "GET":
                body = json.dumps(export_data(conn), ensure_ascii=False, indent=2).encode("utf-8"); filename = "kanban-export-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename); self._security_headers(); self.end_headers(); self.wfile.write(body); return
            if path == "/api/backup/check" and method == "GET": return self._send_json(backup_readiness(conn))
            if path == "/api/backup" and method == "GET": return self._send_backup()
            if path == "/api/import/zip/preview" and method == "POST":
                length = self._content_length(MAX_ZIP_BODY)
                if length == 0: raise ApiError("ZIP 文件不能为空", 400, "INVALID_BACKUP")
                return self._send_json(stage_full_backup(self.rfile, length))
            if path == "/api/import/zip" and method == "POST":
                request = self._read_json_body()
                return self._send_json(restore_full_backup(conn, require_string(request.get("token"), "token", 1, 100), request.get("expected_board_revision")))
            if path == "/api/import/preview" and method == "POST": return self._send_json(import_preview(self._read_json_body(MAX_IMPORT_BODY)))
            if path == "/api/import" and method == "POST": return self._send_json(import_replace(conn, self._read_json_body(MAX_IMPORT_BODY)))
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
        if re.fullmatch(r"/api/cards/\d+", path):
            return "GET, PUT, DELETE, OPTIONS"
        if re.fullmatch(r"/api/cards/\d+/permanent", path):
            return "DELETE, OPTIONS"
        if re.fullmatch(r"/api/cards/\d+/(?:restore|copy)", path):
            return "POST, OPTIONS"
        if re.fullmatch(r"/api/cards/\d+/(?:move|finalize)", path):
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
            return auto_archive_completed_cards(conn, now)
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
    init_db(); start_auto_archive_worker(); server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 60); print("  看板系统已启动（仅限本机访问）"); print("  访问地址: http://127.0.0.1:%d" % PORT)
    print("  数据库: %s" % DB_PATH); print("  按 Ctrl+C 停止"); print("=" * 60)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n正在停止...")
    finally:
        AUTO_ARCHIVE_STOP.set()
        server.shutdown()
        server.server_close()


if __name__ == "__main__": main()
