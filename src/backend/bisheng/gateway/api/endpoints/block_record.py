"""拦截记录查询端点（F120 T024，design §5.4 / §6）。

**只读。** 写入只发生在内容安全命中点（T025 起接入），本端点不提供任何创建、
修改或删除路径——记录是证据。

**没有 ``tenant_id`` 查询参数，这是有意的。** 自动租户过滤从请求 ContextVar 注入
调用方租户，所以租户管理员天然只能看自己租户；平台超管要跨租户查看时走平台既有的
admin-scope 切换（`AdminScopeMiddleware` → `get_current_tenant_id()`），而不是本端点
自己再判一次权限。第二套「谁能看哪个租户」的判定正是 C4 要避免的东西。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from bisheng.common.cursor import CursorDecodeError
from bisheng.common.dependencies.user_deps import UserPayload
from bisheng.common.errcode.gateway import GatewayBlockRecordInvalidCursorError
from bisheng.common.schemas.api import UnifiedResponseModel, resp_200
from bisheng.gateway.domain.models.block_record import GatewayBlockFace, GatewayBlockStage
from bisheng.gateway.domain.schemas.block_record import GatewayBlockRecordPage
from bisheng.gateway.domain.services.block_record_service import MAX_PAGE_SIZE, GatewayBlockRecordService

router = APIRouter(prefix='/gateway/block-records', tags=['GatewayBlockRecord'])


@router.get('', response_model=UnifiedResponseModel[GatewayBlockRecordPage])
async def list_gateway_block_records(
    login_user: UserPayload = Depends(UserPayload.get_tenant_admin_user),
    face: Literal[
        'workstation_chat',
        'dsh_chat',
        'channel_article',
        'knowledge_file',
        'open_api_chat',
        'open_api_model',
    ] | None = Query(default=None),
    stage: Literal['input', 'output'] | None = Query(default=None),
    user_id: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
):
    """分页查询本租户的拦截记录，最新在前。

    Response shape (``GatewayBlockRecordPage``): ``{data, page_size, has_more,
    next_cursor}``。把上一页的 ``next_cursor`` 传给 ``cursor``；首页省略或传空。
    ``page_size`` 上限 ``MAX_PAGE_SIZE``——这张表预期会到百万行，深翻页没有意义。
    """
    try:
        page = await GatewayBlockRecordService.list_blocks(
            face=face,
            stage=stage,
            user_id=user_id,
            cursor=cursor,
            page_size=page_size,
        )
    except CursorDecodeError:
        # A stale or hand-edited cursor is the client's mistake; a stable code
        # lets the page say "refresh" instead of showing a 500.
        raise GatewayBlockRecordInvalidCursorError() from None
    return resp_200(data=page.model_dump(mode='json'))


__all__ = [
    'GatewayBlockFace',
    'GatewayBlockStage',
    'list_gateway_block_records',
    'router',
]
