"""Native-gateway settings (F120 T010, design §5.9).

``frozen + strict + extra='forbid'`` copied from ``core/config/dsh.py`` for one
reason: a typo in ``license_faces`` must be a startup failure, not a silently
ungated commercial face. ``extra='forbid'`` turns that typo into a
``ValidationError`` during ``Settings()`` construction, which is the only place
an operator will look.

Everything ships **off**. ``enabled`` is the master switch: with it false, no
middleware and no route is registered, so a fresh deployment cannot grow a
gateway it never asked for. The per-face sub-switches then decide what is live
once the master switch is on.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GatewayRateLimitSettings(BaseModel):
    """Traffic-control thresholds.

    ``default_qps`` is the fallback when no ``gateway_rate_rule`` row matches,
    aligned with the Java gateway's兜底 value (design §5.9). It is a bound, not
    a latency/coverage SLA.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    enabled: bool = Field(default=False, description="Bind the QPS token-bucket middleware to filter_url.")
    default_qps: int = Field(
        default=10, ge=1, le=100000, description="Requests per second when no gateway_rate_rule row matches."
    )


class GatewayOnlineSettings(BaseModel):
    """WebSocket concurrent-session counting."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    enabled: bool = Field(default=False, description="Bind the online-session counting middleware.")


class GatewayOAuthSettings(BaseModel):
    """Provider-side timeouts for the SSO / OAuth / LDAP flows."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    state_ttl_seconds: int = Field(
        default=600, ge=60, le=3600, description="Lifetime of the anti-CSRF OAuth state nonce in Redis."
    )
    http_timeout_seconds: float = Field(
        default=5.0, gt=0, le=60, description="Per-request timeout when calling a provider's token/introspect endpoint."
    )


class GatewaySettings(BaseModel):
    """``gateway:`` section of ``config.yaml``."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    enabled: bool = Field(
        default=False, description="Master switch; when false no gateway middleware and no gateway route is registered."
    )
    license_gate: bool = Field(
        default=True,
        description="Fail-closed commercial gate: a license_faces path without a valid license_info row is refused.",
    )
    license_codes: list[str] = Field(
        default_factory=lambda: ["etl", "gateway"],
        description="license_info rows whose validity opens the commercial faces; any one valid row is enough.",
    )
    # Default = exactly the four commercial faces (T009 pins this set). The two
    # deliberate omissions:
    #   /api/v1/dsh/**         — DSH chat is an open-source face, gating it would
    #                             lock the desktop out of the product it owns.
    #   /api/v1/gateway/license — the authorization-status query must still answer
    #                             once the license has expired, otherwise the banner
    #                             cannot explain the lock (AC-35).
    license_faces: list[str] = Field(
        default_factory=lambda: [
            "/api/v1/gateway/oauth2/",
            "/api/v1/gateway/sensitive/",
            "/api/v1/gateway/rate-rules",
            "/api/v1/gateway/block-records",
        ],
        description="Path prefixes behind the commercial license gate.",
    )
    filter_url: list[str] = Field(
        default_factory=list,
        description="Path patterns the QPS middleware applies to; empty means rate limiting is not bound to any path.",
    )
    home_url: str = Field(default="", description="Landing page after a successful SSO callback.")
    rate_limit: GatewayRateLimitSettings = Field(default_factory=GatewayRateLimitSettings)
    online: GatewayOnlineSettings = Field(default_factory=GatewayOnlineSettings)
    oauth: GatewayOAuthSettings = Field(default_factory=GatewayOAuthSettings)
    route_ownership: list[str] = Field(
        default_factory=list,
        description="Path prefixes the native gateway owns during the dual-run period with the Java gateway.",
    )
