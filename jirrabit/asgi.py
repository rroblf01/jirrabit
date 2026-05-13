"""ASGI config for jirrabit (HTTP + WebSocket)."""
import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "jirrabit.settings")

django_asgi = get_asgi_application()

from realtime.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi,
        "websocket": AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)
