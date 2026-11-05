from .services import SLUGS


class ServiceSlugConverter:
    """Matches only the slugs in the service registry, so a service entry address can
    never shadow another route and an unknown address is simply not a route."""

    regex = '|'.join(SLUGS)

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value
