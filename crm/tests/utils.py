"""Shared test helpers."""
from django.contrib.auth.models import Group

from crm.permissions import MANAGERS


def make_manager(user):
    """Put a test user in the Managers group (sending, deleting, merging, client links)."""
    user.groups.add(Group.objects.get_or_create(name=MANAGERS)[0])
    return user
