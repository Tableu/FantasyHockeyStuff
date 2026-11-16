"""Who is calling: the browser's fh_user cookie (users.py). The site is open to anyone (the user,
2026-11-15: no password, no accounts, no recovery) -- what a caller sees is decided by which
leagues its cookie links (server.Worker.league), not by a login.

The cookie is `<uid>.<signature>`: the user's id signed with `user_secret` under "plan-server" in
Settings/secrets.json (FH_LOGIN_SECRETS for a test server's own; made on first start), so a
made-up id is never anyone's. Nothing is kept per session -- a restart changes nothing. It is made
when a browser adds its first league (`issue`), so a visitor who adds none leaves nothing behind,
and lasts COOKIE_DAYS, renewed on the first call of each day.

Until 2026-11-15 a password (scrypt hash here) let one person in; every league was his.
"""

import contextvars
import hashlib
import hmac
import os
import secrets
from pathlib import Path

import jsonfile
import users

SECRETS = Path(__file__).resolve().parents[1] / "Settings" / "secrets.json"
ENTRY = "plan-server"
COOKIE = "fh_user"
COOKIE_DAYS = 400

# The caller for this request, set by the middleware: its user id (None: a browser that has added
# nothing) and its user file as read once for the request (users.py), which `leagues_of` answers
# from. contextvars follow the request into the threadpool a sync route runs on -- not into
# threads a route starts itself.
viewer: contextvars.ContextVar = contextvars.ContextVar("viewer", default=None)
viewer_user: contextvars.ContextVar = contextvars.ContextVar("viewer_user", default=None)


def leagues_of(uid) -> list:
    """The caller's leagues: from the user read for this request when it is that user's, else
    read now."""
    user = viewer_user.get()
    if user is not None and user.get("id") == uid:
        return list(user["leagues"])
    return users.linked(uid)


def secrets_path() -> Path:
    """Settings/secrets.json, or FH_LOGIN_SECRETS (a test server's own file)."""
    return Path(os.environ.get("FH_LOGIN_SECRETS") or SECRETS)


def user_secret(path: Path | None = None) -> str:
    """The signing secret, made and saved on first use."""
    def change(data):
        entry = data.setdefault(ENTRY, {})
        if entry.get("user_secret"):
            return None, entry["user_secret"]
        entry["user_secret"] = secrets.token_hex(32)
        return data, entry["user_secret"]
    return jsonfile.update(path or secrets_path(), change)


class Cookies:
    def __init__(self, path: Path | None = None):
        self.secret = user_secret(path)

    def sign(self, uid: str) -> str:
        return hmac.new(self.secret.encode("utf-8"), f"user:{uid}".encode("utf-8"), hashlib.sha256).hexdigest()

    def value(self, uid: str) -> str:
        return f"{uid}.{self.sign(uid)}"

    def user(self, value: str | None) -> dict | None:
        """The user a cookie names, if its signature holds and the user still exists."""
        if not value or "." not in value:
            return None
        uid, signature = value.rsplit(".", 1)
        if not hmac.compare_digest(signature, self.sign(uid)):
            return None
        return users.load(uid)


def install(app, cookies: Cookies):
    """The cookie on a FastAPI app: every call names its caller (`viewer`); POST /forget drops
    the cookie. Returns `issue(response, uid) -> uid`, which gives a browser its user (POST /leagues)."""
    from fastapi import Request
    from fastapi.responses import JSONResponse, Response
    from starlette.concurrency import run_in_threadpool

    def set_cookie(response: Response, uid: str):
        response.set_cookie(COOKIE, cookies.value(uid), max_age=COOKIE_DAYS * 86400, path="/",
                            httponly=True, secure=True, samesite="strict")

    @app.middleware("http")
    async def who(request: Request, call_next):
        path = request.url.path
        if path == "/app" or path.startswith("/app/"):
            return await call_next(request)         # the app's own files: nobody's data
        user = await run_in_threadpool(cookies.user, request.cookies.get(COOKIE))
        uid = user["id"] if user else None
        token, token_user = viewer.set(uid), viewer_user.set(user)
        try:
            response = await call_next(request)
        finally:
            viewer.reset(token)
            viewer_user.reset(token_user)
        # Once a day: last_seen written and the cookie set again, so a browser used now and then
        # never runs out (COOKIE_DAYS from its last day of use).
        if user is not None and await run_in_threadpool(users.touch, user):
            set_cookie(response, uid)
        return response

    @app.post("/forget")
    def forget():
        """This browser forgets its user (its leagues stay for anyone else linked to them)."""
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", httponly=True, secure=True, samesite="strict")
        return response

    def issue(response: Response, uid: str | None) -> str:
        """The caller's user, made now if it has none; the cookie set on `response`."""
        if uid is None:
            uid = users.create()["id"]
        set_cookie(response, uid)
        return uid

    return issue
