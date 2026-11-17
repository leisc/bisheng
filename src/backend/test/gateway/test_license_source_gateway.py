"""T015 — 授权状态查询端点与 Owner 的关系（F120 Wave 2）。

覆盖 design 决策 6 的三条红线：

1. `/api/v1/gateway/license/status` **委托 Owner 的 `list_license_status`** 返回，
   与 `license_info` 行一致（不自己拼一份口径），且**不触发 ETL 刷新**；
2. **仅一条未过期 `etl` 行即可放行商业面**（`license_codes` 默认含 etl）；
3. `POST /api/v1/commercial-licenses/gateway` 外部注入路径**仍可用**，且
   **F120 未改动 `commercial_license/` 下任何文件**——用 `git diff` 断言该目录
   零改动（release-contract 第 53 行只允许读取/调用 Owner Service）；
4. 不因 `BISHENG_PRO=true` 放行（环境变量不是授权依据）。
"""

import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bisheng.commercial_license.domain.models.license_info import LicenseInfo
from bisheng.gateway.api.endpoints.license import get_gateway_license_status
from bisheng.gateway.api.router import router as gateway_router

REPO_ROOT = Path(__file__).resolve().parents[3]

TODAY = date(2026, 11, 17)
STATUS_PATH = "/api/v1/gateway/license/status"
COMMERCIAL_PATH = "/api/v1/gateway/sensitive/policies"
LICENSE_FACES = ("/api/v1/gateway/sensitive/",)
LICENSE_CODES = ("etl", "gateway")


def _row(code: str, expire_date: date | None, display_state: str) -> LicenseInfo:
    return LicenseInfo(
        license_code=code,
        expire_date=expire_date,
        days_remaining=None,
        display_state=display_state,
        source_status=None,
        checked_at=datetime(2026, 11, 17, 10, 0, 0),
        extra={},
    )


@pytest.fixture
async def session():
    """内存 SQLite（aiosqlite）+ 只建 `license_info` 一张表。

    不能 `SQLModel.metadata.create_all`：`usergroup` 等表是复合主键，
    SQLite 不支持其 autoincrement，会 CompileError。
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel.ext.asyncio.session import AsyncSession

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(LicenseInfo.__table__.create)
    async with AsyncSession(engine) as session:
        yield session
    await engine.dispose()


def _routes() -> dict:
    """不碰 DB 的路由形状断言：路径 / 方法 / tag。"""
    app = FastAPI()
    app.include_router(gateway_router, prefix="/api/v1")
    return {(r.path, tuple(sorted(r.methods))): r for r in app.routes if hasattr(r, "methods")}


# --------------------------------------------------------------------------
# 一、委托 Owner 的 list_license_status
# --------------------------------------------------------------------------
# 这些用例直接 await 端点函数，不用 TestClient：TestClient 会把 app 跑到另一个
# 线程的 event loop 里，测试线程建的 AsyncSession 会报
# "attached to a different loop"。路由形状另见 test_status_route_shape。


async def test_status_route_shape():
    routes = _routes()
    assert (STATUS_PATH, ("GET",)) in routes, f"未注册 {STATUS_PATH} GET，实际：{list(routes)}"


async def test_status_endpoint_delegates_to_owner_aggregator(session, monkeypatch):
    """响应必须与 Owner 的 `list_license_status` 逐字段一致，而不是网关自拼口径。"""
    session.add(_row("etl", date(2027, 1, 1), "normal"))
    session.add(_row("gateway", date(2027, 6, 1), "normal"))
    await session.commit()

    # 端点按名导入，必须打在端点模块上（打在 aggregator 上无效）。
    import bisheng.gateway.api.endpoints.license as endpoint_module

    captured = {}
    original = endpoint_module.list_license_status

    async def _spy(now, **kwargs):
        captured["etl_sync"] = kwargs.get("etl_sync")
        return await original(now, **kwargs)

    monkeypatch.setattr(endpoint_module, "list_license_status", _spy)

    body = await get_gateway_license_status(session)
    codes = {item["license_code"] for item in body.data["licenses"]}
    assert codes == {"etl", "gateway"}
    # 只读：不得传 etl_sync，否则会触发 ETL HTTP 刷新（新的写入路径）。
    assert captured["etl_sync"] is None, "授权状态查询必须只读，不得触发 ETL 刷新"


async def test_status_endpoint_matches_license_info_rows(session):
    """与 `license_info` 行一致：`display_state` 必须是 Owner 的重算值，不是缓存列。"""
    session.add(_row("etl", date(2026, 10, 1), "normal"))  # 列写 normal，实际已过期
    await session.commit()

    body = await get_gateway_license_status(session)
    item = body.data["licenses"][0]
    assert item["display_state"] == "expired", "必须用 Owner 的 compute_display_state 重算"
    assert item["expire_date"] == "2026-10-01"
    assert item["days_remaining"] == -47


async def test_status_endpoint_reports_no_licenses_when_table_is_empty(session):
    body = await get_gateway_license_status(session)
    assert body.data["licenses"] == []


async def test_status_endpoint_carries_the_gate_decision(session):
    """`gate_decision` 让前端横幅一次拿全「授权事实 + 我为什么被锁」（AC-35）。"""
    session.add(_row("etl", date(2027, 1, 1), "normal"))
    await session.commit()

    body = await get_gateway_license_status(session)
    decision = body.data["gate_decision"]
    assert decision["allowed"] is True
    assert decision["license_code"] == "etl"
    assert decision["days_remaining"] == 45


async def test_status_endpoint_gate_decision_reflects_expired_license(session):
    session.add(_row("etl", date(2026, 10, 1), "expired"))
    await session.commit()

    body = await get_gateway_license_status(session)
    decision = body.data["gate_decision"]
    assert decision["allowed"] is False
    assert decision["reason"] == "expired"


# --------------------------------------------------------------------------
# 三、F120 未改动 commercial_license/ 下任何文件
# --------------------------------------------------------------------------


def test_commercial_license_directory_is_untouched_by_f120():
    """release-contract 第 53 行：非 Owner 只能读取/调用 Owner Service。

    这条断言把红线变成机器可检：`commercial_license/` 下的任何增删改都会让它红。
    """
    changed = subprocess.run(
        ["git", "diff", "--stat", "HEAD", "--", "src/backend/bisheng/commercial_license/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert changed.stdout.strip() == "", f"F120 不得改动 commercial_license/，实际改动：\n{changed.stdout}"

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "src/backend/bisheng/commercial_license/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert untracked.stdout.strip() == "", f"F120 不得新增 commercial_license/ 文件：{untracked.stdout}"


async def test_gateway_license_injection_path_still_works(session):
    """`POST /api/v1/commercial-licenses/gateway` 外部注入路径仍可用——双跑期
    Java gateway 靠它写 `gateway` 行，F120 不得破坏。

    这里直接调 Owner 的 upsert 服务（它才是权威写入路径），绕开登录态。
    """
    from bisheng.commercial_license.domain.repositories.license_info_repository import LicenseInfoRepository
    from bisheng.commercial_license.domain.services.upsert import upsert_mapped
    from bisheng.gateway.domain.services.license_gate import decide_path

    mapped = {
        "license_code": "gateway",
        "expire_date": date(2027, 1, 1),
        "days_remaining": 45,
        "display_state": "normal",
        "source_status": "normal",
        "extra": {"version": "1.0.0", "expired": False},
    }
    row = await upsert_mapped(LicenseInfoRepository(session), mapped)
    assert row.license_code == "gateway"

    rows = await LicenseInfoRepository(session).get_all()
    decision = decide_path(
        COMMERCIAL_PATH,
        rows,
        license_codes=LICENSE_CODES,
        license_faces=LICENSE_FACES,
        today=TODAY,
    )
    assert decision.allowed
    assert decision.license_code == "gateway"


# --------------------------------------------------------------------------
# 四、BISHENG_PRO 不是授权依据
# --------------------------------------------------------------------------


async def test_bisheng_pro_env_var_does_not_open_the_gate(session, monkeypatch):
    monkeypatch.setenv("BISHENG_PRO", "true")
    from bisheng.commercial_license.domain.repositories.license_info_repository import LicenseInfoRepository
    from bisheng.gateway.domain.services.license_gate import LicenseGate

    gate = LicenseGate(repo=LicenseInfoRepository(session), license_codes=LICENSE_CODES)
    decision = await gate.decide(COMMERCIAL_PATH, license_faces=LICENSE_FACES)
    assert not decision.allowed, "BISHENG_PRO 是进程级开关，与授权事实漂移（K4）"
