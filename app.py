# -*- coding: utf-8 -*-
"""看板系统后端：Python 标准库 + SQLite。"""

import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from html import escape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "kanban.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
HOST = os.environ.get("KANBAN_HOST", "127.0.0.1")
PORT = int(os.environ.get("KANBAN_PORT", "8000"))
SCHEMA_VERSION = 3
EXPORT_VERSION = 2
MAX_JSON_BODY = 1024 * 1024
MAX_IMPORT_BODY = 10 * 1024 * 1024
VALID_PRIORITY = ("high", "medium", "low")
DB_MAINTENANCE_LOCK = threading.RLock()


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
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
    allowed = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u", "s"}
    blocked_tags = {"script", "style", "iframe", "object", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.blocked = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.blocked_tags:
            self.blocked += 1
        elif not self.blocked and tag in self.allowed:
            self.output.append("<%s>" % tag)

    def handle_startendtag(self, tag, attrs):
        if not self.blocked and tag.lower() == "br":
            self.output.append("<br>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.blocked_tags:
            self.blocked = max(0, self.blocked - 1)
        elif not self.blocked and tag in self.allowed and tag != "br":
            self.output.append("</%s>" % tag)

    def handle_data(self, data):
        if not self.blocked:
            self.output.append(escape(data, quote=False))


def sanitize_description(value):
    parser = SafeHtmlParser()
    parser.feed(require_string(value, "description", 0, 100000, trim=False))
    parser.close()
    return "".join(parser.output).strip()


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


def create_backup(prefix="auto", required=True):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    final_path = os.path.join(BACKUP_DIR, "kanban-%s-%s.db" % (prefix, stamp))
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
        return final_path
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        if required:
            raise
        return None


def maybe_daily_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    files = [os.path.join(BACKUP_DIR, n) for n in os.listdir(BACKUP_DIR)
             if n.startswith("kanban-auto-") and n.endswith(".db")]
    if files and datetime.now().timestamp() - max(os.path.getmtime(p) for p in files) < 86400:
        return
    if create_backup("auto", required=False):
        files = sorted((os.path.join(BACKUP_DIR, n) for n in os.listdir(BACKUP_DIR)
                        if n.startswith("kanban-auto-") and n.endswith(".db")),
                       key=os.path.getmtime, reverse=True)
        for path in files[10:]:
            try:
                os.remove(path)
            except OSError:
                pass


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
                    completed_at TEXT,archived_at TEXT,archive_reason TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (column_id) REFERENCES columns(id) ON DELETE CASCADE)""")
                conn.execute("CREATE TABLE board_state (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL)")
                conn.execute("INSERT INTO board_state VALUES (1,1,?)", (now_iso(),))
                conn.executemany("INSERT INTO columns (name,position) VALUES (?,?)", [("待办", 0), ("进行中", 1), ("已完成", 2)])
                conn.execute("PRAGMA user_version = 3")
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
        if conn.execute("SELECT COUNT(*) FROM columns WHERE deleted_at IS NULL").fetchone()[0] == 0:
            with transaction(conn):
                conn.executemany("INSERT INTO columns (name,position) VALUES (?,?)", [("待办", 0), ("进行中", 1), ("已完成", 2)])
                bump_revision(conn)
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("数据库完整性检查失败")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("数据库外键检查失败")
    finally:
        conn.close()
    maybe_daily_backup()


def board_revision(conn):
    return conn.execute("SELECT revision FROM board_state WHERE id=1").fetchone()[0]


def bump_revision(conn):
    conn.execute("UPDATE board_state SET revision=revision+1,updated_at=? WHERE id=1", (now_iso(),))
    return board_revision(conn)


def check_revision(conn, expected):
    if expected is None:
        return
    require_int(expected, "expected_board_revision", 1)
    actual = board_revision(conn)
    if expected != actual:
        raise ApiError("看板已发生变化，请刷新后重试", 409, "BOARD_REVISION_CONFLICT", {"expected": expected, "actual": actual})


def check_version(row, expected):
    if expected is None:
        return
    require_int(expected, "expected_version", 1)
    if expected != row["version"]:
        raise ApiError("内容已被其他操作修改，请刷新后重试", 409, "VERSION_CONFLICT", {"expected": expected, "actual": row["version"]})


def row_to_column(row):
    return {"id": row["id"], "name": row["name"], "position": row["position"], "deleted_at": row["deleted_at"], "version": row["version"]}


def row_to_card(row):
    result = {key: row[key] for key in ("id", "column_id", "title", "description", "labels", "due_date", "priority", "position", "archived", "created_at", "updated_at", "completed_at", "archived_at", "archive_reason", "version")}
    if "column_name" in row.keys():
        result.update(column_name=row["column_name"], column_deleted=bool(row["column_deleted"]))
        if "restore_column_id" in row.keys():
            result["restore_column_id"] = row["restore_column_id"]
    return result


def list_columns(conn, include_deleted=False):
    sql = "SELECT * FROM columns" + ("" if include_deleted else " WHERE deleted_at IS NULL") + " ORDER BY position,id"
    return [row_to_column(r) for r in conn.execute(sql)]


def list_cards(conn, column_id=None, archived=0):
    sql, params = "SELECT * FROM cards WHERE archived=?", [archived]
    if column_id is not None:
        sql += " AND column_id=?"
        params.append(column_id)
    return [row_to_card(r) for r in conn.execute(sql + " ORDER BY position,id", params)]


def get_card(conn, cid):
    row = conn.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
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
    rows = conn.execute("SELECT id FROM cards WHERE column_id=? AND archived=0 AND completed_at IS NOT NULL AND completed_at<=?", (completed["id"], cutoff)).fetchall()
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
    sql = "SELECT 1 FROM columns WHERE deleted_at IS NULL AND name = ? COLLATE NOCASE"
    params = [name]
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_id)
    if conn.execute(sql, params).fetchone() is not None:
        raise ApiError("列名已存在，请使用其他名称", 409, "COLUMN_NAME_EXISTS", {"field": "name"})


def create_column(conn, data):
    name = require_string(require_object(data).get("name"), "name", 1, 100)
    with transaction(conn):
        check_revision(conn, data.get("expected_board_revision"))
        ensure_unique_column_name(conn, name)
        pos = conn.execute("SELECT COALESCE(MAX(position),-1)+1 FROM columns WHERE deleted_at IS NULL").fetchone()[0]
        cur = conn.execute("INSERT INTO columns (name,position) VALUES (?,?)", (name, pos))
        revision = bump_revision(conn)
    return {"column": row_to_column(conn.execute("SELECT * FROM columns WHERE id=?", (cur.lastrowid,)).fetchone()), "revision": revision}


def update_column(conn, cid, data):
    name = require_string(require_object(data).get("name"), "name", 1, 100)
    with transaction(conn):
        row = active_column(conn, cid)
        check_version(row, data.get("expected_version")); check_revision(conn, data.get("expected_board_revision"))
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
    return [r["id"] for r in conn.execute("SELECT id FROM cards WHERE column_id=? AND archived=0 ORDER BY position,id", (column_id,)) if r["id"] != exclude]


def _rewrite_positions(conn, column_id, ids):
    for index, card_id in enumerate(ids):
        conn.execute("UPDATE cards SET position=? WHERE id=?", (index, card_id))


def delete_column(conn, cid, data=None):
    data = require_object(data or {})
    with transaction(conn):
        row = active_column(conn, cid)
        check_version(row, data.get("expected_version")); check_revision(conn, data.get("expected_board_revision"))
        if conn.execute("SELECT COUNT(*) FROM columns WHERE deleted_at IS NULL").fetchone()[0] <= 1:
            raise ApiError("不能删除最后一个列", 409, "LAST_ACTIVE_COLUMN")
        ts = now_iso()
        count = conn.execute("SELECT COUNT(*) FROM cards WHERE column_id=? AND archived=0", (cid,)).fetchone()[0]
        conn.execute("UPDATE cards SET archived=1,archived_at=?,archive_reason='column_deleted',updated_at=?,version=version+1 WHERE column_id=? AND archived=0", (ts, ts, cid))
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
        check_revision(conn, data.get("expected_board_revision"))
        active_ids = [r["id"] for r in conn.execute("SELECT id FROM columns WHERE deleted_at IS NULL ORDER BY position,id")]
        if set(ids) != set(active_ids) or len(ids) != len(active_ids):
            fail("必须提交全部活跃列的完整顺序", field="ids")
        for index, cid in enumerate(ids):
            conn.execute("UPDATE columns SET position=?,version=version+1 WHERE id=?", (index, cid))
        revision = bump_revision(conn)
    return {"columns": list_columns(conn), "revision": revision}


def create_card(conn, data):
    fields = normalize_card_fields(data); column_id = require_int(data.get("column_id"), "column_id", 1)
    with transaction(conn):
        check_revision(conn, data.get("expected_board_revision")); column = active_column(conn, column_id)
        position, ts = len(_active_card_ids(conn, column_id)), now_iso()
        completed_at = ts if is_completed_column(column) else None
        cur = conn.execute("""INSERT INTO cards (column_id,title,description,labels,due_date,priority,position,archived,created_at,updated_at,completed_at)
                            VALUES (?,?,?,?,?,?,?,0,?,?,?)""", (column_id, fields["title"], fields["description"], fields["labels"], fields["due_date"], fields["priority"], position, ts, ts, completed_at))
        revision = bump_revision(conn)
    return {"card": get_card(conn, cur.lastrowid), "revision": revision}


def update_card(conn, cid, data):
    fields = normalize_card_fields(data)
    with transaction(conn):
        row = conn.execute("SELECT * FROM cards WHERE id=? AND archived=0", (cid,)).fetchone()
        if row is None: raise ApiError("卡片不存在或已归档", 404, "CARD_NOT_FOUND")
        check_version(row, data.get("expected_version")); check_revision(conn, data.get("expected_board_revision"))
        conn.execute("UPDATE cards SET title=?,description=?,labels=?,due_date=?,priority=?,updated_at=?,version=version+1 WHERE id=?",
                     (fields["title"], fields["description"], fields["labels"], fields["due_date"], fields["priority"], now_iso(), cid))
        revision = bump_revision(conn)
    return {"card": get_card(conn, cid), "revision": revision}


def move_card(conn, cid, data):
    data = require_object(data); target = require_int(data.get("column_id"), "column_id", 1); position = require_int(data.get("position"), "position", 0)
    with transaction(conn):
        row = conn.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
        if row is None: raise ApiError("卡片不存在", 404, "CARD_NOT_FOUND")
        if row["archived"]: raise ApiError("归档卡片不能移动", 409, "CARD_ARCHIVED")
        check_version(row, data.get("expected_version")); check_revision(conn, data.get("expected_board_revision")); target_column = active_column(conn, target)
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
        row = conn.execute("SELECT * FROM cards WHERE id=? AND archived=0", (cid,)).fetchone()
        if row is None: raise ApiError("卡片不存在或已归档", 404, "CARD_NOT_FOUND")
        check_version(row, data.get("expected_version")); check_revision(conn, data.get("expected_board_revision"))
        ts = now_iso()
        conn.execute("UPDATE cards SET archived=1,archived_at=?,archive_reason='manual',updated_at=?,version=version+1 WHERE id=?", (ts, ts, cid))
        _rewrite_positions(conn, row["column_id"], _active_card_ids(conn, row["column_id"])); revision = bump_revision(conn)
    return {"ok": True, "column_id": row["column_id"], "position": row["position"], "version": row["version"] + 1, "revision": revision}


def restore_card(conn, cid, data=None):
    data = require_object(data or {})
    with transaction(conn):
        row = conn.execute("SELECT * FROM cards WHERE id=? AND archived=1", (cid,)).fetchone()
        if row is None: raise ApiError("归档卡片不存在", 404, "CARD_NOT_FOUND")
        check_version(row, data.get("expected_version")); check_revision(conn, data.get("expected_board_revision"))
        target = data.get("target_column_id")
        target_row = None
        if target is not None:
            target_row = conn.execute("SELECT * FROM columns WHERE id=? AND deleted_at IS NULL", (target,)).fetchone()
        else:
            target_row = conn.execute("SELECT * FROM columns WHERE id=? AND deleted_at IS NULL", (row["column_id"],)).fetchone()
            if target_row is None:
                original = conn.execute("SELECT name FROM columns WHERE id=?", (row["column_id"],)).fetchone()
                if original is not None:
                    target_row = conn.execute(
                        "SELECT * FROM columns WHERE deleted_at IS NULL AND name=? ORDER BY position,id LIMIT 1",
                        (original["name"],),
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
        row = conn.execute("SELECT * FROM cards WHERE id=? AND archived=0", (cid,)).fetchone()
        if row is None: raise ApiError("卡片不存在", 404, "CARD_NOT_FOUND")
        check_version(row, data.get("expected_version")); check_revision(conn, data.get("expected_board_revision"))
        ids = _active_card_ids(conn, row["column_id"], cid); position = min(row["position"] + 1, len(ids)); ts = now_iso()
        cur = conn.execute("""INSERT INTO cards (column_id,title,description,labels,due_date,priority,position,archived,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,0,?,?)""", (row["column_id"], row["title"] + "（副本）", row["description"], row["labels"], row["due_date"], row["priority"], position, ts, ts))
        ids.insert(position, cur.lastrowid); _rewrite_positions(conn, row["column_id"], ids); revision = bump_revision(conn)
    return {"card": get_card(conn, cur.lastrowid), "revision": revision}


def search_cards(conn, q="", date_from=None, date_to=None, priority=None, include_active=False):
    if date_from: validate_due_date(date_from)
    if date_to: validate_due_date(date_to)
    if priority: validate_priority(priority)
    sql = """SELECT cards.*,columns.name AS column_name,
              CASE WHEN columns.deleted_at IS NULL THEN 0 ELSE 1 END AS column_deleted,
              CASE WHEN columns.deleted_at IS NULL THEN columns.id ELSE
                  (SELECT matching.id FROM columns AS matching
                   WHERE matching.deleted_at IS NULL AND matching.name=columns.name
                   ORDER BY matching.position,matching.id LIMIT 1)
              END AS restore_column_id
              FROM cards JOIN columns ON columns.id=cards.column_id WHERE 1=1"""
    params = []
    if not include_active: sql += " AND cards.archived=1"
    if q: sql += " AND (cards.title LIKE ? OR cards.description LIKE ? OR cards.labels LIKE ?)"; params.extend(["%%%s%%" % q] * 3)
    if date_from: sql += " AND substr(cards.due_date,1,10)>=?"; params.append(date_from[:10])
    if date_to: sql += " AND substr(cards.due_date,1,10)<=?"; params.append(date_to[:10])
    if priority: sql += " AND cards.priority=?"; params.append(priority)
    return [row_to_card(r) for r in conn.execute(sql + " ORDER BY cards.updated_at DESC,cards.id DESC LIMIT 1000", params)]


def export_data(conn):
    return {"format": "kanban-export", "format_version": EXPORT_VERSION, "schema_version": SCHEMA_VERSION,
            "board_revision": board_revision(conn), "exported_at": now_iso(), "columns": list_columns(conn, True),
            "cards": list_cards(conn, archived=0) + list_cards(conn, archived=1)}


def normalize_import(data):
    data = require_object(data)
    if data.get("format") not in (None, "kanban-export") or data.get("format_version", 1) not in (1, 2):
        fail("不支持的导入格式或版本", code="UNSUPPORTED_IMPORT_VERSION")
    columns, cards = data.get("columns"), data.get("cards")
    if not isinstance(columns, list) or not isinstance(cards, list) or not columns: fail("导入文件必须包含非空 columns 和 cards 数组")
    normalized_columns, column_ids = [], set()
    for index, item in enumerate(columns):
        item = require_object(item); cid = require_int(item.get("id"), "columns.id", 1)
        if cid in column_ids: fail("列 id 重复")
        column_ids.add(cid); version = item.get("version", 1)
        normalized_columns.append({"id": cid, "name": require_string(item.get("name"), "name", 1, 100), "position": index,
                                   "deleted_at": item.get("deleted_at"), "version": version if isinstance(version, int) and version > 0 else 1})
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
    return {"ok": True, "columns": len(normalized["columns"]), "cards": sum(not c["archived"] for c in normalized["cards"]), "archived_cards": sum(c["archived"] for c in normalized["cards"])}


def import_replace(conn, request):
    request = require_object(request); normalized = normalize_import(request.get("data"))
    with DB_MAINTENANCE_LOCK:
        check_revision(conn, request.get("expected_board_revision")); backup = create_backup("pre-import")
        with transaction(conn):
            check_revision(conn, request.get("expected_board_revision")); conn.execute("DELETE FROM cards"); conn.execute("DELETE FROM columns")
            for col in normalized["columns"]:
                conn.execute("INSERT INTO columns (id,name,position,deleted_at,version) VALUES (?,?,?,?,?)", (col["id"], col["name"], col["position"], col["deleted_at"], col["version"]))
            for card in normalized["cards"]:
                conn.execute("""INSERT INTO cards (id,column_id,title,description,labels,due_date,priority,position,archived,created_at,updated_at,completed_at,archived_at,archive_reason,version)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (card["id"], card["column_id"], card["title"], card["description"], card["labels"], card["due_date"], card["priority"], card["position"], card["archived"], card["created_at"], card["updated_at"], card["completed_at"], card["archived_at"], card["archive_reason"], card["version"]))
            revision = bump_revision(conn)
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None: raise ApiError("导入数据外键检查失败", 422, "INVALID_IMPORT_REFERENCE")
    return {"ok": True, "imported": import_preview(request.get("data")), "backup": os.path.basename(backup), "revision": revision}


class Handler(BaseHTTPRequestHandler):
    server_version = "KanbanServer/2.0"

    def log_message(self, fmt, *args): sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))
    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8"); self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self._security_headers(); self.end_headers(); self.wfile.write(body)

    def _send_error(self, error):
        payload = {"error": {"code": error.code, "message": error.message}}
        if error.details is not None: payload["error"]["details"] = error.details
        self._send_json(payload, error.status)

    def _send_text(self, data, status=200, content_type="text/plain; charset=utf-8"):
        body = data.encode("utf-8") if isinstance(data, str) else data; self.send_response(status)
        self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self._security_headers(); self.end_headers(); self.wfile.write(body)

    def _read_json_body(self, maximum=MAX_JSON_BODY):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json": raise ApiError("请求必须使用 application/json", 415, "UNSUPPORTED_MEDIA_TYPE")
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: raise ApiError("Content-Length 无效", 400, "INVALID_CONTENT_LENGTH")
        if length <= 0: raise ApiError("请求体不能为空", 400, "INVALID_JSON")
        if length > maximum: raise ApiError("请求体过大", 413, "PAYLOAD_TOO_LARGE")
        try: value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError): raise ApiError("请求体不是有效 JSON", 400, "INVALID_JSON")
        return require_object(value)

    def _serve_static(self, rel_path):
        rel_path = rel_path.replace("\\", "/").lstrip("/"); file_path = os.path.join(STATIC_DIR, "index.html") if rel_path in ("", "index.html") else os.path.normpath(os.path.join(STATIC_DIR, rel_path))
        if os.path.commonpath((os.path.abspath(STATIC_DIR), os.path.abspath(file_path))) != os.path.abspath(STATIC_DIR): return self._send_text("Forbidden", 403)
        if not os.path.isfile(file_path): return self._send_text("Not Found", 404)
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml"}.get(os.path.splitext(file_path)[1].lower(), "application/octet-stream")
        with open(file_path, "rb") as handle: self._send_text(handle.read(), content_type=ctype)

    def _send_backup(self):
        with DB_MAINTENANCE_LOCK: path = create_backup("download")
        try:
            filename = "kanban-backup-%s.db" % datetime.now().strftime("%Y%m%d-%H%M%S"); self.send_response(200)
            self.send_header("Content-Type", "application/vnd.sqlite3"); self.send_header("Content-Length", str(os.path.getsize(path))); self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename); self._security_headers(); self.end_headers()
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk: break
                    self.wfile.write(chunk)
        finally:
            try: os.remove(path)
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
        conn = get_conn()
        try:
            if path == "/api/board" and method == "GET": return self._send_json(get_board(conn))
            if path == "/api/columns" and method == "GET": return self._send_json(list_columns(conn))
            if path == "/api/columns" and method == "POST": return self._send_json(create_column(conn, self._read_json_body()), 201)
            if path == "/api/columns/reorder" and method == "POST": return self._send_json(reorder_columns(conn, self._read_json_body()))
            match = re.fullmatch(r"/api/columns/(\d+)", path)
            if match:
                cid = int(match.group(1))
                if method == "PUT": return self._send_json(update_column(conn, cid, self._read_json_body()))
                if method == "DELETE": return self._send_json(delete_column(conn, cid, self._read_json_body() if int(self.headers.get("Content-Length", "0")) else {}))
            if path == "/api/cards" and method == "GET":
                raw = query.get("column_id", [None])[0]
                if raw is not None and not raw.isdigit(): fail("column_id 无效", field="column_id")
                return self._send_json(list_cards(conn, int(raw) if raw is not None else None))
            if path == "/api/cards" and method == "POST": return self._send_json(create_card(conn, self._read_json_body()), 201)
            match = re.fullmatch(r"/api/cards/(\d+)", path)
            if match:
                cid = int(match.group(1))
                if method == "GET": return self._send_json(get_card(conn, cid))
                if method == "PUT": return self._send_json(update_card(conn, cid, self._read_json_body()))
                if method == "DELETE": return self._send_json(archive_card(conn, cid, self._read_json_body() if int(self.headers.get("Content-Length", "0")) else {}))
            for suffix, action, verb in (("move", move_card, "PUT"), ("restore", restore_card, "POST"), ("copy", copy_card, "POST")):
                match = re.fullmatch(r"/api/cards/(\d+)/%s" % suffix, path)
                if match and method == verb: return self._send_json(action(conn, int(match.group(1)), self._read_json_body()))
            if path == "/api/search" and method == "GET": return self._send_json(search_cards(conn, query.get("q", [""])[0], query.get("from", [None])[0], query.get("to", [None])[0], query.get("priority", [None])[0], query.get("all", ["0"])[0].lower() in ("1", "true")))
            if path == "/api/export" and method == "GET":
                body = json.dumps(export_data(conn), ensure_ascii=False, indent=2).encode("utf-8"); filename = "kanban-export-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename); self._security_headers(); self.end_headers(); self.wfile.write(body); return
            if path == "/api/backup" and method == "GET": return self._send_backup()
            if path == "/api/import/preview" and method == "POST": return self._send_json(import_preview(self._read_json_body(MAX_IMPORT_BODY)))
            if path == "/api/import" and method == "POST": return self._send_json(import_replace(conn, self._read_json_body(MAX_IMPORT_BODY)))
            raise ApiError("未知的 API 路径", 404, "NOT_FOUND")
        finally: conn.close()

    def do_GET(self): self._route("GET")
    def do_POST(self): self._route("POST")
    def do_PUT(self): self._route("PUT")
    def do_DELETE(self): self._route("DELETE")
    def do_OPTIONS(self): self.send_response(204); self._security_headers(); self.end_headers()


def main():
    init_db(); server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 60); print("  看板系统已启动"); print("  访问地址: http://%s:%d" % (HOST, PORT))
    if HOST not in ("127.0.0.1", "localhost", "::1"): print("  警告：服务已开放给其他设备，当前系统没有登录认证。")
    print("  数据库: %s" % DB_PATH); print("  按 Ctrl+C 停止"); print("=" * 60)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n正在停止..."); server.shutdown()


if __name__ == "__main__": main()
