import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime
from unittest import mock

import backup
import board
import config
import db
import kanban as app


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "test.db")
        self.backup_dir = os.path.join(self.temp.name, "backups")
        self.attachments_dir = os.path.join(self.temp.name, "attachments")
        self.patches = [
            mock.patch.object(config, "DB_PATH", self.db_path),
            mock.patch.object(config, "BACKUP_DIR", self.backup_dir),
            mock.patch.object(config, "ATTACHMENTS_DIR", self.attachments_dir),
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

    def create_card(self, column_id, title, **overrides):
        payload = {
            "column_id": column_id,
            "title": title,
            "description": "",
            "labels": "",
            "due_date": "",
            "priority": "medium",
        }
        payload.update(overrides)
        return app.create_card(self.conn, self.with_revision(payload))["card"]

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
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertEqual(len(app.list_columns(self.conn)), 3)
        self.assertEqual(app.get_board(self.conn)["schema_version"], 6)
        columns = {row["name"]: row for row in self.conn.execute("PRAGMA table_info(cards)")}
        self.assertIn("planned_date", columns)
        self.assertIn("planned_position", columns)
        indexes = {row["name"] for row in self.conn.execute("PRAGMA index_list(cards)")}
        self.assertIn("idx_cards_planned_date_position", indexes)

    def test_version_five_migration_normalizes_existing_descriptions_and_creates_backup(self):
        column = app.list_columns(self.conn)[0]
        card = self.create_card(column["id"], "旧描述")
        legacy = '<p><span class="rt-bg-clear"></span><span class="rt-fg-red">甲</span><span class="rt-fg-red">乙</span></p>'
        self.conn.execute("UPDATE cards SET description=? WHERE id=?", (legacy, card["id"]))
        self.conn.execute("PRAGMA user_version = 4")
        self.conn.close()
        app.init_db()
        self.conn = app.get_conn()
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertEqual(app.get_card(self.conn, card["id"])["description"], '<p><span class="rt-fg-red">甲乙</span></p>')
        self.assertTrue(any(name.startswith("kanban-pre-migration-") for name in os.listdir(self.backup_dir)))


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
        result = app.search_cards(self.conn, q="Unicode 恢复")[0]
        self.assertTrue(result["column_deleted"])
        self.assertEqual(result["restore_column_id"], recreated["id"])
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


class TodayPlanningTests(DatabaseTestCase):
    DATE = "2026-07-26"

    def planned_cards(self, planned_date=DATE):
        return self.conn.execute(
            "SELECT * FROM cards WHERE planned_date=? AND archived=0 AND is_draft=0 "
            "ORDER BY planned_position,id",
            (planned_date,),
        ).fetchall()

    def test_plan_card_add_reorder_and_remove_keeps_positions_contiguous(self):
        column = app.list_columns(self.conn)[0]
        cards = [self.create_card(column["id"], title) for title in ("A", "B", "C")]
        for card in cards:
            app.plan_card(self.conn, card["id"], self.with_card_tokens(card["id"], {
                "planned_date": self.DATE,
            }))
        self.assertEqual(
            [(row["title"], row["planned_position"]) for row in self.planned_cards()],
            [("A", 0), ("B", 1), ("C", 2)],
        )

        app.plan_card(self.conn, cards[2]["id"], self.with_card_tokens(cards[2]["id"], {
            "planned_date": self.DATE,
            "position": 0,
        }))
        self.assertEqual(
            [(row["title"], row["planned_position"]) for row in self.planned_cards()],
            [("C", 0), ("A", 1), ("B", 2)],
        )

        app.plan_card(self.conn, cards[0]["id"], self.with_card_tokens(cards[0]["id"], {
            "planned_date": "",
        }))
        removed = app.get_card(self.conn, cards[0]["id"])
        self.assertEqual(removed["planned_date"], "")
        self.assertIsNone(removed["planned_position"])
        self.assertEqual(
            [(row["title"], row["planned_position"]) for row in self.planned_cards()],
            [("C", 0), ("B", 1)],
        )

    def test_plan_card_moves_between_dates_and_compacts_both_lists(self):
        column = app.list_columns(self.conn)[0]
        a = self.create_card(column["id"], "A", planned_date=self.DATE)
        b = self.create_card(column["id"], "B", planned_date=self.DATE)
        other_date = "2026-07-27"
        c = self.create_card(column["id"], "C", planned_date=other_date)
        app.plan_card(self.conn, a["id"], self.with_card_tokens(a["id"], {
            "planned_date": other_date,
            "position": 0,
        }))
        self.assertEqual(
            [(row["title"], row["planned_position"]) for row in self.planned_cards()],
            [("B", 0)],
        )
        self.assertEqual(
            [(row["title"], row["planned_position"]) for row in self.planned_cards(other_date)],
            [("A", 0), ("C", 1)],
        )

    def test_import_export_preserves_planned_date_and_normalizes_position(self):
        column = app.list_columns(self.conn)[0]
        self.create_card(column["id"], "A", planned_date=self.DATE)
        self.create_card(column["id"], "B", planned_date=self.DATE)
        exported = app.export_data(self.conn)
        planned = [card for card in exported["cards"] if card["planned_date"] == self.DATE]
        self.assertEqual([card["planned_position"] for card in planned], [0, 1])
        result = app.import_replace(self.conn, {
            "data": exported,
            "expected_board_revision": app.board_revision(self.conn),
        })
        self.assertTrue(result["ok"])
        self.assertEqual(
            [(row["title"], row["planned_position"]) for row in self.planned_cards()],
            [("A", 0), ("B", 1)],
        )


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

    def test_description_normalizes_cross_browser_inline_tags(self):
        self.assertEqual(
            app.sanitize_description("<div><b>粗体</b><i>斜体</i><strike>删除</strike></div>"),
            "<p><strong>粗体</strong><em>斜体</em><s>删除</s></p>",
        )

    def test_description_sanitization_removes_xss_and_is_idempotent(self):
        source = '<div class="bad" onclick="x">安全<img src=x onerror=x><script><b>危险</b></script><i style="x">格式</i></div>'
        sanitized = app.sanitize_description(source)
        self.assertEqual(sanitized, "<p>安全<em>格式</em></p>")
        self.assertEqual(app.sanitize_description(sanitized), sanitized)

    def test_description_color_classes_are_allowlisted_and_canonical(self):
        source = '<span class="evil rt-bg-yellow rt-fg-red rt-fg-blue" style="position:fixed" onclick="x"><strong>重点</strong></span><span class="unknown">普通</span><font color="red">旧格式</font>'
        sanitized = app.sanitize_description(source)
        self.assertEqual(sanitized, '<span class="rt-fg-red rt-bg-yellow"><strong>重点</strong></span>普通旧格式')
        self.assertEqual(app.sanitize_description(sanitized), sanitized)
        self.assertEqual(app.sanitize_description('<span style="color:red;background:url(x)">无样式</span>'), "无样式")
        self.assertEqual(app.sanitize_description('<span class="rt-fg-default">默认</span><span class="rt-bg-clear">无底纹</span>'), '<span class="rt-fg-default">默认</span>无底纹')

    def test_description_removes_empty_and_redundant_spans_and_merges_safe_neighbors(self):
        source = '<p><span class="rt-bg-clear"></span><span class="rt-fg-red"><span class="rt-fg-red">甲</span>中<span class="rt-fg-red">乙</span></span><span class="rt-fg-red">丙</span><span></span></p>'
        expected = '<p><span class="rt-fg-red">甲中乙丙</span></p>'
        self.assertEqual(app.sanitize_description(source), expected)
        self.assertEqual(app.sanitize_description(expected), expected)
        self.assertEqual(app.sanitize_description('<p><span></span><span class="rt-bg-clear"></span></p>'), '<p><br></p>')

    def test_description_preserves_distinct_foreground_background_combinations(self):
        source = '<span class="rt-bg-green rt-fg-blue">甲</span><span class="rt-fg-blue rt-bg-green">乙</span><span class="rt-fg-blue rt-bg-yellow">丙</span>'
        expected = '<span class="rt-fg-blue rt-bg-green">甲乙</span><span class="rt-fg-blue rt-bg-yellow">丙</span>'
        self.assertEqual(app.sanitize_description(source), expected)

    def test_description_color_classes_round_trip_through_card_storage(self):
        column = app.list_columns(self.conn)[0]
        description = '<p><span class="rt-fg-blue rt-bg-green"><em>彩色内容</em></span></p>'
        card = app.create_card(self.conn, self.with_revision({"column_id": column["id"], "title": "颜色", "description": description, "labels": "", "due_date": "", "priority": "medium"}))["card"]
        self.assertEqual(card["description"], description)
        self.assertEqual(app.get_card(self.conn, card["id"])["description"], description)

    def test_backup_validation_rejects_noncanonical_description(self):
        column = app.list_columns(self.conn)[0]
        card = self.create_card(column["id"], "不安全备份")
        self.conn.execute("UPDATE cards SET description=? WHERE id=?", ('<span style="color:red">危险</span>', card["id"]))
        with self.assertRaises(app.ApiError) as raised:
            app.validate_board_invariants(self.conn)
        self.assertEqual(raised.exception.code, "INVALID_CARD_DESCRIPTION")

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
        with mock.patch.object(db.os, "remove", side_effect=fail_old), mock.patch.object(db.sys, "stderr", new=io.StringIO()) as stderr:
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

    def test_json_import_rejects_invalid_board_state(self):
        column = app.list_columns(self.conn)[0]
        self.create_card(column["id"], "脏数据")
        exported = app.export_data(self.conn)
        cases = []
        bad = json.loads(json.dumps(exported, ensure_ascii=False))
        bad["cards"][0]["created_at"] = "not-a-date"
        cases.append((bad, "INVALID_BOARD_TIMESTAMP"))
        bad = json.loads(json.dumps(exported, ensure_ascii=False))
        bad["cards"][0]["archived"] = 1
        cases.append((bad, "INVALID_ARCHIVE_STATE"))
        bad = json.loads(json.dumps(exported, ensure_ascii=False))
        bad["cards"][0]["created_at"], bad["cards"][0]["updated_at"] = "2026-01-02 00:00:00", "2026-01-01 00:00:00"
        cases.append((bad, "INVALID_BOARD_TIMESTAMP"))
        bad = json.loads(json.dumps(exported, ensure_ascii=False))
        bad["cards"][0]["archived"] = 1
        bad["cards"][0]["archived_at"] = bad["cards"][0]["created_at"]
        cases.append((bad, "INVALID_ARCHIVE_STATE"))
        for payload, code in cases:
            with self.subTest(code=code), self.assertRaises(app.ApiError) as raised:
                app.import_replace(self.conn, {"data": payload, "expected_board_revision": app.board_revision(self.conn)})
            self.assertEqual(raised.exception.code, code)
        # 导入全部失败后原数据保持完整
        self.assertEqual([card["title"] for card in app.list_cards(self.conn)], ["脏数据"])


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


class PermanentDeleteTests(DatabaseTestCase):
    def test_active_delete_compacts_positions_bumps_revision_and_cascades_attachments(self):
        column = app.list_columns(self.conn)[0]
        first = self.create_card(column["id"], "A")
        target = self.create_card(column["id"], "B")
        last = self.create_card(column["id"], "C")
        attachment = app.save_attachment(self.conn, target["id"], "data.bin", "", io.BytesIO(b"data"), 4)
        revision = app.board_revision(self.conn)
        result = app.permanently_delete_card(self.conn, target["id"], self.with_card_tokens(target["id"]))
        self.assertEqual(result["revision"], revision + 1)
        self.assertEqual([(card["id"], card["position"]) for card in app.list_cards(self.conn, column["id"])], [(first["id"], 0), (last["id"], 1)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attachments WHERE id=?", (attachment["id"],)).fetchone()[0], 0)
        self.assertFalse(os.path.exists(app.attachment_directory(target["id"])))

    def test_archived_delete_does_not_rewrite_active_positions(self):
        column = app.list_columns(self.conn)[0]
        active = self.create_card(column["id"], "活动")
        archived = self.create_card(column["id"], "归档")
        self.archive_card(archived["id"])
        before = [(card["id"], card["position"]) for card in app.list_cards(self.conn, column["id"])]
        app.permanently_delete_card(self.conn, archived["id"], self.with_card_tokens(archived["id"]))
        self.assertEqual([(card["id"], card["position"]) for card in app.list_cards(self.conn, column["id"])], before)
        self.assertEqual(before, [(active["id"], 0)])

    def test_conflicts_leave_card_and_attachment_directory_untouched(self):
        card = self.create_card(app.list_columns(self.conn)[0]["id"], "冲突")
        app.save_attachment(self.conn, card["id"], "data.bin", "", io.BytesIO(b"data"), 4)
        for payload, code in (
            ({"expected_version": card["version"], "expected_board_revision": app.board_revision(self.conn) + 1}, "BOARD_REVISION_CONFLICT"),
            ({"expected_version": card["version"] + 1, "expected_board_revision": app.board_revision(self.conn)}, "VERSION_CONFLICT"),
        ):
            with self.subTest(code=code), self.assertRaises(app.ApiError) as raised:
                app.permanently_delete_card(self.conn, card["id"], payload)
            self.assertEqual(raised.exception.code, code)
            self.assertTrue(os.path.isfile(app.attachment_path(card["id"], "data.bin")))
            self.assertEqual(app.get_card(self.conn, card["id"])["title"], "冲突")

    def test_transaction_failure_restores_attachment_directory(self):
        card = self.create_card(app.list_columns(self.conn)[0]["id"], "回滚")
        app.save_attachment(self.conn, card["id"], "data.bin", "", io.BytesIO(b"data"), 4)
        with mock.patch.object(board, "bump_revision", side_effect=sqlite3.OperationalError("forced")):
            with self.assertRaises(sqlite3.OperationalError):
                app.permanently_delete_card(self.conn, card["id"], self.with_card_tokens(card["id"]))
        self.assertTrue(os.path.isfile(app.attachment_path(card["id"], "data.bin")))
        self.assertEqual(app.get_card(self.conn, card["id"])["title"], "回滚")

    def test_postcommit_cleanup_failure_keeps_deleted_metadata_and_records_cleanup(self):
        card = self.create_card(app.list_columns(self.conn)[0]["id"], "清理")
        app.save_attachment(self.conn, card["id"], "data.bin", "", io.BytesIO(b"data"), 4)
        real_rmtree = shutil.rmtree
        def fail_rollback(path, *args, **kwargs):
            if ".permanent-delete-" in path:
                raise PermissionError("locked")
            return real_rmtree(path, *args, **kwargs)
        with mock.patch.object(board.shutil, "rmtree", side_effect=fail_rollback):
            app.permanently_delete_card(self.conn, card["id"], self.with_card_tokens(card["id"]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cards WHERE id=?", (card["id"],)).fetchone()[0], 0)
        self.assertTrue(any(".permanent-delete-" in path for path in app.MAINTENANCE_REPORT["cleanup"]))


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
        with mock.patch("attachments.os.remove", side_effect=fail_rollback):
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
        with mock.patch("attachments.os.remove", side_effect=fail_trash):
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


class BatchOperationsTests(DatabaseTestCase):
    def batch_items(self, *cards):
        return [{"id": card["id"], "version": card["version"]} for card in cards]

    def test_batch_archive_compacts_positions_and_bumps_revision_once(self):
        column = app.list_columns(self.conn)[0]
        first = self.create_card(column["id"], "A")
        middle = self.create_card(column["id"], "B")
        last = self.create_card(column["id"], "C")
        revision = app.board_revision(self.conn)
        result = app.batch_archive_cards(self.conn, {"items": self.batch_items(first, last), "expected_board_revision": revision})
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["revision"], revision + 1)
        self.assertEqual([(card["id"], card["position"]) for card in app.list_cards(self.conn, column["id"])], [(middle["id"], 0)])
        for card_id in (first["id"], last["id"]):
            card = app.get_card(self.conn, card_id, include_draft=True)
            self.assertEqual(card["archived"], 1)
            self.assertEqual(card["archive_reason"], "manual")

    def test_batch_restore_uses_original_or_fallback_column(self):
        first_column = app.list_columns(self.conn)[0]
        second_column = self.create_column("规划中")
        card = self.create_card(first_column["id"], "归档卡")
        self.archive_card(card["id"])
        card = app.get_card(self.conn, card["id"], include_draft=True)
        result = app.batch_restore_cards(self.conn, {"items": self.batch_items(card), "expected_board_revision": app.board_revision(self.conn)})
        self.assertEqual(result["count"], 1)
        restored = app.get_card(self.conn, card["id"], include_draft=True)
        self.assertEqual(restored["archived"], 0)
        self.assertEqual(restored["column_id"], first_column["id"])
        # 原列已删除时回退到同名活跃列或第一个活跃列
        card2 = self.create_card(first_column["id"], "归档卡2")
        self.archive_card(card2["id"])
        card2 = app.get_card(self.conn, card2["id"], include_draft=True)
        app.delete_column(self.conn, first_column["id"], self.with_column_tokens(first_column["id"]))
        app.batch_restore_cards(self.conn, {"items": self.batch_items(card2), "expected_board_revision": app.board_revision(self.conn)})
        self.assertEqual(app.get_card(self.conn, card2["id"], include_draft=True)["column_id"], app.list_columns(self.conn)[0]["id"])

    def test_batch_restore_to_specific_column(self):
        first_column = app.list_columns(self.conn)[0]
        second_column = self.create_column("目标列")
        card = self.create_card(first_column["id"], "归档卡")
        self.archive_card(card["id"])
        card = app.get_card(self.conn, card["id"], include_draft=True)
        app.batch_restore_cards(self.conn, {"items": self.batch_items(card), "target_column_id": second_column["id"], "expected_board_revision": app.board_revision(self.conn)})
        self.assertEqual(app.get_card(self.conn, card["id"], include_draft=True)["column_id"], second_column["id"])

    def test_batch_permanent_delete_removes_files_and_rolls_back_on_failure(self):
        column = app.list_columns(self.conn)[0]
        keep = self.create_card(column["id"], "保留")
        doomed = self.create_card(column["id"], "删除")
        app.save_attachment(self.conn, doomed["id"], "data.bin", "", io.BytesIO(b"data"), 4)
        revision = app.board_revision(self.conn)
        result = app.batch_permanently_delete_cards(self.conn, {"items": self.batch_items(doomed), "expected_board_revision": revision})
        self.assertEqual(result["count"], 1)
        self.assertFalse(os.path.exists(app.attachment_directory(doomed["id"])))
        self.assertEqual([(card["id"], card["position"]) for card in app.list_cards(self.conn, column["id"])], [(keep["id"], 0)])
        # 中途失败时整体回滚
        doomed2 = self.create_card(column["id"], "删除2")
        app.save_attachment(self.conn, doomed2["id"], "data.bin", "", io.BytesIO(b"data"), 4)
        with self.assertRaises(app.ApiError):
            app.batch_permanently_delete_cards(self.conn, {"items": self.batch_items(doomed2) + [{"id": 999999, "version": 1}], "expected_board_revision": app.board_revision(self.conn)})
        self.assertEqual(app.get_card(self.conn, doomed2["id"])["title"], "删除2")
        self.assertTrue(os.path.isfile(app.attachment_path(doomed2["id"], "data.bin")))

    def test_batch_validation_and_version_conflicts(self):
        column = app.list_columns(self.conn)[0]
        card = self.create_card(column["id"], "冲突")
        for payload, code in (
            ({"items": [], "expected_board_revision": app.board_revision(self.conn)}, "VALIDATION_ERROR"),
            ({"items": [{"id": card["id"], "version": 1}, {"id": card["id"], "version": 1}], "expected_board_revision": app.board_revision(self.conn)}, "VALIDATION_ERROR"),
            ({"items": [{"id": card["id"]}], "expected_board_revision": app.board_revision(self.conn)}, "VALIDATION_ERROR"),
            ({"items": [{"id": card["id"], "version": card["version"] + 1}], "expected_board_revision": app.board_revision(self.conn)}, "VERSION_CONFLICT"),
            ({"items": [{"id": card["id"], "version": card["version"]}], "expected_board_revision": app.board_revision(self.conn) + 1}, "BOARD_REVISION_CONFLICT"),
        ):
            with self.subTest(code=code), self.assertRaises(app.ApiError) as raised:
                app.batch_archive_cards(self.conn, payload)
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(app.get_card(self.conn, card["id"])["archived"], 0)


if __name__ == "__main__":
    unittest.main()
