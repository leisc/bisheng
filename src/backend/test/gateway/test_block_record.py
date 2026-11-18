"""F120 T023 — block-record write path, keyset page, tenant isolation, retention.

Four things are asserted, and each one guards a failure that would otherwise be
invisible:

1. **写入字段完整 + 截断** — the record is evidence, so a row that silently lost
   the matched word or kept a 900-character copy of user input is worse than no
   row at all. The truncation is the only thing standing between a hit and a
   second copy of the requester's content.
2. **分页不重不漏** — ``create_time`` is second-precision, so a page boundary
   ordered by it alone duplicates some rows while losing others. The
   ``(create_time, id)`` keyset is what makes the page deterministic.
3. **跨租户不可见** — the automatic tenant filter is the whole defence, and it
   only holds if the repository does not grow a ``tenant_id`` argument that a
   caller could pass the wrong way.
4. **90 天边界** — the retention boundary is off-by-one-prone and the job runs
   unattended, so it is pinned here rather than trusted.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from bisheng.common.cursor import CursorDecodeError
from bisheng.core.logger import trace_id_var
from bisheng.gateway.domain.models.block_record import GatewayBlockRecord
from bisheng.gateway.domain.repositories import block_record_repository as repo_mod
from bisheng.gateway.domain.repositories.block_record_repository import RETENTION_DAYS
from bisheng.gateway.domain.schemas.block_record import (
    MAX_AUTO_REPLY_CHARS,
    MAX_MATCHED_WORDS,
    MAX_PREVIEW_CHARS,
    GatewayBlockRecordInput,
)
from bisheng.gateway.domain.services.block_record_service import (
    GatewayBlockRecordService,
    GatewayBlockRecordWriter,
)

TABLE = GatewayBlockRecord.__table__

#: One shared timestamp for every seeded row, so the keyset page boundary is
#: decided by the ``id`` tie-breaker rather than by the clock.
SEED_TIME = datetime(2026, 11, 18, 12, 0, 0)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_jwt_secret(monkeypatch):
    """Keep the app import path away from the config table."""
    from bisheng.common.services import config_service as cs

    monkeypatch.setattr(cs.settings, "jwt_secret", "f120-test-secret", raising=False)


@pytest.fixture()
def engine():
    """In-memory SQLite with only ``gateway_block_record``.

    ``create_all`` over the whole metadata is not an option: unrelated tables
    (``usergroup`` and friends) have composite primary keys SQLite refuses to
    autoincrement, so the DDL fails before this table is ever reached.
    """
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(lambda sync_conn: SQLModel.metadata.create_all(sync_conn, tables=[TABLE]))

    asyncio.run(_create())
    yield eng
    asyncio.run(eng.dispose())


@pytest.fixture()
def session_factory(engine, monkeypatch):
    """Point the repository at the test engine.

    Patched on the repository module, not on ``bisheng.core.database``: the
    repository imported the name directly, so patching the source module has no
    effect on the already-bound reference.
    """

    @asynccontextmanager
    async def _session():
        async with AsyncSession(bind=engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(repo_mod, "get_async_db_session", _session)
    return _session


@pytest.fixture()
def tenant_events(monkeypatch):
    """Attach the real ``before_flush`` / ``do_orm_execute`` listeners.

    Process-global once registered — which is also why every other test in this
    package runs with ``_bypass_tenant_filter`` set. Here the opposite is
    needed: the tenant-isolation assertions are meaningless with the filter off.
    """
    from bisheng.core.database import tenant_filter

    tenant_filter._force_import_all_models()
    monkeypatch.setattr(
        tenant_filter,
        "_tenant_aware_tables",
        tenant_filter._discover_tenant_aware_tables(),
    )
    tenant_filter.register_tenant_filter_events()
    monkeypatch.setattr(tenant_filter, "is_tenant_filter_bypassed", lambda: False)
    return tenant_filter


@pytest.fixture()
def in_tenant():
    """Set ``current_tenant_id`` for the duration of one test."""
    from bisheng.core.context import tenant as tenant_ctx

    def _set(tenant_id: int):
        return tenant_ctx.current_tenant_id.set(tenant_id)

    yield _set
    # ContextVar tokens are per-test; nothing to reset because each test sets
    # its own and the context dies with the test.


@pytest.fixture()
def as_tenant(in_tenant):
    """Run the test body as tenant 7 and reset afterwards."""
    from bisheng.core.context import tenant as tenant_ctx

    token = in_tenant(7)
    yield
    tenant_ctx.current_tenant_id.reset(token)


class _CapturingWriter:
    """Records enqueued rows without a background task."""

    def __init__(self):
        self.rows: list[GatewayBlockRecord] = []

    def enqueue(self, row):
        self.rows.append(row)
        return True


def _hit(**overrides) -> GatewayBlockRecordInput:
    payload = {
        "face": "dsh_chat",
        "stage": "input",
        "tenant_id": 7,
        "user_id": 3,
        "matched_words": [{"word": "炸弹", "count": 2}],
        "user_input": "演示一下",
        "auto_reply": "该内容涉及敏感信息",
        "request_id": "trace-1",
    }
    payload.update(overrides)
    return GatewayBlockRecordInput(**payload)


# ---------------------------------------------------------------------------
# write path: fields and truncation
# ---------------------------------------------------------------------------


def test_record_block_captures_every_field():
    writer = _CapturingWriter()
    service = GatewayBlockRecordService(writer=writer)

    accepted = service.record_block(_hit())

    assert accepted is True
    assert len(writer.rows) == 1
    row = writer.rows[0]
    assert row.face == "dsh_chat"
    assert row.stage == "input"
    assert row.tenant_id == 7
    assert row.user_id == 3
    assert row.matched_words == [{"word": "炸弹", "count": 2}]
    assert row.user_input == "演示一下"
    assert row.auto_reply == "该内容涉及敏感信息"
    assert row.request_id == "trace-1"


def test_user_input_preview_is_truncated_to_500():
    """The queue is the only thing between a requester's input and a second copy
    of it, so the preview cap is enforced before enqueue, not at INSERT."""
    writer = _CapturingWriter()
    service = GatewayBlockRecordService(writer=writer)

    service.record_block(_hit(user_input="x" * 900))

    stored = writer.rows[0].user_input
    assert len(stored) == MAX_PREVIEW_CHARS == 500


def test_auto_reply_is_truncated_to_the_column_width():
    writer = _CapturingWriter()
    service = GatewayBlockRecordService(writer=writer)

    service.record_block(_hit(auto_reply="y" * 900))

    stored = writer.rows[0].auto_reply
    assert len(stored) == MAX_AUTO_REPLY_CHARS == 500


def test_matched_words_are_capped_at_50():
    writer = _CapturingWriter()
    service = GatewayBlockRecordService(writer=writer)

    service.record_block(_hit(matched_words=[{"word": f"w{i}", "count": 1} for i in range(200)]))

    stored = writer.rows[0].matched_words
    assert len(stored) == MAX_MATCHED_WORDS == 50
    # Kept the first 50, not the last: the automaton reports in input order, and
    # the first hit is the one that produced the block.
    assert stored[0]["word"] == "w0"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"word": "炸弹", "count": 3}, {"word": "炸弹", "count": 3}),
        ("炸弹", {"word": "炸弹", "count": 1}),
        ({"word": "坏词", "count": "oops"}, {"word": "坏词", "count": 1}),
        ({"word": "", "count": 5}, None),
    ],
)
def test_matched_words_accept_both_hit_shapes(raw, expected):
    """The input path has the automaton's ``hits`` (word + count); the streaming
    scanner only knows the single word it stopped on. Both must normalise to one
    stored shape."""
    writer = _CapturingWriter()
    service = GatewayBlockRecordService(writer=writer)

    service.record_block(_hit(matched_words=[raw]))

    assert writer.rows[0].matched_words == ([expected] if expected else [])


def test_request_id_falls_back_to_the_trace_id():
    """A hit point with nothing better to pass still produces a joinable row."""
    writer = _CapturingWriter()
    service = GatewayBlockRecordService(writer=writer)
    token = trace_id_var.set("trace-fallback")

    try:
        service.record_block(_hit(request_id=""))
    finally:
        trace_id_var.reset(token)

    assert writer.rows[0].request_id == "trace-fallback"


def test_request_id_is_capped_at_the_column_width():
    writer = _CapturingWriter()
    service = GatewayBlockRecordService(writer=writer)

    service.record_block(_hit(request_id="r" * 200))

    assert len(writer.rows[0].request_id) == 64


@pytest.mark.parametrize("face,stage", [("nope", "input"), ("dsh_chat", "nope")])
def test_unknown_face_or_stage_is_rejected(face, stage):
    """The ORM columns are plain strings; without this check a typo at a hit
    point lands a row no query filter can ever select."""
    # model_construct skips validation, which is the only way a bad value
    # reaches the guard at all — pydantic's Literal rejects it first.
    broken = GatewayBlockRecordInput.model_construct(
        face=face, stage=stage, tenant_id=7, matched_words=[], user_input="", auto_reply="", request_id=""
    )

    # Asserted on ``_build_row``, not ``record_block``: the latter swallows the
    # raise on purpose (a bad row must not fail the user's request), so testing
    # it here would assert the wrong contract.
    with pytest.raises(ValueError, match="must be one of"):
        GatewayBlockRecordService._build_row(broken)


def test_full_queue_reports_the_drop_and_does_not_raise():
    """A full queue must not fail the request that hit — the user still gets
    their blocked answer, and the caller is told the evidence was dropped."""

    class _FullWriter:
        def enqueue(self, row):
            return False

    service = GatewayBlockRecordService(writer=_FullWriter())

    assert service.record_block(_hit()) is False


def test_build_failure_never_propagates_to_the_caller():
    """Losing one audit row is acceptable; losing the user's blocked answer is
    not. ``model_construct`` skips validation, so a bad row is reachable."""
    writer = _CapturingWriter()
    service = GatewayBlockRecordService(writer=writer)
    broken = GatewayBlockRecordInput.model_construct(
        face="dsh_chat", stage="input", tenant_id=7, resource_type=None
    )

    assert service.record_block(broken) is False
    assert writer.rows == []


def test_missing_writer_drops_rather_than_raising():
    service = GatewayBlockRecordService(writer=None)

    assert service.record_block(_hit()) is False


# ---------------------------------------------------------------------------
# write path: the real writer lands the batch
# ---------------------------------------------------------------------------


async def test_writer_batch_insert_lands_rows(session_factory):
    """End-to-end through the shared ``BatchedRecordWriter``: enqueue on the
    request path, flush from the flusher, rows readable with no ContextVar set
    (the flusher is a background task, so the tenant filter must be bypassed
    there or a multi-tenant batch writes nothing)."""
    writer = GatewayBlockRecordWriter()
    service = GatewayBlockRecordService(writer=writer)

    service.record_block(_hit(tenant_id=7, request_id="a"))
    service.record_block(_hit(tenant_id=8, request_id="b", face="open_api_chat"))
    assert service.record_block(_hit(tenant_id=7, request_id="c")) is True

    flushed = await writer.flush_now()
    assert flushed == 3

    async with session_factory() as session:
        rows = list((await session.exec(repo_mod.select(GatewayBlockRecord))).all())
    assert len(rows) == 3
    assert {row.request_id for row in rows} == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# read path: keyset pagination
# ---------------------------------------------------------------------------


async def _seed(session, *, tenant_id: int, count: int, face: str = "dsh_chat") -> list[GatewayBlockRecord]:
    """Insert rows with explicit tenant ids.

    Wrapped in ``bypass_tenant_filter`` so the ``before_flush`` auto-fill does
    not overwrite them: the point of the cross-tenant test is two tenants'
    rows in one table, and the listener would otherwise stamp every row with
    whatever tenant the seeding context happens to carry.
    """
    from bisheng.core.context.tenant import bypass_tenant_filter

    rows = []
    with bypass_tenant_filter():
        for index in range(count):
            row = GatewayBlockRecord(
                tenant_id=tenant_id,
                user_id=index % 2,
                face=face,
                stage="input",
                matched_words=[{"word": f"w{index}", "count": 1}],
                user_input=f"input-{index}",
                auto_reply="blocked",
                request_id=f"r-{tenant_id}-{index}",
                # Explicit, and identical for every row: SQLAlchemy does not
                # apply the column's server_default on INSERT, and a NULL sort
                # key makes the keyset cursor unable to advance at all. Sharing
                # one timestamp is also the hard case — it forces the ``id``
                # tie-breaker to do the work.
                create_time=SEED_TIME,
            )
            session.add(row)
            rows.append(row)
        await session.commit()
    return rows


async def test_keyset_page_walks_every_row_exactly_once(engine, session_factory, tenant_events, as_tenant):
    """Ordered by ``(create_time DESC, id DESC)``. Every row here shares one
    ``create_time`` (second-precision, all inserted in the same statement), so
    this is precisely the case a ``create_time``-only order gets wrong."""
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        await _seed(session, tenant_id=7, count=7)

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = await GatewayBlockRecordService.list_blocks(page_size=3, cursor=cursor)
        seen.extend(item.request_id for item in page.data)
        pages += 1
        cursor = page.next_cursor
        if not page.has_more or pages > 10:
            break

    assert len(seen) == 7
    assert len(set(seen)) == 7, "a row was delivered twice across a page boundary"


async def test_first_page_reports_no_cursor(engine, session_factory, tenant_events, as_tenant):
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        await _seed(session, tenant_id=7, count=2)

    page = await GatewayBlockRecordService.list_blocks(page_size=50)

    assert len(page.data) == 2
    assert page.next_cursor is None
    assert page.has_more is False


async def test_page_is_newest_first(engine, session_factory, tenant_events, as_tenant):
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        await _seed(session, tenant_id=7, count=4)

    page = await GatewayBlockRecordService.list_blocks(page_size=2)

    assert [item.request_id for item in page.data] == ["r-7-3", "r-7-2"]


@pytest.mark.parametrize(
    "filters,expected",
    [
        ({"face": "dsh_chat"}, 5),
        ({"face": "open_api_chat"}, 0),
        ({"stage": "input"}, 5),
        ({"stage": "output"}, 0),
        ({"user_id": 0}, 3),
        ({"user_id": 1}, 2),
    ],
)
async def test_filters_narrow_the_page(engine, session_factory, tenant_events, as_tenant, filters, expected):
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        await _seed(session, tenant_id=7, count=5)

    page = await GatewayBlockRecordService.list_blocks(page_size=50, **filters)

    assert len(page.data) == expected


async def test_cross_tenant_rows_are_invisible(engine, session_factory, tenant_events, as_tenant):
    """The whole tenant defence is the automatic filter. It holds only because
    the repository takes no ``tenant_id`` argument — there is nothing for a
    caller to pass the wrong way."""
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        await _seed(session, tenant_id=7, count=2)
        await _seed(session, tenant_id=8, count=3)

    page = await GatewayBlockRecordService.list_blocks(page_size=50)

    assert len(page.data) == 2
    assert {item.tenant_id for item in page.data} == {7}


async def test_page_size_is_clamped_to_the_ceiling(engine, session_factory, tenant_events, as_tenant):
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        await _seed(session, tenant_id=7, count=1)

    page = await GatewayBlockRecordService.list_blocks(page_size=10_000)

    assert page.page_size <= 200


async def test_malformed_cursor_raises_for_the_endpoint_to_translate(engine, session_factory, tenant_events, as_tenant):
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        await _seed(session, tenant_id=7, count=1)

    with pytest.raises(CursorDecodeError):
        await GatewayBlockRecordService.list_blocks(cursor="not-a-cursor")


async def test_cursor_survives_the_json_round_trip(engine, session_factory, tenant_events, as_tenant):
    """``encode_cursor`` serialises the datetime sort key to an ISO string, and
    a decoded cursor hands that string back. MySQL and DM8 compare it to the
    DateTime column by implicit cast; SQLite compares it as text and matches
    nothing, so page two silently returns page one forever. Rehydration in the
    repository is what keeps this dialect-independent."""
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        await _seed(session, tenant_id=7, count=6)

    page1 = await GatewayBlockRecordService.list_blocks(page_size=3)
    page2 = await GatewayBlockRecordService.list_blocks(page_size=3, cursor=page1.next_cursor)

    first = {item.request_id for item in page1.data}
    second = {item.request_id for item in page2.data}
    assert first and second
    assert not (first & second), "page two repeated page one — the cursor did not advance"
    assert first | second == {f"r-7-{i}" for i in range(6)}


async def test_cursor_holding_a_non_datetime_sort_key_is_refused(engine, session_factory, tenant_events, as_tenant):
    """A hand-edited cursor must fail loudly (26609), not silently scroll."""
    from bisheng.common.cursor import encode_cursor

    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        await _seed(session, tenant_id=7, count=1)

    forged = encode_cursor(["not-a-datetime", 1], context="gateway_block_record")

    with pytest.raises(CursorDecodeError):
        await GatewayBlockRecordService.list_blocks(cursor=forged)


async def test_cursor_from_a_different_sort_context_is_refused(engine, session_factory, tenant_events, as_tenant):
    """A cursor minted under another sort order must not silently scroll to the
    wrong place — the context signature is what makes that a loud failure."""
    from bisheng.common.cursor import encode_cursor

    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        await _seed(session, tenant_id=7, count=3)

    foreign = encode_cursor([datetime(2026, 1, 1), 1], context="some_other_list")

    with pytest.raises(CursorDecodeError):
        await GatewayBlockRecordService.list_blocks(cursor=foreign)


# ---------------------------------------------------------------------------
# retention boundary
# ---------------------------------------------------------------------------


def test_retention_deletes_only_rows_older_than_the_cutoff(monkeypatch):
    """The beat job runs unattended, so the boundary is pinned rather than
    trusted: strictly-older is deleted, exactly-at-the-boundary survives.

    Driven on a sync engine because that is what the Celery task will use — a
    retention job that needs an event loop is a retention job that cannot run in
    a prefork worker without one.
    """
    from sqlmodel import Session, create_engine

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[TABLE])

    @contextmanager
    def _sync_session():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(repo_mod, "get_sync_db_session", _sync_session)

    cutoff = datetime(2026, 11, 18, 12, 0, 0)
    with _sync_session() as session:
        for label, create_time in (
            # one second past the cutoff — the smallest deletable age
            ("old", cutoff - timedelta(seconds=1)),
            # exactly at the cutoff, and one second inside it
            ("boundary", cutoff),
            ("fresh", cutoff + timedelta(seconds=1)),
        ):
            session.add(
                GatewayBlockRecord(
                    tenant_id=7,
                    face="dsh_chat",
                    stage="input",
                    create_time=create_time,
                    request_id=label,
                )
            )
        session.commit()

    deleted = repo_mod.GatewayBlockRecordRepository.delete_records_before_sync(7, cutoff)

    assert deleted == 1
    with _sync_session() as session:
        assert {row.request_id for row in session.exec(repo_mod.select(GatewayBlockRecord)).all()} == {
            "boundary",
            "fresh",
        }
    engine.dispose()


def test_retention_is_scoped_to_the_given_tenant(monkeypatch):
    """One tenant's retention run must not empty another tenant's evidence."""
    from sqlmodel import Session, create_engine

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[TABLE])

    @contextmanager
    def _sync_session():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(repo_mod, "get_sync_db_session", _sync_session)

    cutoff = datetime(2026, 11, 18, 12, 0, 0)
    with _sync_session() as session:
        for tenant_id in (7, 8):
            session.add(
                GatewayBlockRecord(
                    tenant_id=tenant_id,
                    face="dsh_chat",
                    stage="input",
                    create_time=cutoff - timedelta(days=RETENTION_DAYS + 1),
                    request_id=f"t{tenant_id}",
                )
            )
        session.commit()

    deleted = repo_mod.GatewayBlockRecordRepository.delete_records_before_sync(7, cutoff)

    assert deleted == 1
    with _sync_session() as session:
        assert {row.request_id for row in session.exec(repo_mod.select(GatewayBlockRecord)).all()} == {"t8"}
    engine.dispose()
