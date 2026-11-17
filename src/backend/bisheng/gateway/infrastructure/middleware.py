"""原生网关 ASGI 中间件骨架（F120 T014，design §5.2）。

**纯 ASGI**（不继承 `BaseHTTPMiddleware`）：后者会为每个请求起一个线程池任务，
下游对 `ContextVar` 的修改不会泄漏回上游，对要读 `current_tenant_id` 的门来说
语义不对。

**注册顺序**（`main.py`）：Starlette 中间件是 LIFO——后 `add_middleware` 的在外层。
`CustomMiddleware` 从 JWT 解出 `current_tenant_id`，所以网关必须注册在它**之前**
一行（即更靠内），入站时 `CustomMiddleware` 先跑、租户 ContextVar 对网关可见。
`test_gateway_middleware_license.py` 钉住这个顺序。

**路径分流**：`/api/v2` `/api/v3` 用真实 HTTP 状态码（平台信封仍在 body 里），
其余用 200 + `UnifiedResponseModel` 信封——这是平台既有约定
（`main.py:200` 注释、`open_api/api/exception_handlers.py`），不是本模块自创。

**门关闭时本模块不注册**：`gateway.enabled=false` 时 `main.py` 不调用
`install_gateway`，请求链路零开销（T013 断言 `app.user_middleware` 里没有它）。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from loguru import logger
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bisheng.common.schemas.api import UnifiedResponseModel
from bisheng.core.context.tenant import get_current_tenant_id
from bisheng.gateway.domain.services.license_gate import COMMERCIAL_FACE, LicenseGate

TRACE_ID_HEADER = "X-Trace-ID"
METRIC_EVENT = "GATEWAY_METRIC"

# 真实 HTTP 状态码的面（平台既有约定，见模块 docstring）。
REAL_STATUS_PREFIXES = ("/api/v2", "/api/v3")

# 授权门拒绝时的 HTTP 状态码（v2/v3 面）。v1 面仍走 200 + 信封。
LICENSE_DENIED_HTTP_STATUS = 403


@dataclass(frozen=True)
class GatewayMiddlewareSettings:
    """中间件需要的配置切片。显式传入，避免中间件反向摸全局 settings。"""

    license_gate: bool
    license_faces: tuple[str, ...]
    license_codes: tuple[str, ...]


def _real_status_face(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in REAL_STATUS_PREFIXES)


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or ():
        if key == target:
            return value.decode("latin-1")
    return None


def _ensure_trace_id(scope: Scope) -> str:
    """透传调用方 trace id；没有则本次生成一个，回写 scope 供响应头使用。"""
    trace_id = _header(scope, TRACE_ID_HEADER)
    if not trace_id:
        trace_id = uuid.uuid4().hex
    scope.setdefault("state", {})
    scope["gateway_trace_id"] = trace_id
    return trace_id


def _denied_body(exc, trace_id: str) -> dict:
    """v1 信封 / v2 body 共用的载荷。`trace_id` 进 data 便于对账。"""
    return UnifiedResponseModel(
        status_code=exc.code,
        status_message=exc.message,
        data={"trace_id": trace_id, "reason": str(exc)},
    ).model_dump()


async def _send_json(send: Send, status_code: int, body: dict, trace_id: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (TRACE_ID_HEADER.lower().encode("latin-1"), trace_id.encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": json.dumps(body, ensure_ascii=False).encode("utf-8")})


class _TracingSend:
    """给响应头补上 trace id，其余消息原样转发。"""

    def __init__(self, send: Send, trace_id: str) -> None:
        self._send = send
        self._trace_id = trace_id

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            headers: list = list(message.get("headers") or [])
            headers.append((TRACE_ID_HEADER.lower().encode("latin-1"), self._trace_id.encode("latin-1")))
            message = {**message, "headers": headers}
        await self._send(message)


class GatewayMiddleware:
    """路径分流 + 授权门 + 打点。`gate=None`（`license_gate=false`）时只透传与打点。"""

    def __init__(self, app: ASGIApp, *, settings: GatewayMiddlewareSettings, gate: LicenseGate | None = None) -> None:
        self.app = app
        self.settings = settings
        self._gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        path = scope.get("path") or ""
        trace_id = _ensure_trace_id(scope)

        if self._gate is not None:
            decision = await self._gate.decide(path, license_faces=self.settings.license_faces)
            if decision.face == COMMERCIAL_FACE and not decision.allowed:
                await self._reject(send, path, trace_id, decision.reason, started)
                return

        await self.app(scope, receive, _TracingSend(send, trace_id))
        self._metric(path, trace_id, started, decision=None, reason=None)

    async def _reject(self, send: Send, path: str, trace_id: str, reason: str | None, started: float) -> None:
        from bisheng.common.errcode.gateway import GatewayOAuthAuthorizeError

        exc = GatewayOAuthAuthorizeError()
        status = LICENSE_DENIED_HTTP_STATUS if _real_status_face(path) else 200
        await _send_json(send, status, _denied_body(exc, trace_id), trace_id)
        self._metric(path, trace_id, started, decision="denied", reason=reason)

    def _metric(self, path: str, trace_id: str, started: float, decision: str | None, reason: str | None) -> None:
        """`GATEWAY_METRIC` 结构化打点。best-effort：打点失败绝不影响业务流。"""
        try:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.bind(
                event=METRIC_EVENT,
                path=path,
                trace_id=trace_id,
                tenant_id=get_current_tenant_id(),
                duration_ms=duration_ms,
                decision=decision,
                reason=reason,
            ).info("gateway request")
        except Exception:  # pragma: no cover - 打点不得影响业务
            logger.opt(exception=True).debug("gateway metric emission failed")


class SessionBoundLicenseRepo:
    """每次 `get_all` 开一个短异步会话。

    门是每请求裁决一次，但 `LicenseGate` 有 60s 进程内缓存兜底，实际每 60s
    才真正碰一次库；缓存未命中时（首请求、过期后）才付这个开销。
    """

    async def get_all(self):
        # 延迟导入：middleware 在 main.py 顶层导入，此刻 DatabaseManager 可能还没初始化。
        from bisheng.commercial_license.domain.repositories.license_info_repository import (
            LicenseInfoRepository,
        )
        from bisheng.core.database.manager import get_async_db_session

        async with get_async_db_session() as session:
            return await LicenseInfoRepository(session).get_all()


def build_gateway_settings(gateway_conf) -> GatewayMiddlewareSettings:
    """从 `GatewaySettings` 切出中间件需要的字段。"""
    return GatewayMiddlewareSettings(
        license_gate=bool(gateway_conf.license_gate),
        license_faces=tuple(gateway_conf.license_faces or ()),
        license_codes=tuple(gateway_conf.license_codes or ()),
    )


def make_gate(gateway_conf) -> LicenseGate | None:
    """构造读入门。`license_gate=false` 时返回 None——中间件存在但不裁决。"""
    if not bool(gateway_conf.license_gate):
        return None
    return LicenseGate(repo=SessionBoundLicenseRepo(), license_codes=tuple(gateway_conf.license_codes or ()))


def install_gateway(app, gateway_conf, *, gate: LicenseGate | None = None) -> bool:
    """把网关中间件注册到 `app`。返回是否真的注册了（`enabled=false` 时不注册）。

    **必须在 `app.add_middleware(CustomMiddleware)` 之前一行调用**——见模块 docstring
    的注册顺序说明。
    """
    if not bool(gateway_conf.enabled):
        return False
    app.add_middleware(
        GatewayMiddleware,
        settings=build_gateway_settings(gateway_conf),
        gate=gate if gate is not None else make_gate(gateway_conf),
    )
    return True
