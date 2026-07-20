import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime
from unittest import mock

import kanban as app


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "test.db")
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
        self.conn = app.get_conn()

    def tearDown(self):
        self.conn.close()
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def with_revision(self, data=None):
        return {**(data or {}), "expected_board_revision": app.board_revision(self.conn)}

    def with_card_tokens(self, card_id, data=None):
        return {
            **(data or {}),
            "expected_version": app.get_card(self.conn, card_id, include_draft=True)["version"],
            "expected_board_revision": app.board_revision(self.conn),
        }

    def with_column_tokens(self, column_id, data=None):
        column = self.conn.execute("SELECT version FROM columns WHERE id=?", (column_id,)).fetchone()
        return {
            **(data or {}),
            "expected_version": column["version"],
            "expected_board_revision": app.board_revision(self.conn),
        }

    def create_card(self, column_id, title):
        return app.create_card(self.conn, self.with_revision({
            "column_id": column_id,
            "title": title,
            "description": "",
            "labels": "",
            "due_date": "",
            "priority": "medium",
        }))["card"]

    def create_column(self, name):
        return app.create_column(self.conn, self.with_revision({"name": name}))["column"]

    def update_column(self, column_id, data):
        return app.update_column(self.conn, column_id, self.with_column_tokens(column_id, data))

    def delete_column(self, column_id):
        return app.delete_column(self.conn, column_id, self.with_column_tokens(column_id))

    def archive_card(self, card_id):
        return app.archive_card(self.conn, card_id, self.with_card_tokens(card_id))

    def restore_card(self, card_id):
        return app.restore_card(self.conn, card_id, self.with_card_tokens(card_id))


class ServerConfigurationTests(unittest.TestCase):
    def test_main_always_binds_loopback_and_reports_local_only(self):
        server = mock.Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(app, "init_db"), mock.patch.object(app, "start_auto_archive_worker"), mock.patch.object(app, "ThreadingHTTPServer", return_value=server) as factory, mock.patch("builtins.print") as output:
            app.main()
        factory.assert_called_once_with(("127.0.0.1", app.PORT), app.Handler)
        text = "\n".join(" ".join(str(value) for value in call.args) for call in output.call_args_list)
        self.assertIn("仅限本机访问", text)
        self.assertIn("http://127.0.0.1:%d" % app.PORT, text)


class MigrationTests(DatabaseTestCase):
    def test_latest_schema_is_created(self):
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(len(app.list_columns(self.conn)), 3)
        self.assertEqual(app.get_board(self.conn)["schema_version"], 4)


class CardMovementTests(DatabaseTestCase):
    def test_same_column_move_down(self):
        column = app.list_columns(self.conn)[0]
        cards = [self.create_card(column["id"], name) for name in ("A", "B", "C", "D")]
        revision = app.board_revision(self.conn)
        app.move_card(self.conn, cards[1]["id"], {
            "column_id": column["id"], "position": 2,
            "expected_version": cards[1]["version"],
            "expected_board_revision": revision,
        })
        self.assertEqual([card["title"] for card in app.list_cards(self.conn, column["id"])], ["A", "C", "B", "D"])

    def test_cross_column_positions_are_contiguous(self):
        first, second = app.list_columns(self.conn)[:2]
        cards = [self.create_card(first["id"], name) for name in ("A", "B", "C")]
        app.move_card(self.conn, cards[1]["id"], {
            "column_id": second["id"], "position": 0,
            "expected_version": cards[1]["version"],
            "expected_board_revision": app.board_revision(self.conn),
        })
        self.assertEqual([card["position"] for card in app.list_cards(self.conn, first["id"])], [0, 1])
        self.assertEqual([card["title"] for card in app.list_cards(self.conn, second["id"])], ["B"])


    def test_atomic_update_moves_card_and_fields(self):
        first, second = app.list_columns(self.conn)[:2]
        card = self.create_card(first["id"], "旧标题")
        result = app.update_card(self.conn, card["id"], {"column_id": second["id"], "position": 0, "title": "新标题", "description": "", "labels": "", "due_date": "", "priority": "high", "expected_version": card["version"], "expected_board_revision": app.board_revision(self.conn)})
        self.assertEqual(result["card"]["column_id"], second["id"])
        self.assertEqual(result["card"]["title"], "新标题")
        self.assertEqual(result["card"]["position"], 0)

    def test_copy_keeps_positions_and_completed_timer(self):
        completed = next(column for column in app.list_columns(self.conn) if column["name"] == "已完成")
        first = self.create_card(completed["id"], "A")
        self.create_card(completed["id"], "B")
        copied = app.copy_card(self.conn, first["id"], {"expected_version": first["version"], "expected_board_revision": app.board_revision(self.conn)})
        cards = app.list_cards(self.conn, completed["id"])
        self.assertEqual([card["position"] for card in cards], [0, 1, 2])
        self.assertEqual(cards[1]["id"], copied["card"]["id"])
        self.assertIsNotNone(copied["card"]["completed_at"])
        self.assertFalse(copied["attachments_copied"])

    def test_delete_column_preserves_archived_card(self):
        columns = app.list_columns(self.conn)
        card = self.create_card(columns[0]["id"], "保留我")
        app.delete_column(self.conn, columns[0]["id"], {
            "expected_version": columns[0]["version"],
            "expected_board_revision": app.board_revision(self.conn),
        })
        stored = app.get_card(self.conn, card["id"])
        self.assertEqual(stored["archived"], 1)
        result = app.search_cards(self.conn, q="保留我")
        self.assertEqual(result[0]["column_deleted"], True)
        restored = app.restore_card(self.conn, card["id"], {
            "expected_version": stored["version"],
            "expected_board_revision": app.board_revision(self.conn),
        })["card"]
        self.assertNotEqual(restored["column_id"], columns[0]["id"])

    def test_restore_matches_recreated_column_by_name(self):
        columns = app.list_columns(self.conn)
        original = columns[1]
        card = self.create_card(original["id"], "回到进行中")
        app.delete_column(self.conn, original["id"], {
            "expected_version": original["version"],
            "expected_board_revision": app.board_revision(self.conn),
        })
        recreated = app.create_column(self.conn, {
            "name": original["name"],
            "expected_board_revision": app.board_revision(self.conn),
        })["column"]
        archived = app.get_card(self.conn, card["id"])
        result = app.search_cards(self.conn, q="回到进行中")[0]
        self.assertEqual(result["restore_column_id"], recreated["id"])
        restored = app.restore_card(self.conn, card["id"], {
            "expected_version": archived["version"],
            "expected_board_revision": app.board_revision(self.conn),
        })["card"]
        self.assertEqual(restored["column_id"], recreated["id"])

    def test_duplicate_active_column_names_are_rejected(self):
        columns = app.list_columns(self.conn)
        with self.assertRaises(app.ApiError) as created:
            self.create_column(columns[0]["name"])
        self.assertEqual(created.exception.code, "COLUMN_NAME_EXISTS")
        with self.assertRaises(app.ApiError) as renamed:
            self.update_column(columns[1]["id"], {"name": columns[0]["name"]})
        self.assertEqual(renamed.exception.code, "COLUMN_NAME_EXISTS")

    def test_unicode_equivalent_active_column_names_are_rejected(self):
        cases = [
            ("Ångström", "ångström"),
            ("Café", "Cafe\u0301"),
            ("Straße", "STRASSE"),
        ]
        for original, equivalent in cases:
            with self.subTest(original=original, equivalent=equivalent):
                column = self.create_column(original)
                with self.assertRaises(app.ApiError) as raised:
                    self.create_column(equivalent)
                self.assertEqual(raised.exception.code, "COLUMN_NAME_EXISTS")
                self.delete_column(column["id"])

    def test_deleted_unicode_equivalent_name_can_be_recreated_and_restored(self):
        original = self.create_column("Café")
        card = self.create_card(original["id"], "Unicode 恢复")
        self.delete_column(original["id"])
        recreated = self.create_column("Cafe\u0301")
        archived = app.get_card(self.conn, card["id"])
        restored = app.restore_card(self.conn, card["id"], self.with_card_tokens(card["id"]))["card"]
        self.assertEqual(restored["column_id"], recreated["id"])
        self.assertGreater(restored["version"], archived["version"])

    def test_deleted_column_name_can_be_reused(self):
        column = app.list_columns(self.conn)[0]
        self.delete_column(column["id"])
        recreated = self.create_column(column["name"])
        self.assertEqual(recreated["name"], column["name"])

    def test_last_column_cannot_be_deleted(self):
        columns = app.list_columns(self.conn)
        for column in columns[:-1]:
            self.delete_column(column["id"])
        with self.assertRaises(app.ApiError) as raised:
            self.delete_column(columns[-1]["id"])
        self.assertEqual(raised.exception.code, "LAST_ACTIVE_COLUMN")


class ValidationTests(DatabaseTestCase):
    def test_invalid_priority_is_rejected(self):
        column = app.list_columns(self.conn)[0]
        with self.assertRaises(app.ApiError):
            app.create_card(self.conn, self.with_revision({"column_id": column["id"], "title": "x", "description": "", "labels": "", "due_date": "", "priority": "urgent"}))

    def test_description_is_sanitized(self):
        column = app.list_columns(self.conn)[0]
        card = app.create_card(self.conn, self.with_revision({"column_id": column["id"], "title": "x", "description": '<script>alert(1)</script><p onclick="x">安全</p>', "labels": "", "due_date": "", "priority": "medium"}))["card"]
        self.assertNotIn("script", card["description"])
        self.assertNotIn("onclick", card["description"])
        self.assertEqual(card["description"], "<p>安全</p>")

    def test_description_normalizes_contenteditable_blocks_and_empty_lines(self):
        self.assertEqual(
            app.sanitize_description("<div>第一段</div><div><br></div><div>第三段</div>"),
            "<p>第一段</p><p><br></p><p>第三段</p>",
        )
        self.assertEqual(
            app.sanitize_description("<p>第一行<br>第二行</p><p></p>"),
            "<p>第一行<br>第二行</p><p><br></p>",
        )

    def test_description_preserves_lists_inline_formatting_and_nested_lists(self):
        source = "<div><strong>摘要</strong></div><ul><li><em>一级</em><ol><li><u>二级</u></li></ol></li></ul>"
        self.assertEqual(
            app.sanitize_description(source),
            "<p><strong>摘要</strong></p><ul><li><em>一级</em><ol><li><u>二级</u></li></ol></li></ul>",
        )

    def test_description_sanitization_removes_xss_and_is_idempotent(self):
        source = '<div class="bad" onclick="x">安全<img src=x onerror=x><script><b>危险</b></script><i style="x">格式</i></div>'
        sanitized = app.sanitize_description(source)
        self.assertEqual(sanitized, "<p>安全<i>格式</i></p>")
        self.assertEqual(app.sanitize_description(sanitized), sanitized)

    def test_description_preserves_original_plain_text_newlines(self):
        source = "第一行\n\n第三行"
        self.assertEqual(app.sanitize_description(source), source)

    def test_business_layer_rejects_missing_revision_and_version(self):
        column = app.list_columns(self.conn)[0]
        card_data = {"column_id": column["id"], "title": "缺 token", "description": "", "labels": "", "due_date": "", "priority": "medium"}
        with self.assertRaises(app.ApiError) as missing_revision:
            app.create_card(self.conn, card_data)
        self.assertEqual(missing_revision.exception.code, "INVALID_BOARD_REVISION")

        card = self.create_card(column["id"], "版本检查")
        update = {"title": "新标题", "description": "", "labels": "", "due_date": "", "priority": "medium"}
        with self.assertRaises(app.ApiError) as missing_version:
            app.update_card(self.conn, card["id"], self.with_revision(update))
        self.assertEqual(missing_version.exception.code, "INVALID_VERSION")
        with self.assertRaises(app.ApiError) as update_missing_revision:
            app.update_card(self.conn, card["id"], {**update, "expected_version": card["version"]})
        self.assertEqual(update_missing_revision.exception.code, "INVALID_BOARD_REVISION")


class ConcurrencyTests(DatabaseTestCase):
    def test_stale_version_is_rejected(self):
        column = app.list_columns(self.conn)[0]
        card = self.create_card(column["id"], "旧标题")
        payload = {"title": "新标题", "description": "", "labels": "", "due_date": "", "priority": "medium", "expected_version": card["version"], "expected_board_revision": app.board_revision(self.conn)}
        app.update_card(self.conn, card["id"], payload)
        payload["expected_board_revision"] = app.board_revision(self.conn)
        with self.assertRaises(app.ApiError) as raised:
            app.update_card(self.conn, card["id"], payload)
        self.assertEqual(raised.exception.code, "VERSION_CONFLICT")


class AutoArchiveTests(DatabaseTestCase):
    def completed_column(self):
        return next(column for column in app.list_columns(self.conn) if column["name"] == "已完成")

    def test_entering_completed_column_starts_timer(self):
        todo = app.list_columns(self.conn)[0]
        completed = self.completed_column()
        card = self.create_card(todo["id"], "完成任务")
        moved = app.move_card(self.conn, card["id"], {
            "column_id": completed["id"], "position": 0,
            "expected_version": card["version"],
            "expected_board_revision": app.board_revision(self.conn),
        })["card"]
        self.assertIsNotNone(moved["completed_at"])
        moved_again = app.move_card(self.conn, card["id"], {
            "column_id": completed["id"], "position": 0,
            "expected_version": moved["version"],
            "expected_board_revision": app.board_revision(self.conn),
        })["card"]
        self.assertEqual(moved_again["completed_at"], moved["completed_at"])

    def test_leaving_completed_column_clears_timer(self):
        completed = self.completed_column()
        todo = app.list_columns(self.conn)[0]
        card = self.create_card(completed["id"], "重新打开")
        moved = app.move_card(self.conn, card["id"], {
            "column_id": todo["id"], "position": 0,
            "expected_version": card["version"],
            "expected_board_revision": app.board_revision(self.conn),
        })["card"]
        self.assertIsNone(moved["completed_at"])

    def test_cards_older_than_30_days_are_auto_archived(self):
        completed = self.completed_column()
        old = self.create_card(completed["id"], "旧完成卡片")
        recent = self.create_card(completed["id"], "近期完成卡片")
        self.conn.execute("UPDATE cards SET completed_at=? WHERE id=?", ("2026-01-01 00:00:00", old["id"]))
        self.conn.execute("UPDATE cards SET completed_at=? WHERE id=?", ("2026-01-20 00:00:01", recent["id"]))
        count = app.auto_archive_completed_cards(self.conn, datetime(2026, 1, 31, 0, 0, 0))
        self.assertEqual(count, 1)
        archived = app.get_card(self.conn, old["id"])
        self.assertEqual(archived["archived"], 1)
        self.assertEqual(archived["archive_reason"], "auto_completed")
        self.assertEqual(app.get_card(self.conn, recent["id"])["archived"], 0)

    def test_renaming_column_to_completed_starts_timer(self):
        column = self.create_column("验收完成")
        card = self.create_card(column["id"], "已有卡片")
        original_completed = self.completed_column()
        self.delete_column(original_completed["id"])
        updated = self.update_column(column["id"], {"name": "已完成"})["column"]
        self.assertEqual(updated["name"], "已完成")
        self.assertIsNotNone(app.get_card(self.conn, card["id"])["completed_at"])


class BackupImportTests(DatabaseTestCase):
    def test_database_backups_keep_latest_ten_without_touching_other_files(self):
        os.makedirs(self.backup_dir, exist_ok=True)
        unknown = os.path.join(self.backup_dir, "notes.db")
        archive = os.path.join(self.backup_dir, "kanban-auto-20000101-000000-000000.zip")
        temporary = os.path.join(self.backup_dir, ".kanban-auto.tmp")
        for path in (unknown, archive, temporary):
            with open(path, "wb") as output:
                output.write(b"keep")
        for _ in range(11):
            app.create_backup("test")
        formal = sorted(name for name in os.listdir(self.backup_dir) if app.DATABASE_BACKUP_NAME.fullmatch(name))
        self.assertEqual(len(formal), 10)
        for path in (unknown, archive, temporary):
            self.assertTrue(os.path.isfile(path))

    def test_backup_cleanup_failure_does_not_fail_successful_backup(self):
        old_name = "kanban-auto-20000101-000000-000000.db"
        os.makedirs(self.backup_dir, exist_ok=True)
        for index in range(10):
            name = old_name if index == 0 else "kanban-auto-20000101-00000%d-000000.db" % index
            with open(os.path.join(self.backup_dir, name), "wb") as output:
                output.write(b"old")
        real_remove = os.remove
        def fail_old(path):
            if path.endswith(old_name):
                raise OSError("locked")
            return real_remove(path)
        with mock.patch.object(app.os, "remove", side_effect=fail_old), mock.patch.object(app.sys, "stderr", new=io.StringIO()) as stderr:
            path = app.create_backup("test")
        self.assertTrue(os.path.isfile(path))
        self.assertIn("数据库备份清理失败", stderr.getvalue())

    def test_backup_is_valid_database(self):
        path = app.create_backup("test")
        backup = sqlite3.connect(path)
        try:
            self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM columns").fetchone()[0], 3)
        finally:
            backup.close()

    def test_export_import_round_trip(self):
        column = app.list_columns(self.conn)[0]
        self.create_card(column["id"], "往返")
        exported = app.export_data(self.conn)
        preview = app.import_preview(exported)
        self.assertEqual(preview["cards"], 1)
        result = app.import_replace(self.conn, {"data": exported, "expected_board_revision": app.board_revision(self.conn)})
        self.assertTrue(result["ok"])
        self.assertEqual(app.list_cards(self.conn)[0]["title"], "往返")

    def test_json_import_rejects_unicode_equivalent_active_columns(self):
        exported = app.export_data(self.conn)
        exported["columns"][0]["name"] = "Café"
        exported["columns"][1]["name"] = "Cafe\u0301"
        with self.assertRaises(app.ApiError) as raised:
            app.import_preview(exported)
        self.assertEqual(raised.exception.code, "DUPLICATE_COLUMN_NAME")

    def test_full_backup_missing_attachment_leaves_no_zip_or_temp_file(self):
        card = self.create_card(app.list_columns(self.conn)[0]["id"], "缺失附件")
        attachment = app.save_attachment(self.conn, card["id"], "missing.txt", "text/plain", io.BytesIO(b"gone"), 4)
        os.remove(app.attachment_path(card["id"], attachment["file_name"]))
        before = set(os.listdir(self.backup_dir)) if os.path.isdir(self.backup_dir) else set()
        with self.assertRaises(app.ApiError) as raised:
            app.create_full_backup("incomplete")
        self.assertEqual(raised.exception.code, "INCOMPLETE_BACKUP")
        after = set(os.listdir(self.backup_dir)) if os.path.isdir(self.backup_dir) else set()
        self.assertEqual(after, before)
        self.assertFalse(any(name.endswith(".zip") or name.startswith(".kanban-full-") for name in after - before))


    def test_search_cursor_paginates_without_duplicates(self):
        column = app.list_columns(self.conn)[0]
        for index in range(5):
            self.create_card(column["id"], f"分页 {index}")
        first = app.search_cards_page(self.conn, include_active=True, limit=2)
        second = app.search_cards_page(self.conn, include_active=True, cursor=first["next_cursor"], limit=2)
        self.assertTrue(first["has_more"])
        self.assertTrue(set(card["id"] for card in first["items"]).isdisjoint(card["id"] for card in second["items"]))

    def test_search_supports_six_time_sorts_and_archived_default(self):
        column = app.list_columns(self.conn)[0]
        first = self.create_card(column["id"], "第一张")
        second = self.create_card(column["id"], "第二张")
        self.conn.execute("UPDATE cards SET created_at=?,updated_at=? WHERE id=?", ("2026-01-01 08:00:00", "2026-01-03 08:00:00", first["id"]))
        self.conn.execute("UPDATE cards SET created_at=?,updated_at=? WHERE id=?", ("2026-01-02 08:00:00", "2026-01-02 08:00:00", second["id"]))
        app.archive_card(self.conn, first["id"], self.with_card_tokens(first["id"]))
        app.archive_card(self.conn, second["id"], self.with_card_tokens(second["id"]))
        self.conn.execute("UPDATE cards SET archived_at=? WHERE id=?", ("2026-01-04 08:00:00", first["id"]))
        self.conn.execute("UPDATE cards SET archived_at=? WHERE id=?", ("2026-01-05 08:00:00", second["id"]))
        expected = {
            "archived_desc": [second["id"], first["id"]], "archived_asc": [first["id"], second["id"]],
            "updated_desc": [second["id"], first["id"]], "updated_asc": [first["id"], second["id"]],
            "created_desc": [second["id"], first["id"]], "created_asc": [first["id"], second["id"]],
        }
        for sort, ids in expected.items():
            self.assertEqual([card["id"] for card in app.search_cards_page(self.conn, sort=sort)["items"]], ids, sort)
        self.assertEqual([card["id"] for card in app.search_cards_page(self.conn)["items"]], expected["archived_desc"])

    def test_archived_sort_places_active_cards_last_and_validates_cursor_sort(self):
        column = app.list_columns(self.conn)[0]
        archived = self.create_card(column["id"], "归档")
        active = self.create_card(column["id"], "活动")
        app.archive_card(self.conn, archived["id"], self.with_card_tokens(archived["id"]))
        result = app.search_cards_page(self.conn, include_active=True, sort="archived_asc", limit=1)
        self.assertEqual(result["items"][0]["id"], archived["id"])
        second = app.search_cards_page(self.conn, include_active=True, sort="archived_asc", cursor=result["next_cursor"], limit=1)
        self.assertEqual(second["items"][0]["id"], active["id"])
        with self.assertRaises(app.ApiError) as raised:
            app.search_cards_page(self.conn, include_active=True, sort="updated_desc", cursor=result["next_cursor"])
        self.assertEqual(raised.exception.code, "INVALID_CURSOR")
        with self.assertRaises(app.ApiError) as raised:
            app.search_cards_page(self.conn, sort="unknown")
        self.assertEqual(raised.exception.code, "INVALID_SORT")

    def test_attachment_name_utf8_byte_limit(self):
        with self.assertRaises(app.ApiError) as raised:
            app.validate_attachment_name("中" * 86)
        self.assertEqual(raised.exception.code, "ATTACHMENT_NAME_TOO_LONG")

    def test_backup_manifest_contains_hashes(self):
        path = app.create_full_backup("hash")
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["format_version"], 2)
        self.assertEqual(len(manifest["database"]["sha256"]), 64)


class AttachmentTests(DatabaseTestCase):
    def test_draft_is_hidden_and_finalize_preserves_attachment(self):
        column = app.list_columns(self.conn)[0]
        draft = app.create_card_draft(self.conn, {"column_id": column["id"], "expected_board_revision": app.board_revision(self.conn)})["card"]
        self.assertEqual(app.list_cards(self.conn), [])
        attachment = app.save_attachment(self.conn, draft["id"], "需求说明.docx", "application/octet-stream", io.BytesIO(b"draft"), 5)
        finalized = app.finalize_card_draft(self.conn, draft["id"], {"column_id": column["id"], "title": "正式卡片", "description": "", "labels": "", "due_date": "", "priority": "medium", "expected_version": draft["version"], "expected_board_revision": app.board_revision(self.conn)})["card"]
        self.assertEqual(finalized["attachment_count"], 1)
        self.assertEqual(app.list_attachments(self.conn, finalized["id"])[0]["id"], attachment["id"])

    def test_cancel_draft_removes_files(self):
        column = app.list_columns(self.conn)[0]
        draft = app.create_card_draft(self.conn, {"column_id": column["id"], "expected_board_revision": app.board_revision(self.conn)})["card"]
        app.save_attachment(self.conn, draft["id"], "x.bin", "", io.BytesIO(b"abc"), 3)
        app.delete_card_draft(self.conn, draft["id"], draft["version"])
        self.assertFalse(os.path.exists(app.attachment_directory(draft["id"])))
        with self.assertRaises(app.ApiError):
            app.get_card(self.conn, draft["id"], include_draft=True)

    def test_filename_rules_and_casefold_duplicate(self):
        for name in ("需求说明 v2.1.docx", "project.tar.gz", "a b.txt"):
            self.assertEqual(app.validate_attachment_name(name), name)
        for name in (".", "..", "bad/name", "bad\\name", "bad?.txt", "trail. ", "CON.txt", "bad\x00.txt"):
            with self.assertRaises(app.ApiError, msg=name):
                app.validate_attachment_name(name)
        card = self.create_card(app.list_columns(self.conn)[0]["id"], "附件")
        app.save_attachment(self.conn, card["id"], "Report.xlsx", "", io.BytesIO(b"one"), 3)
        with self.assertRaises(app.ApiError) as raised:
            app.save_attachment(self.conn, card["id"], "report.xlsx", "", io.BytesIO(b"two"), 3)
        self.assertEqual(raised.exception.code, "ATTACHMENT_EXISTS")

    def test_upload_replace_delete_search_and_archive(self):
        card = self.create_card(app.list_columns(self.conn)[0]["id"], "附件测试")
        first = app.save_attachment(self.conn, card["id"], "Annual-Report.xlsx", "sheet", io.BytesIO(b"old"), 3)
        self.assertEqual(app.get_card(self.conn, card["id"])["attachment_count"], 1)
        replaced = app.save_attachment(self.conn, card["id"], "annual-report.xlsx", "sheet", io.BytesIO(b"new-data"), 8, True, first["version"])
        self.assertEqual(replaced["id"], first["id"])
        self.assertEqual(replaced["version"], 2)
        with open(app.attachment_path(card["id"], replaced["file_name"]), "rb") as handle:
            self.assertEqual(handle.read(), b"new-data")
        self.archive_card(card["id"])
        self.assertEqual(app.search_cards(self.conn, q="REPORT")[0]["id"], card["id"])
        with self.assertRaises(app.ApiError):
            app.delete_attachment(self.conn, replaced["id"], replaced["version"])
        restored = self.restore_card(card["id"])["card"]
        self.assertEqual(restored["attachment_count"], 1)
        app.delete_attachment(self.conn, replaced["id"], replaced["version"])
        self.assertEqual(app.get_card(self.conn, card["id"])["attachment_count"], 0)

    def test_post_commit_attachment_cleanup_failure_keeps_new_file(self):
        card = self.create_card(app.list_columns(self.conn)[0]["id"], "清理失败")
        original = app.save_attachment(self.conn, card["id"], "data.bin", "", io.BytesIO(b"old"), 3)
        real_remove = os.remove
        def fail_rollback(path):
            if ".rollback-" in path:
                raise PermissionError("locked")
            return real_remove(path)
        with mock.patch("kanban.os.remove", side_effect=fail_rollback):
            replaced = app.save_attachment(self.conn, card["id"], "data.bin", "", io.BytesIO(b"new"), 3, True, original["version"])
        self.assertEqual(replaced["version"], 2)
        with open(app.attachment_path(card["id"], "data.bin"), "rb") as handle:
            self.assertEqual(handle.read(), b"new")

    def test_post_commit_delete_cleanup_failure_does_not_restore_metadata(self):
        card = self.create_card(app.list_columns(self.conn)[0]["id"], "删除清理")
        attachment = app.save_attachment(self.conn, card["id"], "data.bin", "", io.BytesIO(b"old"), 3)
        real_remove = os.remove
        def fail_trash(path):
            if ".deleting-" in path:
                raise PermissionError("locked")
            return real_remove(path)
        with mock.patch("kanban.os.remove", side_effect=fail_trash):
            app.delete_attachment(self.conn, attachment["id"], attachment["version"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attachments WHERE id=?", (attachment["id"],)).fetchone()[0], 0)
        self.assertFalse(os.path.exists(app.attachment_path(card["id"], "data.bin")))

    def test_json_export_metadata_and_full_backup(self):
        card = self.create_card(app.list_columns(self.conn)[0]["id"], "备份")
        app.save_attachment(self.conn, card["id"], "中文.txt", "text/plain", io.BytesIO("内容".encode()), len("内容".encode()))
        exported = app.export_data(self.conn)
        self.assertFalse(exported["attachments"][0]["file_included"])
        path = app.create_full_backup("test")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self.assertIn("kanban.db", names)
            self.assertIn("manifest.json", names)
            self.assertIn("attachments/%s/中文.txt" % card["id"], names)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["attachment_count"], 1)
        preview, extracted = app.inspect_full_backup(path, extract=True)
        self.assertEqual(preview["attachments"], 1)
        import shutil
        shutil.rmtree(extracted)


if __name__ == "__main__":
    unittest.main()
