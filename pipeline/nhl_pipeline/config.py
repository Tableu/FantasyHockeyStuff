"""Reads databaseCredentials.txt (a raw sqlcmd command line) so the DB password
lives in exactly one place, and builds the pyodbc connection string from it."""

import argparse
import json
import shlex
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = PROJECT_ROOT / "databaseCredentials.txt"
SEASON_CONFIG_PATH = PROJECT_ROOT / "season_config.json"
GOOGLE_SHEETS_CREDENTIALS_PATH = PROJECT_ROOT / "googleSheetsCredentials.json"


def load_db_config(path: Path = CREDENTIALS_PATH) -> dict:
    line = path.read_text(encoding="utf-8").strip()
    tokens = shlex.split(line)  # tokens[0] == "sqlcmd"

    parser = argparse.ArgumentParser()
    parser.add_argument("-S")
    parser.add_argument("-U")
    parser.add_argument("-P")
    parser.add_argument("-d")
    args, _ = parser.parse_known_args(tokens[1:])

    return {"server": args.S, "user": args.U, "password": args.P, "database": args.d}


def build_connection_string(cfg: dict) -> str:
    return (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={cfg['server']};DATABASE={cfg['database']};"
        f"UID={cfg['user']};PWD={cfg['password']};"
        "Encrypt=yes;TrustServerCertificate=yes;"
    )


def get_connection_string() -> str:
    return build_connection_string(load_db_config())


def load_season_config(path: Path = SEASON_CONFIG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
