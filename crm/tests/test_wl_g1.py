"""Worklist 2026-10-28, W1 and W4: "Not a deal" takes a POST only, and the pipeline table hides closed deals."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.models import Company, DealSuggestionDismissal, Opportunity, OpportunityStage

User = get_user_model()


class NotADealNeedsPostTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", first_name="Kellen")
        self.client.force_login(self.user)
        self.company = Company.objects.create(name="Firehawk Aerospace")

    def test_a_plain_link_does_not_hide_the_suggestion(self):
        r = self.client.get(reverse("crm:suggestion_dismiss", args=[self.company.pk]))
        self.assertEqual(r.status_code, 405)
        self.assertFalse(DealSuggestionDismissal.objects.exists())

    def test_the_button_still_works(self):
        self.client.post(reverse("crm:suggestion_dismiss", args=[self.company.pk]))
        self.assertTrue(DealSuggestionDismissal.objects.filter(company=self.company).exists())


class PipelineTableClosedDealsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", first_name="Kellen")
        self.client.force_login(self.user)
        for name, stage in [("Lost Co", OpportunityStage.LOST), ("Held Co", OpportunityStage.ON_HOLD),
                            ("Signed Co", OpportunityStage.SIGNED), ("Lease Co", OpportunityStage.LEASE_NEGOTIATION),
                            ("Prospect Co", OpportunityStage.PROSPECT)]:
            Opportunity.objects.create(name=name, company=Company.objects.create(name=name), stage=stage)
        self.url = reverse("crm:opportunity_board") + "?view=table"

    def test_table_hides_on_hold_and_lost_and_puts_open_deals_first(self):
        r = self.client.get(self.url)
        stages = [o.stage for o in r.context["deals"]]
        self.assertEqual(stages, [OpportunityStage.LEASE_NEGOTIATION, OpportunityStage.PROSPECT, OpportunityStage.SIGNED])
        self.assertContains(r, "Show closed (2)")
        self.assertNotContains(r, "Lost Co")

    def test_show_closed_lists_every_deal_with_closed_ones_last(self):
        r = self.client.get(self.url + "&closed=1")
        stages = [o.stage for o in r.context["deals"]]
        self.assertEqual(len(stages), 5)
        self.assertEqual(stages[0], OpportunityStage.LEASE_NEGOTIATION)
        self.assertEqual(stages[2], OpportunityStage.SIGNED)
        self.assertEqual(set(stages[-2:]), {OpportunityStage.LOST, OpportunityStage.ON_HOLD})
        self.assertContains(r, "Hide closed")

    def test_picking_the_lost_stage_still_shows_lost_deals(self):
        r = self.client.get(self.url + "&stage=lost")
        self.assertEqual([o.name for o in r.context["deals"]], ["Lost Co"])
