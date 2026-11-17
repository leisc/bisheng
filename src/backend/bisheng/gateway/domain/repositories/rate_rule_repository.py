from __future__ import annotations

from typing import List, Sequence, Tuple

from sqlmodel import select

from bisheng.core.database import get_async_db_session
from bisheng.gateway.domain.models.rate_rule import (
    GatewayRateRule,
    GatewayRateResourceType,
    GatewayRateScopeType,
)


class GatewayRateRuleRepository:
    """Rate-rule storage and resolution.

    ``resolve_rule`` takes the caller's scope candidates **already ordered most
    specific first** (the middleware builds them from the request's user, groups
    and department) and returns the first rule that matches. Doing the ordering
    in SQL with ``CASE`` would be cleverer and much harder to read; the
    candidate list is at most four entries, so four indexed lookups are cheaper
    than the confusion.
    """

    @classmethod
    async def create_rule(cls, row: GatewayRateRule) -> GatewayRateRule:
        async with get_async_db_session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    @classmethod
    async def get_rule(cls, rule_id: int) -> GatewayRateRule | None:
        async with get_async_db_session() as session:
            return await session.get(GatewayRateRule, rule_id)

    @classmethod
    async def list_rules(cls, tenant_id: int) -> List[GatewayRateRule]:
        statement = (
            select(GatewayRateRule)
            .where(
                GatewayRateRule.tenant_id == tenant_id,
                GatewayRateRule.logic_delete == 0,
            )
            .order_by(GatewayRateRule.id.desc())
        )
        async with get_async_db_session() as session:
            return list((await session.exec(statement)).all())

    @classmethod
    async def update_rule(cls, row: GatewayRateRule) -> GatewayRateRule:
        async with get_async_db_session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    @classmethod
    async def delete_rule(cls, rule_id: int) -> bool:
        async with get_async_db_session() as session:
            row = await session.get(GatewayRateRule, rule_id)
            if row is None or row.logic_delete != 0:
                return False
            row.logic_delete = 1
            session.add(row)
            await session.commit()
        return True

    @classmethod
    async def resolve_rule(
        cls,
        tenant_id: int,
        scope_candidates: Sequence[Tuple[str, str]],
        resource_type: str,
        resource_id: str = '',
    ) -> GatewayRateRule | None:
        """First matching rule in ``scope_candidates`` order, or ``None``.

        A candidate matches when a live, enabled rule exists for that scope and
        either targets this exact resource or targets the whole resource type
        (``resource_id == ''``). The specific-resource rule wins over the
        type-wide one within the same scope.
        """
        if not scope_candidates:
            return None

        scope_pairs = list(scope_candidates)
        statement = select(GatewayRateRule).where(
            GatewayRateRule.tenant_id == tenant_id,
            GatewayRateRule.logic_delete == 0,
            GatewayRateRule.enabled.is_(True),
            GatewayRateRule.resource_type == resource_type,
            GatewayRateRule.scope_id.in_([scope_id for _, scope_id in scope_pairs]),
        )
        async with get_async_db_session() as session:
            rows = list((await session.exec(statement)).all())

        by_scope = {(row.scope_type, row.scope_id): row for row in rows}
        for scope_type, scope_id in scope_pairs:
            candidates = [
                row
                for row in rows
                if row.scope_type == scope_type and row.scope_id == scope_id
            ]
            if not candidates:
                continue
            exact = [row for row in candidates if row.resource_id == resource_id and resource_id]
            if exact:
                return exact[0]
            type_wide = [row for row in candidates if row.resource_id == '']
            if type_wide:
                return type_wide[0]
        return None

    @classmethod
    async def list_concurrent_limits(
        cls,
        tenant_id: int,
        scope_candidates: Sequence[Tuple[str, str]],
    ) -> List[GatewayRateRule]:
        """Rules that set a WS concurrency ceiling, most specific first."""
        pairs = list(scope_candidates)
        statement = select(GatewayRateRule).where(
            GatewayRateRule.tenant_id == tenant_id,
            GatewayRateRule.logic_delete == 0,
            GatewayRateRule.enabled.is_(True),
            GatewayRateRule.concurrent_limit > 0,
            GatewayRateRule.scope_id.in_([scope_id for _, scope_id in pairs]),
        )
        async with get_async_db_session() as session:
            rows = list((await session.exec(statement)).all())
        return [row for scope_type, scope_id in pairs for row in rows if (row.scope_type, row.scope_id) == (scope_type, scope_id)]

    @classmethod
    def default_candidates(cls, user_id: int, department_key: str | None, group_ids: Sequence[int]) -> List[Tuple[str, str]]:
        """Build the ordered candidate list the middleware resolves against."""
        candidates: List[Tuple[str, str]] = [(GatewayRateScopeType.USER, str(user_id))]
        candidates.extend((GatewayRateScopeType.USER_GROUP, str(group_id)) for group_id in group_ids or [])
        if department_key:
            candidates.append((GatewayRateScopeType.DEPARTMENT, department_key))
        candidates.append((GatewayRateScopeType.GLOBAL, ''))
        return candidates

    @classmethod
    def is_known_resource_type(cls, resource_type: str) -> bool:
        return resource_type in GatewayRateResourceType.ALL
