import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import app


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "test.db")
        self.backup_dir = os.path.join(self.temp.name, "backups")
        self.patches = [
            mock.patch.object(app, "DB_PATH", self.db_path),
            mock.patch.object(app, "BACKUP_DIR", self.backup_dir),
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
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(len(app.list_columns(self.conn)), 3)
        self.assertEqual(app.get_board(self.conn)["schema_version"], 3)


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


class ColumnDeletionTests(DatabaseTestCase):
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


if __name__ == "__main__":
    unittest.main()
