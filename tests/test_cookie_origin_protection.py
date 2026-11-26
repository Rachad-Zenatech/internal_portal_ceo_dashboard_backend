from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from services.shared.cookie_origin_protection import CookieOriginProtection


async def endpoint(request):
    return JSONResponse({"ok": True})


def test_cookie_requests_require_trusted_origin():
    app = Starlette(routes=[Route("/", endpoint, methods=["GET", "POST"])])
    app.add_middleware(CookieOriginProtection, cookie_names={"access_token", "zenatech_access_token"},
                       allowed_origins=["https://portal.example"],
                       allowed_origin_regex=r"^http://localhost(:\d+)?$")
    client = TestClient(app, base_url="https://api.example")
    assert client.post("/").status_code == 200
    for name in ("access_token", "zenatech_access_token"):
        client.cookies.set(name, "test")
        assert client.get("/").status_code == 200
        assert client.post("/").status_code == 403
        assert client.post("/", headers={"origin": "https://evil.example"}).status_code == 403
        assert client.post("/", headers={"origin": "https://portal.example"}).status_code == 200
        assert client.post("/", headers={"origin": "https://api.example"}).status_code == 200
        assert client.post("/", headers={"origin": "http://localhost:5173"}).status_code == 200
        assert client.post("/", headers={"origin": "https://portal.example.evil"}).status_code == 403
        client.cookies.clear()
