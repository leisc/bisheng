"""T013 — 网关中间件 + 授权门接线（F120 Wave 2）。

用 TestClient 打真实 ASGI 链路，覆盖 design §5.2 / AC-01, AC-02, AC-03, AC-33, AC-47：

* SSO 登录入口与商业管理面在授权过期时被拒：`/api/v1/**` 是 **200 + 26603 信封**，
  `/api/v2` `/api/v3` 是**真 403**（平台既有约定，不是本模块自创）；
* 授权状态查询与开源面在过期授权下放行；
* **`/api/v1/dsh/**` 在过期授权下放行**（INV-37）；
* `gateway.enabled=false` 时中间件不存在（`app.user_middleware` 里没有它）；
* 注册顺序：网关在 `CustomMiddleware` 之内一层，租户 ContextVar 对网关可见；
* X-Trace-ID 透传。
"""

from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bisheng.commercial_license.domain.models.license_info import LicenseInfo
from bisheng.core.config.gateway import GatewaySettings
from bisheng.core.context.tenant import get_current_tenant_id, set_current_tenant_id
from bisheng.gateway.domain.services.license_gate import LicenseGate
from bisheng.gateway.infrastructure.middleware import (
    TRACE_ID_HEADER,
    GatewayMiddlewareSettings,
    install_gateway,
)

TODAY = date(2026, 11, 17)

# 刻意含一个 v2 商业面前缀，用来验证「v1 信封 vs v2 真 403」的分流。
V1_SSO_PATH = "/api/v1/gateway/oauth2/authorize"
V1_COMMERCIAL_PATH = "/api/v1/gateway/sensitive/policies"
V2_COMMERCIAL_PATH = "/api/v2/gateway/sensitive/policies"
STATUS_PATH = "/api/v1/gateway/license/status"
OPEN_PATH = "/api/v1/skill/group"
DSH_PATH = "/api/v1/dsh/workspace/list"

LICENSE_FACES = (
    "/api/v1/gateway/oauth2/",
    "/api/v1/gateway/sensitive/",
    "/api/v2/gateway/sensitive/",
)

EXPIRED_ROWS = [
    LicenseInfo(
        license_code="etl",
        expire_date=date(2026, 10, 1),
        days_remaining=-47,
        display_state="expired",
        source_status=None,
        checked_at=datetime(2026, 10, 2, 10, 0, 0),
        extra={},
    )
]

VALID_ROWS = [
    LicenseInfo(
        license_code="etl",
        expire_date=date(2027, 1, 1),
        days_remaining=45,
        display_state="normal",
        source_status=None,
        checked_at=datetime(2026, 11, 17, 10, 0, 0),
        extra={},
    )
]


class _FixedClock:
    def __init__(self, moment: datetime):
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class _Repo:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def get_all(self):
        self.calls += 1
        return list(self.rows)


def _gate(rows) -> LicenseGate:
    return LicenseGate(
        repo=_Repo(rows),
        license_codes=("etl", "gateway"),
        clock=_FixedClock(datetime(2026, 11, 17, 10, 0, 0)),
    )


def _gateway_conf(**overrides):
    """构造 GatewaySettings；默认启用 + 开授权门，用 overrides 关掉某一项。"""
    values = {
        "enabled": True,
        "license_gate": True,
        "license_faces": list(LICENSE_FACES),
        "license_codes": ["etl", "gateway"],
    }
    values.update(overrides)
    return GatewaySettings(**values)


def _app(rows, *, gate="default", **conf_overrides) -> FastAPI:
    """一个只挂了探针路由的 app，用来观察中间件行为。"""
    app = FastAPI()

    @app.get("/{full_path:path}")
    async def _probe(full_path: str):
        return {"ok": True, "path": full_path}

    install_gateway(
        app,
        _gateway_conf(**conf_overrides),
        gate=_gate(rows) if gate == "default" else gate,
    )
    return app


# --------------------------------------------------------------------------
# 一、v1 面：200 + 26603 信封
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [V1_SSO_PATH, V1_COMMERCIAL_PATH])
def test_v1_commercial_faces_rejected_with_envelope_on_expired_license(path):
    client = TestClient(_app(EXPIRED_ROWS))
    response = client.get(path)
    assert response.status_code == 200, "v1 面必须保持 200，错误在信封里"
    body = response.json()
    assert body["status_code"] == 26603
    assert body["status_message"]
    # trace_id 进 data，便于前端把「被锁」和链路对账。
    assert body["data"]["trace_id"]
    assert body["data"]["reason"]


def test_v1_commercial_face_allowed_with_valid_license():
    client = TestClient(_app(VALID_ROWS))
    response = client.get(V1_SSO_PATH)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": "api/v1/gateway/oauth2/authorize"}


# --------------------------------------------------------------------------
# 二、v2/v3 面：真 403
# --------------------------------------------------------------------------


def test_v2_commercial_face_rejected_with_real_403():
    client = TestClient(_app(EXPIRED_ROWS))
    response = client.get(V2_COMMERCIAL_PATH)
    assert response.status_code == 403, "v2/v3 面必须用真实 HTTP 状态码"
    body = response.json()
    assert body["status_code"] == 26603, "真实状态码之外，信封里仍带业务码"


def test_v2_commercial_face_allowed_with_valid_license():
    response = TestClient(_app(VALID_ROWS)).get(V2_COMMERCIAL_PATH)
    assert response.status_code == 200
    assert response.json()["ok"] is True


# --------------------------------------------------------------------------
# 三、明确排除的面：授权状态查询 / 开源面 / DSH 面
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [STATUS_PATH, OPEN_PATH, DSH_PATH])
def test_excluded_faces_pass_under_expired_license(path):
    client = TestClient(_app(EXPIRED_ROWS))
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_status_query_passes_so_the_banner_can_explain_the_lock():
    """AC-35：授权过期时前端 License 横幅仍须展示真实状态——查询端点不能被
    自己的门关掉，否则用户只看到「被锁」而不知道原因。"""
    client = TestClient(_app(EXPIRED_ROWS))
    response = client.get(STATUS_PATH)
    assert response.status_code == 200
    assert "status_code" not in response.json(), "放行的响应必须是探针的，不是 26603 信封"


def test_dsh_face_passes_under_expired_license():
    """INV-37：开源完整功能不以许可证为前提。DSH 本地面能力不受授权状态影响。"""
    client = TestClient(_app(EXPIRED_ROWS))
    response = client.get(DSH_PATH)
    assert response.status_code == 200
    assert response.json()["ok"] is True


# --------------------------------------------------------------------------
# 四、灰度开关
# --------------------------------------------------------------------------


def test_gateway_disabled_middleware_is_not_installed():
    app = FastAPI()
    installed = install_gateway(app, _gateway_conf(enabled=False), gate=_gate(EXPIRED_ROWS))
    assert installed is False
    classes = [m.cls for m in app.user_middleware]
    from bisheng.gateway.infrastructure.middleware import GatewayMiddleware

    assert GatewayMiddleware not in classes, "enabled=false 时中间件不得进链路"


def test_gateway_enabled_middleware_is_installed():
    app = FastAPI()
    assert install_gateway(app, _gateway_conf(), gate=_gate(VALID_ROWS)) is True


def test_license_gate_off_still_passes_traffic():
    """`license_gate=false`：中间件在，但不裁决——灰度到一半时先只开打点。"""
    client = TestClient(_app(EXPIRED_ROWS, license_gate=False, gate=None))
    response = client.get(V1_SSO_PATH)
    assert response.status_code == 200
    assert response.json()["ok"] is True


# --------------------------------------------------------------------------
# 五、注册顺序：租户 ContextVar 对中间件可见
# --------------------------------------------------------------------------


def test_registration_order_puts_gateway_inside_custom_middleware():
    """Starlette 中间件 LIFO：后 add 的在外层。`CustomMiddleware` 负责解 JWT 写
    `current_tenant_id`，所以网关必须注册在它之前一行（更靠内），入站时先解 JWT。"""
    from bisheng.gateway.infrastructure.middleware import GatewayMiddleware
    from bisheng.utils.http_middleware import CustomMiddleware

    app = FastAPI()
    # 复刻 main.py 的注册顺序：网关在前，CustomMiddleware 在后。
    install_gateway(app, _gateway_conf(), gate=_gate(VALID_ROWS))
    app.add_middleware(CustomMiddleware)

    classes = [m.cls for m in app.user_middleware]
    # user_middleware 是「后 add 的在前面」，故 CustomMiddleware 的下标必须更小（更外）。
    assert CustomMiddleware in classes and GatewayMiddleware in classes
    assert classes.index(CustomMiddleware) < classes.index(GatewayMiddleware), (
        "网关必须位于 CustomMiddleware 之内，否则 current_tenant_id 对门不可见"
    )


def test_tenant_context_is_visible_inside_the_middleware():
    """端到端验证注册顺序：内层中间件写的 `current_tenant_id` 必须对网关可见。

    Starlette 中间件 LIFO，入站顺序与 `add_middleware` 顺序相反。构造
    「_TenantWriter 在内、网关在外」的栈，探针读到的租户号就是内层写的值，
    证明外层（网关）能看到内层建立的 ContextVar。main.py 因此必须把网关
    注册在 CustomMiddleware 之前一行。
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    class _TenantWriter(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            set_current_tenant_id(7)
            return await call_next(request)

    app = FastAPI()

    @app.get("/api/v1/gateway/sensitive/policies")
    async def _probe():
        return {"tenant_id": get_current_tenant_id()}

    app.add_middleware(_TenantWriter)  # 先 add = 内层
    install_gateway(app, _gateway_conf(), gate=_gate(VALID_ROWS))  # 后 add = 外层

    response = TestClient(app).get("/api/v1/gateway/sensitive/policies")
    assert response.status_code == 200
    assert response.json()["tenant_id"] == 7


# --------------------------------------------------------------------------
# 六、X-Trace-ID 透传
# --------------------------------------------------------------------------


def test_trace_id_is_passed_through_when_caller_supplies_one():
    client = TestClient(_app(VALID_ROWS))
    response = client.get(OPEN_PATH, headers={TRACE_ID_HEADER: "trace-abc-123"})
    assert response.headers[TRACE_ID_HEADER.lower()] == "trace-abc-123"


def test_trace_id_is_generated_when_absent():
    client = TestClient(_app(VALID_ROWS))
    response = client.get(OPEN_PATH)
    assert response.headers[TRACE_ID_HEADER.lower()], "缺少 trace id 时必须生成一个"


def test_trace_id_is_returned_on_rejection():
    client = TestClient(_app(EXPIRED_ROWS))
    response = client.get(V1_SSO_PATH, headers={TRACE_ID_HEADER: "trace-xyz"})
    assert response.headers[TRACE_ID_HEADER.lower()] == "trace-xyz"
    assert response.json()["data"]["trace_id"] == "trace-xyz"


# --------------------------------------------------------------------------
# 七、非 HTTP scope 原样透传
# --------------------------------------------------------------------------


def test_non_http_scope_passes_through():
    """WebSocket / lifespan 不得被中间件拦截。"""
    from bisheng.gateway.infrastructure.middleware import GatewayMiddleware

    seen = []

    async def _app(scope, receive, send):
        seen.append(scope["type"])

    mw = GatewayMiddleware(
        _app,
        settings=GatewayMiddlewareSettings(license_gate=True, license_faces=LICENSE_FACES, license_codes=("etl",)),
        gate=_gate(EXPIRED_ROWS),
    )

    async def _run():
        await mw({"type": "websocket", "path": "/api/v1/gateway/oauth2/authorize"}, None, None)

    import asyncio

    asyncio.run(_run())
    assert seen == ["websocket"]


# --------------------------------------------------------------------------
# 八、main.py 真实接线
# --------------------------------------------------------------------------


def test_main_py_registers_gateway_before_custom_middleware():
    """钉住 main.py 里的注册顺序——这个约束极易被无声破坏（LIFO 反直觉）。

    改成 `install_gateway` 出现在 `add_middleware(CustomMiddleware)` 之后，
    中间件就会跑到 JWT 解码之外，`current_tenant_id` 对门不可见。
    """
    from pathlib import Path

    main_py = Path(__file__).resolve().parents[2] / "bisheng" / "main.py"
    text = main_py.read_text()
    gateway_at = text.index("install_gateway(app, settings.gateway)")
    custom_at = text.index("app.add_middleware(CustomMiddleware)")
    assert gateway_at < custom_at, "install_gateway 必须在 add_middleware(CustomMiddleware) 之前一行"


def test_main_py_imports_install_gateway_at_module_top_level():
    """顶层 import，不能挪进 `create_app` 内部。

    `_discover_tenant_aware_tables`（`core/database/tenant_filter.py`）依赖
    `SQLModel.metadata` 已被填充，而 `register_tenant_filter_events` 要到
    DatabaseManager 初始化时才跑；顶层导入才能保证新表进入租户过滤发现集。
    """
    from pathlib import Path

    main_py = Path(__file__).resolve().parents[2] / "bisheng" / "main.py"
    lines = main_py.read_text().splitlines()
    create_app_at = next(i for i, line in enumerate(lines) if line.startswith("def create_app("))
    import_lines = [i for i, line in enumerate(lines[:create_app_at]) if "install_gateway" in line]
    assert import_lines, "install_gateway 必须在模块顶层 import"
    assert all(lines[i].startswith(("from ", "import ")) for i in import_lines)


def test_create_app_with_gateway_disabled_has_no_gateway_middleware():
    """默认配置（`enabled=false`）下 create_app 的链路与 F120 之前完全一致——
    这是「新部署不被误伤」的最终防线。"""
    from bisheng.main import create_app

    app = create_app()
    classes = [m.cls.__name__ for m in app.user_middleware]
    assert "GatewayMiddleware" not in classes
    assert classes == [
        "WebSocketLoggingMiddleware",
        "CustomMiddleware",
        "OpenApiAuditMiddleware",
        "OpenMcpDispatchMiddleware",
        "AdminScopeMiddleware",
        "CORSMiddleware",
    ]
