from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Integer, String, UniqueConstraint, text
from sqlmodel import Field

from bisheng.common.models.base import SQLModelSerializable
from bisheng.core.database.dialect_helpers import UPDATE_TIME_SERVER_DEFAULT


class GatewayRateScopeType:
    """Who a rate rule applies to. Resolution order is the tuple below —
    most specific first. The middleware asks the repository for the first
    candidate that has a rule; getting the order wrong silently applies the
    global limit to a user who should have their own.
    """

    USER = 'user'
    USER_GROUP = 'user_group'
    DEPARTMENT = 'department'
    GLOBAL = 'global'

    RESOLUTION_ORDER = (USER, USER_GROUP, DEPARTMENT, GLOBAL)
    ALL = RESOLUTION_ORDER


class GatewayRateResourceType:
    ASSISTANT = 'assistant'
    SKILL = 'skill'
    WORKFLOW = 'workflow'
    PATH = 'path'

    ALL = (ASSISTANT, SKILL, WORKFLOW, PATH)


class GatewayRateRuleBase(SQLModelSerializable):
    tenant_id: int = Field(
        sa_column=Column(Integer, nullable=False, index=True, comment='租户ID'),
    )
    scope_type: str = Field(
        sa_column=Column(String(32), nullable=False, index=True, comment='global/department/user_group/user'),
    )
    #: Business key of the scope: dept key ``BS@xxx`` / user_group id / user id.
    #: Empty for ``global``.
    scope_id: str = Field(
        default='',
        sa_column=Column(String(64), nullable=False, server_default=text("''"), comment='作用域业务键，global 为空'),
    )
    resource_type: str = Field(
        sa_column=Column(String(32), nullable=False, index=True, comment='assistant/skill/workflow/path'),
    )
    #: Empty means "every resource of this type" — the common case.
    resource_id: str = Field(
        default='',
        sa_column=Column(String(64), nullable=False, server_default=text("''"), comment='空=该类型全量'),
    )
    #: 0 = unlimited. The bucket is a Redis Lua token bucket keyed by
    #: (tenant, subject, route_group) — see gateway/infrastructure/lua.
    qps_limit: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default=text('0'), comment='QPS 上限，0=不限'))
    burst: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default=text('0'), comment='突发桶容量'))
    #: WS concurrent-session ceiling for this scope; 0 = unlimited.
    #: Combined with the department's own ``concurrent_session_limit``.
    concurrent_limit: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text('0'), comment='WS 在线并发上限，0=不限'),
    )
    enabled: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text('1'), comment='是否启用'),
    )
    created_by: Optional[int] = Field(default=None, sa_column=Column(Integer, nullable=True, comment='创建人ID'))
    updated_by: Optional[int] = Field(default=None, sa_column=Column(Integer, nullable=True, comment='更新人ID'))
    create_time: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime, nullable=False, server_default=text('CURRENT_TIMESTAMP')),
    )
    update_time: Optional[datetime] = Field(
        default=None,
        sa_column=Column(
            DateTime,
            nullable=False,
            server_default=UPDATE_TIME_SERVER_DEFAULT,
            onupdate=UPDATE_TIME_SERVER_DEFAULT,
        ),
    )
    logic_delete: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text('0'), comment='逻辑删除标记'),
    )


class GatewayRateRule(GatewayRateRuleBase, table=True):
    __tablename__ = 'gateway_rate_rule'
    __table_args__ = (
        # ``logic_delete`` in the key: after a soft delete the same business key
        # must be insertable again, otherwise a tenant can never re-create a
        # rule it once removed.
        UniqueConstraint(
            'tenant_id',
            'scope_type',
            'scope_id',
            'resource_type',
            'resource_id',
            'logic_delete',
            name='uk_gateway_rate_rule',
        ),
        {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
