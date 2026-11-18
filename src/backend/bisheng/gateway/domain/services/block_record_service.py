"""Block-record write-behind and query service (F120 T024, design §5.4).

A content-safety rejection is evidence, and the request that produced it is
still in flight — so the row must never be written inline. Everything here
follows that constraint:

* the hit point calls :meth:`GatewayBlockRecordService.record_block`, which
  truncates and enqueues, and returns. No ``await`` on a database session, so a
  slow or dead database cannot turn a safety hit into a user-visible failure;
* one background flusher owns the INSERT (``BatchedRecordWriter``, shared with
  the Open API audit path);
* a full queue or a failed batch is logged at error level and emits a metric,
  because a silently dropped row is worse than a missing one.

The truncation happens on the request path, **before** enqueue, for a reason
that is easy to get wrong: the queue is the only thing standing between the
requester's input and a second copy of it. ``user_input`` is a 500-character
preview and nothing longer ever enters the queue.
"""

from __future__ import annotations

from loguru import logger

from bisheng.common.services.metric_log import emit_metric
from bisheng.core.logger import trace_id_var
from bisheng.gateway.domain.models.block_record import GatewayBlockFace, GatewayBlockRecord, GatewayBlockStage
from bisheng.gateway.domain.repositories.block_record_repository import GatewayBlockRecordRepository
from bisheng.gateway.domain.schemas.block_record import (
    MAX_AUTO_REPLY_CHARS,
    MAX_MATCHED_WORDS,
    MAX_PREVIEW_CHARS,
    GatewayBlockRecordInput,
    GatewayBlockRecordItem,
    GatewayBlockRecordPage,
)
from bisheng.open_api.domain.services.batched_writer import BatchedRecordWriter

RECORD_QUEUE_MAX_SIZE = 5000
RECORD_BATCH_SIZE = 200
RECORD_FLUSH_INTERVAL_SECONDS = 1.0

#: Page-size ceiling for the query endpoint. The list is scrolled, so a caller
#: asking for 10 000 rows is a mistake, not a use case.
MAX_PAGE_SIZE = 200


class GatewayBlockRecordWriter(BatchedRecordWriter[GatewayBlockRecord]):
    """Flusher for ``gateway_block_record``.

    Own writer, own table: the Open API audit page and the model-call page are
    separate writers for exactly this reason — one chatty face must not flood
    the queue that another face's evidence depends on.
    """

    def __init__(
        self,
        *,
        max_queue_size: int = RECORD_QUEUE_MAX_SIZE,
        batch_size: int = RECORD_BATCH_SIZE,
        flush_interval_seconds: float = RECORD_FLUSH_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(
            name='gateway-block-record-flusher',
            write_batch=self._write_record_batch,
            max_queue_size=max_queue_size,
            batch_size=batch_size,
            flush_interval_seconds=flush_interval_seconds,
            log_event='gateway.block_record.write_failed',
        )

    async def _write_record_batch(self, batch: list[GatewayBlockRecord]) -> None:
        await GatewayBlockRecordRepository.ainsert_batch(batch)
        emit_metric('gateway_block_record', status='written', batch_size=len(batch))

    def _on_drop(self, count: int) -> None:
        emit_metric('gateway_block_record', status='dropped', batch_size=count)


class GatewayBlockRecordService:
    """Write path (truncate + enqueue) and read path (keyset page)."""

    def __init__(self, writer: GatewayBlockRecordWriter | None = None) -> None:
        # A writer may be injected so tests can assert on the queue without
        # starting a background task; production passes the module singleton.
        self._writer = writer

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record_block(self, hit: GatewayBlockRecordInput) -> bool:
        """Truncate, enqueue, return. Never raises, never awaits a database.

        Returns ``False`` only when the queue is full — the caller is told the
        evidence was dropped rather than being left to assume it landed. Every
        failure mode is logged by the writer itself, so this method stays
        silent on success and loud on drop.
        """
        try:
            row = self._build_row(hit)
        except Exception:
            # A malformed hit must not take down the request that produced it.
            # Losing one audit row is acceptable; losing the user's answer is not.
            logger.opt(exception=True).warning(
                'gateway.block_record.build_failed | face={} stage={}',
                hit.face,
                hit.stage,
            )
            emit_metric('gateway_block_record', status='build_failed')
            return False
        if self._writer is None:
            emit_metric('gateway_block_record', status='dropped', batch_size=1)
            return False
        return self._writer.enqueue(row)

    @classmethod
    def _build_row(cls, hit: GatewayBlockRecordInput) -> GatewayBlockRecord:
        face = cls._validate_enum('face', hit.face, GatewayBlockFace.ALL)
        stage = cls._validate_enum('stage', hit.stage, GatewayBlockStage.ALL)
        return GatewayBlockRecord(
            tenant_id=hit.tenant_id,
            user_id=hit.user_id,
            face=face,
            stage=stage,
            resource_type=hit.resource_type[:32],
            resource_id=hit.resource_id[:64],
            matched_words=cls._normalize_matched_words(hit.matched_words),
            user_input=cls._truncate(hit.user_input, MAX_PREVIEW_CHARS),
            auto_reply=cls._truncate(hit.auto_reply, MAX_AUTO_REPLY_CHARS),
            # Read here, on the request path: the flusher runs later in a task
            # with no ContextVar, and the correlation must survive that handoff.
            request_id=cls._request_id(hit.request_id),
        )

    @staticmethod
    def _validate_enum(field: str, value: str, allowed: tuple[str, ...]) -> str:
        if value not in allowed:
            raise ValueError(f'{field} must be one of {allowed}, got {value!r}')
        return value

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if not text:
            return ''
        if len(text) <= limit:
            return text
        return text[:limit]

    @classmethod
    def _normalize_matched_words(cls, matched: list) -> list[dict]:
        """Accept ``SensitiveWordHit``-shaped dicts or bare strings.

        The input path has the automaton's ``hits`` (word + count); the
        streaming scanner only knows the single word it stopped on. Normalising
        here keeps both call sites trivial and keeps one stored shape.
        """
        normalized: list[dict] = []
        for entry in matched[:MAX_MATCHED_WORDS]:
            if isinstance(entry, str):
                word, count = entry, 1
            elif isinstance(entry, dict):
                word = str(entry.get('word', ''))
                try:
                    count = int(entry.get('count', 1))
                except (TypeError, ValueError):
                    count = 1
            else:
                word = str(getattr(entry, 'word', ''))
                count = int(getattr(entry, 'count', 1))
            if not word:
                continue
            normalized.append({'word': word, 'count': max(count, 1)})
        return normalized

    @staticmethod
    def _request_id(explicit: str) -> str:
        if explicit:
            return explicit[:64]
        # The trace id is the request's own correlation id; falling back to it
        # means a hit point that has nothing better to pass still produces a
        # joinable row.
        return (trace_id_var.get() or '')[:64]

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    @classmethod
    async def list_blocks(
        cls,
        *,
        face: str | None = None,
        stage: str | None = None,
        user_id: int | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> GatewayBlockRecordPage:
        """One keyset page, newest first.

        Raises:
            CursorDecodeError: the cursor is malformed or belongs to a different
                sort context. The endpoint translates it into a stable business
                error rather than a 500.
        """
        size = max(1, min(page_size, MAX_PAGE_SIZE))
        rows, next_cursor = await GatewayBlockRecordRepository.alist_blocks(
            face=face,
            stage=stage,
            user_id=user_id,
            cursor=cursor,
            limit=size,
        )
        return GatewayBlockRecordPage(
            data=[cls._to_item(row) for row in rows],
            page_size=size,
            has_more=next_cursor is not None,
            next_cursor=next_cursor,
        )

    @staticmethod
    def _to_item(row: GatewayBlockRecord) -> GatewayBlockRecordItem:
        return GatewayBlockRecordItem(
            id=row.id or 0,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            face=row.face,  # type: ignore[arg-type]
            stage=row.stage,  # type: ignore[arg-type]
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            matched_words=list(row.matched_words or []),
            user_input=row.user_input,
            auto_reply=row.auto_reply,
            request_id=row.request_id,
            create_time=row.create_time,  # type: ignore[arg-type]
        )


block_record_writer = GatewayBlockRecordWriter()

# The singleton hit points call. ``record_block`` is an instance method because
# it needs the writer, and there is exactly one writer in the process (the one
# ``main.py`` starts), so there is exactly one service — same shape as
# ``model_call_record_writer``. Tests that want to watch the queue construct
# their own ``GatewayBlockRecordService(writer=...)`` instead of reaching for
# this one.
gateway_block_record_service = GatewayBlockRecordService(writer=block_record_writer)


__all__ = [
    'MAX_PAGE_SIZE',
    'RECORD_BATCH_SIZE',
    'RECORD_FLUSH_INTERVAL_SECONDS',
    'RECORD_QUEUE_MAX_SIZE',
    'GatewayBlockRecordService',
    'GatewayBlockRecordWriter',
    'block_record_writer',
    'gateway_block_record_service',
]
