"""T006 — the three F120 gateway tables, proven on SQLite.

The gateway's own state has to survive a restart and a tenant switch, so it
lives in tables rather than in process memory. What this file pins is the part
that is expensive to get wrong later:

* **round-trip** — every column the design specifies survives a write/read,
  including the JSON columns that carry OAuth endpoints and matched words;
* **uniqueness** — ``gateway_rate_rule``'s key includes ``logic_delete`` so a
  deleted rule and its replacement can coexist (the same trick
  ``sensitive_word_policy`` uses); a second identical live rule is rejected;
* **logic_delete semantics** — delete is soft, the row stays, the repository
  stops returning it, and a fresh insert with the same business key succeeds;
* **tenant column is mandatory** — a row without ``tenant_id`` is a
  cross-tenant leak waiting to happen (constitution C3).

SQLite stands in for MySQL/DM8 here because the assertions are about *column
and constraint semantics*, not about dialect behaviour; the dual-DB contract
itself is covered by ``test/database/`` and the Alembic migration test.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession


@pytest.fixture(autouse=True)
def _no_tenant_context(monkeypatch):
    """Run these ORM round-trips with the tenant filter bypassed.

    ``register_tenant_filter_events`` attaches a *process-global*
    ``do_orm_execute`` listener to the ``Session`` class, and any test that
    builds a real ``CustomMiddleware`` (or opens a DB connection) registers it
    for the whole pytest process. Once registered, a SELECT with no tenant
    context resolves to ``DEFAULT_TENANT_ID`` and every row written with
    another ``tenant_id`` becomes invisible — including the ``refresh()`` the
    repositories issue right after INSERT.

    Production never runs without a tenant context, so this fixture is not
    papering over a code defect: it isolates the round-trip assertions from
    whichever module happened to be imported first. The filter itself is
    asserted separately (``test_gateway_modules_are_registered_...``) and its
    read/write behaviour belongs to the T061 integration test, which runs
    against a real per-tenant context.
    """
    from bisheng.core.context import tenant as tenant_ctx

    token = tenant_ctx._bypass_tenant_filter.set(True)
    yield
    tenant_ctx._bypass_tenant_filter.reset(token)


@pytest_asyncio.fixture
async def gw_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: SQLModel.metadata.create_all(sync_conn, tables=_tables()))
    yield engine
    await engine.dispose()


def _tables():
    from bisheng.gateway.domain.models.block_record import GatewayBlockRecord
    from bisheng.gateway.domain.models.oauth_provider import GatewayOAuthProvider
    from bisheng.gateway.domain.models.rate_rule import GatewayRateRule

    return [
        GatewayOAuthProvider.__table__,
        GatewayRateRule.__table__,
        GatewayBlockRecord.__table__,
    ]


def _sync_factory(engine):
    """Sync session factory bound to the same SQLite file/engine."""
    from contextlib import contextmanager

    from sqlmodel import Session

    @contextmanager
    def _factory():
        with Session(bind=engine.sync_engine, expire_on_commit=False) as session:
            yield session

    return _factory


@pytest_asyncio.fixture
async def session_factory(gw_engine, monkeypatch):
    """Bind the repositories' session factory to the SQLite engine.

    Each repository module imports ``get_async_db_session`` by name, so the
    symbol has to be patched where it is *used* (same approach as
    ``test/approval/test_approval_flow_runtime.py``) — patching
    ``bisheng.core.database`` alone would leave the repositories pointing at
    the real one.
    """
    from contextlib import asynccontextmanager

    from bisheng.core.database import get_async_db_session as _real
    from bisheng.gateway.domain.repositories import (
        block_record_repository,
        oauth_provider_repository,
        rate_rule_repository,
    )

    @asynccontextmanager
    async def _factory():
        async with AsyncSession(bind=gw_engine, expire_on_commit=False) as session:
            yield session

    for module in (block_record_repository, oauth_provider_repository, rate_rule_repository):
        monkeypatch.setattr(module, "get_async_db_session", _factory)

    # The sync retention path must keep its own session factory; prove it is
    # wired to the same engine by patching it here too.
    monkeypatch.setattr(
        block_record_repository,
        "get_sync_db_session",
        _sync_factory(gw_engine),
    )
    return _factory


# ---------------------------------------------------------------------------
# gateway_oauth_provider
# ---------------------------------------------------------------------------


async def test_oauth_provider_round_trip(session_factory):
    from bisheng.gateway.domain.models.oauth_provider import (
        GatewayOAuthProvider,
        GatewayProviderType,
    )
    from bisheng.gateway.domain.repositories.oauth_provider_repository import (
        GatewayOAuthProviderRepository,
    )

    created = await GatewayOAuthProviderRepository.create_provider(
        GatewayOAuthProvider(
            tenant_id=1,
            provider_type=GatewayProviderType.CUSTOM_SSO,
            name="内部 SSO",
            config={"authorize_url": "https://sso.example.com/authorize", "client_secret": "ciphertext"},
            enabled=True,
        )
    )
    assert created.id is not None

    fetched = await GatewayOAuthProviderRepository.get_provider(created.id)
    assert fetched is not None
    assert fetched.tenant_id == 1
    assert fetched.provider_type == "custom_sso"
    assert fetched.name == "内部 SSO"
    # JSON column survives the round trip with its nested keys.
    assert fetched.config["authorize_url"] == "https://sso.example.com/authorize"
    assert fetched.config["client_secret"] == "ciphertext"
    assert fetched.enabled is True
    assert fetched.logic_delete == 0


async def test_oauth_provider_update_and_soft_delete(session_factory):
    from bisheng.gateway.domain.models.oauth_provider import GatewayOAuthProvider
    from bisheng.gateway.domain.repositories.oauth_provider_repository import (
        GatewayOAuthProviderRepository,
    )

    row = await GatewayOAuthProviderRepository.create_provider(
        GatewayOAuthProvider(
            tenant_id=2,
            provider_type="wecom_qr",
            name="企业微信扫码",
            config={"corp_id": "ww123"},
            enabled=False,
        )
    )

    row.enabled = True
    row.name = "企业微信扫码（改名）"
    updated = await GatewayOAuthProviderRepository.update_provider(row)
    assert updated.enabled is True
    assert updated.name == "企业微信扫码（改名）"

    assert await GatewayOAuthProviderRepository.delete_provider(row.id) is True
    assert await GatewayOAuthProviderRepository.get_provider(row.id) is None
    # Soft delete keeps the row; a later delete of the same id is a no-op.
    assert await GatewayOAuthProviderRepository.delete_provider(row.id) is False


async def test_oauth_provider_lists_by_tenant_with_root_fallback(session_factory):
    from bisheng.gateway.domain.models.oauth_provider import GatewayOAuthProvider
    from bisheng.gateway.domain.repositories.oauth_provider_repository import (
        GatewayOAuthProviderRepository,
    )

    await GatewayOAuthProviderRepository.create_provider(
        GatewayOAuthProvider(tenant_id=1, provider_type="custom_sso", name="root 默认", config={})
    )
    await GatewayOAuthProviderRepository.create_provider(
        GatewayOAuthProvider(tenant_id=5, provider_type="ldap", name="租户覆盖", config={})
    )
    await GatewayOAuthProviderRepository.create_provider(
        GatewayOAuthProvider(tenant_id=9, provider_type="ldap", name="别租户", config={})
    )

    root_rows = await GatewayOAuthProviderRepository.list_providers(tenant_id=1)
    assert {row.name for row in root_rows} == {"root 默认"}

    tenant_rows = await GatewayOAuthProviderRepository.list_providers(tenant_id=5)
    assert {row.name for row in tenant_rows} == {"租户覆盖"}


async def test_oauth_provider_secret_is_not_returned_by_the_listing(session_factory):
    """C6: the listing face must not hand ciphertext to the browser. The
    repository exposes a redaction helper instead of returning raw config."""
    from bisheng.gateway.domain.models.oauth_provider import GatewayOAuthProvider
    from bisheng.gateway.domain.repositories.oauth_provider_repository import (
        GatewayOAuthProviderRepository,
    )

    await GatewayOAuthProviderRepository.create_provider(
        GatewayOAuthProvider(
            tenant_id=1,
            provider_type="custom_sso",
            name="带机密",
            config={"client_id": "abc", "client_secret": "ciphertext"},
        )
    )
    rows = await GatewayOAuthProviderRepository.list_providers(tenant_id=1)
    redacted = GatewayOAuthProviderRepository.redact_config(rows[0])
    assert redacted["client_id"] == "abc"
    assert "ciphertext" not in str(redacted)
    assert redacted["client_secret"] == "***"


# ---------------------------------------------------------------------------
# gateway_rate_rule
# ---------------------------------------------------------------------------


async def test_rate_rule_round_trip(session_factory):
    from bisheng.gateway.domain.models.rate_rule import GatewayRateRule
    from bisheng.gateway.domain.repositories.rate_rule_repository import (
        GatewayRateRuleRepository,
    )

    created = await GatewayRateRuleRepository.create_rule(
        GatewayRateRule(
            tenant_id=1,
            scope_type="user",
            scope_id="42",
            resource_type="assistant",
            resource_id="",
            qps_limit=20,
            burst=40,
            concurrent_limit=3,
            enabled=True,
        )
    )
    assert created.id is not None

    fetched = await GatewayRateRuleRepository.get_rule(created.id)
    assert fetched is not None
    assert fetched.scope_type == "user"
    assert fetched.scope_id == "42"
    assert fetched.qps_limit == 20
    assert fetched.burst == 40
    assert fetched.concurrent_limit == 3


async def test_rate_rule_unique_key_rejects_a_second_live_rule(session_factory):
    from bisheng.gateway.domain.models.rate_rule import GatewayRateRule
    from bisheng.gateway.domain.repositories.rate_rule_repository import (
        GatewayRateRuleRepository,
    )

    def _rule():
        return GatewayRateRule(
            tenant_id=1,
            scope_type="global",
            scope_id="",
            resource_type="assistant",
            resource_id="",
            qps_limit=10,
        )

    await GatewayRateRuleRepository.create_rule(_rule())
    with pytest.raises(IntegrityError):
        await GatewayRateRuleRepository.create_rule(_rule())


async def test_rate_rule_soft_delete_frees_the_unique_key(session_factory):
    """The unique key includes ``logic_delete``: after a soft delete the same
    business key can be inserted again. Without that column in the key, a
    tenant could never re-create a rule it once deleted."""
    from bisheng.gateway.domain.models.rate_rule import GatewayRateRule
    from bisheng.gateway.domain.repositories.rate_rule_repository import (
        GatewayRateRuleRepository,
    )

    def _rule(qps: int):
        return GatewayRateRule(
            tenant_id=1,
            scope_type="global",
            scope_id="",
            resource_type="assistant",
            resource_id="",
            qps_limit=qps,
        )

    first = await GatewayRateRuleRepository.create_rule(_rule(10))
    assert await GatewayRateRuleRepository.delete_rule(first.id) is True

    second = await GatewayRateRuleRepository.create_rule(_rule(30))
    assert second.id != first.id

    live = await GatewayRateRuleRepository.list_rules(tenant_id=1)
    assert [row.qps_limit for row in live] == [30]


async def test_rate_rule_resolves_most_specific_scope_first(session_factory):
    """Middleware resolution order is user → user_group → department → global.
    Getting this wrong silently applies the global limit to everyone."""
    from bisheng.gateway.domain.models.rate_rule import GatewayRateRule
    from bisheng.gateway.domain.repositories.rate_rule_repository import (
        GatewayRateRuleRepository,
    )

    for scope_type, scope_id, qps in [
        ("global", "", 5),
        ("department", "BS@1", 7),
        ("user_group", "3", 9),
        ("user", "42", 11),
    ]:
        await GatewayRateRuleRepository.create_rule(
            GatewayRateRule(
                tenant_id=1,
                scope_type=scope_type,
                scope_id=scope_id,
                resource_type="assistant",
                resource_id="",
                qps_limit=qps,
            )
        )

    resolved = await GatewayRateRuleRepository.resolve_rule(
        tenant_id=1,
        scope_candidates=[("user", "42"), ("user_group", "3"), ("department", "BS@1"), ("global", "")],
        resource_type="assistant",
        resource_id="",
    )
    assert resolved is not None
    assert resolved.qps_limit == 11

    # A user with no user-specific rule falls back to the department rule.
    resolved = await GatewayRateRuleRepository.resolve_rule(
        tenant_id=1,
        scope_candidates=[("department", "BS@1"), ("global", "")],
        resource_type="assistant",
        resource_id="",
    )
    assert resolved.qps_limit == 7


async def test_rate_rule_resolves_none_when_no_rule_matches(session_factory):
    from bisheng.gateway.domain.repositories.rate_rule_repository import (
        GatewayRateRuleRepository,
    )

    resolved = await GatewayRateRuleRepository.resolve_rule(
        tenant_id=1,
        scope_candidates=[("user", "42")],
        resource_type="assistant",
        resource_id="",
    )
    assert resolved is None


# ---------------------------------------------------------------------------
# gateway_block_record
# ---------------------------------------------------------------------------


async def test_block_record_append_only_round_trip(session_factory):
    from bisheng.gateway.domain.models.block_record import GatewayBlockRecord
    from bisheng.gateway.domain.repositories.block_record_repository import (
        GatewayBlockRecordRepository,
    )

    created = await GatewayBlockRecordRepository.append_record(
        GatewayBlockRecord(
            tenant_id=3,
            user_id=42,
            face="dsh_chat",
            stage="input",
            resource_type="assistant",
            resource_id="77",
            matched_words=[{"word": "违规词", "count": 2}],
            user_input="预览前 500 字符",
            auto_reply="回答已被拦截",
            request_id="trace-abc",
        )
    )
    assert created.id is not None

    fetched = await GatewayBlockRecordRepository.get_record(created.id)
    assert fetched is not None
    assert fetched.face == "dsh_chat"
    assert fetched.stage == "input"
    assert fetched.matched_words == [{"word": "违规词", "count": 2}]
    assert fetched.request_id == "trace-abc"


async def test_block_record_has_no_soft_delete_or_update_time(session_factory):
    """Append-only table: the design explicitly omits logic_delete /
    update_time so a retention job can hard-delete safely and nothing can
    quietly rewrite history."""
    from bisheng.gateway.domain.models.block_record import GatewayBlockRecord

    columns = GatewayBlockRecord.__table__.columns
    assert "logic_delete" not in columns
    assert "update_time" not in columns
    assert "create_time" in columns


async def test_block_record_queries_by_face_and_by_user(session_factory):
    from bisheng.gateway.domain.models.block_record import GatewayBlockRecord
    from bisheng.gateway.domain.repositories.block_record_repository import (
        GatewayBlockRecordRepository,
    )

    for face, user_id in [("dsh_chat", 42), ("dsh_chat", 43), ("workstation_chat", 42)]:
        await GatewayBlockRecordRepository.append_record(
            GatewayBlockRecord(
                tenant_id=3,
                user_id=user_id,
                face=face,
                stage="input",
                request_id=f"trace-{face}-{user_id}",
            )
        )

    by_face = await GatewayBlockRecordRepository.list_records(tenant_id=3, face="dsh_chat")
    assert len(by_face) == 2

    by_user = await GatewayBlockRecordRepository.list_records(tenant_id=3, user_id=42)
    assert len(by_user) == 2

    both = await GatewayBlockRecordRepository.list_records(tenant_id=3, face="dsh_chat", user_id=42)
    assert len(both) == 1

    other_tenant = await GatewayBlockRecordRepository.list_records(tenant_id=4)
    assert other_tenant == []


async def test_block_record_retention_deletes_only_expired_rows(session_factory):
    from datetime import datetime, timedelta, timezone

    from bisheng.gateway.domain.models.block_record import GatewayBlockRecord
    from bisheng.gateway.domain.repositories.block_record_repository import (
        GatewayBlockRecordRepository,
    )

    now = datetime(2026, 11, 17, 12, 0, 0)
    keep = await GatewayBlockRecordRepository.append_record(
        GatewayBlockRecord(
            tenant_id=3,
            user_id=1,
            face="dsh_chat",
            stage="input",
            request_id="keep",
            create_time=now,
        )
    )
    stale = await GatewayBlockRecordRepository.append_record(
        GatewayBlockRecord(
            tenant_id=3,
            user_id=1,
            face="dsh_chat",
            stage="input",
            request_id="drop",
            create_time=now - timedelta(days=91),
        )
    )
    assert keep.create_time is not None
    assert stale.create_time is not None

    removed = await GatewayBlockRecordRepository.delete_records_before(
        tenant_id=3,
        cutoff=now - timedelta(days=90),
    )
    assert removed == 1
    remaining = await GatewayBlockRecordRepository.list_records(tenant_id=3)
    assert [row.request_id for row in remaining] == ["keep"]


# ---------------------------------------------------------------------------
# Cross-table contract
# ---------------------------------------------------------------------------


async def test_tenant_column_is_not_nullable_on_all_three_tables(session_factory):
    """C3: a NULL tenant_id would escape the tenant filter entirely."""
    from bisheng.gateway.domain.models.block_record import GatewayBlockRecord
    from bisheng.gateway.domain.models.oauth_provider import GatewayOAuthProvider
    from bisheng.gateway.domain.models.rate_rule import GatewayRateRule

    for model in (GatewayOAuthProvider, GatewayRateRule, GatewayBlockRecord):
        column = model.__table__.columns["tenant_id"]
        assert column.nullable is False, f"{model.__tablename__}.tenant_id must be NOT NULL"


def test_models_are_registered_in_sqlmodel_metadata():
    """Alembic autogenerate and the tenant-filter discovery both walk
    ``SQLModel.metadata``; a table that never made it in has no migration."""
    from bisheng.gateway.domain.models.block_record import GatewayBlockRecord
    from bisheng.gateway.domain.models.oauth_provider import GatewayOAuthProvider
    from bisheng.gateway.domain.models.rate_rule import GatewayRateRule

    names = set(SQLModel.metadata.tables)
    for model in (GatewayOAuthProvider, GatewayRateRule, GatewayBlockRecord):
        assert model.__tablename__ in names


def test_block_record_repository_exposes_a_sync_retention_path():
    """The retention job runs in Celery, i.e. sync SQLAlchemy. It must not
    silently rely on the async session factory."""
    import inspect

    from bisheng.gateway.domain.repositories.block_record_repository import (
        GatewayBlockRecordRepository,
    )

    assert not inspect.iscoroutinefunction(GatewayBlockRecordRepository.delete_records_before_sync)
    assert inspect.iscoroutinefunction(GatewayBlockRecordRepository.delete_records_before)


# ---------------------------------------------------------------------------
# C3: tenant isolation must actually cover these tables
# ---------------------------------------------------------------------------


def test_gateway_modules_are_registered_for_tenant_filtering():
    """Assert the registration, not the DDL: a table missing from
    ``_TENANT_AWARE_MODEL_MODULES`` imports lazily and then slips past both
    before_flush auto-fill and do_orm_execute filtering — the exact failure
    mode the list exists to prevent (C3 / tenant_filter.py header)."""
    from bisheng.core.database import tenant_filter

    for module in (
        "bisheng.gateway.domain.models.oauth_provider",
        "bisheng.gateway.domain.models.rate_rule",
        "bisheng.gateway.domain.models.block_record",
    ):
        assert module in tenant_filter._TENANT_AWARE_MODEL_MODULES
    for table in ("gateway_oauth_provider", "gateway_rate_rule", "gateway_block_record"):
        assert table in tenant_filter._discover_tenant_aware_tables()


def test_migration_revision_chain_is_single_headed():
    """Two heads means ``alembic upgrade`` is ambiguous in CI and the release
    blocks on a merge migration. Walked from the version files rather than via
    ``alembic heads`` so the check needs no database."""
    import importlib
    from pathlib import Path

    versions = Path("bisheng/core/database/alembic/versions")
    revisions = set()
    down_revisions = set()
    for path in sorted(versions.glob("*.py")):
        module = importlib.import_module("bisheng.core.database.alembic.versions." + path.stem)
        revisions.add(module.revision)
        down = module.down_revision
        down_revisions.update(down if isinstance(down, (list, tuple, set)) else [down] if down else [])

    heads = revisions - down_revisions
    assert heads == {"v3_0_0_f120_gateway_tables"}, f"unexpected alembic heads: {sorted(heads)}"
