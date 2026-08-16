# -*- coding: utf-8 -*-
"""业务层:列/卡片 CRUD、移动与排序、批量操作、搜索、JSON 导入导出。"""

import base64
import json
import os
import re
import shutil
import uuid

import backup
import config
import db
from db import (transaction, check_revision, check_version, board_revision, bump_revision,
                list_columns, list_cards, get_card, is_completed_column, active_column,
                _active_card_ids, _rewrite_positions, _set_card_plan, _rewrite_planned_positions)
from errors import ApiError, fail, require_object, require_int, require_string, validate_priority, validate_due_date, validate_planned_date
from security import sanitize_description, column_name_key, attachment_name_key, attachment_directory, attachment_path
from sync import DB_MAINTENANCE_LOCK, MAINTENANCE_REPORT
from utils import now_iso, file_in_use_error


def normalize_card_fields(data):
    require_object(data)
    return {
        "title": require_string(data.get("title"), "title", 1, 300),
        "description": sanitize_description(data.get("description", "")),
        "labels": require_string(data.get("labels", ""), "labels", 0, 2000),
        "due_date": validate_due_date(data.get("due_date", "")),
        "planned_date": validate_planned_date(data.get("planned_date", "")),
        "priority": validate_priority(data.get("priority", "medium")),
    }


def create_column(conn, data):
    name = require_string(require_object(data).get("name"), "name", 1, 100)
    with transaction(conn):
        check_revision(conn, data.get("expected_board_revision"), required=True)
        db.ensure_unique_column_name(conn, name)
        pos = conn.execute("SELECT COALESCE(MAX(position),-1)+1 FROM columns WHERE deleted_at IS NULL").fetchone()[0]
        cur = conn.execute("INSERT INTO columns (name,position) VALUES (?,?)", (name, pos))
        revision = bump_revision(conn)
    return {"column": db.row_to_column(conn.execute("SELECT * FROM columns WHERE id=?", (cur.lastrowid,)).fetchone()), "revision": revision}


def update_column(conn, cid, data):
    name = require_string(require_object(data).get("name"), "name", 1, 100)
    with transaction(conn):
        row = active_column(conn, cid)
        check_version(row, data.get("expected_version"), required=True); check_revision(conn, data.get("expected_board_revision"), required=True)
        db.ensure_unique_column_name(conn, name, cid)
        was_completed = is_completed_column(row)
        becomes_completed = name.strip() == "已完成"
        conn.execute("UPDATE columns SET name=?,version=version+1 WHERE id=?", (name, cid))
        if becomes_completed and not was_completed:
            conn.execute("UPDATE cards SET completed_at=? WHERE column_id=? AND archived=0", (now_iso(), cid))
        elif was_completed and not becomes_completed:
            conn.execute("UPDATE cards SET completed_at=NULL WHERE column_id=? AND archived=0", (cid,))
        revision = bump_revision(conn)
    return {"column": db.row_to_column(conn.execute("SELECT * FROM columns WHERE id=?", (cid,)).fetchone()), "revision": revision}


def delete_column(conn, cid, data=None):
    data = require_object(data or {})
    with transaction(conn):
        row = active_column(conn, cid)
        check_version(row, data.get("expected_version"), required=True); check_revision(conn, data.get("expected_board_revision"), required=True)
        if conn.execute("SELECT COUNT(*) FROM columns WHERE deleted_at IS NULL").fetchone()[0] <= 1:
            raise ApiError("不能删除最后一个列", 409, "LAST_ACTIVE_COLUMN")
        ts = now_iso()
        count = conn.execute("SELECT COUNT(*) FROM cards WHERE column_id=? AND archived=0 AND is_draft=0", (cid,)).fetchone()[0]
        planned_dates = [r["planned_date"] for r in conn.execute(
            "SELECT DISTINCT planned_date FROM cards "
            "WHERE column_id=? AND archived=0 AND is_draft=0 AND planned_date!=''",
            (cid,),
        )]
        conn.execute("UPDATE cards SET archived=1,archived_at=?,archive_reason='column_deleted',updated_at=?,version=version+1 WHERE column_id=? AND archived=0 AND is_draft=0", (ts, ts, cid))
        for planned_date in planned_dates:
            _rewrite_planned_positions(conn, planned_date)
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
        conn.execute("""UPDATE cards SET column_id=?,title=?,description=?,labels=?,due_date=?,planned_date=?,priority=?,position=?,completed_at=?,updated_at=?,is_draft=0,version=version+1 WHERE id=?""",
                     (column_id, fields["title"], fields["description"], fields["labels"], fields["due_date"], fields["planned_date"], fields["priority"], position, completed_at, ts, cid))
        if fields["planned_date"]:
            _set_card_plan(conn, cid, "", fields["planned_date"])
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
        cur = conn.execute("""INSERT INTO cards (column_id,title,description,labels,due_date,planned_date,priority,position,archived,created_at,updated_at,completed_at)
                            VALUES (?,?,?,?,?,?,?, ?,0,?,?,?)""", (column_id, fields["title"], fields["description"], fields["labels"], fields["due_date"], fields["planned_date"], fields["priority"], position, ts, ts, completed_at))
        if fields["planned_date"]:
            _set_card_plan(conn, cur.lastrowid, "", fields["planned_date"])
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
        row = conn.execute("SELECT * FROM cards WHERE id=? AND is_draft=0", (cid,)).fetchone()
        if row is None:
            raise ApiError("卡片不存在或已归档", 404, "CARD_NOT_FOUND")
        check_version(row, data.get("expected_version"), required=True); check_revision(conn, data.get("expected_board_revision"), required=True)
        if row["archived"]:
            # 归档卡片:允许编辑内容字段,保持归档状态;列/位置/计划日期不变
            ts = now_iso()
            conn.execute("UPDATE cards SET title=?,description=?,labels=?,due_date=?,priority=?,updated_at=?,version=version+1 WHERE id=?",
                         (fields["title"], fields["description"], fields["labels"], fields["due_date"], fields["priority"], ts, cid))
            revision = bump_revision(conn)
            return {"card": get_card(conn, cid), "revision": revision}
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
        conn.execute("""UPDATE cards SET column_id=?,position=?,title=?,description=?,labels=?,due_date=?,planned_date=?,priority=?,completed_at=?,updated_at=?,version=version+1 WHERE id=?""",
                     (target_column_id, position, fields["title"], fields["description"], fields["labels"], fields["due_date"], fields["planned_date"], fields["priority"], completed_at, ts, cid))
        _set_card_plan(conn, cid, row["planned_date"], fields["planned_date"])
        revision = bump_revision(conn)
    return {"card": get_card(conn, cid), "revision": revision}


def plan_card(conn, cid, data):
    data = require_object(data)
    planned_date = validate_planned_date(data.get("planned_date"))
    position = data.get("position")
    if position is not None:
        position = require_int(position, "position", 0)
    with transaction(conn):
        row = conn.execute("SELECT * FROM cards WHERE id=? AND archived=0 AND is_draft=0", (cid,)).fetchone()
        if row is None:
            raise ApiError("卡片不存在或已归档", 404, "CARD_NOT_FOUND")
        check_version(row, data.get("expected_version"), required=True)
        check_revision(conn, data.get("expected_board_revision"), required=True)
        _set_card_plan(conn, cid, row["planned_date"], planned_date, position)
        conn.execute("UPDATE cards SET updated_at=?,version=version+1 WHERE id=?", (now_iso(), cid))
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
        _rewrite_positions(conn, row["column_id"], _active_card_ids(conn, row["column_id"]))
        _rewrite_planned_positions(conn, row["planned_date"])
        revision = bump_revision(conn)
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
                    _rewrite_planned_positions(conn, row["planned_date"])
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
            raise file_in_use_error(error)


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
            target_row = _resolve_restore_target(conn, row)
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
        cur = conn.execute("""INSERT INTO cards (column_id,title,description,labels,due_date,planned_date,planned_position,priority,position,archived,created_at,updated_at,completed_at)
                            VALUES (?,?,?,?,?,'',NULL,?,?,0,?,?,?)""", (row["column_id"], row["title"] + "（副本）", row["description"], row["labels"], row["due_date"], row["priority"], position, ts, ts, completed_at))
        ids.insert(position, cur.lastrowid)
        _rewrite_positions(conn, row["column_id"], ids)
        revision = bump_revision(conn)
    return {"card": get_card(conn, cur.lastrowid), "revision": revision, "attachments_copied": False}


def _normalize_batch_items(data):
    items = require_object(data).get("items")
    if not isinstance(items, list) or not items:
        fail("items 必须是非空数组", field="items")
    if len(items) > 200:
        fail("批量操作一次最多处理 200 张卡片", field="items")
    result, seen = [], set()
    for index, item in enumerate(items):
        item = require_object(item)
        cid = require_int(item.get("id"), "items.%d.id" % index, 1)
        if cid in seen:
            fail("卡片 id 不能重复", field="items.%d.id" % index)
        seen.add(cid)
        version = item.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            fail("缺少卡片版本", field="items.%d.version" % index)
        result.append((cid, version))
    return result


def batch_archive_cards(conn, data):
    items = _normalize_batch_items(data)
    with transaction(conn):
        check_revision(conn, data.get("expected_board_revision"), required=True)
        ts = now_iso()
        affected_columns, affected_planned_dates = set(), set()
        for cid, expected_version in items:
            row = conn.execute("SELECT * FROM cards WHERE id=? AND archived=0 AND is_draft=0", (cid,)).fetchone()
            if row is None:
                raise ApiError("卡片不存在或已归档", 404, "CARD_NOT_FOUND")
            check_version(row, expected_version, required=True)
            conn.execute("UPDATE cards SET archived=1,archived_at=?,archive_reason='manual',updated_at=?,version=version+1 WHERE id=?", (ts, ts, cid))
            affected_columns.add(row["column_id"])
            if row["planned_date"]:
                affected_planned_dates.add(row["planned_date"])
        for column_id in affected_columns:
            _rewrite_positions(conn, column_id, _active_card_ids(conn, column_id))
        for planned_date in affected_planned_dates:
            _rewrite_planned_positions(conn, planned_date)
        revision = bump_revision(conn)
    return {"ok": True, "count": len(items), "revision": revision}


def _resolve_restore_target(conn, row, requested_target=None):
    if requested_target is not None:
        target_row = conn.execute("SELECT * FROM columns WHERE id=? AND deleted_at IS NULL", (requested_target,)).fetchone()
        if target_row is None:
            raise ApiError("指定的恢复列不存在或已删除", 404, "COLUMN_NOT_FOUND")
        return target_row
    target_row = conn.execute("SELECT * FROM columns WHERE id=? AND deleted_at IS NULL", (row["column_id"],)).fetchone()
    if target_row is None:
        original = conn.execute("SELECT name FROM columns WHERE id=?", (row["column_id"],)).fetchone()
        if original is not None:
            target_row = conn.execute(
                "SELECT * FROM columns WHERE deleted_at IS NULL AND column_name_key(name)=? ORDER BY position,id LIMIT 1",
                (column_name_key(original["name"]),),
            ).fetchone()
    if target_row is None:
        target_row = conn.execute("SELECT * FROM columns WHERE deleted_at IS NULL ORDER BY position,id LIMIT 1").fetchone()
    return target_row


def batch_restore_cards(conn, data):
    items = _normalize_batch_items(data)
    target = data.get("target_column_id")
    if target is not None:
        target = require_int(target, "target_column_id", 1)
    with transaction(conn):
        check_revision(conn, data.get("expected_board_revision"), required=True)
        ts = now_iso()
        for cid, expected_version in items:
            row = conn.execute("SELECT * FROM cards WHERE id=? AND archived=1 AND is_draft=0", (cid,)).fetchone()
            if row is None:
                raise ApiError("归档卡片不存在", 404, "CARD_NOT_FOUND")
            check_version(row, expected_version, required=True)
            target_row = _resolve_restore_target(conn, row, target)
            if target_row is None:
                raise ApiError("没有可用列", 409, "NO_ACTIVE_COLUMN")
            ids = _active_card_ids(conn, target_row["id"], cid)
            position = len(ids)
            ids.insert(position, cid)
            _rewrite_positions(conn, target_row["id"], ids)
            completed_at = ts if is_completed_column(target_row) else None
            conn.execute("UPDATE cards SET archived=0,column_id=?,position=?,completed_at=?,archived_at=NULL,archive_reason=NULL,updated_at=?,version=version+1 WHERE id=?",
                         (target_row["id"], position, completed_at, ts, cid))
        revision = bump_revision(conn)
    return {"ok": True, "count": len(items), "revision": revision}


def batch_permanently_delete_cards(conn, data):
    items = _normalize_batch_items(data)
    rollbacks = []
    committed = False
    with DB_MAINTENANCE_LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                check_revision(conn, data.get("expected_board_revision"), required=True)
                affected_columns, affected_planned_dates = set(), set()
                for cid, expected_version in items:
                    row = conn.execute("SELECT * FROM cards WHERE id=? AND is_draft=0", (cid,)).fetchone()
                    if row is None:
                        raise ApiError("卡片不存在", 404, "CARD_NOT_FOUND")
                    check_version(row, expected_version, required=True)
                    directory = attachment_directory(cid)
                    if os.path.isdir(directory):
                        rollback = directory + ".permanent-delete-" + uuid.uuid4().hex
                        os.replace(directory, rollback)
                        rollbacks.append(rollback)
                    conn.execute("DELETE FROM cards WHERE id=? AND is_draft=0", (cid,))
                    if not row["archived"]:
                        affected_columns.add(row["column_id"])
                        if row["planned_date"]:
                            affected_planned_dates.add(row["planned_date"])
                for column_id in affected_columns:
                    _rewrite_positions(conn, column_id, _active_card_ids(conn, column_id))
                for planned_date in affected_planned_dates:
                    _rewrite_planned_positions(conn, planned_date)
                revision = bump_revision(conn)
                conn.commit()
                committed = True
            except Exception:
                conn.rollback()
                for rollback in reversed(rollbacks):
                    original = rollback.split(".permanent-delete-", 1)[0]
                    if os.path.isdir(rollback) and not os.path.exists(original):
                        os.replace(rollback, original)
                raise
            for rollback in rollbacks:
                try:
                    shutil.rmtree(rollback)
                except OSError:
                    MAINTENANCE_REPORT["cleanup"].append(rollback)
            return {"ok": True, "count": len(items), "revision": revision}
        except Exception as error:
            if not committed:
                for rollback in reversed(rollbacks):
                    original = rollback.split(".permanent-delete-", 1)[0]
                    try:
                        if os.path.isdir(rollback) and not os.path.exists(original):
                            os.replace(rollback, original)
                    except OSError:
                        pass
            raise file_in_use_error(error)


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


def like_escape(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_cards_page(conn, q="", date_from=None, date_to=None, priority=None, include_active=False, cursor=None, limit=50, sort="archived_desc", created_from=None, created_to=None, updated_from=None, updated_to=None):
    if date_from: validate_due_date(date_from)
    if date_to: validate_due_date(date_to)
    if created_from: validate_due_date(created_from)
    if created_to: validate_due_date(created_to)
    if updated_from: validate_due_date(updated_from)
    if updated_to: validate_due_date(updated_to)
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
        name_query = "%%%s%%" % like_escape(attachment_name_key(q))
        sql += """ AND (cards.title LIKE ? ESCAPE '\\' OR cards.description LIKE ? ESCAPE '\\' OR cards.labels LIKE ? ESCAPE '\\' OR EXISTS
                     (SELECT 1 FROM attachments WHERE attachments.card_id=cards.id AND attachments.name_key LIKE ? ESCAPE '\\'))"""
        params.extend(["%%%s%%" % like_escape(q)] * 3 + [name_query])
    if date_from: sql += " AND substr(cards.due_date,1,10)>=?"; params.append(date_from[:10])
    if date_to: sql += " AND substr(cards.due_date,1,10)<=?"; params.append(date_to[:10])
    if created_from: sql += " AND substr(cards.created_at,1,10)>=?"; params.append(created_from[:10])
    if created_to: sql += " AND substr(cards.created_at,1,10)<=?"; params.append(created_to[:10])
    if updated_from: sql += " AND substr(cards.updated_at,1,10)>=?"; params.append(updated_from[:10])
    if updated_to: sql += " AND substr(cards.updated_at,1,10)<=?"; params.append(updated_to[:10])
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
    items = [db.row_to_card(r) for r in rows]
    if has_more and rows:
        last = rows[-1]
        value = last[sort_field]
        next_cursor = encode_search_cursor(sort, 1 if value is None else 0, value or "", last["id"])
    else:
        next_cursor = None
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


def search_cards(conn, q="", date_from=None, date_to=None, priority=None, include_active=False, sort="archived_desc", created_from=None, created_to=None, updated_from=None, updated_to=None):
    return search_cards_page(conn, q, date_from, date_to, priority, include_active, sort=sort, created_from=created_from, created_to=created_to, updated_from=updated_from, updated_to=updated_to)["items"]


def export_data(conn):
    attachments = [dict(row) for row in conn.execute("""SELECT attachments.id,attachments.card_id,attachments.file_name,attachments.content_type,
                 attachments.size,attachments.created_at,attachments.updated_at,attachments.version FROM attachments
                 JOIN cards ON cards.id=attachments.card_id WHERE cards.is_draft=0 ORDER BY attachments.id""")]
    for attachment in attachments:
        attachment["file_included"] = False
    from config import SCHEMA_VERSION, EXPORT_VERSION
    return {"format": "kanban-export", "format_version": EXPORT_VERSION, "schema_version": SCHEMA_VERSION,
            "board_revision": board_revision(conn), "exported_at": now_iso(), "columns": list_columns(conn, True),
            "cards": list_cards(conn, archived=0) + list_cards(conn, archived=1), "attachments": attachments}


def normalize_import(data):
    data = require_object(data)
    from config import EXPORT_VERSION
    if data.get("format") not in (None, "kanban-export") or data.get("format_version", 1) not in (1, EXPORT_VERSION):
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
    normalized_cards, card_ids, per_column, per_planned_date = [], set(), {}, {}
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
                "version": version if isinstance(version, int) and version > 0 else 1, "source_position": item.get("position", 0),
                "source_planned_position": item.get("planned_position", 0)}
        normalized_cards.append(card)
        if not card["archived"]:
            per_column.setdefault(column_id, []).append(card)
            if card["planned_date"]:
                per_planned_date.setdefault(card["planned_date"], []).append(card)
    for values in per_column.values():
        values.sort(key=lambda c: (c["source_position"] if isinstance(c["source_position"], int) else 0, c["id"]))
        for index, card in enumerate(values): card["position"] = index
    for values in per_planned_date.values():
        values.sort(key=lambda c: (c["source_planned_position"] if isinstance(c["source_planned_position"], int) else 0, c["id"]))
        for index, card in enumerate(values): card["planned_position"] = index
    for card in normalized_cards:
        card.setdefault("position", 0)
        card.setdefault("planned_position", None)
        card.pop("source_position", None)
        card.pop("source_planned_position", None)
    return {"columns": normalized_columns, "cards": normalized_cards}


def import_preview(data):
    normalized = normalize_import(data)
    ignored = len(data.get("attachments", [])) if isinstance(data, dict) and isinstance(data.get("attachments"), list) else 0
    return {"ok": True, "columns": len(normalized["columns"]), "cards": sum(not c["archived"] for c in normalized["cards"]),
            "archived_cards": sum(c["archived"] for c in normalized["cards"]), "attachments_ignored": ignored}


def import_replace(conn, request):
    request = require_object(request); normalized = normalize_import(request.get("data"))
    rollback_dir = config.ATTACHMENTS_DIR + ".import-rollback-" + uuid.uuid4().hex
    backup_file = None
    with DB_MAINTENANCE_LOCK:
        check_revision(conn, request.get("expected_board_revision"), required=True); backup_file = backup.create_full_backup("pre-import")
        if os.path.isdir(config.ATTACHMENTS_DIR): os.replace(config.ATTACHMENTS_DIR, rollback_dir)
        os.makedirs(config.ATTACHMENTS_DIR, exist_ok=True)
        try:
            with transaction(conn):
                check_revision(conn, request.get("expected_board_revision"), required=True); conn.execute("DELETE FROM cards"); conn.execute("DELETE FROM columns")
                for col in normalized["columns"]:
                    conn.execute("INSERT INTO columns (id,name,position,deleted_at,version) VALUES (?,?,?,?,?)", (col["id"], col["name"], col["position"], col["deleted_at"], col["version"]))
                for card in normalized["cards"]:
                    conn.execute("""INSERT INTO cards (id,column_id,title,description,labels,due_date,planned_date,planned_position,priority,position,archived,created_at,updated_at,completed_at,archived_at,archive_reason,is_draft,version)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""", (card["id"], card["column_id"], card["title"], card["description"], card["labels"], card["due_date"], card["planned_date"], card["planned_position"], card["priority"], card["position"], card["archived"], card["created_at"], card["updated_at"], card["completed_at"], card["archived_at"], card["archive_reason"], card["version"]))
                revision = bump_revision(conn)
                if conn.execute("PRAGMA foreign_key_check").fetchone() is not None: raise ApiError("导入数据外键检查失败", 422, "INVALID_IMPORT_REFERENCE")
                backup.validate_board_invariants(conn)
        except Exception:
            shutil.rmtree(config.ATTACHMENTS_DIR, ignore_errors=True)
            if os.path.isdir(rollback_dir): os.replace(rollback_dir, config.ATTACHMENTS_DIR)
            raise
        shutil.rmtree(rollback_dir, ignore_errors=True)
    return {"ok": True, "imported": import_preview(request.get("data")), "backup": os.path.basename(backup_file), "revision": revision}
