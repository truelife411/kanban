# -*- coding: utf-8 -*-
"""数据库层:连接、事务、建表与迁移、看板版本、基础查询与位置维护。"""

import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import config
import security
from errors import ApiError, require_int, fail
from security import column_name_key, sanitize_description, attachment_directory, attachment_path
from sync import MAINTENANCE_GATE, DB_MAINTENANCE_LOCK, MAINTENANCE_REPORT
from utils import now_iso

DRAFT_TTL = 24 * 60 * 60
CLEANUP_AGE = 24 * 60 * 60
MAX_DATABASE_BACKUPS = 10
DATABASE_BACKUP_NAME = re.compile(r"^kanban-[A-Za-z0-9_-]+-(\d{8}-\d{6}-\d{6})\.db$")


def get_conn(path=None):
    conn = sqlite3.connect(path or config.DB_PATH, timeout=5.0, isolation_level=None)
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


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _column_exists(conn, table, name):
    return any(row["name"] == name for row in conn.execute("PRAGMA table_info(%s)" % table))


def _rotate_database_backups():
    try:
        names = []
        for name in os.listdir(config.BACKUP_DIR):
            match = DATABASE_BACKUP_NAME.fullmatch(name)
            if match and os.path.isfile(os.path.join(config.BACKUP_DIR, name)):
                names.append((match.group(1), name))
        names.sort()
    except OSError as error:
        sys.stderr.write("数据库备份清理失败：%s\n" % error)
        return
    for _, name in names[:-MAX_DATABASE_BACKUPS]:
        try:
            os.remove(os.path.join(config.BACKUP_DIR, name))
        except OSError as error:
            sys.stderr.write("数据库备份清理失败（%s）：%s\n" % (name, error))


def create_backup(prefix="auto", required=True):
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    while True:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        final_path = os.path.join(config.BACKUP_DIR, "kanban-%s-%s.db" % (prefix, stamp))
        if not os.path.exists(final_path):
            break
        time.sleep(0.000001)
    fd, temp_path = tempfile.mkstemp(prefix=".kanban-", suffix=".tmp", dir=config.BACKUP_DIR)
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
    os.makedirs(config.ATTACHMENTS_DIR, exist_ok=True)
    for name in os.listdir(config.ATTACHMENTS_DIR):
        if ".permanent-delete-" not in name:
            continue
        rollback = os.path.join(config.ATTACHMENTS_DIR, name)
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
    for root, _, files in os.walk(config.ATTACHMENTS_DIR):
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
                    planned_date TEXT NOT NULL DEFAULT '',planned_position INTEGER,
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
                conn.execute("CREATE INDEX idx_cards_planned_date_position ON cards(planned_date,planned_position,id)")
                conn.execute("INSERT INTO board_state VALUES (1,1,?)", (now_iso(),))
                conn.executemany("INSERT INTO columns (name,position) VALUES (?,?)", [("待办", 0), ("进行中", 1), ("已完成", 2)])
                conn.execute("PRAGMA user_version = 6")
        else:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > config.SCHEMA_VERSION:
                raise RuntimeError("数据库版本高于当前程序支持版本")
            if version < config.SCHEMA_VERSION:
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
            if version < 6:
                with transaction(conn):
                    if not _column_exists(conn, "cards", "planned_date"):
                        conn.execute("ALTER TABLE cards ADD COLUMN planned_date TEXT NOT NULL DEFAULT ''")
                    if not _column_exists(conn, "cards", "planned_position"):
                        conn.execute("ALTER TABLE cards ADD COLUMN planned_position INTEGER")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_planned_date_position ON cards(planned_date,planned_position,id)")
                    conn.execute("PRAGMA user_version = 6")
                version = 6
            with transaction(conn):
                if not _column_exists(conn, "cards", "is_draft"):
                    conn.execute("ALTER TABLE cards ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_columns_active_position ON columns(deleted_at,position,id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_active_position ON cards(is_draft,archived,column_id,position,id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_archived_updated ON cards(is_draft,archived,updated_at,id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_auto_archive ON cards(is_draft,archived,column_id,completed_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_draft_updated ON cards(is_draft,updated_at,id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_planned_date_position ON cards(planned_date,planned_position,id)")
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
    os.makedirs(config.ATTACHMENTS_DIR, exist_ok=True)


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
    result = {key: row[key] for key in ("id", "column_id", "title", "description", "labels", "due_date", "planned_date", "planned_position", "priority", "position", "archived", "created_at", "updated_at", "completed_at", "archived_at", "archive_reason", "version")}
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
        result = {"schema_version": config.SCHEMA_VERSION, "revision": board_revision(conn), "columns": list_columns(conn), "cards": list_cards(conn)}
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


def _active_card_ids(conn, column_id, exclude=None):
    return [r["id"] for r in conn.execute("SELECT id FROM cards WHERE column_id=? AND archived=0 AND is_draft=0 ORDER BY position,id", (column_id,)) if r["id"] != exclude]


def _rewrite_positions(conn, column_id, ids):
    for index, card_id in enumerate(ids):
        conn.execute("UPDATE cards SET position=? WHERE id=?", (index, card_id))


def _planned_card_ids(conn, planned_date, exclude=None):
    return [r["id"] for r in conn.execute("""SELECT id FROM cards WHERE planned_date=? AND archived=0 AND is_draft=0
                                             ORDER BY planned_position,id""", (planned_date,)) if r["id"] != exclude]


def _rewrite_planned_positions(conn, planned_date, ids=None):
    if not planned_date:
        return
    ids = _planned_card_ids(conn, planned_date) if ids is None else ids
    for index, card_id in enumerate(ids):
        conn.execute("UPDATE cards SET planned_position=? WHERE id=?", (index, card_id))


def _set_card_plan(conn, cid, old_date, new_date, requested_position=None):
    if old_date:
        old_ids = _planned_card_ids(conn, old_date, cid)
        _rewrite_planned_positions(conn, old_date, old_ids)
    if not new_date:
        conn.execute("UPDATE cards SET planned_date='',planned_position=NULL WHERE id=?", (cid,))
        return
    new_ids = _planned_card_ids(conn, new_date, cid)
    if requested_position is None:
        if old_date == new_date:
            current_position = conn.execute(
                "SELECT planned_position FROM cards WHERE id=?", (cid,)
            ).fetchone()[0]
            position = min(
                current_position if current_position is not None else len(new_ids),
                len(new_ids),
            )
        else:
            position = len(new_ids)
    else:
        position = min(requested_position, len(new_ids))
    new_ids.insert(position, cid)
    conn.execute("UPDATE cards SET planned_date=? WHERE id=?", (new_date, cid))
    _rewrite_planned_positions(conn, new_date, new_ids)
