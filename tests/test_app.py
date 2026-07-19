import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime
from unittest import mock

import app


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

    def create_card(self, column_id, title):
        return app.create_card(self.conn, {
            "column_id": column_id,
            "title": title,
            "description": "",
            "labels": "",
            "due_date": "",
            "priority": "medium",
        })["card"]


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
            app.create_column(self.conn, {"name": columns[0]["name"]})
        self.assertEqual(created.exception.code, "COLUMN_NAME_EXISTS")
        with self.assertRaises(app.ApiError) as renamed:
            app.update_column(self.conn, columns[1]["id"], {"name": columns[0]["name"]})
        self.assertEqual(renamed.exception.code, "COLUMN_NAME_EXISTS")

    def test_deleted_column_name_can_be_reused(self):
        column = app.list_columns(self.conn)[0]
        app.delete_column(self.conn, column["id"])
        recreated = app.create_column(self.conn, {"name": column["name"]})["column"]
        self.assertEqual(recreated["name"], column["name"])

    def test_last_column_cannot_be_deleted(self):
        columns = app.list_columns(self.conn)
        for column in columns[:-1]:
            app.delete_column(self.conn, column["id"])
        with self.assertRaises(app.ApiError) as raised:
            app.delete_column(self.conn, columns[-1]["id"])
        self.assertEqual(raised.exception.code, "LAST_ACTIVE_COLUMN")


class ValidationTests(DatabaseTestCase):
    def test_invalid_priority_is_rejected(self):
        column = app.list_columns(self.conn)[0]
        with self.assertRaises(app.ApiError):
            app.create_card(self.conn, {"column_id": column["id"], "title": "x", "description": "", "labels": "", "due_date": "", "priority": "urgent"})

    def test_description_is_sanitized(self):
        column = app.list_columns(self.conn)[0]
        card = app.create_card(self.conn, {"column_id": column["id"], "title": "x", "description": '<script>alert(1)</script><p onclick="x">安全</p>', "labels": "", "due_date": "", "priority": "medium"})["card"]
        self.assertNotIn("script", card["description"])
        self.assertNotIn("onclick", card["description"])
        self.assertEqual(card["description"], "<p>安全</p>")


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
        column = app.create_column(self.conn, {"name": "验收完成"})["column"]
        card = self.create_card(column["id"], "已有卡片")
        original_completed = self.completed_column()
        app.delete_column(self.conn, original_completed["id"])
        updated = app.update_column(self.conn, column["id"], {"name": "已完成"})["column"]
        self.assertEqual(updated["name"], "已完成")
        self.assertIsNotNone(app.get_card(self.conn, card["id"])["completed_at"])


class BackupImportTests(DatabaseTestCase):
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


    def test_search_cursor_paginates_without_duplicates(self):
        column = app.list_columns(self.conn)[0]
        for index in range(5):
            self.create_card(column["id"], f"分页 {index}")
        first = app.search_cards_page(self.conn, include_active=True, limit=2)
        second = app.search_cards_page(self.conn, include_active=True, cursor=first["next_cursor"], limit=2)
        self.assertTrue(first["has_more"])
        self.assertTrue(set(card["id"] for card in first["items"]).isdisjoint(card["id"] for card in second["items"]))

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
        draft = app.create_card_draft(self.conn, {"column_id": column["id"]})["card"]
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
        app.archive_card(self.conn, card["id"])
        self.assertEqual(app.search_cards(self.conn, q="REPORT")[0]["id"], card["id"])
        with self.assertRaises(app.ApiError):
            app.delete_attachment(self.conn, replaced["id"], replaced["version"])
        restored = app.restore_card(self.conn, card["id"])["card"]
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
        with mock.patch("app.os.remove", side_effect=fail_rollback):
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
        with mock.patch("app.os.remove", side_effect=fail_trash):
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
