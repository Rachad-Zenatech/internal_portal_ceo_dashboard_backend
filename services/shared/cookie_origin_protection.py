"""Reject cross-origin state-changing requests carrying a browser session."""
import re
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class CookieOriginProtection(BaseHTTPMiddleware):
    def __init__(self, app, cookie_names, allowed_origins, allowed_origin_regex=None):
        super().__init__(app)
        self.cookie_names = tuple(cookie_names)
        self.allowed_origins = frozenset(origin.rstrip("/") for origin in allowed_origins)
        self.allowed_origin_regex = re.compile(allowed_origin_regex) if allowed_origin_regex else None

    def trusted(self, origin, host):
        if not origin or not host:
            return False
        try:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path
                    or parsed.query or parsed.fragment or parsed.username or parsed.password):
                return False
            if origin in self.allowed_origins:
                return True
            if self.allowed_origin_regex and self.allowed_origin_regex.fullmatch(origin):
                return True
            return (parsed.netloc.lower() == host.lower()
                    and (parsed.scheme == "https" or parsed.hostname in {"localhost", "127.0.0.1", "::1"}))
        except ValueError:
            return False

    async def dispatch(self, request, call_next):
        if (request.method not in {"GET", "HEAD", "OPTIONS"}
                and any(request.cookies.get(name) for name in self.cookie_names)
                and not self.trusted(request.headers.get("origin"), request.headers.get("host"))):
            return JSONResponse(status_code=403, content={"detail": "Untrusted request origin"})
        return await call_next(request)
