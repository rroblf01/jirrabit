"""Issue a known API key for the demo user, so an agent can be pointed at a
public instance without anyone having to register first.

The token defaults to :data:`DEFAULT_DEMO_TOKEN`, a fixed value that is also
printed in the repository README, and ``JIRRABIT_DEMO_API_KEY`` overrides it if
you would rather mint a different one.

Making it the default is a deliberate reversal of the earlier design, which
required the variable and refused to invent anything. The reasoning was that a
predictable credential is a published credential — true, and it is why the
value is in the README rather than generated. But ``seed_demo`` already creates
``alice_pm`` with the password ``demopass`` and makes her a superuser, so any
instance running it is already reachable with a published superuser credential
through the web login, which grants strictly more than an API key does. Guarding
the API key behind an environment variable while leaving that wide open was
incoherent, and in practice it meant the published token did not work until
someone found the variable.

Why this exists: the demo instance ships with ``alice_pm`` / ``demopass``
(``seed_demo``), so anyone can already log in as the demo user through the web
UI. An agent cannot, because the MCP server authenticates with an API key and
the UI does not hand one out without a logged-in session. This closes that gap
for the demo, and only for the demo.

An operator who wants their own token passes it through the environment rather
than as an argument, so it does not end up in a process listing:

    JIRRABIT_DEMO_API_KEY=… python manage.py seed_demo_api_key

The token belongs to a **superuser**, which is the point and also the risk: a
superuser sees every project on the instance. It is only meaningful on a demo,
and ``seed_demo`` is a demo seeder — never point it at an instance that matters.

Idempotent, because ``seed_demo`` may run on every boot: re-running grants the
same key, and un-revokes it if somebody revoked it.

``seed_demo`` calls this itself, at the end, when the variable is set. That is
not redundant: ``seed_demo`` truncates every table, keys included, so a key
minted by any means stops existing the next time it runs. Minting separately
meant the published token worked only until the next daily seed, and then
stopped with nothing logged. The call is still opt-in — unset the variable and
this never runs.
"""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import APIKey

#: The user the token belongs to. Matches seed_demo's demo product manager, who
#: is the superuser there — so the token carries exactly the same access that
#: ``demopass`` does, and nothing more.
DEMO_USERNAME = "alice_pm"

ENV_VAR = "JIRRABIT_DEMO_API_KEY"

#: The token minted when the environment does not name one. It is public: it is
#: in this file, in the README and therefore on the internet. That is acceptable
#: only because it exists to serve a demo instance, and it is checked for length
#: like any other token so a truncated constant fails loudly rather than minting
#: something weak.
DEFAULT_DEMO_TOKEN = "jirrabit-public-demo-token-2026-do-not-use"


class Command(BaseCommand):
    help = (
        "Issue a fixed API key for the demo user so an agent can use a public "
        f"instance without registering. Uses the published demo token unless "
        f"{ENV_VAR} names a different one."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            default=DEMO_USERNAME,
            help="User the key belongs to. Default: %(default)s",
        )
        parser.add_argument(
            "--name",
            default="demo mcp",
            help="Label shown in the API keys list.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Mint the key even if the user is not marked as a demo account.",
        )

    def handle(self, *args, **opts):
        # Synchronous on purpose, and not by preference: Django 6's BaseCommand
        # has no async support at all — execute() calls handle() and writes the
        # result to stdout, so an ``async def handle`` would hand it a coroutine
        # and fail with "'coroutine' object has no attribute 'endswith'". A
        # management command's ORM calls therefore have to be the sync ones.
        # Everything outside a management command in this project is async; see
        # jirrabit/api.py for the pattern.
        token = (os.environ.get(ENV_VAR) or "").strip() or DEFAULT_DEMO_TOKEN
        if len(token) < 16:
            raise CommandError(
                f"{ENV_VAR} is too short to be a usable token (got {len(token)} "
                "characters, want at least 16)."
            )

        from django.contrib.auth import get_user_model

        username = opts["username"]
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(
                f"No user named {username!r}. Run seed_demo first: it creates the "
                "demo accounts and this command only attaches a key to one of them."
            )

        if not user.is_superuser and not opts["force"]:
            raise CommandError(
                f"{username!r} is not a superuser. Minting a key for a "
                "non-superuser is fine, but pass --force if that is what you meant: "
                "the published token is expected to see the whole instance."
            )

        # A with block rather than a decorator: Django's command machinery
        # inspects handle(), and a wrapped function confuses it.
        with transaction.atomic():
            key, _plain = APIKey.create_with_token(owner=user, name=opts["name"], plain=token)

        self.stdout.write(self.style.SUCCESS(f"API key ready for {username} (id {key.pk})."))
        self.stdout.write(
            self.style.WARNING(
                "This token is now public: it is in the repository README. Anyone "
                "who can reach this instance can act as this user. Do not use it "
                "on anything that matters."
            )
        )
