from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, text
from sqlmodel import Field

from bisheng.common.models.base import SQLModelSerializable
from bisheng.core.database.dialect_helpers import JsonType, LargeText


class GatewayBlockFace:
    """Which surface produced a block. Every face that consults the sensitive-word
    engine writes one row, so the query page can answer "what got blocked where".
    """

    WORKSTATION_CHAT = 'workstation_chat'
    DSH_CHAT = 'dsh_chat'
    CHANNEL_ARTICLE = 'channel_article'
    KNOWLEDGE_FILE = 'knowledge_file'
    OPEN_API_CHAT = 'open_api_chat'
    OPEN_API_MODEL = 'open_api_model'

    ALL = (
        WORKSTATION_CHAT,
        DSH_CHAT,
        CHANNEL_ARTICLE,
        KNOWLEDGE_FILE,
        OPEN_API_CHAT,
        OPEN_API_MODEL,
    )


class GatewayBlockStage:
    INPUT = 'input'
    OUTPUT = 'output'

    ALL = (INPUT, OUTPUT)


class GatewayBlockRecordBase(SQLModelSerializable):
    """Append-only audit of every content-safety rejection.

    Deliberately **no ``logic_delete`` and no ``update_time``**: a block record
    is evidence. Soft-deleting or rewriting it would let a tenant quietly
    rewrite its own safety history, and the 90-day retention job can hard-delete
    freely because nothing else references these rows.
    """

    #: BIGINT on purpose — this table grows with traffic and is the only F120
    #: table expected to reach millions of rows. The SQLite variant keeps the
    #: column an INTEGER PRIMARY KEY (rowid alias) so local/CI tests get
    #: autoincrement without a second schema definition.
    id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger().with_variant(Integer, 'sqlite'), primary_key=True, autoincrement=True),
    )
    tenant_id: int = Field(sa_column=Column(Integer, nullable=False, index=True, comment='租户ID'))
    user_id: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default=text('0'), comment='用户ID'))
    face: str = Field(sa_column=Column(String(32), nullable=False, comment='拦截面'))
    stage: str = Field(sa_column=Column(String(16), nullable=False, comment='input/output'))
    resource_type: str = Field(
        default='',
        sa_column=Column(String(32), nullable=False, server_default=text("''"), comment='资源类型'),
    )
    resource_id: str = Field(
        default='',
        sa_column=Column(String(64), nullable=False, server_default=text("''"), comment='资源ID'),
    )
    #: Truncated to at most 50 words + hit_count by the writer.
    matched_words: List[Any] = Field(
        default_factory=list,
        sa_column=Column(JsonType, nullable=True, comment='命中词与次数，最多 50 条'),
    )
    #: Preview only, truncated to 500 characters. Never the full user input —
    #: the gateway must not become a second copy of user content.
    user_input: str = Field(
        default='',
        sa_column=Column(LargeText, nullable=False, server_default=text("''"), comment='输入预览，截断 500 字符'),
    )
    auto_reply: str = Field(
        default='',
        sa_column=Column(String(500), nullable=False, server_default=text("''"), comment='命中提示话术'),
    )
    #: Correlates with the request's trace id.
    request_id: str = Field(
        default='',
        sa_column=Column(String(64), nullable=False, server_default=text("''"), index=True, comment='trace_id'),
    )
    create_time: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime, nullable=False, server_default=text('CURRENT_TIMESTAMP'), index=True),
    )


class GatewayBlockRecord(GatewayBlockRecordBase, table=True):
    __tablename__ = 'gateway_block_record'
    __table_args__ = (
        # The two access patterns of the query page: "what happened on this
        # face recently" and "what did this user hit".
        Index('idx_gateway_block_record_face', 'tenant_id', 'face', 'create_time'),
        Index('idx_gateway_block_record_user', 'tenant_id', 'user_id', 'create_time'),
        {'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'},
    )
