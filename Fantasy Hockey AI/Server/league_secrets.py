"""The ESPN cookies (espn_s2, SWID) of the private leagues users add in the app, encrypted:
state/league_secrets.json ({league name: a Fernet token}), its key `league_key` under "plan-server"
in Settings/secrets.json (or FH_LOGIN_SECRETS, a test server's own) -- made on first use. The two
files are apart so a copy of state/ alone reads nothing. The cookies are only ever sent to ESPN,
to read the league (platforms/espn.py).
"""

import json
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import jsonfile
import leagues

_lock = threading.Lock()
SERVER_ENTRY = "plan-server"


def secrets_path() -> Path:
    return Path(os.environ.get("FH_LOGIN_SECRETS") or leagues.SECRETS)


def _store() -> Path:
    return leagues.state_root() / "league_secrets.json"


def _fernet() -> Fernet:
    def change(data):
        entry = data.setdefault(SERVER_ENTRY, {})
        if entry.get("league_key"):
            return None, entry["league_key"]
        entry["league_key"] = Fernet.generate_key().decode("ascii")
        return data, entry["league_key"]
    return Fernet(jsonfile.update(secrets_path(), change).encode("ascii"))


def _read() -> dict:
    return jsonfile.read(_store(), {})


def put(name: str, cookies: dict) -> None:
    """Keep a league's cookies ({"espn_s2", "SWID"})."""
    with _lock:
        data = _read()
        data[name] = _fernet().encrypt(json.dumps(cookies).encode("utf-8")).decode("ascii")
        jsonfile.write(_store(), data)


def get(name: str) -> dict | None:
    token = _read().get(name)
    if not token:
        return None
    try:
        return json.loads(_fernet().decrypt(token.encode("ascii")))
    except InvalidToken:
        return None


def remove(name: str) -> None:
    with _lock:
        data = _read()
        if data.pop(name, None) is not None:
            jsonfile.write(_store(), data)
