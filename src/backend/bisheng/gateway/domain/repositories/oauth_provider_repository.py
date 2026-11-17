from __future__ import annotations

from typing import Any, Dict, List

from sqlmodel import select

from bisheng.core.database import get_async_db_session
from bisheng.gateway.domain.models.oauth_provider import GatewayOAuthProvider

#: Config keys whose values must never reach a browser or a log line.
_SECRET_CONFIG_KEYS = frozenset(
    {
        'client_secret',
        'corp_secret',
        'bind_password',
        'token',
        'access_token',
        'refresh_token',
    }
)
_REDACTED = '***'


class GatewayOAuthProviderRepository:
    """Read/write access to OAuth provider configuration.

    Every query filters on ``tenant_id`` explicitly. The tenant-filter event
    listener (``core/database/tenant_filter.py``) also injects a predicate once
    the module is registered there (tasks T061/T062), but a repository that
    relies on the listener alone is one refactor away from leaking another
    tenant's OAuth config.
    """

    @classmethod
    async def create_provider(cls, row: GatewayOAuthProvider) -> GatewayOAuthProvider:
        async with get_async_db_session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    @classmethod
    async def get_provider(cls, provider_id: int) -> GatewayOAuthProvider | None:
        """A live provider only — a soft-deleted row is invisible here."""
        statement = select(GatewayOAuthProvider).where(
            GatewayOAuthProvider.id == provider_id,
            GatewayOAuthProvider.logic_delete == 0,
        )
        async with get_async_db_session() as session:
            return (await session.exec(statement)).first()

    @classmethod
    async def get_provider_by_type(
        cls,
        tenant_id: int,
        provider_type: str,
    ) -> GatewayOAuthProvider | None:
        statement = select(GatewayOAuthProvider).where(
            GatewayOAuthProvider.tenant_id == tenant_id,
            GatewayOAuthProvider.provider_type == provider_type,
            GatewayOAuthProvider.logic_delete == 0,
        )
        async with get_async_db_session() as session:
            return (await session.exec(statement)).first()

    @classmethod
    async def list_providers(cls, tenant_id: int) -> List[GatewayOAuthProvider]:
        statement = (
            select(GatewayOAuthProvider)
            .where(
                GatewayOAuthProvider.tenant_id == tenant_id,
                GatewayOAuthProvider.logic_delete == 0,
            )
            .order_by(GatewayOAuthProvider.id.desc())
        )
        async with get_async_db_session() as session:
            return list((await session.exec(statement)).all())

    @classmethod
    async def update_provider(cls, row: GatewayOAuthProvider) -> GatewayOAuthProvider:
        async with get_async_db_session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    @classmethod
    async def delete_provider(cls, provider_id: int) -> bool:
        """Soft delete. Returns False when the row was already deleted or gone."""
        async with get_async_db_session() as session:
            row = await session.get(GatewayOAuthProvider, provider_id)
            if row is None or row.logic_delete != 0:
                return False
            row.logic_delete = 1
            session.add(row)
            await session.commit()
        return True

    # ------------------------------------------------------------------
    # C6: secrets must not travel to the browser
    # ------------------------------------------------------------------

    @classmethod
    def redact_config(cls, row: GatewayOAuthProvider) -> Dict[str, Any]:
        """A copy of ``config`` with every secret key masked.

        The management face returns this instead of the raw row; the login flow
        reads the raw row in-process and decrypts there.
        """
        config = row.config or {}
        return {
            key: (_REDACTED if key in _SECRET_CONFIG_KEYS and value else value) for key, value in config.items()
        }
