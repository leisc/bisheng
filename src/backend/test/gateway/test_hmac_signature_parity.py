"""T001 — HMAC parity between the canonical implementation and its two copies.

Why this file exists: three call sites sign ``METHOD\\nPATH\\nBODY`` with
HMAC-SHA256 — ``common/utils/hmac_signature.py`` (canonical),
``app_runtime/domain/services/hmac_auth.py`` (verifies inbound app-proxy calls)
and ``app_runtime/domain/services/orchestrator_client.py`` (signs outbound
manager calls). T002 collapses the two copies onto the canonical one; this test
is the net that makes the collapse provably behaviour-preserving.

Byte-for-byte equality is the assertion, not "both produce *a* signature": the
Java gateway and the runtime-manager process are separate binaries that
implement the same formula, so a single differing byte silently breaks
app-proxy → backend authentication.
"""

from __future__ import annotations

import pytest

from bisheng.common.utils.hmac_signature import compute_hmac_signature

SECRET = "parity-secret"

# (method, path, body) — the shapes that actually occur: empty body (GET/DELETE),
# JSON body, and a body whose bytes are non-UTF-8-safe raw payload.
CASES = [
    ("GET", "/api/v1/internal/app-proxy/authorize", b""),
    ("POST", "/api/v1/internal/app-proxy/authorize", b'{"user_id": 1}'),
    ("POST", "/api/v1/app/runtime/material", b"\x00\x01\xff\xfe binary \x80"),
    ("delete", "/api/v1/app/123", b""),
    ("PATCH", "/api/v1/app/123/state", b'{"state":"stopped"}'),
]


@pytest.mark.parametrize(("method", "path", "body"), CASES)
def test_hmac_auth_compute_signature_matches_canonical(method: str, path: str, body: bytes) -> None:
    from bisheng.app_runtime.domain.services.hmac_auth import compute_signature as auth_compute

    assert auth_compute(method, path, body, SECRET) == compute_hmac_signature(method, path, body, SECRET)


@pytest.mark.parametrize(("method", "path", "body"), CASES)
def test_orchestrator_client_compute_signature_matches_canonical(method: str, path: str, body: bytes) -> None:
    from bisheng.app_runtime.domain.services.orchestrator_client import compute_signature as client_compute

    assert client_compute(method, path, body, SECRET) == compute_hmac_signature(method, path, body, SECRET)


def test_canonical_uppercases_method_and_ignores_body_none() -> None:
    # ``raw_body=None`` must behave like an empty body, not raise.
    assert compute_hmac_signature("post", "/p", None, SECRET) == compute_hmac_signature("POST", "/p", b"", SECRET)


def test_signature_header_name_is_identical_across_call_sites() -> None:
    from bisheng.app_runtime.domain.services.hmac_auth import SIGNATURE_HEADER
    from bisheng.app_runtime.domain.services.orchestrator_client import OrchestratorClient

    assert SIGNATURE_HEADER == "X-Signature"
    # The client takes the header as a constructor default — assert it is the same name.
    assert OrchestratorClient()._signature_header == "X-Signature"


def test_canonical_digest_is_stable_hex_sha256() -> None:
    import hashlib
    import hmac as hmac_module

    expected = hmac_module.new(SECRET.encode(), b"GET\n/p\n", hashlib.sha256).hexdigest()
    assert compute_hmac_signature("GET", "/p", b"", SECRET) == expected
