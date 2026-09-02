from app.http.middleware.test_middleware import TestMiddleware
from swiftpy import Application
from swiftpy import Request
from swiftpy import Response, json_response

app = Application()


@app.get("/ping")
async def ping(request: Request) -> Response:
    return json_response({"message": "pong"})


@app.get("/show-user")
async def show_user(request: Request) -> Response:
    user = {"name": "John Doe", "age": 30, "email": "john.doe@example.com"}
    return json_response({"user": user})


@app.group("/api/v1", middleware=[TestMiddleware])
def v1_routes(group):

    @group.get("/users/{user_id}")
    async def show_users(request: Request, user_id: int) -> Response:
        return json_response(
            {
                "user_id": user_id,
                "middleware_ran": request.state["middleware_ran"],
                "ok": True,
            }
        )
