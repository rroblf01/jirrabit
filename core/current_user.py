"""The user behind the current request, readable from inside a signal.

``post_save`` has no idea who triggered the write, but the realtime broadcast
needs to know: the browser that made a change must not be told to refresh, or
the person who just moved a card gets a "there are changes to refresh" banner
for a board they already updated themselves.

Passing it down through every call site would mean threading ``request.user``
into ``Issue.save()``, which is not a thing a model can see. A context variable
is, because asgiref propagates the context into the worker thread that
``asave()`` runs the write on — verified, including through a real ``post_save``.

The value is best-effort and its absence is not an error: a management command,
a shell, or a background task has no request, and the broadcast then simply
carries no actor and every client reacts to it.

Read it with :func:`actor_id` rather than reaching for ``_actor`` directly; the
value is reset after every request either way.
"""

import contextvars

#: The primary key of the user whose request is in flight, or None.
current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "jirrabit_current_user_id", default=None
)


def set_current_user(user) -> contextvars.Token:
    """Record who is acting, and hand back the token that undoes it."""
    user_id = getattr(user, "pk", None) if user is not None else None
    return current_user_id.set(user_id)


def reset_current_user(token) -> None:
    try:
        current_user_id.reset(token)
    except ValueError:
        # The context this token was minted in is gone, which happens when a
        # response is streamed across contexts. Nothing to undo then.
        pass


def actor_id() -> int | None:
    """The acting user's pk, or None when there is no request behind the write."""
    return current_user_id.get()
