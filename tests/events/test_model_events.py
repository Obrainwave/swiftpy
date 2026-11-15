from __future__ import annotations

from typing import Any, ClassVar

import pytest

from swiftpy.database.orm.model import Model
from swiftpy.database.pool import DatabasePool
from swiftpy.events.dispatcher import EventDispatcher


class EventUser(Model):
    __table__ = "event_users"
    id: int
    name: str
    email: str = ""
    __guarded__: ClassVar[list[str]] = []


class AuditLogEntry(Model):
    __table__ = "event_audit_log_entries"
    id: int
    action: str
    metadata: str = ""
    __guarded__: ClassVar[list[str]] = []


@pytest.fixture(autouse=True)
async def setup_event_schema(ensure_db_pool: DatabasePool) -> None:
    disp = EventDispatcher()
    EventDispatcher.set_instance(disp)

    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS event_audit_log_entries, event_users CASCADE;

            CREATE TABLE event_users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE event_audit_log_entries (
                id SERIAL PRIMARY KEY,
                action TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT ''
            );
            """
        )


async def test_model_created_event_on_create() -> None:
    dispatcher = EventDispatcher.get_instance()
    dispatched_events: list[tuple[str, Any]] = []

    def on_user_created(payload: Any) -> None:
        dispatched_events.append(("event_user.created", payload))

    dispatcher.listen("event_user.created", on_user_created)

    user = await EventUser.create(name="Alice", email="alice@example.com")

    assert len(dispatched_events) == 1
    event_name, payload = dispatched_events[0]
    assert event_name == "event_user.created"
    assert isinstance(payload, EventUser)
    assert payload.id == user.id
    assert payload.name == "Alice"
    assert payload.email == "alice@example.com"


async def test_model_created_event_on_save_new() -> None:
    dispatcher = EventDispatcher.get_instance()
    dispatched_events: list[tuple[str, Any]] = []

    dispatcher.listen(
        "event_user.created", lambda p: dispatched_events.append(("created", p))
    )

    user = EventUser(name="Bob", email="bob@example.com")
    await user.save()

    assert len(dispatched_events) == 1
    action, payload = dispatched_events[0]
    assert action == "created"
    assert isinstance(payload, EventUser)
    assert payload.id is not None
    assert payload.name == "Bob"


async def test_model_updated_event_on_save_existing() -> None:
    dispatcher = EventDispatcher.get_instance()
    user = await EventUser.create(name="Charlie", email="charlie@example.com")

    updated_events: list[Any] = []
    dispatcher.listen("event_user.updated", lambda p: updated_events.append(p))

    user.name = "Charlie Updated"
    await user.save()

    assert len(updated_events) == 1
    payload = updated_events[0]
    assert isinstance(payload, EventUser)
    assert payload.id == user.id
    assert payload.name == "Charlie Updated"


async def test_model_deleted_event_on_delete() -> None:
    dispatcher = EventDispatcher.get_instance()
    user = await EventUser.create(name="Dave", email="dave@example.com")

    deleted_events: list[Any] = []
    dispatcher.listen("event_user.deleted", lambda p: deleted_events.append(p))

    await user.delete()

    assert len(deleted_events) == 1
    payload = deleted_events[0]
    assert isinstance(payload, EventUser)
    assert payload.id == user.id
    assert payload._exists is False


async def test_multiword_model_name_lifecycle_events() -> None:
    dispatcher = EventDispatcher.get_instance()
    events_log: list[str] = []

    dispatcher.listen("audit_log_entry.created", lambda p: events_log.append("created"))
    dispatcher.listen("audit_log_entry.updated", lambda p: events_log.append("updated"))
    dispatcher.listen("audit_log_entry.deleted", lambda p: events_log.append("deleted"))

    entry = await AuditLogEntry.create(action="login", metadata="IP:127.0.0.1")
    assert events_log == ["created"]

    entry.action = "logout"
    await entry.save()
    assert events_log == ["created", "updated"]

    await entry.delete()
    assert events_log == ["created", "updated", "deleted"]


async def test_wildcard_listener_captures_all_model_lifecycle_events() -> None:
    dispatcher = EventDispatcher.get_instance()
    captured: list[tuple[str, Any]] = []

    def on_any_user_event(event_name: str, payload: Any) -> None:
        captured.append((event_name, payload))

    dispatcher.listen("event_user.*", on_any_user_event)

    user = await EventUser.create(name="Eve", email="eve@example.com")
    user.email = "eve_updated@example.com"
    await user.save()
    await user.delete()

    assert len(captured) == 6
    assert [name for name, _ in captured] == [
        "event_user.creating",
        "event_user.created",
        "event_user.updating",
        "event_user.updated",
        "event_user.deleting",
        "event_user.deleted",
    ]


async def test_model_crud_without_listeners() -> None:
    # Verify ORM operations proceed seamlessly when no listeners are attached
    user = await EventUser.create(name="Frank", email="frank@example.com")
    assert user.id is not None

    user.name = "Frank Updated"
    await user.save()
    assert user.name == "Frank Updated"

    await user.delete()
    assert user._exists is False


async def test_model_failed_db_operation_emits_no_event() -> None:
    dispatcher = EventDispatcher.get_instance()
    dispatched_events: list[Any] = []
    dispatcher.listen("event_user.created", lambda p: dispatched_events.append(p))

    # Insert first user with explicit primary key 1
    await EventUser.create(id=1, name="Original", email="orig@example.com")
    assert len(dispatched_events) == 1

    # Attempt to insert second user with conflicting primary key 1
    with pytest.raises(Exception):  # noqa: B017
        await EventUser.create(id=1, name="Conflict", email="conflict@example.com")

    # The failed create operation must NOT emit any event
    assert len(dispatched_events) == 1
    assert dispatched_events[0].name == "Original"


async def test_model_save_repeatedly_transitions_created_to_updated() -> None:
    dispatcher = EventDispatcher.get_instance()
    lifecycle_log: list[str] = []

    dispatcher.listen("event_user.created", lambda p: lifecycle_log.append("created"))
    dispatcher.listen("event_user.updated", lambda p: lifecycle_log.append("updated"))

    user = EventUser(name="First", email="first@example.com")

    # First save on new instance emits created
    await user.save()
    assert lifecycle_log == ["created"]

    # Second save on existing instance emits updated
    user.name = "Second"
    await user.save()
    assert lifecycle_log == ["created", "updated"]

    # Third save on existing instance emits updated again
    user.name = "Third"
    await user.save()
    assert lifecycle_log == ["created", "updated", "updated"]


async def test_model_delete_unsaved_raises_and_emits_no_event() -> None:
    dispatcher = EventDispatcher.get_instance()
    deleted_events: list[Any] = []
    dispatcher.listen("event_user.deleted", lambda p: deleted_events.append(p))

    user = EventUser(name="Unsaved", email="unsaved@example.com")

    with pytest.raises(RuntimeError, match="Cannot delete unsaved"):
        await user.delete()

    assert len(deleted_events) == 0


async def test_model_lifecycle_listener_failure_propagates() -> None:
    dispatcher = EventDispatcher.get_instance()

    def bad_listener(p: Any) -> None:
        raise PermissionError("Listener rejected model creation")

    dispatcher.listen("event_user.creating", bad_listener)

    with pytest.raises(PermissionError, match="Listener rejected model creation"):
        await EventUser.create(name="RejectMe", email="reject@example.com")
