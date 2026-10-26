from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.models import Company, Task

User = get_user_model()


class TaskEditTests(TestCase):
    def setUp(self):
        self.kellen = User.objects.create_user("kellen", password="x", first_name="Kellen")
        self.client.force_login(self.kellen)
        self.co = Company.objects.create(name="Acme Energetics")
        self.task = Task.objects.create(title="Send site brief", notes="Include QD arcs.", due_date=date(2026, 11, 3),
                                        label=Task.Label.LAND_USE, assignee=self.kellen, company=self.co)
        self.url = reverse("crm:task_text", args=[self.task.pk])

    def test_page_offers_rename_and_description_edit(self):
        r = self.client.get(self.task.get_absolute_url())
        self.assertContains(r, 'aria-label="Rename task"')
        self.assertContains(r, 'name="title" value="Send site brief"')
        self.assertContains(r, '<textarea name="notes"')
        self.assertNotContains(r, 'name="notes" cols')  # the side form no longer carries a second copy

    def test_rename_leaves_everything_else_alone(self):
        r = self.client.post(self.url, {"title": "  Send the Huntsville site brief "})
        self.assertRedirects(r, self.task.get_absolute_url(), fetch_redirect_response=False)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Send the Huntsville site brief")
        self.assertEqual((self.task.notes, self.task.due_date, self.task.label, self.task.assignee, self.task.company),
                         ("Include QD arcs.", date(2026, 11, 3), Task.Label.LAND_USE, self.kellen, self.co))

    def test_description_edit_keeps_the_title(self):
        self.client.post(self.url, {"notes": "Include QD arcs and the TVA power letter."})
        self.task.refresh_from_db()
        self.assertEqual((self.task.title, self.task.notes), ("Send site brief", "Include QD arcs and the TVA power letter."))
        self.client.post(self.url, {"notes": ""})
        self.task.refresh_from_db()
        self.assertEqual(self.task.notes, "")

    def test_blank_name_is_refused(self):
        self.client.post(self.url, {"title": "   "})
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Send site brief")

    def test_side_form_save_keeps_title_and_description(self):
        r = self.client.post(self.task.get_absolute_url(), {"label": "", "due_date": "2026-11-10", "assignee": self.kellen.pk})
        self.assertEqual(r.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual((self.task.title, self.task.notes, self.task.due_date),
                         ("Send site brief", "Include QD arcs.", date(2026, 11, 10)))

    def test_task_rows_link_to_edit(self):
        r = self.client.get(reverse("crm:task_list"))
        self.assertContains(r, f'href="{self.task.get_absolute_url()}#edit"')


class TaskListOrderTests(TestCase):
    def test_the_list_is_due_first_even_with_the_comment_count(self):
        """Meta.ordering is dropped on a GROUP BY query, so the comment count left the page in database order."""
        from crm.models import Comment

        kellen = User.objects.create_user("kellen", password="x")
        self.client.force_login(kellen)
        later = Task.objects.create(title="Later", due_date=date(2026, 11, 20), assignee=kellen)
        soon = Task.objects.create(title="Soon", due_date=date(2026, 11, 1), assignee=kellen)
        someday = Task.objects.create(title="Someday", assignee=kellen)
        Comment.objects.create(body="Any news?", author=kellen, task=later)
        r = self.client.get(reverse("crm:task_list"))
        self.assertEqual([t.title for t in r.context["page"].object_list], ["Soon", "Later", "Someday"])
        self.assertEqual([t.n_comments for t in r.context["page"].object_list], [0, 1, 0])
