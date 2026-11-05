from .services import is_released

def service_for_path(path):
    """The released service an address belongs to, or None.

    Taken from the first segment of the path and nothing else, so it cannot be set by
    anything a person sends; and only for a service that is open, so a switched-off
    service has no scope at all."""
    # Exactly one leading slash, then the first segment: '//classroom/' has an empty first segment.
    first = path.split('/', 2)[1] if path.startswith('/') else ''
    return first if is_released(first) else None


class ServiceMiddleware:
    """Sets ``request.service`` on every request. Queries that depend on it fail
    closed when it is None (accounts/scoping.py)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.service = service_for_path(request.path_info)
        # Team sessions use the shared assessment views, but all their owner
        # URLs must remain below the Team service boundary. A path-selected
        # URLconf keeps reverse() and template URL tags on /team/app/ while
        # the views continue to scope records using request.service.
        first = request.path_info.split('/', 2)[1] if request.path_info.startswith('/') else ''
        if first == 'team':
            request.urlconf = 'config.team_urls'
        return self.get_response(request)
