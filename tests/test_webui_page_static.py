import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FileInputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.label_depth = 0
        self.file_input_in_label = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "label" and "file-button" in attributes.get("class", "").split():
            self.label_depth += 1
        elif tag == "input" and self.label_depth and attributes.get("type") == "file":
            self.file_input_in_label = True

    def handle_endtag(self, tag):
        if tag == "label" and self.label_depth:
            self.label_depth -= 1


class PageStaticTests(unittest.TestCase):
    def test_hidden_file_input_has_keyboard_focus_indicator(self):
        parser = FileInputParser()
        parser.feed((ROOT / "pages/settings/index.html").read_text(encoding="utf-8"))
        css = (ROOT / "pages/settings/style.css").read_text(encoding="utf-8")

        self.assertTrue(parser.file_input_in_label)
        self.assertIn(".file-button:focus-within", css)
        self.assertRegex(css, r"\.file-button:focus-within\s*\{[^}]*outline:")

    def test_page_handles_bridge_readable_field_error_responses(self):
        javascript = (ROOT / "pages/settings/app.js").read_text(encoding="utf-8")

        self.assertGreaterEqual(javascript.count('if (result.saved === false)'), 3)
        self.assertIn("showErrors(result.errors)", javascript)


if __name__ == "__main__":
    unittest.main()
