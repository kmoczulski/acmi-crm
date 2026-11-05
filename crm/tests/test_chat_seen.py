"""Chat "Seen by": who has read your latest message, from each teammate's own Chat read time."""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from crm.chat import service
from crm.chat.api import ChatError
from crm.models import ChatConnection

SENT = datetime(2026, 11, 5, 14, 0, tzinfo=dt_timezone.utc)
SPACE = "spaces/AAA"


def stamp(dt):
    return dt.isoformat().replace("+00:00", "Z")


class FakeTeammateClient:
    def __init__(self, reads, who):
        self.reads, self.who = reads, who

    def read_state(self, space):
        value = self.reads[self.who]
        if isinstance(value, Exception):
            raise value
        return {"lastReadTime": stamp(value)} if value else {}


class SeenByTests(TestCase):
    def setUp(self):
        cache.clear()
        self.me = User.objects.create_user("kellen", email="kellen@acmigroup.com")
        self.simon = User.objects.create_user("simon", email="simon@acmigroup.com")
        self.dave = User.objects.create_user("dave", email="dave@acmigroup.com")
        for u in (self.me, self.simon, self.dave):
            ChatConnection.objects.create(user=u, enabled=True, google_email=u.email, can_read_state=True)
        self.directory = {
            "users/1": {"name": "Kellen", "email": "kellen@acmigroup.com"},
            "users/2": {"name": "Simon", "email": "simon@acmigroup.com"},
            "users/3": {"name": "Dave", "email": "dave@acmigroup.com"},
            "users/4": {"name": "Will", "email": "will@firehawkaerospace.com"},  # no hub account: never counted
        }
        self.reads = {"simon": SENT + timedelta(minutes=5), "dave": SENT - timedelta(hours=1)}

    def run_seen(self, directory=None):
        with mock.patch.object(service, "members_directory", return_value=directory or self.directory), \
                mock.patch.object(service, "me_of", return_value="kellen@acmigroup.com"), \
                mock.patch.object(service.connect, "is_on", return_value=True), \
                mock.patch.object(service, "client_for", side_effect=lambda u: FakeTeammateClient(self.reads, u.username)):
            return service.seen_by(self.me, mock.Mock(), SPACE, SENT)

    def test_names_those_who_read_it_and_counts_who_it_could_ask(self):
        out = self.run_seen()
        self.assertEqual(out["by"], ["Simon"])  # Dave read an hour before it was sent
        self.assertEqual((out["known"], out["others"]), (2, 3))  # Will has no hub account: not asked, not "unseen"

    def test_a_teammate_whose_chat_fails_is_left_out(self):
        self.reads["dave"] = ChatError("nope")
        out = self.run_seen()
        self.assertEqual((out["by"], out["known"]), (["Simon"], 1))

    def test_nobody_known_means_no_line(self):
        ChatConnection.objects.filter(user__in=[self.simon, self.dave]).update(can_read_state=False)
        out = self.run_seen()
        self.assertEqual((out["by"], out["known"]), ([], 0))

    def test_a_big_space_is_not_asked_about(self):
        big = {f"users/{i}": {"name": f"P{i}", "email": f"p{i}@acmigroup.com"} for i in range(30)}
        big["users/1"] = {"name": "Kellen", "email": "kellen@acmigroup.com"}
        out = self.run_seen(big)
        self.assertEqual((out["by"], out["known"]), ([], 0))

    def test_the_endpoint_needs_a_space_and_a_time(self):
        self.client.force_login(self.me)
        with mock.patch("crm.chat.views.connect.is_on", return_value=True), \
                mock.patch("crm.chat.views.service.client_for", return_value=mock.Mock()):
            r = self.client.get(reverse("crm:chat_seen") + "?s=spaces/AAA")
        self.assertEqual(r.status_code, 404)
