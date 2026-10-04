"""Helpers for tests: a lecturer signed in through DevPerf, without DevPerf.

Organizations are DevPerf's, so a test organization is just the id and name
DevPerf's token would carry. ``sign_in`` puts the lecturer in the same state a
real sign-in leaves them in: logged in, with the organizations they may run
LiveGrade for in the session.
"""
import itertools

from .access import ACTIVE_KEY, ORGS_KEY
from .models import User

_org_ids = itertools.count(1)


class TestOrganization:
    """What a test needs of an organization: an id and a name."""

    __test__ = False

    def __init__(self, name, slug=''):
        self.pk = self.id = next(_org_ids)
        self.name = name
        self.slug = slug or name.lower().replace(' ', '-')

    def as_claim(self):
        return {'id': self.pk, 'slug': self.slug, 'name': self.name}


def make_org(name, slug=''):
    return TestOrganization(name, slug)


def make_user(username, **fields):
    return User.objects.create(sub=fields.pop('sub', username), username=username, **fields)


def sign_in(client, user, *organizations, active=None):
    """Log ``user`` in as DevPerf would send them: allowed in ``organizations``,
    working in ``active`` (the first by default)."""
    client.force_login(user)
    session = client.session
    session[ORGS_KEY] = [org.as_claim() for org in organizations]
    if active is not None:
        session[ACTIVE_KEY] = active.pk
    session.save()
    return user
