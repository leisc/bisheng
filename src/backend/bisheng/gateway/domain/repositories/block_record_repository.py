from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlmodel import select

from bisheng.core.database import get_async_db_session, get_sync_db_session
from bisheng.gateway.domain.models.block_record import GatewayBlockRecord

#: Retention window for block records (design §5.4). Kept here rather than in
#: settings so the migration, the beat task and the query page cannot disagree.
RETENTION_DAYS = 90


class GatewayBlockRecordRepository:
    """Append-only storage for content-safety rejections.

    Async for the request path, sync for the Celery retention task — the
    retention job deletes by tenant and date, which is exactly the shape of the
    ``(tenant_id, face, create_time)`` index.
    """

    # ------------------------------------------------------------------
    # Request path (async)
    # ------------------------------------------------------------------

    @classmethod
    async def append_record(cls, row: GatewayBlockRecord) -> GatewayBlockRecord:
        async with get_async_db_session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    @classmethod
    async def get_record(cls, record_id: int) -> Optional[GatewayBlockRecord]:
        async with get_async_db_session() as session:
            return await session.get(GatewayBlockRecord, record_id)

    @classmethod
    async def list_records(
        cls,
        tenant_id: int,
        face: Optional[str] = None,
        user_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[GatewayBlockRecord]:
        statement = select(GatewayBlockRecord).where(GatewayBlockRecord.tenant_id == tenant_id)
        if face:
            statement = statement.where(GatewayBlockRecord.face == face)
        if user_id is not None:
            statement = statement.where(GatewayBlockRecord.user_id == user_id)
        statement = statement.order_by(GatewayBlockRecord.id.desc()).limit(limit)
        async with get_async_db_session() as session:
            return list((await session.exec(statement)).all())

    @classmethod
    async def delete_records_before(cls, tenant_id: int, cutoff: datetime) -> int:
        async with get_async_db_session() as session:
            statement = select(GatewayBlockRecord).where(
                GatewayBlockRecord.tenant_id == tenant_id,
                GatewayBlockRecord.create_time < cutoff,
            )
            rows = list((await session.exec(statement)).all())
            for row in rows:
                await session.delete(row)
            await session.commit()
        return len(rows)

    # ------------------------------------------------------------------
    # Celery path (sync)
    # ------------------------------------------------------------------

    @classmethod
    def delete_records_before_sync(cls, tenant_id: int, cutoff: datetime) -> int:
        """Same contract as :meth:`delete_records_before`, for the beat task.

        Sync on purpose: Celery workers here run sync SQLAlchemy sessions, and
        an ``asyncio.run`` inside a task is how event-loop leaks start.
        """
        with get_sync_db_session() as session:
            statement = select(GatewayBlockRecord).where(
                GatewayBlockRecord.tenant_id == tenant_id,
                GatewayBlockRecord.create_time < cutoff,
            )
            rows = list(session.exec(statement).all())
            for row in rows:
                session.delete(row)
            session.commit()
        return len(rows)
