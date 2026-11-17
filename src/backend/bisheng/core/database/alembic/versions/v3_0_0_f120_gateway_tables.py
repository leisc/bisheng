"""F120: native gateway tables (OAuth providers, rate rules, block records).

Revision ID: v3_0_0_f120_gateway_tables
Revises: f114_app_authorization
Create Date: 2026-11-17

``down_revision`` is the output of ``uv run alembic heads`` on the day this
migration was written (``f114_app_authorization``). File-name prefixes do not
imply topological order — ``core/database/alembic/AGENTS.md`` warns about
exactly that, so the value is re-checked, not inferred.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

from bisheng.core.database.alembic_helpers.online import table_exists
from bisheng.core.database.dialect_helpers import JsonType, LargeText, update_time_server_default

revision: str = 'v3_0_0_f120_gateway_tables'
down_revision: Union[str, Sequence[str], None] = 'f114_app_authorization'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OAUTH_PROVIDER = 'gateway_oauth_provider'
_RATE_RULE = 'gateway_rate_rule'
_BLOCK_RECORD = 'gateway_block_record'


def upgrade() -> None:
    conn = op.get_bind()
    update_time_default = update_time_server_default(conn)

    if not table_exists(_OAUTH_PROVIDER):
        op.create_table(
            _OAUTH_PROVIDER,
            sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column('tenant_id', sa.Integer, nullable=False, comment='租户ID，1=root 全局默认'),
            sa.Column('provider_type', sa.String(length=32), nullable=False, comment='custom_sso/wecom_qr/wecom_web/ldap'),
            sa.Column('name', sa.String(length=128), nullable=False, server_default=sa.text("''"), comment='展示名'),
            # JSON on MySQL, CLOB on DM8, TEXT elsewhere — see dialect_helpers.
            sa.Column('config', JsonType(), nullable=True, comment='端点与凭据，机密字段为 Fernet 密文'),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('0'), comment='是否启用'),
            sa.Column('created_by', sa.Integer, nullable=True, comment='创建人ID'),
            sa.Column('updated_by', sa.Integer, nullable=True, comment='更新人ID'),
            sa.Column('create_time', sa.DateTime, nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('update_time', sa.DateTime, nullable=False, server_default=update_time_default),
            sa.Column('logic_delete', sa.Integer, nullable=False, server_default=sa.text('0'), comment='逻辑删除标记'),
            # logic_delete is part of the key so a deleted provider can be
            # re-created with the same provider_type.
            sa.UniqueConstraint('tenant_id', 'provider_type', 'logic_delete', name='uk_gateway_oauth_provider'),
            mysql_charset='utf8mb4',
            mysql_collate='utf8mb4_unicode_ci',
        )
        op.create_index('ix_gateway_oauth_provider_tenant_id', _OAUTH_PROVIDER, ['tenant_id'])
        op.create_index('ix_gateway_oauth_provider_type', _OAUTH_PROVIDER, ['provider_type'])

    if not table_exists(_RATE_RULE):
        op.create_table(
            _RATE_RULE,
            sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column('tenant_id', sa.Integer, nullable=False, comment='租户ID'),
            sa.Column('scope_type', sa.String(length=32), nullable=False, comment='global/department/user_group/user'),
            sa.Column('scope_id', sa.String(length=64), nullable=False, server_default=sa.text("''"), comment='作用域业务键，global 为空'),
            sa.Column('resource_type', sa.String(length=32), nullable=False, comment='assistant/skill/workflow/path'),
            sa.Column('resource_id', sa.String(length=64), nullable=False, server_default=sa.text("''"), comment='空=该类型全量'),
            sa.Column('qps_limit', sa.Integer, nullable=False, server_default=sa.text('0'), comment='QPS 上限，0=不限'),
            sa.Column('burst', sa.Integer, nullable=False, server_default=sa.text('0'), comment='突发桶容量'),
            sa.Column('concurrent_limit', sa.Integer, nullable=False, server_default=sa.text('0'), comment='WS 在线并发上限，0=不限'),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('1'), comment='是否启用'),
            sa.Column('created_by', sa.Integer, nullable=True, comment='创建人ID'),
            sa.Column('updated_by', sa.Integer, nullable=True, comment='更新人ID'),
            sa.Column('create_time', sa.DateTime, nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('update_time', sa.DateTime, nullable=False, server_default=update_time_default),
            sa.Column('logic_delete', sa.Integer, nullable=False, server_default=sa.text('0'), comment='逻辑删除标记'),
            sa.UniqueConstraint(
                'tenant_id',
                'scope_type',
                'scope_id',
                'resource_type',
                'resource_id',
                'logic_delete',
                name='uk_gateway_rate_rule',
            ),
            mysql_charset='utf8mb4',
            mysql_collate='utf8mb4_unicode_ci',
        )
        op.create_index('ix_gateway_rate_rule_tenant_id', _RATE_RULE, ['tenant_id'])
        op.create_index('ix_gateway_rate_rule_scope', _RATE_RULE, ['tenant_id', 'scope_type', 'scope_id'])

    if not table_exists(_BLOCK_RECORD):
        op.create_table(
            _BLOCK_RECORD,
            # Append-only evidence table: no logic_delete, no update_time, so a
            # tenant cannot rewrite its own safety history and the retention
            # job can hard-delete safely.
            sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column('tenant_id', sa.Integer, nullable=False, comment='租户ID'),
            sa.Column('user_id', sa.Integer, nullable=False, server_default=sa.text('0'), comment='用户ID'),
            sa.Column('face', sa.String(length=32), nullable=False, comment='拦截面'),
            sa.Column('stage', sa.String(length=16), nullable=False, comment='input/output'),
            sa.Column('resource_type', sa.String(length=32), nullable=False, server_default=sa.text("''"), comment='资源类型'),
            sa.Column('resource_id', sa.String(length=64), nullable=False, server_default=sa.text("''"), comment='资源ID'),
            sa.Column('matched_words', JsonType(), nullable=True, comment='命中词与次数，最多 50 条'),
            sa.Column('user_input', LargeText(), nullable=False, server_default=sa.text("''"), comment='输入预览，截断 500 字符'),
            sa.Column('auto_reply', sa.String(length=500), nullable=False, server_default=sa.text("''"), comment='命中提示话术'),
            sa.Column('request_id', sa.String(length=64), nullable=False, server_default=sa.text("''"), comment='trace_id'),
            sa.Column('create_time', sa.DateTime, nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            mysql_charset='utf8mb4',
            mysql_collate='utf8mb4_unicode_ci',
        )
        # The query page's two access patterns: recent activity per face, and
        # per-user history. Both are prefix-compatible with the retention
        # delete (tenant_id + create_time).
        op.create_index('ix_gateway_block_record_tenant_id', _BLOCK_RECORD, ['tenant_id'])
        op.create_index('ix_gateway_block_record_request_id', _BLOCK_RECORD, ['request_id'])
        op.create_index('idx_gateway_block_record_face', _BLOCK_RECORD, ['tenant_id', 'face', 'create_time'])
        op.create_index('idx_gateway_block_record_user', _BLOCK_RECORD, ['tenant_id', 'user_id', 'create_time'])


def downgrade() -> None:
    # Whole-batch drop is safe: only this feature reads or writes these three
    # tables, and they hold no data another object depends on.
    for table, indexes in (
        (
            _BLOCK_RECORD,
            (
                'idx_gateway_block_record_user',
                'idx_gateway_block_record_face',
                'ix_gateway_block_record_request_id',
                'ix_gateway_block_record_tenant_id',
            ),
        ),
        (_RATE_RULE, ('ix_gateway_rate_rule_scope', 'ix_gateway_rate_rule_tenant_id')),
        (_OAUTH_PROVIDER, ('ix_gateway_oauth_provider_type', 'ix_gateway_oauth_provider_tenant_id')),
    ):
        if not table_exists(table):
            continue
        for index in indexes:
            op.drop_index(index, table_name=table)
        op.drop_table(table)
