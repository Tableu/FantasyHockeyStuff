#!/usr/bin/env python
"""The plan server's login (Phone App Remote Access plan): the server is reachable from the internet
through the tunnel, so every call but the app's own files needs a session (by password). Everything
lives under "plan-server" in Settings/secrets.json (gitignored): the password's scrypt hash and the
secret that signs sessions. (Until 2026-10-24 a key let plan_gui in too; it went with the window.)

    python auth.py set-password                 # asks twice; signs every phone out
    python auth.py set-password --stdin         # the password on standard input (scripts, tests)

A session is a signed cookie holding its expiry -- nothing kept on the server, so a restart keeps
everyone logged in, and a new password (a new signing secret) logs everyone out. A few wrong
passwords from one address and it has to wait.
"""

import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path

SECRETS = Path(__file__).resolve().parents[1] / "Settings" / "secrets.json"
ENTRY = "plan-server"
COOKIE = "fh_session"
SESSION_DAYS = 90
RENEW_DAYS = 60                 # a session with less left than this is renewed on its next call
FAILS_ALLOWED = 5               # wrong passwords from one address ...
FAILS_WINDOW_S = 15 * 60        # ... within this long, and it waits this long from the last one
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
OPEN_PATHS = {"/login", "/logout"}      # and the app's files, /app/... (no league data in them)


def secrets_path() -> Path:
    """Settings/secrets.json, or FH_LOGIN_SECRETS (a test server's own file)."""
    return Path(os.environ.get("FH_LOGIN_SECRETS") or SECRETS)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, salt, digest = stored.split("$")
    except ValueError:
        return False
    if kind != "scrypt":
        return False
    got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), dklen=32, **SCRYPT)
    return hmac.compare_digest(got.hex(), digest)


def load_entry(path: Path | None = None) -> dict | None:
    """The server's login settings, or None before a password is set."""
    path = path or secrets_path()
    if not path.exists():
        return None
    entry = json.loads(path.read_text(encoding="utf-8")).get(ENTRY)
    if not entry or not all(entry.get(k) for k in ("password_hash", "session_secret")):
        return None
    return entry


def set_password(password: str, path: Path | None = None) -> dict:
    """A new hash and signing secret (every session ends)."""
    path = path or secrets_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[ENTRY] = {"password_hash": hash_password(password), "session_secret": secrets.token_hex(32)}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data[ENTRY]


class Login:
    """Checks every call; makes and checks session cookies. Reads secrets.json again when it
    changes, so a new password needs no restart."""

    def __init__(self, path: Path | None = None):
        self.path = path or secrets_path()
        self.mtime, self.entry = None, None
        self.fails: dict[str, list[float]] = {}

    def settings(self) -> dict | None:
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if mtime != self.mtime:
            self.mtime, self.entry = mtime, load_entry(self.path)
        return self.entry

    def _sign(self, secret: str, expires: int) -> str:
        return hmac.new(secret.encode("utf-8"), f"session:{expires}".encode("utf-8"), hashlib.sha256).hexdigest()

    def new_session(self) -> tuple[str, int]:
        """A cookie value and its lifetime in seconds."""
        expires = int(time.time()) + SESSION_DAYS * 86400
        return f"{expires}.{self._sign(self.settings()['session_secret'], expires)}", SESSION_DAYS * 86400

    def session_left(self, value: str | None) -> float | None:
        """Seconds left on a valid session cookie, else None."""
        entry = self.settings()
        if not value or entry is None or "." not in value:
            return None
        expires, signature = value.split(".", 1)
        if not expires.isdigit() or not hmac.compare_digest(signature, self._sign(entry["session_secret"], int(expires))):
            return None
        left = int(expires) - time.time()
        return left if left > 0 else None

    def wait_left(self, address: str) -> int:
        """Seconds this address must wait before another try (0: it may try now)."""
        now = time.time()
        recent = [t for t in self.fails.get(address, []) if now - t < FAILS_WINDOW_S]
        self.fails[address] = recent
        return int(recent[-1] + FAILS_WINDOW_S - now) + 1 if len(recent) >= FAILS_ALLOWED else 0

    def try_password(self, address: str, password: str) -> bool:
        if check_password(password, self.settings()["password_hash"]):
            self.fails.pop(address, None)
            return True
        self.fails.setdefault(address, []).append(time.time())
        return False


def install(app, login: Login):
    """The login on a FastAPI app: POST /login and /logout, and every other call but the app's
    files refused without a session."""
    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse, Response
    from pydantic import BaseModel

    class Password(BaseModel):
        password: str

    def address(request: Request) -> str:
        # Through the tunnel every call comes from cloudflared; Cloudflare names the real one.
        return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")

    def set_cookie(response: Response):
        value, max_age = login.new_session()
        response.set_cookie(COOKIE, value, max_age=max_age, path="/", httponly=True, secure=True, samesite="strict")

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path in OPEN_PATHS or path == "/app" or path.startswith("/app/"):
            return await call_next(request)
        if login.settings() is None:
            return JSONResponse({"detail": "no login set on the server: python auth.py set-password"}, status_code=503)
        left = login.session_left(request.cookies.get(COOKIE))
        if left is None:
            return JSONResponse({"detail": "log in"}, status_code=401)
        response = await call_next(request)
        if left < RENEW_DAYS * 86400:
            set_cookie(response)
        return response

    @app.post("/login")
    def log_in(body: Password, request: Request):
        if login.settings() is None:
            raise HTTPException(503, "no login set on the server: python auth.py set-password")
        who = address(request)
        wait = login.wait_left(who)
        if wait:
            raise HTTPException(429, f"too many wrong passwords: try again in {(wait + 59) // 60} min")
        if not login.try_password(who, body.password):
            raise HTTPException(401, "wrong password")
        response = JSONResponse({"ok": True})
        set_cookie(response)
        return response

    @app.post("/logout")
    def log_out():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", httponly=True, secure=True, samesite="strict")
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["set-password"])
    parser.add_argument("--stdin", action="store_true", help="Read the password from standard input")
    args = parser.parse_args()
    if args.stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Again: ") != password:
            raise SystemExit("the two didn't match; nothing changed")
    if len(password) < 8:
        raise SystemExit("at least 8 characters; nothing changed")
    set_password(password)
    print(f"password set in {secrets_path()} ({ENTRY}): everyone logs in again")


if __name__ == "__main__":
    main()
