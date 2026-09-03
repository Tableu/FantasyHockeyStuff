"""Google Sheets client for surgical (range/cell-level) edits to a live spreadsheet,
as opposed to build_aggregate_workbook.py which regenerates a whole local .xlsx.

Auth is a service account: create one in Google Cloud Console, enable the Sheets API,
download its JSON key to googleSheetsCredentials.json at the project root (see
config.GOOGLE_SHEETS_CREDENTIALS_PATH), and share the target spreadsheet with the
service account's client_email as an Editor. Nothing here works until that file exists.

Usage:
    from nhl_pipeline.sheets import open_spreadsheet, update_range

    sh = open_spreadsheet("1AbC...spreadsheetId...")
    ws = sh.worksheet("Rankings")
    update_range(ws, "A1:C3", [[1, 2, 3], [4, 5, 6], [7, 8, 9]])
"""

import gspread
from google.oauth2.service_account import Credentials

from nhl_pipeline.config import GOOGLE_SHEETS_CREDENTIALS_PATH

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def open_spreadsheet(spreadsheet_id: str) -> gspread.Spreadsheet:
    return get_client().open_by_key(spreadsheet_id)


def update_cell(worksheet: gspread.Worksheet, cell: str, value) -> None:
    worksheet.update(range_name=cell, values=[[value]])


def update_range(worksheet: gspread.Worksheet, range_name: str, values: list[list]) -> None:
    """values is a 2D list matching range_name's shape, e.g. update_range(ws, "A1:B2",
    [[1, 2], [3, 4]])."""
    worksheet.update(range_name=range_name, values=values)


def append_rows(worksheet: gspread.Worksheet, rows: list[list]) -> None:
    worksheet.append_rows(rows, value_input_option="USER_ENTERED")
