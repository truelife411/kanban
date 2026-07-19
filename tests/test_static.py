import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ElementIndex(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if attributes.get("id"):
            self.ids.add(attributes["id"])


class AccessibilityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        cls.parser = ElementIndex()
        cls.parser.feed(cls.html)

    def test_dialogs_have_modal_semantics_and_valid_labels(self):
        dialogs = [attrs for _, attrs in self.parser.elements if attrs.get("role") == "dialog"]
        self.assertGreaterEqual(len(dialogs), 2)
        for dialog in dialogs:
            self.assertEqual(dialog.get("aria-modal"), "true")
            self.assertIn(dialog.get("aria-labelledby"), self.parser.ids)

    def test_editor_and_live_regions_are_present(self):
        editor = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "card-description")
        self.assertEqual(editor.get("role"), "textbox")
        self.assertEqual(editor.get("aria-multiline"), "true")
        toast = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "toast")
        self.assertEqual(toast.get("aria-live"), "polite")

    def test_topbar_icon_actions_have_tooltips(self):
        theme = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "theme-button")
        self.assertEqual(theme.get("title"), "切换主题")
        self.assertEqual(theme.get("aria-label"), "切换主题")
        self.assertGreaterEqual(self.html.count("toolbar-icon-only"), 4)

    def test_auto_archive_hint_is_rendered_for_completed_column(self):
        app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('column.name.trim()==="已完成"', app_js)
        self.assertIn("auto-archive-hint", app_js)
        self.assertIn("满 30 天后", app_js)
        self.assertIn(".auto-archive-hint", self.css)

    def test_view_switcher_uses_tab_semantics(self):
        self.assertIn('role="tablist"', self.html)
        self.assertEqual(self.html.count('role="tab"'), 2)
        self.assertEqual(self.html.count('role="tabpanel"'), 2)
        self.assertIn('aria-controls="view-board"', self.html)
        self.assertIn('aria-controls="view-history"', self.html)
        app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("setupViewTabs", app_js)
        self.assertIn('event.key==="ArrowRight"', app_js)
        self.assertIn('event.key==="ArrowLeft"', app_js)

    def test_theme_menu_and_four_themes_exist(self):
        self.assertIn('id="theme-menu"', self.html)
        self.assertIn('data-theme="douban-classic"', self.html)
        for theme in ("douban-classic", "douban-modern", "dark-tech", "office", "cloud-blue", "navy-blue", "aurora-blue"):
            self.assertIn(theme, self.html)
        for selector in ('data-theme="douban-modern"', 'data-theme="dark-tech"', 'data-theme="office"', 'data-theme="cloud-blue"', 'data-theme="navy-blue"', 'data-theme="aurora-blue"'):
            self.assertIn(selector, self.css)

    def test_attachment_ui_contract(self):
        app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        attachment_input = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "attachment-input")
        self.assertIn("multiple", attachment_input)
        self.assertIn('aria-live="polite"', self.html)
        for name in ("selectAttachments", "processAttachmentQueue", "uploadAttachment", "toggleResultAttachments", "attachment_count"):
            self.assertIn(name, app_js)
        for selector in (".attachment-item", ".attachment-progress", ".history-attachments"):
            self.assertIn(selector, self.css)
        self.assertIn("application/zip,.zip", self.html)
        self.assertIn("完整备份（含附件）", self.html)

    def test_native_date_controls_are_styled_and_accessible(self):
        app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        dates_js = (ROOT / "static" / "js" / "dates.js").read_text(encoding="utf-8")
        due = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "card-due")
        due_time = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "card-due-time")
        self.assertEqual(due.get("type"), "date")
        self.assertEqual(due.get("aria-label"), "截止日期")
        self.assertEqual(due_time.get("type"), "time")
        self.assertEqual(due_time.get("step"), "60")
        self.assertEqual(due_time.get("aria-label"), "截止时间（可选）")
        self.assertNotIn('id="card-time-hint"', self.html)
        self.assertIn('id="card-date-clear"', self.html)
        self.assertIn('id="card-time-clear"', self.html)
        self.assertIn("date-picker-trigger", self.html)
        self.assertIn("history-date-range", self.html)
        self.assertIn('id="search-from-clear"', self.html)
        self.assertIn('id="search-to-clear"', self.html)
        self.assertIn("history-date-trigger", self.html)
        self.assertIn("date-range-separator", self.html)
        self.assertIn("input[type=time]", self.css)
        self.assertIn(".date-input-shell", self.css)
        self.assertIn(".history-date-range", self.css)
        self.assertIn("syncHistoryDateControl", dates_js)
        self.assertIn("clearHistoryDate", dates_js)
        self.assertIn("起始日期不能晚于结束日期", app_js)
        self.assertIn("calendarDayNumber", dates_js)
        self.assertIn("openNativePicker", dates_js)
        self.assertIn("syncCardDateControl", dates_js)
        self.assertIn("clearCardDate", dates_js)
        self.assertIn("showPicker", dates_js)
        self.assertIn("syncCardTimeControl", dates_js)
        self.assertIn("clearCardTime", dates_js)
        self.assertIn("请先选择截止日期", app_js)
        self.assertIn(".time-clear-btn", self.css)

    def test_es_module_and_busy_state_contracts(self):
        app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('type="module"', self.html)
        for module in ("state.js", "api.js", "ui.js", "dates.js"):
            self.assertTrue((ROOT / "static" / "js" / module).is_file())
        self.assertIn("AbortController", app_js)
        self.assertIn("aria-pressed", app_js)
        self.assertIn("search-load-more", app_js)
        self.assertIn("卡片和附件均已保存", app_js)
        self.assertIn("附件上传、覆盖和删除会立即生效", self.html)
        self.assertIn('id="choice-modal"', self.html)

    def test_focus_and_responsive_css_exists(self):
        self.assertIn(":focus-visible", self.css)
        self.assertIn("@media (max-width: 720px)", self.css)
        self.assertIn("prefers-reduced-motion", self.css)


if __name__ == "__main__":
    unittest.main()
