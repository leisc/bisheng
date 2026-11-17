"""F120 T009 — ``GatewaySettings`` contract.

The defaults are the contract, not convenience values:

* ``license_faces`` must be **exactly** the four commercial face prefixes. A
  fifth prefix silently drags an open-source face behind the commercial license
  gate; a missing one leaves a commercial face ungated. Both are invisible
  until someone deploys without a license.
* ``/api/v1/dsh/**`` and the authorization-status query must NOT be in it —
  DSH chat is not a commercial face, and the status query has to keep answering
  after the license expires, otherwise the UI cannot explain *why* it is locked
  (AC-35).
* ``license_codes`` defaults to ``[etl, gateway]`` because a self-hosted
  deployment only ever has an ETL row; during the dual-run period the Java
  gateway injects a ``gateway`` row, and either one being valid opens the faces
  (design §6.2, decision 6).
* strict mode rejects unknown keys — a typo like ``lisence_faces`` would
  otherwise look like "the gate is off" instead of a config error.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from bisheng.core.config.settings import Settings

MODULE = "bisheng.core.config.gateway"

LICENSE_FACES = (
    "/api/v1/gateway/oauth2/",
    "/api/v1/gateway/sensitive/",
    "/api/v1/gateway/rate-rules",
    "/api/v1/gateway/block-records",
)

FORBIDDEN_FACES = ("/api/v1/dsh/", "/api/v1/gateway/license/", "/api/v1/gateway/license")


def _settings(**overrides):
    module = importlib.import_module(MODULE)
    return module.GatewaySettings(**overrides)


def test_settings_are_importable():
    assert importlib.import_module(MODULE) is not None


def test_defaults_are_all_off():
    """A fresh deployment must not grow a gateway it did not ask for, and must
    not start throttling traffic it was never asked to police."""
    settings = _settings()
    assert settings.enabled is False
    assert settings.rate_limit.enabled is False
    assert settings.online.enabled is False


def test_default_license_faces_are_exactly_the_four_commercial_prefixes():
    assert list(_settings().license_faces) == list(LICENSE_FACES)


def test_license_faces_exclude_dsh_and_the_status_query():
    """AC-35: the status query must answer after expiry, or the banner cannot
    explain the lock. DSH chat is not a commercial face."""
    faces = _settings().license_faces
    for forbidden in FORBIDDEN_FACES:
        assert forbidden not in faces, f"{forbidden} must not sit behind the license gate"


def test_default_license_codes_are_etl_and_gateway():
    assert list(_settings().license_codes) == ["etl", "gateway"]


def test_rate_limit_defaults():
    rate_limit = _settings().rate_limit
    assert rate_limit.default_qps == 10
    assert rate_limit.enabled is False


def test_filter_url_defaults_to_empty():
    """Empty = rate limiting is not bound to any path, so a fresh deployment
    never throttles anything it did not opt into."""
    assert list(_settings().filter_url) == []


def test_settings_are_frozen():
    """Frozen so a request handler cannot flip the gate at runtime — the whole
    point of fail-closed is that the decision is made at startup."""
    settings = _settings()
    with pytest.raises(ValidationError):
        settings.enabled = True


def test_strict_mode_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        _settings(lisence_faces=["/api/v1/gateway/oauth2/"])


def test_license_faces_accepts_a_tighter_set_for_gray_release():
    """The set is configurable so a face can be gated before it ships."""
    settings = _settings(license_faces=["/api/v1/gateway/sensitive/"])
    assert list(settings.license_faces) == ["/api/v1/gateway/sensitive/"]


def test_enabled_gateway_requires_no_extra_fields():
    """Turning the gateway on must not require an OAuth client secret etc. —
    those are per-tenant rows in ``gateway_oauth_provider``, not global config."""
    settings = _settings(enabled=True)
    assert settings.enabled is True


def test_settings_mount_on_the_global_settings_object():
    settings = Settings()
    assert settings.gateway.enabled is False
    assert list(settings.gateway.license_faces) == list(LICENSE_FACES)


def test_config_yaml_templates_document_the_gateway_section():
    """Every **tracked** config template must show the section commented out, so an
    operator learns the switch exists without it being on by default.

    The local dev copy ``src/backend/bisheng/config.yaml`` is deliberately NOT
    used here: it is in ``.gitignore``, so a fresh clone would fail this test on
    a file nobody commits."""
    repo = Path(__file__).resolve().parents[4]
    templates = [
        repo / "docker" / "bisheng" / "config" / "config.yaml",
        repo / "docker" / "helm" / "bisheng" / "files" / "config.yaml",
    ]
    for path in templates:
        assert path.exists(), f"missing tracked config template {path}"
        text = path.read_text()
        assert "# gateway:" in text, f"{path} must document the gateway section"
        assert "\ngateway:" not in text, f"{path} must ship gateway disabled"


def test_local_dev_config_yaml_stays_git_ignored():
    """Guard the assumption in the test above: the dev copy stays ignored, so its
    content must never become load-bearing."""
    import subprocess

    repo = Path(__file__).resolve().parents[4]
    local = repo / "src" / "backend" / "bisheng" / "config.yaml"
    if not local.exists():
        return
    result = subprocess.run(["git", "check-ignore", str(local)], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, f"{local} is no longer ignored; T009 must not depend on it"
