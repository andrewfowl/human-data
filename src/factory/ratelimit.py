"""Fixed-window rate limiting, per credential (or client IP when anonymous).

Configured with HDF_RATE_LIMIT_PER_MINUTE (default 120; 0 disables). State is
in-process — on serverless platforms the limit applies per warm instance,
which is a sane baseline; put a shared limiter (edge/WAF or Redis) in front
for hard global guarantees. `/healthz` is exempt so orchestrators can probe.
"""

from __future__ import annotations

import hashlib
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import settings

EXEMPT_PATHS = {"/healthz"}


class _FixedWindow:
    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, int]] = {}  # key -> (window, count)

    def hit(self, key: str, limit: int) -> tuple[bool, int]:
        """Returns (allowed, seconds_until_reset)."""
        now = time.time()
        window = int(now // 60)
        prev_window, count = self._counts.get(key, (window, 0))
        if prev_window != window:
            count = 0
        count += 1
        self._counts[key] = (window, count)
        if len(self._counts) > 10_000:  # bound memory: drop stale windows
            self._counts = {k: v for k, v in self._counts.items() if v[0] == window}
        return count <= limit, max(1, int(60 - (now % 60)))


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app) -> None:
        super().__init__(app)
        self._window = _FixedWindow()

    @staticmethod
    def _identity(request: Request) -> str:
        auth_header = request.headers.get("authorization")
        if auth_header:
            return hashlib.sha256(auth_header.encode()).hexdigest()[:32]
        actor = request.headers.get("x-actor-id")
        if actor:
            return f"actor:{actor}"
        client = request.client.host if request.client else "unknown"
        return f"ip:{client}"

    async def dispatch(self, request: Request, call_next):
        limit = settings.rate_limit_per_minute
        if limit <= 0 or request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        allowed, reset_in = self._window.hit(self._identity(request), limit)
        if not allowed:
            return JSONResponse(
                {"detail": "rate limit exceeded", "retry_after_seconds": reset_in},
                status_code=429,
                headers={"Retry-After": str(reset_in)},
            )
        return await call_next(request)
