"""The NHL Injury Viz injury database (https://nhlinjuryviz.blogspot.com/2015/11/nhl-injury-
database.html) -- ~25,000 injury spells from 2000-01 on, compiled from PuckPedia/CBS/TSN/
CapFriendly and published as a Tableau Public workbook. The download link serves a .twbx
(a zip) whose Data/TableauTemp/*.hyper file is the extract; read here with tableauhyperapi
(pip install tableauhyperapi). Table "Extract"."Extract", one row per spell:

    Team, Player ("Last, First"), Player2 (Player plus a disambiguating "(2)"/"(D)" suffix
    for same-named players), Position (F/D/G, or e.g. 'D "Retired"' for an LTIR contract
    dump), Season ("2025/26", or "2025/26 (playoffs)"), Injury Type, Injury type group
    (1..15 body region), Games Missed, Start/End (team game numbers of the first/last game
    missed -- NULL on every playoffs row), Cap Hit ($M), CHIP (cap hit lost).

Verified on the June 2026 rebuild: every regular-season row has Start/End and Games Missed
== End - Start + 1. Absences only (injury/illness) -- no healthy scratches, suspensions, or
pre-game DTD/IR designations.
"""

import logging
import zipfile
from pathlib import Path

import requests

log = logging.getLogger("api.nhl_injury_viz")

WORKBOOK_URL = "https://public.tableau.com/workbooks/NHLinjurydatabase.twb"
HYPER_TABLE = ("Extract", "Extract")


def download_workbook(dest_dir: Path) -> Path:
    """Downloads the .twbx to dest_dir and returns the path of the .hyper extract inside it
    (unzipped alongside). Re-downloads every call -- the workbook is rebuilt upstream a few
    times a season and there's no version header to check against."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    twbx_path = dest_dir / "NHLinjurydatabase.twbx"

    log.info("Downloading %s ...", WORKBOOK_URL)
    response = requests.get(WORKBOOK_URL, timeout=60, headers={"User-Agent": "nhl-pipeline/1.0"})
    response.raise_for_status()
    twbx_path.write_bytes(response.content)
    return extract_hyper(twbx_path)


def extract_hyper(twbx_path: Path) -> Path:
    twbx_path = Path(twbx_path)
    with zipfile.ZipFile(twbx_path) as zf:
        hyper_members = [m for m in zf.namelist() if m.lower().endswith(".hyper")]
        if len(hyper_members) != 1:
            raise ValueError(f"Expected exactly one .hyper in {twbx_path}, found {hyper_members}")
        target = twbx_path.parent / "NHLinjurydatabase.hyper"
        target.write_bytes(zf.read(hyper_members[0]))
    return target


def read_rows(hyper_path: Path) -> list:
    """Every row of the extract as {column_name: value}. Imported lazily so the rest of the
    pipeline doesn't need tableauhyperapi installed."""
    from tableauhyperapi import Connection, HyperProcess, TableName, Telemetry

    table = TableName(*HYPER_TABLE)
    # log_config "" stops the Hyper engine dropping a hyperd.log into the working directory.
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_config": ""}) as hyper:
        with Connection(hyper.endpoint, str(hyper_path)) as connection:
            columns = [c.name.unescaped for c in connection.catalog.get_table_definition(table).columns]
            rows = connection.execute_list_query(f"SELECT * FROM {table}")
    return [dict(zip(columns, row)) for row in rows]
