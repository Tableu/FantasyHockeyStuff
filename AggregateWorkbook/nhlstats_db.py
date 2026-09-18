"""Read-only connection to NHLStats for the aggregate workbook build.

The build only ever SELECTs (projections, positions, ADP, the schedule), so it connects with
the read-only FantasyAssistant SQL login (db_datareader -- the same one the AI assistant's
MCP server uses) rather than the pipeline's writer login, and doesn't import nhl_pipeline at
all: AggregateWorkbook/ stands on its own next to pipeline/.

Credentials live in databaseCredentials.txt next to this file (gitignored), as a raw sqlcmd
command line -- the same one-line format as pipeline/databaseCredentials.txt:

    sqlcmd -S localhost -U FantasyAssistant -P <password> -d NHLStats
"""

import argparse
import shlex
from pathlib import Path

import pyodbc

CREDENTIALS_PATH = Path(__file__).resolve().parent / "databaseCredentials.txt"


def connection_string(path: Path = CREDENTIALS_PATH) -> str:
    if not path.exists():
        raise SystemExit(
            f"{path} not found -- create it with one line: "
            "sqlcmd -S <server> -U FantasyAssistant -P <password> -d NHLStats"
        )
    tokens = shlex.split(path.read_text(encoding="utf-8").strip())  # tokens[0] == "sqlcmd"
    parser = argparse.ArgumentParser()
    for flag in ("-S", "-U", "-P", "-d"):
        parser.add_argument(flag)
    args, _ = parser.parse_known_args(tokens[1:])
    return (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={args.S};DATABASE={args.d};UID={args.U};PWD={args.P};"
        "Encrypt=yes;TrustServerCertificate=yes;"
    )


def connect() -> pyodbc.Connection:
    return pyodbc.connect(connection_string())
