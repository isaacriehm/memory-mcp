import base64
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse


def _append_query(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def _client_creds(request: Request, form: dict[str, str]) -> tuple[str | None, str | None]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            raw = base64.b64decode(auth[6:]).decode("utf-8")
            user, pwd = raw.split(":", 1)
            return user, pwd
        except Exception:
            return None, None
    return form.get("client_id"), form.get("client_secret")


def _allowed_redirect_uris() -> set[str]:
    raw = os.getenv("OAUTH_ALLOWED_REDIRECT_URIS", "https://claude.ai/api/mcp/auth_callback")
    return {u.strip() for u in raw.split(",") if u.strip()}


@dataclass
class AuthCode:
    redirect_uri: str
    code_challenge: str
    expires_at: float


class MinimalOAuthBridge:
    """Minimal OAuth bridge for MCP connectors.

    This is intentionally simple: it validates Authorization Code + PKCE flow,
    then returns `API_KEY` as the bearer access token.
    """

    def __init__(self, api_key: str, client_id: str = "api-key", code_ttl_s: int = 300):
        self.api_key = api_key
        self.client_id = client_id
        self.code_ttl_s = code_ttl_s
        self._codes: dict[str, AuthCode] = {}

    def _cleanup(self) -> None:
        now = time.time()
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}

    def _issuer(self, request: Request) -> str:
        forced = os.getenv("OAUTH_ISSUER")
        if forced:
            return forced.rstrip("/")
        return str(request.base_url).rstrip("/")

    async def authorize(self, request: Request):
        self._cleanup()
        q = request.query_params
        response_type = q.get("response_type", "")
        client_id = q.get("client_id", "")
        redirect_uri = q.get("redirect_uri", "")
        code_challenge = q.get("code_challenge", "")
        code_challenge_method = q.get("code_challenge_method", "")
        state = q.get("state", "")

        if response_type != "code":
            return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
        if client_id != self.client_id:
            return JSONResponse({"error": "unauthorized_client"}, status_code=401)
        if redirect_uri not in _allowed_redirect_uris():
            return JSONResponse({"error": "invalid_request", "error_description": "redirect_uri not allowed"}, status_code=400)
        if code_challenge_method != "S256" or not code_challenge:
            return RedirectResponse(
                _append_query(redirect_uri, {"error": "invalid_request", "error_description": "S256 PKCE required", "state": state}),
                status_code=302,
            )

        code = secrets.token_urlsafe(24)
        self._codes[code] = AuthCode(
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            expires_at=time.time() + self.code_ttl_s,
        )
        return RedirectResponse(_append_query(redirect_uri, {"code": code, "state": state}), status_code=302)

    async def token(self, request: Request):
        self._cleanup()
        body = (await request.body()).decode("utf-8")
        form = dict(parse_qsl(body, keep_blank_values=True))
        grant_type = form.get("grant_type", "")
        code = form.get("code", "")
        redirect_uri = form.get("redirect_uri", "")
        code_verifier = form.get("code_verifier", "")

        client_id, client_secret = _client_creds(request, form)
        if client_id != self.client_id or client_secret != self.api_key:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if grant_type != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        if not code or not code_verifier:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        payload = self._codes.pop(code, None)
        if not payload or payload.expires_at <= time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if payload.redirect_uri != redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if _pkce_s256(code_verifier) != payload.code_challenge:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        return JSONResponse(
            {
                "access_token": self.api_key,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": form.get("scope", "claudeai"),
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    async def auth_server_metadata(self, request: Request):
        issuer = self._issuer(request)
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["claudeai"],
            },
            headers={"Cache-Control": "no-store"},
        )

    async def resource_metadata(self, request: Request):
        issuer = self._issuer(request)
        return JSONResponse(
            {
                "resource": f"{issuer}/mcp",
                "authorization_servers": [issuer],
                "bearer_methods_supported": ["header"],
            },
            headers={"Cache-Control": "no-store"},
        )


def register_oauth_routes(mcp, api_key: str, client_id: str = "api-key") -> None:
    oauth = MinimalOAuthBridge(api_key=api_key, client_id=client_id)

    @mcp.custom_route("/authorize", methods=["GET"])
    async def oauth_authorize(request: Request):
        return await oauth.authorize(request)

    @mcp.custom_route("/token", methods=["POST"])
    async def oauth_token(request: Request):
        return await oauth.token(request)

    @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def oauth_authz_metadata(request: Request):
        return await oauth.auth_server_metadata(request)

    @mcp.custom_route("/mcp/.well-known/oauth-authorization-server", methods=["GET"])
    async def oauth_authz_metadata_scoped(request: Request):
        return await oauth.auth_server_metadata(request)

    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def oauth_resource_metadata(request: Request):
        return await oauth.resource_metadata(request)

    @mcp.custom_route("/mcp/.well-known/oauth-protected-resource", methods=["GET"])
    async def oauth_resource_metadata_scoped(request: Request):
        return await oauth.resource_metadata(request)

    @mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])
    async def oauth_resource_metadata_suffix(request: Request):
        return await oauth.resource_metadata(request)
