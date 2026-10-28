from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


def pinned_versions():
    """Package name -> pinned version tuple, read from requirements.txt."""
    pins = {}
    for line in (Path(settings.BASE_DIR) / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" not in line:
            continue
        name, version = line.split("==", 1)
        name = name.split("[", 1)[0].strip().lower()
        pins[name] = tuple(int(part) for part in version.strip().split("."))
    return pins


class RequirementsClearAdvisoriesTests(SimpleTestCase):
    # W11: pypdf 6.20.1 and pillow 12.3.0 have published advisories (Audit 9).
    def test_pypdf_and_pillow_are_pinned_past_their_advisories(self):
        pins = pinned_versions()
        self.assertGreaterEqual(pins["pypdf"], (6, 21, 2))
        self.assertGreaterEqual(pins["pillow"], (13, 0, 0))
