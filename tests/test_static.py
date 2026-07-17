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

    def test_focus_and_responsive_css_exists(self):
        self.assertIn(":focus-visible", self.css)
        self.assertIn("@media (max-width: 720px)", self.css)
        self.assertIn("prefers-reduced-motion", self.css)


if __name__ == "__main__":
    unittest.main()
