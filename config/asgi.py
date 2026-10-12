import os

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

django_asgi_app = get_asgi_application()

import assessments.routing  # noqa: E402  (needs the apps loaded first)

# Students never sign in, so the socket is open to anyone holding a session's
# unguessable uuid (see assessments/consumers.py) and needs no auth middleware.
# The origin check is what stops another website opening the socket from a
# visitor's browser: only pages served from ALLOWED_HOSTS may connect.
application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AllowedHostsOriginValidator(
        URLRouter(assessments.routing.websocket_urlpatterns)),
})
