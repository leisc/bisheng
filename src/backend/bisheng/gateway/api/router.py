"""F120 网关 API 聚合。endpoint 模块各自持有鉴权策略。"""

from fastapi import APIRouter

from bisheng.gateway.api.endpoints.block_record import router as block_record_router
from bisheng.gateway.api.endpoints.license import router as license_router

router = APIRouter()
router.include_router(license_router)
router.include_router(block_record_router)
