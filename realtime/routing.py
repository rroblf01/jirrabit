from django.urls import re_path

from .consumers import IssuePresenceConsumer, ProjectConsumer

websocket_urlpatterns = [
    re_path(r"^ws/projects/(?P<key>[A-Z0-9]+)/$", ProjectConsumer.as_asgi()),
    re_path(r"^ws/issues/(?P<key>[A-Z0-9-]+)/presence/$", IssuePresenceConsumer.as_asgi()),
]
