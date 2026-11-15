from __future__ import annotations

from typing import Any, ClassVar

import pytest

from swiftpy.database.orm.model import Model
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import db
from swiftpy.events.dispatcher import EventDispatcher


class TxEventItem(Model):
    __table__ = "test_tx_event_items"
    id: int
    name: str
    __guarded__: ClassVar[list[str]] = []


@pytest.fixture
async def setup_tx_items_table(ensure_db_pool: DatabasePool) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS test_tx_event_items CASCADE;
            CREATE TABLE test_tx_event_items (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL
            );
            """
        )


async def test_before_event_creating_aborts_insert_on_failure(
    setup_tx_items_table: Any,
) -> None:
    disp = EventDispatcher()
    EventDispatcher.set_instance(disp)

    def abort_creating(item: Any) -> None:
        raise ValueError("Cannot create item: validation error in before-hook")

    disp.listen("tx_event_item.creating", abort_creating)

    with pytest.raises(ValueError, match="validation error in before-hook"):
        await TxEventItem.create(name="Forbidden")

    # Verify no record was inserted
    count = await TxEventItem.query().count()
    assert count == 0


async def test_after_event_deferred_to_transaction_commit(
    setup_tx_items_table: Any,
) -> None:
    disp = EventDispatcher()
    EventDispatcher.set_instance(disp)

    created_events: list[str] = []
    disp.listen("tx_event_item.created", lambda item: created_events.append(item.name))

    async with db.transaction():
        item = await TxEventItem.create(name="CommittedItem")
        assert item.id is not None
        # Inside the transaction before commit, created event should NOT have fired yet!
        assert len(created_events) == 0

    # Outside the transaction after commit, created event must have fired!
    assert len(created_events) == 1
    assert created_events[0] == "CommittedItem"


async def test_after_event_discarded_on_transaction_rollback(
    setup_tx_items_table: Any,
) -> None:
    disp = EventDispatcher()
    EventDispatcher.set_instance(disp)

    created_events: list[str] = []
    disp.listen("tx_event_item.created", lambda item: created_events.append(item.name))

    try:
        async with db.transaction():
            await TxEventItem.create(name="RolledBackItem")
            # Force a rollback
            raise RuntimeError("Intentional transaction failure")
    except RuntimeError:
        pass

    # The created event must NOT be delivered because transaction was rolled back
    assert len(created_events) == 0

    # Verify record was rolled back from database
    count = await TxEventItem.query().count()
    assert count == 0


async def test_listener_exception_on_after_event_does_not_fail_committed_write(
    setup_tx_items_table: Any,
) -> None:
    disp = EventDispatcher()
    EventDispatcher.set_instance(disp)

    def failing_listener(item: Any) -> None:
        raise RuntimeError("Crash in background after-event listener")

    disp.listen("tx_event_item.created", failing_listener)

    # By default (strict_events=False), listener error on created event does not fail create()
    item = await TxEventItem.create(name="SafeWrite")
    assert item.id is not None
    assert item.name == "SafeWrite"

    # Confirm write actually exists in DB
    found = await TxEventItem.find(item.id)
    assert found is not None
    assert found.name == "SafeWrite"


async def test_strict_events_raises_listener_exception_on_after_event(
    setup_tx_items_table: Any,
) -> None:
    disp = EventDispatcher()
    EventDispatcher.set_instance(disp)

    def failing_listener(item: Any) -> None:
        raise RuntimeError("Strict mode after-event crash")

    disp.listen("tx_event_item.created", failing_listener)

    TxEventItem.strict_events = True
    try:
        with pytest.raises(RuntimeError, match="Strict mode after-event crash"):
            await TxEventItem.create(name="StrictWrite")
    finally:
        TxEventItem.strict_events = False
