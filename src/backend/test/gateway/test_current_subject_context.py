"""T003 — ``current_subject`` ContextVar behaviour, proven on the real middleware.

Why this file exists: the native gateway (F120) needs the request's decoded
identity inside its ASGI middleware, which runs *outside* the route handlers.
Reading the cookie and re-decoding the JWT there would duplicate
``CustomMiddleware``'s work and could disagree with it; a ContextVar filled by
the existing middleware is the single source of truth.

The three properties under test:

1. after ``CustomMiddleware`` runs, ``current_subject`` is readable and
   *identical* to what the route itself sees (no second decode, no drift);
2. unauthenticated / exempt paths yield ``None`` rather than a stale subject;
3. the var is reset when the request ends, so nothing leaks into the next
   request on the same worker (a ``set()`` without a matching ``reset()`` is
   how leakage starts).
"""

from __future__ import annotations

import pytest

from bisheng.core.context.user import current_subject, get_current_subject

AUTH_PATH = "/api/v1/assistant/list"
EXEMPT_PATH = "/api/v1/user/login"


@pytest.fixture(autouse=True)
def _pin_jwt_secret(monkeypatch):
    """Settings ships no default secret; pin one so AuthJwt does not reach the
    config table (same pattern as test/tenant/test_tenant_auth.py)."""
    from bisheng.common.services import config_service as cs

    monkeypatch.setattr(cs.settings, "jwt_secret", "f120-test-secret", raising=False)


@pytest.fixture()
def subject_app():
    """Minimal ASGI app: routes that report ``current_subject``.

    Built on the real ``CustomMiddleware`` so the assertion covers the real
    wiring, not a re-implementation of it.
    """
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from starlette.testclient import TestClient

    from bisheng.utils.http_middleware import CustomMiddleware

    app = FastAPI()
    app.add_middleware(CustomMiddleware)

    @app.get(AUTH_PATH)
    async def _route():
        return JSONResponse({"subject": get_current_subject()})

    @app.get(EXEMPT_PATH)
    async def _exempt():
        return JSONResponse({"subject": get_current_subject()})

    return TestClient(app)


def _make_token(*, user_id: int = 7, user_name: str = "alice", tenant_id: int = 1) -> str:
    from bisheng.user.domain.services.auth import AuthJwt

    return AuthJwt().create_access_token(
        subject={"user_id": user_id, "user_name": user_name, "tenant_id": tenant_id}
    )


def test_subject_is_readable_inside_route(subject_app):
    resp = subject_app.get(AUTH_PATH, cookies={"access_token_cookie": _make_token()})
    assert resp.status_code == 200, resp.text
    subject = resp.json()["subject"]
    assert subject is not None, "CustomMiddleware ran but current_subject stayed None"
    assert subject["user_id"] == 7
    assert subject["user_name"] == "alice"


def test_subject_matches_the_middlewares_own_decode(subject_app):
    """The ContextVar must carry the *same* dict the middleware decoded — not a
    re-decode that could drift if the JWT helper changes."""
    from bisheng.utils.http_middleware import _decode_jwt_subject

    token = _make_token(user_id=42)
    resp = subject_app.get(AUTH_PATH, cookies={"access_token_cookie": token})
    assert resp.json()["subject"] == _decode_jwt_subject(token)


def test_subject_is_none_without_token(subject_app):
    resp = subject_app.get(AUTH_PATH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] is None


def test_subject_is_none_on_exempt_path_even_with_token(subject_app):
    """Login / SSO callbacks are exempt — they must not publish a subject."""
    resp = subject_app.get(EXEMPT_PATH, cookies={"access_token_cookie": _make_token()})
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] is None


def test_subject_is_reset_after_request_ends(subject_app):
    subject_app.get(AUTH_PATH, cookies={"access_token_cookie": _make_token()})
    # A later read must not see the earlier request's subject.
    assert current_subject.get() is None


def test_current_subject_helper_returns_none_by_default():
    assert get_current_subject() is None
    assert current_subject.get() is None
