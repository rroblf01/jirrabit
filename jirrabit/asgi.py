"""ASGI config for jirrabit (HTTP + WebSocket)."""

import os
from pathlib import Path

from blacknoise import BlackNoise
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "jirrabit.settings")
BASE_DIR = Path(__file__).resolve().parent.parent
django_asgi = BlackNoise(get_asgi_application())
django_asgi.add(BASE_DIR / "static", "/static")

from realtime.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi,
        "websocket": AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)
