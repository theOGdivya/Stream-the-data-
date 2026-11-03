"""Authentication.

* ``/api/agent/*``: capture agents and data pushers, ``Authorization: Bearer <AGENT_TOKEN>``.
* everything else (dashboard + its API): HTTP Basic auth against ``DASHBOARD_USERS``.
* ``/healthz``: open, for uptime checks.
"""
from __future__ import annotations

import base64
import binascii
import json
import secrets

from .config import Settings

OPEN_PATHS = {"/healthz"}


class AuthMiddleware:
    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        path: str = scope["path"]
        if path.startswith("/api/agent/"):
            token = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not token or not secrets.compare_digest(token.encode(), self.settings.agent_token.encode()):
                return await _deny(send, "invalid agent token", basic=False)
            scope.setdefault("state", {})["user"] = "agent"
            return await self.app(scope, receive, send)

        user = _check_basic(auth, self.settings.dashboard_users)
        if user is None:
            return await _deny(send, "login required", basic=True)
        scope.setdefault("state", {})["user"] = user
        return await self.app(scope, receive, send)


def _check_basic(header: str, users: dict[str, str]) -> str | None:
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    name, _, password = decoded.partition(":")
    expected = users.get(name)
    if expected is None:
        secrets.compare_digest(password.encode(), b"x" * 16)  # keep timing similar
        return None
    return name if secrets.compare_digest(password.encode(), expected.encode()) else None


async def _deny(send, message: str, basic: bool) -> None:
    body = json.dumps({"detail": message}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if basic:
        headers.append((b"www-authenticate", b'Basic realm="AQ test platform", charset="UTF-8"'))
    await send({"type": "http.response.start", "status": 401, "headers": headers})
    await send({"type": "http.response.body", "body": body})
