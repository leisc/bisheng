from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, Column, DateTime, Integer, String, UniqueConstraint, text
from sqlmodel import Field

from bisheng.common.models.base import SQLModelSerializable
from bisheng.core.database.dialect_helpers import UPDATE_TIME_SERVER_DEFAULT, JsonType


class GatewayProviderType:
    """OAuth provider flavours the gateway can speak.

    Kept as plain string constants (not an Enum) because the value is stored in
    a ``varchar(32)`` column and compared against config written by the Java
    gateway during the dual-run period — an Enum would make every new provider
    a schema change.
    """

    CUSTOM_SSO = 'custom_sso'
    WECOM_QR = 'wecom_qr'
    WECOM_WEB = 'wecom_web'
    LDAP = 'ldap'

    ALL = (CUSTOM_SSO, WECOM_QR, WECOM_WEB, LDAP)


class GatewayOAuthProviderBase(SQLModelSerializable):
    tenant_id: int = Field(
        sa_column=Column(Integer, nullable=False, index=True, comment='租户ID，1=root 全局默认'),
    )
    provider_type: str = Field(
        sa_column=Column(String(32), nullable=False, index=True, comment='custom_sso/wecom_qr/wecom_web/ldap'),
    )
    name: str = Field(
        default='',
        sa_column=Column(String(128), nullable=False, server_default=text("''"), comment='展示名'),
    )
    #: Endpoints, client_id and *ciphertext* secrets. Plaintext never lands here
    #: (C6): the service layer Fernet-encrypts before writing — see
    #: ``gateway/domain/services/oauth_config_security.py``.
    config: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JsonType, nullable=True, comment='端点与凭据，机密字段为 Fernet 密文'),
    )
    enabled: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text('0'), comment='是否启用'),
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


class GatewayOAuthProvider(GatewayOAuthProviderBase, table=True):
    __tablename__ = 'gateway_oauth_provider'
    __table_args__ = (
        # One live row per (tenant, provider_type). ``logic_delete`` is part of
        # the key so a deleted provider can be re-created with the same type.
        UniqueConstraint(
            'tenant_id',
            'provider_type',
            'logic_delete',
            name='uk_gateway_oauth_provider',
        ),
        {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
