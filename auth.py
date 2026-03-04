import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Validates Authorization: Bearer <token> on every request.

    Uses secrets.compare_digest to prevent timing-based token oracle attacks.
    Only used when API_KEY is set in the environment.
    """

    def __init__(self, app, api_key: str, exempt_paths: set[str] | None = None):
        super().__init__(app)
        self.api_key = api_key
        self.exempt_paths = exempt_paths or set()

    def _is_exempt(self, path: str) -> bool:
        return path in self.exempt_paths

    async def dispatch(self, request: Request, call_next):
        if self._is_exempt(request.url.path):
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if not secrets.compare_digest(token.encode(), self.api_key.encode()):
            return JSONResponse(
                {"error": "Unauthorized", "detail": "Valid Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
