"""F120 T008 — error-code hygiene for module 266.

Four properties, and each one has a real failure mode behind it:

* **uniqueness** — two classes sharing a code makes the frontend's switch on
  ``status_code`` pick whichever branch was written first;
* **band** — a code drifting outside 26600-26699 collides with a neighbouring
  module (265 = app_publish, 270 = commercial_license);
* **inheritance** — a code that is not a ``BaseErrorCode`` subclass loses
  ``return_resp`` / ``http_exception`` / ``to_sse_event`` and the endpoint
  raises instead of returning a structured body;
* **no cross-module collision** — module 266 could collide with 261/270 just as
  easily as with itself.

The collision check walks **every** module in ``bisheng.common.errcode``, which
is also what proves the eight F120 numbers were free.
"""

from __future__ import annotations

import importlib
import json
import pkgutil

from bisheng.common.errcode import base

GATEWAY_MODULE = "bisheng.common.errcode.gateway"
BAND_START = 26600
BAND_END = 26699


def _errcode_modules():
    import bisheng.common.errcode as package

    for info in pkgutil.iter_modules(package.__path__):
        if info.name in {"base", "http_error"}:
            continue
        yield f"{package.__name__}.{info.name}"


def _classes(module_name):
    """Error-code classes *defined* in ``module_name`` that carry a Code.

    Abstract bases are skipped: ``model_face.ModelFaceError`` exists only to be
    subclassed and has no ``Code`` of its own.
    """
    module = importlib.import_module(module_name)
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, type) and issubclass(obj, base.BaseErrorCode) and obj is not base.BaseErrorCode:
            if obj.__module__ == module_name and hasattr(obj, "Code"):
                yield obj


def test_gateway_codes_are_unique_within_the_module():
    seen = {}
    for cls in _classes(GATEWAY_MODULE):
        assert cls.Code not in seen, f"{cls.__name__} and {seen[cls.Code]} both use {cls.Code}"
        seen[cls.Code] = cls.__name__
    assert len(seen) >= 8, f"expected at least the 8 documented codes, found {len(seen)}"


def test_gateway_codes_stay_inside_the_266_band():
    for cls in _classes(GATEWAY_MODULE):
        assert BAND_START <= cls.Code <= BAND_END, f"{cls.__name__} uses {cls.Code}, outside {BAND_START}-{BAND_END}"


def test_gateway_codes_do_not_collide_with_any_other_module():
    owners = {}
    for module_name in _errcode_modules():
        for cls in _classes(module_name):
            owners.setdefault(cls.Code, set()).add(module_name)
    gateway_codes = {cls.Code for cls in _classes(GATEWAY_MODULE)}
    assert gateway_codes, "no gateway codes found"
    for code in sorted(gateway_codes):
        others = owners.get(code, set()) - {GATEWAY_MODULE}
        assert not others, f"code {code} is also defined in {sorted(others)}"


def test_gateway_codes_inherit_base_error_code():
    for cls in _classes(GATEWAY_MODULE):
        assert issubclass(cls, base.BaseErrorCode)
        for method in ("return_resp", "http_exception", "to_sse_event"):
            assert callable(getattr(cls, method)), f"{cls.__name__} lost {method}"


def test_documented_codes_exist_with_the_documented_meaning():
    """The eight codes named in tasks.md T008. A rename or a re-numbering is a
    contract break for the frontend, so the numbers are pinned here."""
    expected = {
        26601: "GatewayRateLimitError",
        26602: "GatewayOnlineLimitError",
        26603: "GatewayOAuthAuthorizeError",
        26604: "GatewayOAuthStateError",
        26605: "GatewayOAuthProviderError",
        26606: "GatewayOAuthTokenRefreshError",
        26607: "GatewayLdapBindError",
        26608: "GatewayConfigInvalidError",
    }
    module = importlib.import_module(GATEWAY_MODULE)
    for code, class_name in expected.items():
        cls = getattr(module, class_name, None)
        assert cls is not None, f"{class_name} missing from {GATEWAY_MODULE}"
        assert cls.Code == code, f"{class_name} is {cls.Code}, expected {code}"
        assert isinstance(cls.Msg, str) and cls.Msg, f"{class_name} has no Msg"


def test_structured_helpers_still_work_for_a_gateway_code():
    """Spot-check the contract the endpoints rely on: a WS error event carries
    the code and the message, and does not raise."""
    module = importlib.import_module(GATEWAY_MODULE)
    event = module.GatewayRateLimitError.to_sse_event()
    assert event["event"] == "error"
    payload = json.loads(event["data"])
    assert payload["status_code"] == 26601
    assert payload["status_message"] == "请求过于频繁，请稍后重试"
