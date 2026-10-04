import os

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

django_asgi_app = get_asgi_application()

import assessments.routing  # noqa: E402  (needs the apps loaded first)

# Students never sign in, so the socket is open to anyone holding a session's
# unguessable uuid (see assessments/consumers.py) and needs no auth middleware.
application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': URLRouter(assessments.routing.websocket_urlpatterns),
})
