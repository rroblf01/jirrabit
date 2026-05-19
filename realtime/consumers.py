"""WebSocket consumer for live project updates.

A connected client joins the ``project.<key>`` group; whenever an Issue is
saved (see :mod:`realtime.broadcast`), every member of the group receives a
small JSON message. Front-end can swap an HTMX fragment or refresh the
board on receipt.
"""

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

    async def reaction_event(self, event):
        await self.send_json({"type": "reaction", **event["payload"]})


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """Per-user channel for the topbar bell badge.

    Each authenticated client joins ``user.<pk>`` on connect and gets a
    ``{"type": "unread", "count": N}`` message every time the unread
    count changes (creation, mark-as-read). Replaces the old 30s
    polling so the badge updates instantly.
    """

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close()
            return
        self.user = user
        self.group = f"user.{user.pk}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        # Push the current count immediately so the badge syncs on every
        # reconnect (refresh, tab regain focus, etc.).
        from accounts.models import Notification
        count = await Notification.objects.filter(recipient=user, read=False).acount()
        await self.send_json({"type": "unread", "count": count})

    async def disconnect(self, code):
        if getattr(self, "group", None):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def notif_unread(self, event):
        await self.send_json({"type": "unread", "count": event["count"]})


class IssuePresenceConsumer(AsyncJsonWebsocketConsumer):
    """Live presence for an issue detail page.

    Each viewer joins the ``issue.<key>`` group on connect and announces
    itself; everyone else in the group receives a ``join`` event. On
    disconnect the consumer sends a ``leave`` so peers can drop the avatar.
    A ``ping`` mechanism lets a newcomer ask the room to re-announce so it
    can paint the existing viewers without polling.
    """

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close()
            return
        self.user = user
        self.key = self.scope["url_route"]["kwargs"]["key"]
        self.group = f"issue.{self.key}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self.channel_layer.group_send(self.group, {
            "type": "presence.join",
            "username": user.username,
            "display": str(user),
        })
        await self.channel_layer.group_send(self.group, {
            "type": "presence.ping",
            "from_channel": self.channel_name,
        })

    async def disconnect(self, code):
        if getattr(self, "group", None):
            await self.channel_layer.group_send(self.group, {
                "type": "presence.leave",
                "username": self.user.username,
            })
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def presence_join(self, event):
        await self.send_json({
            "type": "join",
            "username": event["username"],
            "display": event.get("display", event["username"]),
        })

    async def presence_leave(self, event):
        await self.send_json({"type": "leave", "username": event["username"]})

    async def presence_ping(self, event):
        if event.get("from_channel") == self.channel_name:
            return
        await self.channel_layer.send(event["from_channel"], {
            "type": "presence.join",
            "username": self.user.username,
            "display": str(self.user),
        })
