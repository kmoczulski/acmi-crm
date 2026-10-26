"""Boards: ordering and moves, the board page, the card dialog and its actions, search, notifications, the sweep."""
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from crm import notify
from crm.boards import logic
from crm.models import (
    Board, BoardList, Campus, Card, CardEvent, Checklist, ChecklistItem, Comment, Company, Notification, Opportunity,
    SavedView,
)

from .utils import make_manager

User = get_user_model()


class BoardLogicTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", first_name="Kellen", last_name="M")
        self.board = logic.create_board("MEIH board", self.user, template="project")

    def test_template_makes_lists_and_labels_and_joins_creator(self):
        self.assertEqual([l.name for l in self.board.lists.all()],
                         ["Land sites", "Critical dates", "Deal and design", "Contacts and consultants", "Open tasks"])
        self.assertEqual(self.board.labels.count(), 7)
        self.assertIn(self.user, self.board.members.all())

    def test_positions_grow_by_gap_and_moves_land_between_neighbours(self):
        lst = self.board.lists.first()
        a, b, c = (logic.add_card(lst, t, self.user) for t in ("A", "B", "C"))
        self.assertEqual([a.position, b.position, c.position], [1000, 2000, 3000])
        logic.place(c, lst, 0, self.user)  # to the front
        self.assertEqual(list(lst.cards.values_list("title", flat=True)), ["C", "A", "B"])
        logic.place(a, lst, 2, self.user)  # to the end
        self.assertEqual(list(lst.cards.values_list("title", flat=True)), ["C", "B", "A"])
        logic.place(a, lst, 1, self.user)  # between C and B
        c.refresh_from_db(); b.refresh_from_db(); a.refresh_from_db()
        self.assertTrue(c.position < a.position < b.position)

    def test_crowded_list_is_renumbered(self):
        lst = self.board.lists.first()
        cards = [logic.add_card(lst, f"T{i}", self.user) for i in range(3)]
        for c, pos in zip(cards, (1, 2, 3)):
            Card.objects.filter(pk=c.pk).update(position=pos)
        new = logic.add_card(lst, "New", self.user)
        logic.place(new, lst, 1, self.user)  # no room between 1 and 2
        order = list(lst.cards.values_list("title", "position"))
        self.assertEqual([t for t, _ in order], ["T0", "New", "T1", "T2"])
        self.assertEqual([p for _, p in order], [1000, 2000, 3000, 4000])

    def test_move_across_lists_logs_an_event(self):
        src, dst = self.board.lists.all()[:2]
        card = logic.add_card(src, "Call the county", self.user)
        logic.place(card, dst, 0, self.user)
        card.refresh_from_db()
        self.assertEqual(card.list, dst)
        self.assertTrue(CardEvent.objects.filter(card=card, kind="moved", text__contains="Critical dates").exists())

    def test_record_link_from_search_url(self):
        company = Company.objects.create(name="Apex Optics")
        card = logic.add_card(self.board.lists.first(), "Apex", self.user)
        self.assertEqual(logic.link_from_url(f"/companies/{company.pk}/"), ("company", company.pk))
        self.assertEqual(logic.link_from_url("/meetings/?month=2026-10&open=7"), ("meeting", 7))
        self.assertEqual(logic.link_from_url("/nowhere/"), (None, None))
        logic.set_record(card, "company", company.pk, self.user)
        self.assertEqual(card.record(), company)
        self.assertEqual(card.record_kind, "Company")
        self.assertEqual(logic.record_facts(card)[0], ("Stage", "Prospect"))
        self.assertEqual(list(logic.record_cards(company)), [card])
        logic.set_record(card, None, None, self.user)
        self.assertIsNone(card.record())

    def test_due_state(self):
        card = logic.add_card(self.board.lists.first(), "X", self.user)
        today = timezone.localdate()
        card.due = today - timedelta(days=1)
        self.assertEqual(card.due_state, "late")
        card.due = today
        self.assertEqual(card.due_state, "today")
        card.due = today + timedelta(days=3)
        self.assertEqual(card.due_state, "soon")
        card.done = True
        self.assertEqual(card.due_state, "")


class BoardPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", first_name="Kellen", last_name="M")
        self.other = User.objects.create_user("dave", password="x", first_name="Dave", last_name="H")
        self.client.force_login(self.user)
        self.board = logic.create_board("Anvil", self.user, template="deal")
        self.todo, self.doing = self.board.lists.all()[:2]
        self.card = logic.add_card(self.todo, "Send the pro forma", self.user)

    def test_index_lists_my_boards_and_creates_one(self):
        r = self.client.get(reverse("crm:boards"))
        self.assertContains(r, "Anvil")
        self.assertContains(r, "My boards")
        r = self.client.post(reverse("crm:board_create"), {"name": "NSIH", "template": "project", "kind": "campus", "colour": "teal"})
        board = Board.objects.get(name="NSIH")
        self.assertRedirects(r, board.get_absolute_url())
        self.assertEqual(board.lists.count(), 5)

    def test_board_page_shows_lists_cards_and_filters(self):
        r = self.client.get(self.board.get_absolute_url())
        self.assertContains(r, "Send the pro forma")
        self.assertContains(r, 'data-list="%d"' % self.todo.pk)
        self.assertContains(r, 'aria-live="polite"')
        self.card.members.add(self.other)
        r = self.client.get(self.board.get_absolute_url() + "?mine=1")
        self.assertNotContains(r, "Send the pro forma")
        r = self.client.get(self.board.get_absolute_url() + f"?member={self.other.pk}")
        self.assertContains(r, "Send the pro forma")
        Card.objects.filter(pk=self.card.pk).update(due=timezone.localdate() - timedelta(days=2))
        r = self.client.get(self.board.get_absolute_url() + "?due=late")
        self.assertContains(r, "Overdue")

    def test_add_card_by_fetch_returns_the_card_fragment(self):
        r = self.client.post(reverse("crm:card_add", args=[self.todo.pk]), {"title": "Book site visit"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Book site visit")
        self.assertContains(r, 'class="bl-card')
        card = Card.objects.get(title="Book site visit")
        self.assertEqual(card.position, 2000)
        r = self.client.post(reverse("crm:card_add", args=[self.todo.pk]), {"title": "  "}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.status_code, 400)

    def test_move_endpoint_and_board_boundary(self):
        r = self.client.post(reverse("crm:card_move", args=[self.card.pk]), {"list": self.doing.pk, "index": 0})
        self.assertEqual(r.json()["ok"], True)
        self.card.refresh_from_db()
        self.assertEqual(self.card.list, self.doing)
        other_board = logic.create_board("Elsewhere", self.user, template="deal")
        r = self.client.post(reverse("crm:card_move", args=[self.card.pk]), {"list": other_board.lists.first().pk, "index": 0})
        self.assertEqual(r.status_code, 400)

    def test_card_dialog_and_edits(self):
        url = reverse("crm:card", args=[self.card.pk])
        r = self.client.get(url, HTTP_X_REQUESTED_WITH="fetch")
        self.assertContains(r, "Send the pro forma")
        self.assertContains(r, "Add to card")
        self.assertRedirects(self.client.get(url), self.card.get_absolute_url())  # opened directly: the board with the card open
        edit = reverse("crm:card_edit", args=[self.card.pk])
        self.client.post(edit, {"action": "title", "title": "Send the Anvil pro forma"})
        self.client.post(edit, {"action": "description", "description": "Include the rail spur option."})
        due = timezone.localdate() + timedelta(days=2)
        self.client.post(edit, {"action": "dates", "start": "", "due": due.isoformat(), "due_time": "14:30"})
        label = self.board.labels.first()
        self.client.post(edit, {"action": "labels", "label": [label.pk]})
        self.client.post(edit, {"action": "members", "member": [self.other.pk]})
        self.client.post(edit, {"action": "cover", "cover": "red"})
        self.card.refresh_from_db()
        self.assertEqual(self.card.title, "Send the Anvil pro forma")
        self.assertEqual(self.card.description, "Include the rail spur option.")
        self.assertEqual((self.card.due, str(self.card.due_time)), (due, "14:30:00"))
        self.assertEqual(list(self.card.labels.all()), [label])
        self.assertEqual(list(self.card.members.all()), [self.other])
        self.assertEqual(self.card.cover, "red")
        self.assertTrue(Notification.objects.filter(user=self.other, kind="card", text__contains="added you to a card").exists())
        kinds = set(CardEvent.objects.filter(card=self.card).values_list("kind", flat=True))
        self.assertTrue({"created", "renamed", "due", "label", "member"} <= kinds)

    def test_link_record_move_copy_archive_and_delete_rules(self):
        edit = reverse("crm:card_edit", args=[self.card.pk])
        company = Company.objects.create(name="Harbor Point Bank")
        self.client.post(edit, {"action": "link", "record_url": f"/companies/{company.pk}/", "rename": "1"})
        self.card.refresh_from_db()
        self.assertEqual((self.card.company, self.card.title), (company, "Harbor Point Bank"))
        r = self.client.get(company.get_absolute_url())
        self.assertContains(r, "Anvil")  # the Boards panel on the company page
        self.client.post(edit, {"action": "move", "list": self.doing.pk, "position": "1"})
        self.card.refresh_from_db()
        self.assertEqual(self.card.list, self.doing)
        r = self.client.post(edit, {"action": "copy"})
        copy = Card.objects.exclude(pk=self.card.pk).get()
        self.assertEqual((copy.title, copy.company, copy.list), (self.card.title, company, self.doing))
        self.assertRedirects(r, copy.get_absolute_url())
        r = self.client.post(edit, {"action": "delete"})  # not archived, not a manager
        self.assertTrue(Card.objects.filter(pk=self.card.pk).exists())
        self.client.post(edit, {"action": "archive"})
        self.card.refresh_from_db()
        self.assertTrue(self.card.archived)
        self.client.post(edit, {"action": "delete"})
        self.assertTrue(Card.objects.filter(pk=self.card.pk).exists())  # still refused: not a manager
        make_manager(self.user)
        self.client.post(edit, {"action": "delete"})
        self.assertFalse(Card.objects.filter(pk=self.card.pk).exists())

    def test_add_to_board_from_a_record_page(self):
        company = Company.objects.create(name="Nova Munitions")
        r = self.client.post(reverse("crm:record_add_to_board"), {"field": "company", "pk": company.pk, "board": self.board.pk,
                                                                   "next": company.get_absolute_url()})
        card = Card.objects.get(company=company)
        self.assertEqual((card.title, card.list), ("Nova Munitions", self.todo))
        self.assertRedirects(r, company.get_absolute_url())

    def test_checklists_and_attachments(self):
        url = reverse("crm:card_checklist", args=[self.card.pk])
        self.client.post(url, {"action": "add", "name": "Before the call"})
        cl = Checklist.objects.get(card=self.card)
        self.client.post(url, {"action": "item_add", "checklist": cl.pk, "text": "Pull the budget"})
        item = ChecklistItem.objects.get()
        r = self.client.post(url, {"action": "item_toggle", "item": item.pk}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.json(), {"ok": True, "done": True})
        r = self.client.get(reverse("crm:card", args=[self.card.pk]), HTTP_X_REQUESTED_WITH="fetch")
        self.assertContains(r, "Pull the budget")
        self.client.post(reverse("crm:card_attach", args=[self.card.pk]), {"action": "add", "url": "https://drive.example/x/budget", "name": "Budget"})
        self.assertEqual(self.card.attachments.get().name, "Budget")
        r = self.client.get(self.board.get_absolute_url())
        self.assertContains(r, "☑ 1/1")
        self.assertContains(r, "📎 1")

    def test_uploaded_attachments_open_in_the_page_only_when_they_are_pdfs_or_pictures(self):
        """The type follows the stored file, never the typed name, so a web page named like a document downloads
        instead of running on the hub's origin."""
        url = reverse("crm:card_attach", args=[self.card.pk])
        self.client.post(url, {"action": "add", "name": "Notes.pdf",
                               "file": SimpleUploadedFile("page.html", b"<script>alert(1)</script>", content_type="text/html")})
        att = self.card.attachments.get(name="Notes.pdf")
        r = self.client.get(reverse("crm:card_attachment_file", args=[att.pk]))
        b"".join(r.streaming_content)
        self.assertTrue(r["Content-Disposition"].startswith("attachment"), r["Content-Disposition"])
        self.assertIn("Notes.pdf.html", r["Content-Disposition"])
        self.assertNotEqual(r["Content-Type"], "application/pdf")
        self.client.post(url, {"action": "add", "name": "Plan",
                               "file": SimpleUploadedFile("plan.pdf", b"%PDF-1.4 x", content_type="application/pdf")})
        att = self.card.attachments.get(name="Plan")
        r = self.client.get(reverse("crm:card_attachment_file", args=[att.pk]))
        b"".join(r.streaming_content)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r["Content-Disposition"].startswith("inline"))
        self.assertIn("Plan.pdf", r["Content-Disposition"])

    def test_comments_on_cards_notify_members(self):
        self.card.members.add(self.other)
        r = self.client.post(reverse("crm:comment_add"), {"card": self.card.pk, "body": "Draft is in the folder.", "next": self.card.get_absolute_url()})
        self.assertEqual(r.status_code, 302)
        c = Comment.objects.get()
        self.assertEqual((c.card, c.record()), (self.card, self.card))
        self.assertTrue(Notification.objects.filter(user=self.other, kind="comment").exists())

    def test_lists_rename_move_sort_archive(self):
        url = reverse("crm:board_list_edit", args=[self.todo.pk])
        self.client.post(url, {"action": "rename", "name": "Backlog"})
        self.client.post(url, {"action": "move", "to": "right"})
        self.assertEqual([l.name for l in self.board.lists.filter(archived=False)][:2], ["Doing", "Backlog"])
        logic.add_card(self.todo, "Alpha", self.user)
        Card.objects.filter(pk=self.card.pk).update(due=timezone.localdate())
        self.client.post(url, {"action": "sort", "by": "name"})
        self.assertEqual(list(self.todo.cards.values_list("title", flat=True)), ["Alpha", "Send the pro forma"])
        self.client.post(url, {"action": "archive"})
        self.assertTrue(BoardList.objects.get(pk=self.todo.pk).archived)
        r = self.client.get(self.board.get_absolute_url())
        self.assertNotContains(r, 'data-list="%d"' % self.todo.pk)
        r = self.client.post(reverse("crm:board_edit", args=[self.board.pk]), {"action": "list_add", "name": "Waiting on county"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertContains(r, "Waiting on county")

    def test_board_settings_members_archive_delete(self):
        url = reverse("crm:board_edit", args=[self.board.pk])
        self.client.post(url, {"action": "settings", "name": "Anvil deals", "colour": "rose", "description": "Phase 1"})
        self.client.post(url, {"action": "members", "members": [self.other.pk]})
        self.client.post(url, {"action": "label_add", "name": "Bank", "colour": "lime"})
        self.board.refresh_from_db()
        self.assertEqual((self.board.name, self.board.colour, list(self.board.members.all())), ("Anvil deals", "rose", [self.other]))
        self.assertTrue(self.board.labels.filter(name="Bank", colour="lime").exists())
        r = self.client.post(url, {"action": "archive"})
        self.board.refresh_from_db()
        self.assertFalse(self.board.archived)  # Managers only
        make_manager(self.user)
        self.client.post(url, {"action": "archive"})
        self.board.refresh_from_db()
        self.assertTrue(self.board.archived)
        self.client.post(url, {"action": "delete"})
        self.assertFalse(Board.objects.filter(pk=self.board.pk).exists())

    def test_saved_views_per_board(self):
        r = self.client.post(reverse("crm:saved_view_save"), {"object_type": f"board:{self.board.pk}", "query": "due=late&mine=1", "name": "My overdue",
                                                               "next": self.board.get_absolute_url()})
        self.assertRedirects(r, self.board.get_absolute_url() + "?due=late&mine=1")
        self.assertTrue(SavedView.objects.filter(object_type=f"board:{self.board.pk}", name="My overdue").exists())
        r = self.client.get(self.board.get_absolute_url())
        self.assertContains(r, "My overdue")

    def test_search_finds_cards_and_new_menu_has_card(self):
        r = self.client.get(reverse("crm:search") + "?q=pro forma")
        groups = {g["label"]: g["items"] for g in r.json()["groups"]}
        self.assertEqual(groups["Cards"][0]["sub"], "Anvil › To do")
        r = self.client.get(reverse("crm:boards") + "?new=card")
        self.assertContains(r, 'id="new-card-form"')
        r = self.client.post(reverse("crm:quick_card"), {"list": self.todo.pk, "title": "Quick one"})
        card = Card.objects.get(title="Quick one")
        self.assertRedirects(r, card.get_absolute_url())
        self.assertIn(self.user, card.members.all())
        r = self.client.get("/")
        self.assertContains(r, "?new=card")

    def test_due_sweep_covers_cards(self):
        self.card.members.add(self.user)
        Card.objects.filter(pk=self.card.pk).update(due=timezone.localdate() - timedelta(days=3))
        notify.due_sweep(self.user)
        n = Notification.objects.get(user=self.user, kind="due")
        self.assertIn("Overdue 3 days: Send the pro forma (Anvil)", n.text)
        notify.due_sweep(self.user)
        self.assertEqual(Notification.objects.filter(user=self.user, kind="due").count(), 1)

    def test_board_page_query_count_stays_flat(self):
        for i in range(40):
            c = logic.add_card(self.todo if i % 2 else self.doing, f"Card {i}", self.user)
            c.members.add(self.other)
            c.labels.add(self.board.labels.first())
        self.client.get(self.board.get_absolute_url())  # the first request of a session runs the daily due sweep
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(self.board.get_absolute_url())
        self.assertEqual(r.status_code, 200)
        self.assertLess(len(ctx), 26, [q["sql"][:90] for q in ctx.captured_queries])
