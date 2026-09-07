"""Session-based login for the web UI."""
from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

SESSION_USER_KEY = "user"
SESSION_ROLE_KEY = "role"


def _is_prod() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() in {"prod", "production"}


def session_secret() -> str:
    secret = (os.environ.get("SESSION_SECRET") or "").strip()
    if secret:
        return secret
    if _is_prod():
        raise RuntimeError("SESSION_SECRET is required when APP_ENV=production")
    return os.environ.get("BASIC_AUTH_PASSWORD") or "cloud-cost-reporter-dev-secret"


def verify_login(username: str, password: str) -> str | None:
    """Return the user's role on success, else None.

    Checks the DB ``app_users`` table first, then the bootstrap admin from
    BASIC_AUTH_USER / BASIC_AUTH_PASSWORD (used when no DB users exist yet).
    """
    from .users import bootstrap_admin, get_user, verify_password

    try:
        user = get_user(None, username)
        if user and verify_password(password, user["password_hash"]):
            return user["role"]
    except Exception:  # noqa: BLE001 - fall back to bootstrap admin if DB down
        pass

    b_user, b_pwd = bootstrap_admin()
    if (
        b_pwd
        and secrets.compare_digest(username, b_user)
        and secrets.compare_digest(password, b_pwd)
    ):
        return "admin"
    return None


def login_user(request: Request, username: str, role: str = "viewer") -> None:
    request.session[SESSION_USER_KEY] = username
    request.session[SESSION_ROLE_KEY] = role


def logout_user(request: Request) -> None:
    request.session.clear()


def current_user(request: Request) -> str | None:
    session = request.scope.get("session")
    if session is None:
        return None
    return session.get(SESSION_USER_KEY)


def current_role(request: Request) -> str | None:
    session = request.scope.get("session")
    if session is None:
        return None
    return session.get(SESSION_ROLE_KEY)


def is_admin(request: Request) -> bool:
    return current_role(request) == "admin"


class RequireLoginMiddleware:
    """Pure ASGI auth gate — must sit inside SessionMiddleware on the stack."""

    def __init__(self, app: ASGIApp, *, public_paths: frozenset[str]) -> None:
        self.app = app
        self.public_paths = public_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path not in self.public_paths and not current_user(Request(scope)):
            from fastapi.responses import JSONResponse, RedirectResponse
            from starlette import status

            if path.startswith("/api/"):
                response: Any = JSONResponse({"detail": "Unauthorized"}, status_code=401)
            else:
                response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
