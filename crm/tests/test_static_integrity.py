"""Guards against a push that empties or cuts a file. On 2026-11-03 a large-file push left crm/views.py and
crm/templates/crm/inbox.html empty, crm/static/crm/recipients.js a one-word "PLACEHOLDER" and the first line of a rule in
app.css gone; the first two were noticed at once, the other two days later (address suggestions had stopped working)."""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

STATIC = Path(settings.BASE_DIR) / "crm" / "static" / "crm"


class StaticFileIntegrityTests(SimpleTestCase):
    def test_no_script_or_stylesheet_is_empty_or_a_placeholder(self):
        files = sorted([*STATIC.glob("*.js"), *STATIC.glob("*.css")])
        self.assertTrue(files)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertGreater(len(text.strip()), 200, f"{path.name} is empty or a placeholder")
            self.assertIsNone(re.search(r"^(PLACEHOLDER|CONTENT_FROM_FILE)\s*$", text, flags=re.M), path.name)

    def test_the_stylesheet_has_balanced_braces(self):
        css = re.sub(r"/\*.*?\*/", "", (STATIC / "app.css").read_text(encoding="utf-8"), flags=re.S)
        depth = lowest = 0
        for char in css:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                lowest = min(lowest, depth)
        self.assertEqual(lowest, 0, "app.css has a } with no { before it: a rule lost its first line?")
        self.assertEqual(depth, 0, "app.css has a { that is never closed")
