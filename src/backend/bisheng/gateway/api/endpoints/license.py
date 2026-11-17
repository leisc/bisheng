"""授权状态聚合端点（F120 T016，design 决策 6 / §6.2）。

**只读。** 委托 Owner（`commercial_license`）的 `list_license_status` 取口径，
再叠加网关自己的门裁决结果，让前端横幅能一次拿全「授权事实 + 我为什么被锁」。

不新增任何写入路径：不触发 ETL 刷新（`etl_sync=None`）、不写 `license_info`。
`etl_sync.py` 与 `POST /api/v1/commercial-licenses/gateway` 保持原样——前者由
Celery beat 定时刷新，后者供双跑期 Java gateway 注入 `gateway` 行。

本端点**不参与**自己的授权门：`STATUS_FACE` 在 `license_gate.classify_path` 里
被无条件排除（AC-35），否则授权过期后前端无法解释为何被锁。
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from bisheng.commercial_license.domain.repositories.license_info_repository import LicenseInfoRepository
from bisheng.commercial_license.domain.services.aggregator import list_license_status
from bisheng.common.dependencies.core_deps import get_db_session
from bisheng.common.schemas.api import UnifiedResponseModel, resp_200
from bisheng.gateway.domain.services.license_gate import LicenseGate

router = APIRouter(prefix="/gateway/license", tags=["GatewayLicense"])


@router.get("/status", response_model=UnifiedResponseModel)
async def get_gateway_license_status(session: AsyncSession = Depends(get_db_session)):
    """授权事实 + 门当前裁决。`gate_decision` 是附加信息，不影响本端点可达性。"""
    now = datetime.now()
    repo = LicenseInfoRepository(session)
    # etl_sync=None：只读，不触发 ETL HTTP 刷新（决策 6）。
    data = await list_license_status(now, repo=repo, etl_sync=None)

    gate = LicenseGate(repo=repo, license_codes=_license_codes())
    decision = await gate.decide("/api/v1/gateway/sensitive/policies", license_faces=_license_faces())
    data["gate_decision"] = {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "license_code": decision.license_code,
        "days_remaining": decision.days_remaining,
    }
    return resp_200(data)


def _license_codes() -> tuple[str, ...]:
    from bisheng.common.services.config_service import settings

    return tuple(settings.gateway.license_codes or ())


def _license_faces() -> tuple[str, ...]:
    from bisheng.common.services.config_service import settings

    return tuple(settings.gateway.license_faces or ())
