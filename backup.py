# -*- coding: utf-8 -*-
"""备份与恢复:数据库安全备份、完整 ZIP 备份、恢复预览与替换。"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime

import config
import db
from db import get_conn, transaction, board_revision
from errors import ApiError, require_string, validate_priority, validate_due_date
from security import attachment_path, validate_attachment_name, column_name_key, sanitize_description
from sync import MAINTENANCE_GATE, DB_MAINTENANCE_LOCK, MAINTENANCE_REPORT
from utils import ensure_free_space, stream_copy_limited, sha256_file, now_iso

RESTORE_TOKENS = {}
RESTORE_TOKEN_LOCK = threading.Lock()
RESTORE_TOKEN_TTL = 60 * 60


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
    database_size = os.path.getsize(config.DB_PATH) if os.path.isfile(config.DB_PATH) else 0
    ensure_free_space(config.BACKUP_DIR, database_size * 2 + attachment_size)
    return {"ok": True, "attachment_count": len(rows), "attachment_size": attachment_size}


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


def create_full_backup(prefix="backup"):
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    with MAINTENANCE_GATE.exclusive(), DB_MAINTENANCE_LOCK:
        db_snapshot = db.create_backup("snapshot")
        final_path = os.path.join(config.BACKUP_DIR, "kanban-%s-%s.zip" % (prefix, datetime.now().strftime("%Y%m%d-%H%M%S-%f")))
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".kanban-full-", suffix=".tmp", dir=config.BACKUP_DIR)
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
            ensure_free_space(config.BACKUP_DIR, required_output)
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(db_snapshot, "kanban.db")
                for item in attachment_manifest:
                    card_id, file_name = item["path"].split("/", 2)[1:]
                    archive.write(attachment_path(int(card_id), file_name), item["path"])
                manifest = {"format": "kanban-full-backup", "format_version": 2, "schema_version": config.SCHEMA_VERSION,
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
    if len(entries) > config.MAX_ZIP_ENTRIES:
        raise ApiError("ZIP 文件条目过多", 413, "ZIP_TOO_MANY_ENTRIES")
    expanded = sum(info.file_size for info in entries)
    if config.MAX_ZIP_EXPANDED_BYTES and expanded > config.MAX_ZIP_EXPANDED_BYTES:
        raise ApiError("ZIP 展开后内容过大", 413, "ZIP_EXPANDED_TOO_LARGE")
    seen = set()
    result = []
    for info in entries:
        name = info.filename
        if not name or name.endswith("/"):
            continue
        if info.file_size and info.compress_size and info.file_size / max(info.compress_size, 1) > config.MAX_ZIP_RATIO:
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
    extract_dir = tempfile.mkdtemp(prefix="kanban-restore-", dir=config.BACKUP_DIR) if extract else None
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
            fd, db_path = tempfile.mkstemp(prefix=".restore-check-", suffix=".db", dir=config.BACKUP_DIR)
            os.close(fd)
            with archive.open("kanban.db") as source, open(db_path, "wb") as output:
                stream_copy_limited(source, output, by_name["kanban.db"].file_size, config.BACKUP_DIR)
            if version == 2:
                database = manifest.get("database") or {}
                if database.get("size") != os.path.getsize(db_path) or database.get("sha256") != sha256_file(db_path):
                    raise ApiError("备份数据库哈希校验失败", 422, "BACKUP_HASH_MISMATCH")
        check = get_conn(db_path)
        try:
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok" or check.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ApiError("备份数据库完整性检查失败", 422, "INVALID_BACKUP_DATABASE")
            if check.execute("PRAGMA user_version").fetchone()[0] != config.SCHEMA_VERSION:
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
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    ensure_free_space(config.BACKUP_DIR, min(content_length, 65536))
    fd, path = tempfile.mkstemp(prefix=".restore-upload-", suffix=".zip", dir=config.BACKUP_DIR)
    os.close(fd)
    try:
        with open(path, "wb") as output:
            stream_copy_limited(input_stream, output, content_length, config.BACKUP_DIR)
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
    rollback_db = config.DB_PATH + ".restore-rollback-" + operation
    rollback_wal = rollback_db + "-wal"
    rollback_shm = rollback_db + "-shm"
    rollback_dir = config.ATTACHMENTS_DIR + ".restore-rollback-" + operation
    new_db = os.path.join(extracted, "kanban.db")
    new_dir = os.path.join(extracted, "attachments")
    os.makedirs(new_dir, exist_ok=True)
    backup_file = None
    moved = {"db": False, "wal": False, "shm": False, "attachments": False}
    try:
        with MAINTENANCE_GATE.exclusive(), DB_MAINTENANCE_LOCK:
            check_revision(conn, expected_revision, required=True)
            old_revision = board_revision(conn)
            backup_file = create_full_backup("pre-restore")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            moved["db"] = _move_if_exists(config.DB_PATH, rollback_db)
            moved["wal"] = _move_if_exists(config.DB_PATH + "-wal", rollback_wal)
            moved["shm"] = _move_if_exists(config.DB_PATH + "-shm", rollback_shm)
            moved["attachments"] = _move_if_exists(config.ATTACHMENTS_DIR, rollback_dir)
            try:
                os.replace(new_db, config.DB_PATH)
                os.replace(new_dir, config.ATTACHMENTS_DIR)
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
                for path in (config.DB_PATH + "-wal", config.DB_PATH + "-shm"):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass
                shutil.rmtree(config.ATTACHMENTS_DIR, ignore_errors=True)
                try:
                    if os.path.isfile(config.DB_PATH):
                        os.remove(config.DB_PATH)
                except OSError:
                    pass
                if moved["db"]:
                    os.replace(rollback_db, config.DB_PATH)
                if moved["wal"]:
                    os.replace(rollback_wal, config.DB_PATH + "-wal")
                if moved["shm"]:
                    os.replace(rollback_shm, config.DB_PATH + "-shm")
                if moved["attachments"]:
                    os.replace(rollback_dir, config.ATTACHMENTS_DIR)
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
        return {"ok": True, "preview": preview, "backup": os.path.basename(backup_file), "revision": new_revision}
    except Exception:
        with RESTORE_TOKEN_LOCK:
            current = RESTORE_TOKENS.get(token)
            if current:
                current["claimed"] = False
        raise
    finally:
        shutil.rmtree(extracted, ignore_errors=True)
