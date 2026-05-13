from django.urls import re_path

from .consumers import ProjectConsumer

websocket_urlpatterns = [
    re_path(r"^ws/projects/(?P<key>[A-Z0-9]+)/$", ProjectConsumer.as_asgi()),
]
