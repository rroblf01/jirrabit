"""Issue a known API key for the demo user, so an agent can be pointed at a
public instance without anyone having to register first.

Opt-in, and off by default: ``JIRRABIT_DEMO_API_KEY`` must be set to the token
you want minted. A predictable credential is a published credential, so this
never invents one.

Why this exists: the demo instance ships with ``alice_pm`` / ``demopass``
(``seed_demo``), so anyone can already log in as the demo user through the web
UI. An agent cannot, because the MCP server authenticates with an API key and
the UI does not hand one out without a logged-in session. This closes that gap
for the demo, and only for the demo.

Read the token from the environment rather than accepting it as an argument, so
it does not end up in a process listing:

    JIRRABIT_DEMO_API_KEY=… python manage.py seed_demo_api_key

The token belongs to a **superuser**, which is the point and also the risk: a
superuser sees every project on the instance. Never point this at anything real.
The repository README publishes a token for a public demo, and that token must
not be valid anywhere else.

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


class Command(BaseCommand):
    help = (
        "Issue a fixed API key for the demo user so an agent can use a public "
        f"instance without registering. Requires {ENV_VAR} to be set."
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
        token = (os.environ.get(ENV_VAR) or "").strip()
        if not token:
            raise CommandError(
                f"{ENV_VAR} is not set. This command will not invent a token: a "
                "predictable credential in a public repository is a published "
                "credential. Set the variable to the token you want, or leave it "
                "unset and have each user create their own key in their profile."
            )
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
