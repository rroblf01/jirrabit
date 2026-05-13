"""WebSocket consumer for live project updates.

A connected client joins the ``project.<key>`` group; whenever an Issue is
saved (see :mod:`realtime.broadcast`), every member of the group receives a
small JSON message. Front-end can swap an HTMX fragment or refresh the
board on receipt.
"""
import json

from channels.generic.websocket import AsyncJsonWebsocketConsumer


class ProjectConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close()
            return
        self.key = self.scope["url_route"]["kwargs"]["key"]
        self.group = f"project.{self.key}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self.send_json({"type": "hello", "project": self.key})

    async def disconnect(self, code):
        if getattr(self, "group", None):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def issue_event(self, event):
        await self.send_json({"type": "issue", **event["payload"]})

    async def comment_event(self, event):
        await self.send_json({"type": "comment", **event["payload"]})
