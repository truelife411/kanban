# -*- coding: utf-8 -*-
"""附件层:附件元数据的增删查与文件落盘/回滚。"""

import errno
import os
import sqlite3
import tempfile
import uuid

import config
import security
from errors import ApiError
from security import attachment_name_key, attachment_path, attachment_directory, validate_attachment_name
from sync import MAINTENANCE_GATE, DB_MAINTENANCE_LOCK, MAINTENANCE_REPORT
from utils import ensure_free_space, stream_copy_limited, now_iso, file_in_use_error


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
        raise file_in_use_error(error)
    except Exception as error:
        if not committed:
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
        raise file_in_use_error(error)


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
        raise file_in_use_error(error)
