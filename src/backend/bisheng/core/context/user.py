"""Request-scoped identity context (F120 T004).

The gateway's ASGI middleware runs *outside* route handlers, so it cannot read
``request.user`` — that object does not exist yet at middleware depth. Re-decoding
the JWT there would duplicate ``CustomMiddleware``'s work and, worse, could
disagree with it (two decoders of one token is how "the middleware thinks I am
user A while the route thinks I am user B" bugs are born).

So ``CustomMiddleware`` publishes the dict it already decoded into this
ContextVar, and the gateway reads it. Same source, same bytes, no second decode.

Contract:

* :func:`set_current_subject` / :func:`reset_current_subject` are the only
  writers. Callers must reset in a ``finally`` — the HTTP middleware, the
  WebSocket middleware and Celery task hooks each own their own token.
* Default is ``None``: an unauthenticated or tenant-exempt request has *no*
  subject, and gateway code must treat ``None`` as "anonymous", never as
  "the previous request's user".
* The value is the decoded JWT payload (``user_id`` / ``user_name`` /
  ``tenant_id`` / ...), not a ``UserPayload``. It is deliberately the raw dict
  the middleware produced, so anything that mutates it would be visible to
  every reader — treat it as read-only.
"""

from contextvars import ContextVar

#: Decoded JWT payload for the current request, or ``None`` when anonymous.
current_subject: ContextVar[dict | None] = ContextVar("current_subject", default=None)


def set_current_subject(subject: dict | None):
    """Publish the decoded subject for this request; returns the reset token."""
    return current_subject.set(subject)


def reset_current_subject(token) -> None:
    """Undo :func:`set_current_subject`. Always called from a ``finally``."""
    current_subject.reset(token)


def get_current_subject() -> dict | None:
    """The current request's decoded subject, or ``None`` when anonymous."""
    return current_subject.get()


def get_current_user_id() -> int | None:
    """Convenience for the hot path (rate limiting, block records, WS counting)."""
    subject = current_subject.get()
    if not subject:
        return None
    user_id = subject.get("user_id")
    return int(user_id) if isinstance(user_id, (int, str)) and str(user_id).isdigit() else None
