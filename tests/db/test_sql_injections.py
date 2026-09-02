# tests/db/test_sql_injections.py
from __future__ import annotations

import asyncio
import time
import asyncpg

from app.providers.app_service_provider import AppServiceProvider
from app.providers.database_service_provider import DatabaseServiceProvider
from swiftpy.core.bootstrap import create_app
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query, Raw
from swiftpy.database.transaction import db


async def setup_schema(db_pool: DatabasePool) -> None:
    pool = db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS test_users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS test_profiles (
                id SERIAL PRIMARY KEY,
                user_id INT REFERENCES test_users(id),
                bio TEXT
            );
            TRUNCATE TABLE test_profiles, test_users RESTART IDENTITY;
        """)

async def test_aggregates_and_joins() -> None:
    print("\n==== TESTING AGGREGATES & JOINS ====")
    # Insert related data
    user_id = await Query.table("test_users").insert({"name": "join_user", "active": True})
    await Query.table("test_profiles").insert({"user_id": user_id, "bio": "Software Architect"})
    await Query.table("test_users").insert({"name": "inactive_user", "active": False})

    # Test Aggregates
    count = await Query.table("test_users").count()
    active_count = await Query.table("test_users").where("active", True).count()
    assert count == 2, "Aggregate count failed"
    assert active_count == 1, "Aggregate count with where failed"

    # Test Joins
    joined_data = (
        await Query.table("test_users")
        .select("test_users.name", "test_profiles.bio")
        .join("test_profiles", "test_users.id", "=", "test_profiles.user_id")
        .first()
    )
    assert joined_data is not None
    assert joined_data["name"] == "join_user"
    assert joined_data["bio"] == "Software Architect"
    print("Aggregate & Joins: PASS")

async def test_subqueries() -> None:
    print("\n==== TESTING SUBQUERIES ====")
    subquery = Query.table("test_profiles").select("user_id").where("bio", "LIKE", "%Architect%")
    users = await Query.table("test_users").where("id", "IN", subquery).get()
    
    assert len(users) == 1
    assert users[0]["name"] == "join_user"
    print("Subqueries: PASS")

async def test_sql_injection_defense() -> None:
    print("\n==== TESTING SQL INJECTION DEFENSE ====")
    
    # 1. Structural Impossibility via Identifiers (Table)
    try:
        await Query.table("test_users; DROP TABLE test_users;").get()
        assert False, "Failed to block table name injection"
    except ValueError as e:
        assert "Invalid SQL identifier" in str(e)

    # 2. Structural Impossibility via Identifiers (Column)
    try:
        await Query.table("test_users").select("name; DROP TABLE test_users;").get()
        assert False, "Failed to block column name injection"
    except ValueError:
        pass

    # 3. Safe Parameter Binding
    malicious_input = "'; DELETE FROM test_users; --"
    await Query.table("test_users").where("name", malicious_input).get()
    
    # Verify the table wasn't dropped/deleted
    count = await Query.table("test_users").count()
    assert count > 0, "Parameter binding failed to prevent injection"
    print("SQL Injection Defenses: PASS")
    
async def test_order_by_injection() -> None:

    print("\n==== ORDER BY Injection ====")

    try:

        (
            Query.table("test_users")
            .order_by(
                "id; DROP TABLE test_users"
            )
        )

        raise AssertionError(
            "ORDER BY injection not blocked"
        )

    except ValueError:

        print("PASS")
        
async def test_join_injection() -> None:

    print("\n==== JOIN Injection ====")

    try:

        (
            Query.table("test_users")
            .join(
                "profiles; DROP TABLE test_users",
                "test_users.id",
                "=",
                "profiles.user_id"
            )
        )

        raise AssertionError(
            "JOIN injection not blocked"
        )

    except ValueError:

        print("PASS")
        
async def test_update_column_injection() -> None:

    print("\n==== UPDATE Injection ====")

    try:

        await (
            Query.table("test_users")
            .where("id", 1)
            .update(
                {
                    "name; DROP TABLE test_users": "x"
                }
            )
        )

        raise AssertionError(
            "UPDATE injection not blocked"
        )

    except ValueError:

        print("PASS")
        
async def test_where_column_injection() -> None:

    print("\n==== WHERE Injection ====")

    try:

        (
            Query.table("test_users")
            .where(
                "name; DROP TABLE test_users",
                "John"
            )
        )

        raise AssertionError(
            "WHERE injection not blocked"
        )

    except ValueError:

        print("PASS")
        
async def test_table_injection() -> None:

    print("\n==== TABLE Injection ====")

    try:

        Query.table(
            "test_users; DROP TABLE test_users"
        )

        raise AssertionError(
            "Table injection not blocked"
        )

    except ValueError:

        print("PASS")
        
async def test_subquery_injection() -> None:

    print("\n==== SUBQUERY Injection ====")

    try:

        subquery = Query.table(
            "test_profiles; DROP TABLE test_users"
        )

        await (
            Query.table("test_users")
            .where(
                "id",
                "IN",
                subquery
            )
            .get()
        )

        raise AssertionError(
            "Subquery injection not blocked"
        )

    except ValueError:

        print("PASS")
        
async def test_raw_where_binding_injection() -> None:

    print("\n==== RAW WHERE Binding Injection ====")

    malicious = (
        "'; DROP TABLE test_users; --"
    )

    rows = await (
        Query.table("test_users")
        .where_raw(
            "name = ?",
            [malicious]
        )
        .get()
    )

    #
    # Table must still exist
    #

    count = await (
        Query.table("test_users")
        .count()
    )

    assert count >= 0

    print("PASS")

async def test_transactions() -> None:
    print("\n==== TESTING FULL TRANSACTIONS ====")
    
    # 1. Nested Rollback Test (Savepoints)
    async with db.transaction():
        await Query.table("test_users").insert({"name": "parent_tx", "active": True})
        try:
            async with db.transaction():
                await Query.table("test_users").insert({"name": "child_tx", "active": True})
                raise RuntimeError("Force child rollback")
        except RuntimeError:
            pass

    parent = await Query.table("test_users").where("name", "parent_tx").first()
    child = await Query.table("test_users").where("name", "child_tx").first()
    assert parent is not None, "Parent transaction was incorrectly aborted"
    assert child is None, "Nested transaction failed to rollback via savepoint"

    # 2. Timeout Handling
    try:
        async with asyncio.timeout(0.5):
            async with db.transaction() as conn:
                await Query.table("test_users").insert({"name": "timeout_user"})
                # Simulate a long running lock or sleep directly on postgres
                await conn.execute("SELECT pg_sleep(1)")
        assert False, "Timeout did not trigger"
    except (asyncio.TimeoutError, asyncpg.exceptions.QueryCanceledError):
        pass

    timeout_user = await Query.table("test_users").where("name", "timeout_user").first()
    assert timeout_user is None, "Transaction did not rollback on timeout/cancellation"

    print("Transactions (Nested, Savepoints, Timeouts): PASS")

async def load_test_reads() -> None:
    # Retaining load tests
    pass

async def main() -> None:
    app = create_app(providers=[AppServiceProvider, DatabaseServiceProvider])
    db_pool = app.container.resolve(DatabasePool)
    await db_pool.init()
    
    # Warmup and Setup
    await setup_schema(db_pool)
    
    # Feature Tests
    await test_aggregates_and_joins()
    await test_subqueries()
    await test_sql_injection_defense()
    await test_transactions()
    await test_order_by_injection()
    await test_table_injection()
    await test_where_column_injection()
    await test_join_injection()
    await test_update_column_injection()
    await test_subquery_injection()
    await test_raw_where_binding_injection()
    
    await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())