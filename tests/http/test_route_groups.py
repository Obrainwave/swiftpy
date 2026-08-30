from app.http.middleware.test_middleware import TestMiddleware
from swiftpy.core.application import Application
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response

app = Application()


@app.group(
    "/api/v1",
    middleware=[TestMiddleware],
)
def v1_routes(group):

    @group.get("/users/{user_id}")
    async def show_user(request: Request, user_id: int) -> Response:
        return json_response(
            {
                "user_id": user_id,
                "middleware_ran": request.state["middleware_ran"],
            }
        )
