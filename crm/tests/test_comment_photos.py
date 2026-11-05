"""A picture on a comment (crm/comment_photos.py, views.comment_add / comment_photo), and people's phone numbers on the
company card, the company page's People tab and its People widget (the phone_numbers filter).

Kellen, 2026-11-04: "I thought I added the ability to upload photos to comments on company cards? also within the
company cards, people needs to add phone numbers if we have them." The photo work had been committed on the
photo-comments branch on 2026-11-03 and never merged, so it was not live.
"""
import io
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image

from crm import comment_photos
from crm.models import Comment, Company, Opportunity, Person
from crm.templatetags.crm_tags import phone_numbers

User = get_user_model()


def picture(size=(40, 30), fmt="JPEG", **save):
    out = io.BytesIO()
    Image.new("RGB", size, (200, 60, 40)).save(out, fmt, **save)
    return out.getvalue()


def upload(data, name="site visit.JPG", kind="image/jpeg"):
    return SimpleUploadedFile(name, data, content_type=kind)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class CommentPhotoTests(TestCase):
    def setUp(self):
        self.kellen = User.objects.create_user("kellen", password="x", first_name="Kellen")
        self.client.force_login(self.kellen)
        self.co = Company.objects.create(name="Acme Energetics")
        self.deal = Opportunity.objects.create(name="Acme · MEIH", company=self.co)

    def post(self, **extra):
        data = {"company": self.co.pk, "body": "Site walk with Ann", "next": self.co.get_absolute_url()}
        data.update(extra)
        return self.client.post("/comments/add/", data)

    def test_a_picture_is_stored_under_our_own_name_and_served_to_signed_in_users(self):
        data = picture()
        self.post(photo=upload(data))
        c = Comment.objects.get()
        self.assertTrue(c.photo.name.startswith("comments/") and c.photo.name.endswith(".jpg"), c.photo.name)
        self.assertNotIn("site visit", c.photo.name)  # never the uploaded name
        r = self.client.get(c.photo_url())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/jpeg")
        self.assertTrue(r["Content-Disposition"].startswith("inline"))
        self.assertIn("private", r["Cache-Control"])
        self.assertEqual(b"".join(r.streaming_content), data)  # small, so stored exactly as sent
        self.client.logout()
        self.assertEqual(self.client.get(c.photo_url()).status_code, 302)  # sign-in required, like documents

    def test_the_picture_shows_in_the_feed_the_deal_comments_and_both_cards(self):
        self.post(photo=upload(picture()))
        self.post(opportunity=self.deal.pk, company="", body="Photo of the pad", photo=upload(picture(), "pad.jpg"))
        first, second = Comment.objects.order_by("pk")
        self.assertContains(self.client.get(self.co.get_absolute_url()), first.photo_url())  # the feed
        self.assertContains(self.client.get(self.deal.get_absolute_url()), second.photo_url())  # the comments panel
        card = self.client.get(f"/companies/{self.co.pk}/card/")
        self.assertContains(card, first.photo_url())
        self.assertContains(card, 'class="comment-photo"')
        self.assertContains(self.client.get(f"/opportunities/{self.deal.pk}/card/"), second.photo_url())

    def test_both_comment_forms_can_send_a_file(self):
        for url in (self.co.get_absolute_url(), self.deal.get_absolute_url()):
            r = self.client.get(url)
            self.assertContains(r, 'enctype="multipart/form-data"')
            self.assertContains(r, 'name="photo" accept="image/*"')

    def test_a_comment_without_a_picture_posts_as_before(self):
        self.post()
        c = Comment.objects.get()
        self.assertFalse(c.photo)
        self.assertEqual(c.photo_url(), "")
        self.assertNotContains(self.client.get(self.co.get_absolute_url()), 'class="comment-photo"')

    def test_a_file_that_is_not_a_picture_posts_nothing(self):
        html = upload(b"<script>alert(1)</script>", "evil.png", "image/png")  # the type the browser claims means nothing
        r = self.post(photo=html)
        self.assertEqual(Comment.objects.count(), 0)
        self.assertContains(self.client.get(r["Location"]), "Nothing was posted")
        self.post(photo=upload(picture()[:60], "cut.jpg"))  # a picture cut off in the middle
        self.assertEqual(Comment.objects.count(), 0)

    def test_a_picture_over_the_size_limit_posts_nothing(self):
        with mock.patch.object(comment_photos, "MAX_BYTES", 100):
            r = self.post(photo=upload(picture((300, 300))))
        self.assertEqual(Comment.objects.count(), 0)
        self.assertContains(self.client.get(r["Location"]), "the limit is")

    def test_deleting_the_comment_deletes_its_picture(self):
        self.post(photo=upload(picture()))
        c = Comment.objects.get()
        name = c.photo.name
        self.assertTrue(default_storage.exists(name))
        self.client.post(f"/comments/{c.pk}/delete/", {"next": "/"})
        self.assertFalse(Comment.objects.exists())
        self.assertFalse(default_storage.exists(name))

    def test_a_missing_file_is_a_404_not_a_crash(self):
        self.post(photo=upload(picture()))
        c = Comment.objects.get()
        default_storage.delete(c.photo.name)
        self.assertEqual(self.client.get(c.photo_url()).status_code, 404)
        self.assertEqual(self.client.get("/comments/99999/photo/").status_code, 404)


class PreparePhotoTests(SimpleTestCase):
    def test_a_small_picture_is_kept_exactly_as_sent_with_an_extension_that_matches_what_it_is(self):
        data = picture(fmt="PNG")
        f = comment_photos.prepare(upload(data, "looks-like.jpg", "image/jpeg"))
        self.assertTrue(f.name.endswith(".png"))
        self.assertEqual(f.read(), data)

    def test_a_big_photo_is_scaled_down_and_its_rotation_is_baked_in(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # "rotate 90 clockwise to view": a phone held upright. Stored 3000 wide, shown 2000 wide.
        data = picture((3000, 2000), exif=exif)
        f = comment_photos.prepare(upload(data))
        img = Image.open(io.BytesIO(f.read()))
        self.assertEqual(img.size, (1600, 2400))  # portrait, longest side 2400
        self.assertLess(len(data), 10 * 1024 * 1024)
        self.assertTrue(f.name.endswith(".jpg"))

    def test_an_animated_gif_is_never_flattened(self):
        frames = [Image.new("P", (40, 30), i) for i in (1, 2)]
        out = io.BytesIO()
        frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
        f = comment_photos.prepare(upload(out.getvalue(), "spin.gif", "image/gif"))
        self.assertEqual(f.read(), out.getvalue())
        self.assertTrue(f.name.endswith(".gif"))

    def test_text_svg_and_pictures_with_too_many_pixels_are_refused(self):
        for data in (b"hello", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', b""):
            with self.assertRaises(comment_photos.BadPhoto):
                comment_photos.prepare(upload(data, "x.png", "image/png"))
        with mock.patch.object(comment_photos, "MAX_PIXELS", 100), self.assertRaisesRegex(comment_photos.BadPhoto, "pixels"):
            comment_photos.prepare(upload(picture((40, 30))))


class PhoneNumbersFilterTests(SimpleTestCase):
    def test_a_label_and_an_extension_show_but_are_not_dialled(self):
        rows = phone_numbers(["301-555-0100 x204 (office)", "(301) 555-0142 (mobile)", "+1 215.555.0199", "Ext. 12"])
        self.assertEqual(rows[0], {"text": "301-555-0100 x204", "label": "office", "href": "tel:3015550100"})
        self.assertEqual(rows[1], {"text": "(301) 555-0142", "label": "mobile", "href": "tel:3015550142"})
        self.assertEqual(rows[2], {"text": "+1 215.555.0199", "label": "", "href": "tel:+12155550199"})
        self.assertEqual(rows[3]["href"], "")  # nothing to dial

    def test_nothing_in_nothing_out(self):
        self.assertEqual(phone_numbers([]), [])
        self.assertEqual(phone_numbers(None), [])
        self.assertEqual(phone_numbers(["", "  ", " (office)"]), [])


class PhonesOnTheCompanyPageTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("kellen", password="x"))
        self.co = Company.objects.create(name="Acme Energetics")
        self.ann = Person.objects.create(first_name="Ann", last_name="Smith", company=self.co,
                                         phones=["256-555-0100 x12 (office)", "256-555-0142 (mobile)"])
        self.bob = Person.objects.create(first_name="Bob", last_name="Jones", company=self.co)

    def test_the_company_card_lists_each_persons_numbers_as_tap_to_call_links(self):
        r = self.client.get(f"/companies/{self.co.pk}/card/")
        self.assertContains(r, '<a href="tel:2565550100">256-555-0100 x12</a> <span class="phone-label">office</span>', html=False)
        self.assertContains(r, '<a href="tel:2565550142">256-555-0142</a> <span class="phone-label">mobile</span>', html=False)
        self.assertContains(r, f'href="{self.ann.get_absolute_url()}"')  # the name still opens their card
        self.assertContains(r, f'href="{self.bob.get_absolute_url()}"')
        self.assertEqual(r.content.decode().count("pk-phones"), 1)  # Bob has none, so no empty line under him

    def test_the_people_tab_and_the_people_widget_show_them_too(self):
        tab = self.client.get(self.co.get_absolute_url() + "?tab=people")
        self.assertContains(tab, "<th>Phone</th>", html=False)
        self.assertContains(tab, 'href="tel:2565550142"')
        overview = self.client.get(self.co.get_absolute_url())
        self.assertContains(overview, 'href="tel:2565550100"')
