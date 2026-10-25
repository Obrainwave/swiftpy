from __future__ import annotations

import asyncio
from typing import Any

import pytest

from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.core.application import Application
from swiftpy.core.exceptions import UnauthorizedError
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient


@pytest.mark.asyncio
async def test_100_simultaneous_authenticated_requests_no_state_bleed() -> None:
    """
    Stress-test concurrency: 100 simultaneous authenticated requests executing
    in parallel across distinct users. Verifies that all 100 requests succeed (HTTP 200)
    and that zero context or identity bleed occurs across concurrent asyncio tasks.
    """
    app = Application()
    issuer = JWTIssuer(secret="concurrency-test-secret-key-32-bytes!!")
    client = TestClient(app)

    @app.get("/user/profile")
    async def get_profile(request: Request) -> Response:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise UnauthorizedError("Missing or invalid Authorization header")

        token = auth_header[7:]
        payload = issuer.decode_token(token)

        # Micro-yield to force event loop scheduling interleaving
        await asyncio.sleep(0.005)

        return json_response(
            {
                "user_id": int(payload["sub"]),
                "email": payload.get("email"),
                "tenant": payload.get("tenant"),
            }
        )

    # 1. Create 10 distinct users and generate valid JWT tokens for each
    users: list[dict[str, Any]] = []
    for uid in range(1, 11):
        token_pair = issuer.issue_token_pair(
            subject=str(uid),
            claims={
                "email": f"user_{uid}@example.com",
                "tenant": f"tenant_{uid % 3}",
            },
        )
        users.append(
            {
                "user_id": uid,
                "email": f"user_{uid}@example.com",
                "tenant": f"tenant_{uid % 3}",
                "token": token_pair["access_token"],
            }
        )

    # 2. Build 100 concurrent request tasks (10 requests per user, interleaved)
    expected_results: list[dict[str, Any]] = []
    tasks = []

    for i in range(100):
        user_info = users[i % len(users)]
        expected_results.append(user_info)
        task = client.with_token(user_info["token"]).get("/user/profile")
        tasks.append(task)

    # 3. Fire all 100 requests concurrently
    responses = await asyncio.gather(*tasks)

    # 4. Strict assertions: all 100 must succeed and match expected user with ZERO state bleed
    assert len(responses) == 100

    for i, res in enumerate(responses):
        expected = expected_results[i]

        res.assert_ok()
        data = res.json()

        assert data["user_id"] == expected["user_id"], (
            f"State bleed detected at request {i}: expected user_id {expected['user_id']} but got {data['user_id']}"
        )
        assert data["email"] == expected["email"], (
            f"State bleed detected at request {i}: expected email {expected['email']} but got {data['email']}"
        )
        assert data["tenant"] == expected["tenant"], (
            f"State bleed detected at request {i}: expected tenant {expected['tenant']} but got {data['tenant']}"
        )


@pytest.mark.asyncio
async def test_100_concurrent_requests_through_framework_auth_pipeline() -> None:
    """
    Verify complete task-scoped context isolation under 100 concurrent requests
    routed through SwiftPY's real JWTGuard middleware, AuthManager, current_user(),
    and container-resolved CurrentUser dependency injection.
    """
    from swiftpy.auth.auth_manager import AuthManager, CurrentUser, current_user
    from swiftpy.auth.jwt_guard import JWTGuard
    from swiftpy.http.router import RouteGroup

    app = Application()
    issuer = JWTIssuer(secret="full-pipeline-concurrency-secret-key-32b!!")
    app.container.instance(JWTIssuer, issuer)

    # Register AuthManager and CurrentUser scoped DI binding
    auth_mgr = AuthManager(app.container)
    auth_mgr.register_container_bindings(app.container)

    # Protect route using JWTGuard middleware on RouteGroup
    @app.group("/api", middleware=[JWTGuard])
    def api_routes(g: RouteGroup) -> None:
        @g.get("/identity")
        async def identity_handler(request: Request) -> Response:
            # 1. Resolve current user from SwiftPY Context helper
            context_user = current_user()
            assert context_user is not None

            # 2. Resolve scoped CurrentUser directly from the DI container
            scoped_user = app.container.resolve(CurrentUser)
            assert scoped_user.is_authenticated

            # Micro-yield to force asyncio scheduler interleaving
            await asyncio.sleep(0.005)

            # Both context and DI-resolved identity must strictly match
            assert str(context_user["sub"]) == str(scoped_user.id)

            return json_response(
                {
                    "user_id": int(scoped_user.id),
                    "email": scoped_user.email,
                    "tenant": scoped_user.get("tenant"),
                    "role": scoped_user.get("role"),
                }
            )

    client = TestClient(app)

    # Create 10 distinct user identities
    users: list[dict[str, Any]] = []
    for uid in range(1, 11):
        token_pair = issuer.issue_token_pair(
            subject=str(uid),
            claims={
                "email": f"developer_{uid}@enterprise.org",
                "tenant": f"org_{uid % 4}",
                "role": "admin" if uid % 2 == 0 else "member",
            },
        )
        users.append(
            {
                "user_id": uid,
                "email": f"developer_{uid}@enterprise.org",
                "tenant": f"org_{uid % 4}",
                "role": "admin" if uid % 2 == 0 else "member",
                "token": token_pair["access_token"],
            }
        )

    # Build 100 interleaved concurrent requests
    expected_results: list[dict[str, Any]] = []
    tasks = []

    for i in range(100):
        user_info = users[i % len(users)]
        expected_results.append(user_info)
        task = client.with_token(user_info["token"]).get("/api/identity")
        tasks.append(task)

    responses = await asyncio.gather(*tasks)
    assert len(responses) == 100

    for i, res in enumerate(responses):
        expected = expected_results[i]
        res.assert_ok()
        data = res.json()

        assert data["user_id"] == expected["user_id"], (
            f"State bleed at request {i}: expected user_id {expected['user_id']} but got {data['user_id']}"
        )
        assert data["email"] == expected["email"], (
            f"State bleed at request {i}: expected email {expected['email']} but got {data['email']}"
        )
        assert data["tenant"] == expected["tenant"], (
            f"State bleed at request {i}: expected tenant {expected['tenant']} but got {data['tenant']}"
        )
        assert data["role"] == expected["role"], (
            f"State bleed at request {i}: expected role {expected['role']} but got {data['role']}"
        )
