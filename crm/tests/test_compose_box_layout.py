"""The compose box in the mail dock (Kellen, 2026-11-06: "when I am drafting an email in the popup box from the bottom of the
screen the tools at the top disappear"). The message used to grow with the text, so the whole page scrolled and To, Subject and
the formatting tools went out of sight. Now the box keeps them in view and only the message scrolls (app.css, compose.html)."""
import re
from pathlib import Path

from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from .test_mail_dock import SEND, DockTestCase

CSS = Path(settings.BASE_DIR) / "crm" / "static" / "crm" / "app.css"


@override_settings(**SEND)
class ComposeBoxLayoutTests(DockTestCase):
    def test_the_box_page_marks_its_body_so_only_it_gets_the_fixed_layout(self):
        r = self.client.get(reverse("crm:inbox_compose"), {"window": "1", "w": "ab12"})
        self.assertContains(r, '<body class="dock-body" data-dock-compose>')
        # Neither the full page nor the box that reads a conversation is touched: they scroll as pages do.
        self.assertNotContains(self.client.get(reverse("crm:inbox_compose")), "data-dock-compose")
        r = self.client.get(reverse("crm:inbox_window"), {"t": "a1", "w": "t1"})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "data-dock-compose")

    def test_the_stylesheet_fixes_the_boxs_height_and_scrolls_the_message_inside_it(self):
        css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
        rules = {" ".join(sel.split()): " ".join(body.split()) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                 if "data-dock-compose" in sel}
        self.assertEqual(sorted(rules), [
            "body[data-dock-compose] .compose-window",
            "body[data-dock-compose] .compose-window .compose-editor",
            "body[data-dock-compose] .dock-main",
        ])
        self.assertIn("height: 100vh", rules["body[data-dock-compose] .dock-main"])  # the box is as tall as its frame
        self.assertIn("overflow: hidden", rules["body[data-dock-compose] .dock-main"])  # so the page itself never scrolls
        self.assertIn("min-height: 0", rules["body[data-dock-compose] .compose-window"])
        self.assertIn("overflow-y: auto", rules["body[data-dock-compose] .compose-window"])  # only if everything else is too tall
        editor = rules["body[data-dock-compose] .compose-window .compose-editor"]
        self.assertIn("flex: 1 1 0px", editor)  # the message takes what is left and scrolls inside it (it is overflow: auto)
        self.assertIn("min-height: 90px", editor)
