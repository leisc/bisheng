"""Append-only storage for content-safety rejections (F120 T007/T024)."""

from __future__ import annotations

from datetime import datetime

from sqlmodel import col, select

from bisheng.common.cursor import CursorDecodeError, decode_cursor, encode_cursor
from bisheng.core.context.tenant import bypass_tenant_filter
from bisheng.core.database import get_async_db_session, get_sync_db_session
from bisheng.database.utils.keyset import build_keyset_where
from bisheng.gateway.domain.models.block_record import GatewayBlockRecord

#: Retention window for block records (design §5.4). Kept here rather than in
#: settings so the migration, the beat task and the query page cannot disagree.
RETENTION_DAYS = 90

#: Sort-context signature for the cursor. Changing the sort order (or adding a
#: filter that moves rows across the page boundary) must invalidate outstanding
#: cursors instead of silently scrolling to the wrong place — hence the same
#: signature is passed to both encode and decode.
_CURSOR_CONTEXT = 'gateway_block_record'
_CURSOR_KEY_LEN = 2


def _coerce_cursor_datetime(value: object) -> datetime:
    """Rehydrate a decoded cursor sort key into a ``datetime``.

    ``encode_cursor`` JSON-serialises datetimes to ISO strings, so a decoded
    cursor hands back a *string* where the sort column is a ``DateTime``.
    MySQL and DM8 compare the two by implicit cast — which is why the F027
    knowledge lists pass the raw string straight through — but SQLite compares
    them as text and matches nothing at all, so page two silently returns page
    one. Rehydrating here keeps the keyset correct on every dialect rather than
    relying on a cast only some of them perform.

    Raises:
        CursorDecodeError: when the value is not an ISO datetime, so the API
            layer answers 26609 instead of a 500.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise CursorDecodeError(f'cursor sort key is not an ISO datetime: {value!r}') from exc
    raise CursorDecodeError(f'cursor sort key is not a datetime: {type(value).__name__}')


class GatewayBlockRecordRepository:
    """Two access paths, deliberately asymmetric.

    * the **request path** is async and batched — the flusher owns the only
      INSERT, and a hit point never waits on a database round trip;
    * the **retention path** is sync, because Celery workers here run sync
      SQLAlchemy sessions and an ``asyncio.run`` inside a task is how event-loop
      leaks start.
    """

    # ------------------------------------------------------------------
    # Request path (async)
    # ------------------------------------------------------------------

    @classmethod
    async def ainsert_batch(cls, rows: list[GatewayBlockRecord]) -> None:
        """One ``INSERT`` per batch, from the write-behind flusher.

        Runs with the tenant filter bypassed: the flusher is a background task
        with no request ContextVar, and a single batch legitimately spans tenants
        (the queue is shared). Every row already carries an explicit
        ``tenant_id`` — the request path filled it in, and the ``before_flush``
        auto-fill would write nothing at all on a multi-tenant deployment.
        """
        if not rows:
            return
        with bypass_tenant_filter():
            async with get_async_db_session() as session:
                session.add_all(rows)
                await session.commit()

    @classmethod
    async def get_record(cls, record_id: int) -> GatewayBlockRecord | None:
        async with get_async_db_session() as session:
            return await session.get(GatewayBlockRecord, record_id)

    @classmethod
    async def alist_blocks(
        cls,
        *,
        face: str | None = None,
        stage: str | None = None,
        user_id: int | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[GatewayBlockRecord], str | None]:
        """One page newest-first, plus the cursor for the next one.

        Ordered by ``(create_time DESC, id DESC)``: ``create_time`` is
        second-precision, so rows written inside the same second are otherwise
        unordered and a page boundary duplicates some rows while losing others.

        **No ``tenant_id`` argument on purpose.** The automatic tenant filter
        injects the caller's tenant from the request ContextVar, so a tenant
        administrator's query is narrowed without them having to pass anything,
        and an omitted argument cannot become a wider read. The platform
        super-administrator's cross-tenant view is the endpoint's concern, not
        the repository's — see ``block_record.py``.
        """
        statement = select(GatewayBlockRecord)
        if face:
            statement = statement.where(GatewayBlockRecord.face == face)
        if stage:
            statement = statement.where(GatewayBlockRecord.stage == stage)
        if user_id is not None:
            statement = statement.where(GatewayBlockRecord.user_id == user_id)
        if cursor:
            decoded = decode_cursor(
                cursor,
                expected_key_len=_CURSOR_KEY_LEN,
                expected_context=_CURSOR_CONTEXT,
            )
            if decoded is not None:
                cursor_time = _coerce_cursor_datetime(decoded[0])
                cursor_id = decoded[1]
                statement = statement.where(
                    build_keyset_where(
                        (col(GatewayBlockRecord.create_time), col(GatewayBlockRecord.id)),
                        (cursor_time, cursor_id),
                        descending=True,
                    )
                )
        statement = statement.order_by(
            col(GatewayBlockRecord.create_time).desc(),
            col(GatewayBlockRecord.id).desc(),
        ).limit(limit)

        async with get_async_db_session() as session:
            rows = list((await session.exec(statement)).all())
        next_cursor = None
        if len(rows) == limit and rows:
            last = rows[-1]
            if last.create_time is not None and last.id is not None:
                next_cursor = encode_cursor(
                    [last.create_time, last.id],
                    context=_CURSOR_CONTEXT,
                )
        return rows, next_cursor

    # ------------------------------------------------------------------
    # Celery path (sync)
    # ------------------------------------------------------------------

    @classmethod
    def delete_records_before_sync(cls, tenant_id: int, cutoff: datetime) -> int:
        """Hard-delete rows older than ``cutoff`` for one tenant.

        Deletes by tenant and date, which is exactly the shape of the
        ``(tenant_id, face, create_time)`` index — the retention job stays cheap
        even on a table with millions of rows.

        A hard delete is safe here and nowhere else: the model carries no
        ``logic_delete``, and nothing else references these rows.
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
