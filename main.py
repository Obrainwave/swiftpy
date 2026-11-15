from __future__ import annotations

from app.http.middleware.test_middleware import TestMiddleware
from routes.api import router as api_router
from swiftpy import Application, Request, Response, RouteGroup, json_response
from swiftpy.admin import admin

app = Application()

# Mount default API routes under /api
app.include_router(api_router, prefix="/api")

# Mount admin panel
admin.mount(app, prefix="/admin")


@app.get("/")
async def root(request: Request) -> Response:
    return json_response(
        {
            "message": "Welcome to SwiftPY!",
            "framework": "SwiftPY",
            "version": app.version,
            "status": "online",
            "endpoints": {
                "ping": "/ping",
                "show_user": "/show-user",
                "api_user": "/api/v1/users/1",
                "admin": "/admin",
                "admin_login": "/admin/login",
            },
        }
    )


@app.get("/ping")
async def ping(request: Request) -> Response:
    return json_response({"message": "pong"})


@app.get("/show-user")
async def show_user(request: Request) -> Response:
    user = {"name": "John Doe", "age": 30, "email": "john.doe@example.com"}
    return json_response({"user": user})


@app.group("/api/v1", middleware=[TestMiddleware])
def v1_routes(group: RouteGroup) -> None:

    @group.get("/users/{user_id}")
    async def show_users(request: Request, user_id: int) -> Response:
        return json_response(
            {
                "user_id": user_id,
                "middleware_ran": request.state["middleware_ran"],
                "ok": True,
            }
        )
