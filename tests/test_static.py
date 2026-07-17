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

    def test_focus_and_responsive_css_exists(self):
        self.assertIn(":focus-visible", self.css)
        self.assertIn("@media (max-width: 720px)", self.css)
        self.assertIn("prefers-reduced-motion", self.css)


if __name__ == "__main__":
    unittest.main()
