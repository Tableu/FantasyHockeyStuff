"""openpyxl-shaped compatibility adapter over the Google Sheets API (via gspread).

build_aggregate_workbook.py's ~40 fill_*/rebuild_* phase functions were already written to
be Google-Sheets-safe (see that module's docstring) -- the only thing tying them to Excel was
the openpyxl I/O surface they're called through. This module implements just that surface
(Workbook.__getitem__/.remove/.sheetnames, Worksheet.cell/.max_row/.max_column/.iter_rows/
.delete_cols/.delete_rows/.merged_cells/.unmerge_cells) on top of gspread, plus a
set_defined_name() replacement, so the phase functions can run unmodified against a live
Google Sheet instead of a local .xlsx.

Read model: everything is fetched once (as formulas, not computed values) when the workbook
is opened and cached in memory per-sheet; reads never round-trip to the API mid-run, matching
how openpyxl already behaves (purely in-memory until save()).

Write model: cell writes go to the in-memory cache immediately (so later reads in the same
run see them) and are staged in a pending-writes buffer. wb.save() flushes every sheet's
buffer in one values_batch_update call, row-run-length-encoded so contiguous written columns
in a row collapse into one range instead of one per cell -- without ever writing to a cell
nobody asked for (which would risk clobbering untouched cells sitting between explicit
writes).

Structural changes (delete_cols/delete_rows/unmerge_cells, set_defined_name) fire their real
Sheets API request immediately (cheap -- a full run only makes ~15-20 such calls) and update
the local cache/bookkeeping so subsequent reads in the same run stay consistent. Google
Sheets' deleteDimension request auto-adjusts formulas and named ranges that reference cells
after the deleted range (same engine as the UI's column-delete), which is why this module
doesn't need an equivalent of openpyxl's manual shifted-reference patching.
"""

import datetime
import logging
import re
import time
from pathlib import Path

import gspread
from gspread.exceptions import APIError
from gspread.utils import Dimension, a1_range_to_grid_range, rowcol_to_a1

log = logging.getLogger("gsheets_io")

DEFAULT_CREDENTIALS_PATH = Path(__file__).parent / "googleSheetsCredentials.json"


def _coerce(value):
    """Serializes datetime.date/datetime to an ISO string so USER_ENTERED reliably parses
    them as real dates regardless of spreadsheet locale (Sheets values are plain JSON
    scalars -- python date objects aren't one)."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


class _CellProxy:
    __slots__ = ("_ws", "row", "column")

    def __init__(self, ws, row, column):
        self._ws = ws
        self.row = row
        self.column = column

    @property
    def value(self):
        return self._ws._get(self.row, self.column)

    @value.setter
    def value(self, v):
        self._ws._set(self.row, self.column, v)


class _MergedCellsProxy:
    """Mimics openpyxl's ws.merged_cells.ranges -- an iterable of range strings (openpyxl's
    own range objects stringify to A1 notation; plain strings already satisfy str(x))."""

    def __init__(self, ws):
        self._ws = ws

    @property
    def ranges(self):
        return list(self._ws._merges)


class CompatWorksheet:
    def __init__(self, wb, gs_ws, sheet_id):
        self._wb = wb
        self._gs = gs_ws
        self.sheet_id = sheet_id
        self.title = gs_ws.title
        self._cache = {}
        self._pending = {}
        self._max_row = 1
        self._max_col = 1
        self._merges = []
        self.merged_cells = _MergedCellsProxy(self)

    def _seed_values(self, grid_values):
        for r, row in enumerate(grid_values, start=1):
            for c, v in enumerate(row, start=1):
                if v not in (None, ""):
                    self._cache[(r, c)] = v
        self._max_row = max(len(grid_values), 1)
        self._max_col = max((len(row) for row in grid_values), default=0)
        self._max_col = max(self._max_col, 1)

    def _seed_merges(self, merge_grid_ranges):
        self._merges = [
            f"{rowcol_to_a1(m['startRowIndex'] + 1, m['startColumnIndex'] + 1)}:"
            f"{rowcol_to_a1(m['endRowIndex'], m['endColumnIndex'])}"
            for m in merge_grid_ranges
        ]

    @property
    def max_row(self):
        return self._max_row

    @property
    def max_column(self):
        return self._max_col

    def cell(self, row, column, value=None):
        if value is not None:
            self._set(row, column, value)
        return _CellProxy(self, row, column)

    def _get(self, row, column):
        return self._cache.get((row, column))

    def _set(self, row, column, value):
        value = _coerce(value)
        self._cache[(row, column)] = value
        self._pending[(row, column)] = value
        if row > self._max_row:
            self._max_row = row
        if column > self._max_col:
            self._max_col = column

    def get_computed_values(self, first_row, first_col, last_row, last_col):
        """Reads a range's live COMPUTED values (UNFORMATTED_VALUE) directly from the API,
        bypassing this module's own cache entirely -- every cell this module otherwise reads
        holds FORMULA text, not a computed result (see open_result_workbook's
        valueRenderOption=FORMULA fetch), which is right for round-tripping formulas
        unmodified but useless for a caller that needs an actual computed value (e.g. a
        MISC3-style sheet's resolved-name column, itself a formula depending on
        NamesMasterList/fixnames). Returns a 2D list; like values.get always does, a row or
        the whole response is shortened wherever trailing cells are blank -- pad it yourself
        if the caller needs every row/column position to line up."""
        range_a1 = _a1_range(self.title, first_row, first_col, last_row, last_col)
        resp = self._gs.spreadsheet.values_get(range_a1, params={"valueRenderOption": "UNFORMATTED_VALUE"})
        return resp.get("values", [])

    def iter_rows(self, min_row=1, max_row=None, min_col=1, max_col=None):
        max_row = self._max_row if max_row is None else max_row
        max_col = self._max_col if max_col is None else max_col
        for r in range(min_row, max_row + 1):
            yield [_CellProxy(self, r, c) for c in range(min_col, max_col + 1)]

    def delete_cols(self, idx, amount=1):
        self._gs.delete_dimension(Dimension.cols, idx, idx + amount - 1)
        self._shift_cache(axis=1, at=idx, amount=amount)
        self._max_col = max(self._max_col - amount, 0)

    def delete_rows(self, idx, amount=1):
        self._gs.delete_dimension(Dimension.rows, idx, idx + amount - 1)
        self._shift_cache(axis=0, at=idx, amount=amount)
        self._max_row = max(self._max_row - amount, 0)

    def _shift_cache(self, axis, at, amount):
        for store in (self._cache, self._pending):
            shifted = {}
            for key, v in store.items():
                idx = key[axis]
                if at <= idx < at + amount:
                    continue  # cell itself deleted
                if idx >= at + amount:
                    key = (key[0] - amount, key[1]) if axis == 0 else (key[0], key[1] - amount)
                shifted[key] = v
            store.clear()
            store.update(shifted)

    def unmerge_cells(self, range_str):
        self._gs.unmerge_cells(range_str)
        self._merges = [r for r in self._merges if r != range_str]

    def set_number_format(self, first_row, first_col, last_row, last_col, pattern):
        """Applies a NUMBER format (e.g. "0.00") to a rectangular range, fired immediately
        like delete_cols/unmerge_cells above rather than staged with the value writes --
        values.update (what wb.save() sends) only ever touches a cell's value, never its
        format, so a freshly-written formula silently inherits whatever format (often none,
        i.e. full float precision) that cell already had. Needed for any sheet this module
        actively rewrites where the underlying template's format can't be trusted to already
        be correct (confirmed empirically: Available - Cats/Pts' VAL/VORP columns had no
        format at all in RESULT despite the template having one, apparently lost somewhere
        in this workbook's history) -- callers that only ever fill an already-correctly-
        formatted template range don't need this."""
        self._gs.spreadsheet.batch_update({"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": self.sheet_id,
                    "startRowIndex": first_row - 1, "endRowIndex": last_row,
                    "startColumnIndex": first_col - 1, "endColumnIndex": last_col,
                },
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        }]})

    def set_data_validation(self, row, col, source_range_a1):
        """Sets a dropdown (ONE_OF_RANGE validation) on one cell, sourced from
        source_range_a1 (e.g. "'AllProjections_S'!$A$5:$A$745" -- a sheet-qualified A1 range,
        not a named range: named ranges can go stale/get corrupted into #REF! by a
        copyTo/duplicate operation the same way any other formula reference can, which is
        exactly the failure this exists to fix -- see rebuild_source_comparison, where this is
        called fresh every run with the CURRENT player-name range instead of trusting
        whatever validation rule happened to survive from before). Fired immediately, like
        set_number_format above, since values.update never touches validation rules."""
        self._gs.spreadsheet.batch_update({"requests": [{
            "setDataValidation": {
                "range": {
                    "sheetId": self.sheet_id,
                    "startRowIndex": row - 1, "endRowIndex": row,
                    "startColumnIndex": col - 1, "endColumnIndex": col,
                },
                "rule": {
                    "condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": f"={source_range_a1}"}]},
                    "strict": True,
                    "showCustomUi": True,
                },
            }
        }]})

    def set_cell_format(self, first_row, first_col, last_row, last_col, user_entered_format, fields):
        """Applies an arbitrary userEnteredFormat (background color, text format/bold/font,
        alignment, ...) to a rectangular range -- the general-purpose sibling of
        set_number_format above, fired immediately for the same reason (values.update never
        touches format). `fields` is the usual Sheets API field mask, e.g.
        "backgroundColor,textFormat.bold" -- only pass what you're actually setting, since an
        empty/unset key isn't the same as "leave whatever was already there alone"."""
        self._gs.spreadsheet.batch_update({"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": self.sheet_id,
                    "startRowIndex": first_row - 1, "endRowIndex": last_row,
                    "startColumnIndex": first_col - 1, "endColumnIndex": last_col,
                },
                "cell": {"userEnteredFormat": user_entered_format},
                "fields": f"userEnteredFormat({fields})",
            }
        }]})

    def update_borders(self, first_row, first_col, last_row, last_col, **borders):
        """Sets outer/inner borders on a rectangular range in one shot (Sheets' updateBorders
        request) -- the right primitive for a bordered block with thin dividers between rows,
        which repeatCell can't express in a single call (repeatCell's border field always
        means every cell's own four edges, so a shared inner edge would need setting from both
        sides consistently; updateBorders instead thinks in terms of the block's outer edges
        plus one shared inner grid, which is also how the Sheets UI itself presents border
        options). Keys: top/bottom/left/right/innerHorizontal/innerVertical, each either None
        (leave that edge alone) or a {"style": "SOLID"|"SOLID_MEDIUM"|..., "width": int} dict."""
        request = {
            "range": {
                "sheetId": self.sheet_id,
                "startRowIndex": first_row - 1, "endRowIndex": last_row,
                "startColumnIndex": first_col - 1, "endColumnIndex": last_col,
            },
        }
        for edge, spec in borders.items():
            if spec is not None:
                request[edge] = spec
        self._gs.spreadsheet.batch_update({"requests": [{"updateBorders": request}]})


class CompatWorkbook:
    def __init__(self, gc, spreadsheet):
        self._gc = gc
        self._sh = spreadsheet
        self._sheets = {}  # title -> CompatWorksheet
        self._order = []
        self._named_range_ids = {}  # name -> namedRangeId, seeded by open_result_workbook

    def __getitem__(self, name):
        return self._sheets[name]

    @property
    def sheetnames(self):
        return list(self._order)

    def remove(self, ws):
        gs_ws = self._sh.worksheet(ws.title)
        self._sh.del_worksheet(gs_ws)
        del self._sheets[ws.title]
        self._order.remove(ws.title)

    def add_worksheet(self, title, rows=1000, cols=26):
        """Creates a brand-new, empty sheet (not copied from anywhere -- unlike every sheet
        open_result_workbook wires up, which all come from SOURCE via copyTo) and returns its
        CompatWorksheet, registered the same way so subsequent wb[title] lookups find it. For a
        caller that needs a sheet the template doesn't carry a copy of at all (see
        ensure_raw_source_sheet in build_aggregate_workbook.py) -- ordinary missing-sheet
        bootstrapping from a template still goes through open_result_workbook."""
        resp = self._sh.batch_update({"requests": [{
            "addSheet": {"properties": {
                "title": title,
                "gridProperties": {"rowCount": rows, "columnCount": cols},
            }}
        }]})
        props = resp["replies"][0]["addSheet"]["properties"]
        gs_ws = gspread.Worksheet(self._sh, props, self._sh.id, self._sh.client)
        cws = CompatWorksheet(self, gs_ws, props["sheetId"])
        self._sheets[title] = cws
        self._order.append(title)
        return cws

    def save(self):
        self._grow_sheets_as_needed()

        data = []
        for ws in self._sheets.values():
            data.extend(_flush_ranges(ws))
            ws._pending.clear()
        if not data:
            log.info("save(): nothing to flush")
            return
        CHUNK = 2000
        for i in range(0, len(data), CHUNK):
            batch = data[i : i + CHUNK]
            self._sh.values_batch_update(
                {"valueInputOption": "USER_ENTERED", "data": batch}
            )
            log.info("Flushed %d range(s) (%d/%d)", len(batch), min(i + CHUNK, len(data)), len(data))

    def _grow_sheets_as_needed(self):
        """Unlike openpyxl, the Sheets API doesn't auto-grow a sheet's grid when a write
        targets a cell past its current dimensions -- values_batch_update rejects it outright
        ("Range ... exceeds grid limits"). Grows any sheet a pending write reaches past,
        before the values flush below."""
        for ws in self._sheets.values():
            needed_rows, needed_cols = ws._max_row, ws._max_col
            cur_rows, cur_cols = ws._gs.row_count, ws._gs.col_count
            if needed_rows > cur_rows or needed_cols > cur_cols:
                new_rows, new_cols = max(needed_rows, cur_rows), max(needed_cols, cur_cols)
                ws._gs.resize(rows=new_rows, cols=new_cols)
                log.info("Grew '%s' to %d rows x %d cols", ws.title, new_rows, new_cols)


def _a1_range(title, r1, c1, r2, c2):
    return f"'{title}'!{rowcol_to_a1(r1, c1)}:{rowcol_to_a1(r2, c2)}"


def _api_value(v):
    """USER_ENTERED (this module's only write mode) parses a plain string the same way the
    Sheets UI would if typed by hand -- a non-formula value like "+/-" is read as the start of
    a numeric expression and lands as a #ERROR! parse failure instead of literal text
    (confirmed empirically on a category-code cell this codebase writes fresh every run). A
    leading apostrophe is the UI's own force-text escape and is stripped back out by Sheets on
    entry, so prefixing one here is enough to protect any non-formula string starting with a
    character USER_ENTERED treats as the possible start of a number -- deliberately excludes
    a leading "=" (a real formula, left untouched) and "@" (Sheets' smart-chip trigger,
    unrelated to this bug and not worth risking a behavior change for)."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("+", "-"):
        return "'" + v
    return v


def _flush_ranges(ws):
    """Encodes ws._pending into (range, values) ValueRange dicts, merged on two axes so a
    table of hundreds of player rows -- every one writing the exact same columns -- becomes a
    handful of 2D blocks instead of one tiny range per row (confirmed empirically: this was
    the single largest contributor to a multi-minute save() on this workbook's size, since the
    Sheets API's per-range overhead in one batchUpdate scales with range COUNT, not just total
    cells). First, each row's written columns collapse into contiguous runs (as before) --
    gaps stay separate so an untouched cell between two writes is never sent (and so never
    silently blanked). Then rows that are themselves consecutive AND have the identical set of
    runs (same shape) are merged into one range per run, spanning all of them, with a proper
    2D values array -- e.g. C5:CJ809 as a single 805-row block rather than 805 single-row
    ranges. A row whose shape differs from its neighbors (a header row, a one-off edit) simply
    starts its own block; correctness doesn't depend on any particular write order, only on
    what ended up in _pending."""
    row_runs = {}
    for (r, c), v in ws._pending.items():
        row_runs.setdefault(r, []).append(c)

    shapes = {}
    for r, cols in row_runs.items():
        cols.sort()
        runs, run = [], [cols[0]]
        for c in cols[1:]:
            if c == run[-1] + 1:
                run.append(c)
            else:
                runs.append((run[0], run[-1]))
                run = [c]
        runs.append((run[0], run[-1]))
        shapes.setdefault(tuple(runs), []).append(r)

    out = []
    for shape, rows in shapes.items():
        rows.sort()
        block = [rows[0]]
        for r in rows[1:]:
            if r == block[-1] + 1:
                block.append(r)
            else:
                out.extend(_shape_block_entries(ws, shape, block))
                block = [r]
        out.extend(_shape_block_entries(ws, shape, block))
    return out


def _shape_block_entries(ws, shape, rows):
    """shape: the (first_col, last_col) runs shared by every row in `rows` (itself a
    contiguous run of row numbers). One range per run, each a 2D block spanning all of
    `rows`."""
    pending = ws._pending
    r1, r2 = rows[0], rows[-1]
    return [
        {
            "range": _a1_range(ws.title, r1, first, r2, last),
            "values": [[_api_value(pending[(r, c)]) for c in range(first, last + 1)] for r in rows],
        }
        for first, last in shape
    ]


# ---------------------------------------------------------------------------
# Workbook open + bootstrap
# ---------------------------------------------------------------------------

def open_result_workbook(source_spreadsheet_id, result_spreadsheet_id, drop_sheets,
                          credentials_path=None):
    """Opens result_spreadsheet_id as a CompatWorkbook, copying over any of
    source_spreadsheet_id's sheets/named-ranges it's still missing (drop_sheets: sheet
    titles to never copy -- stale/orphaned template sheets this workflow doesn't use) and
    removing any of RESULT's own sheets that are now in drop_sheets (a title moved into
    drop_sheets after already being bootstrapped in some earlier run -- drop_sheets alone only
    stops it being copied back in, it doesn't remove a copy that's already there).
    Idempotent: a sheet or named range already present/absent as expected is left untouched."""
    gc = gspread.service_account(filename=str(credentials_path or DEFAULT_CREDENTIALS_PATH))
    source_sh = gc.open_by_key(source_spreadsheet_id)
    result_sh = gc.open_by_key(result_spreadsheet_id)

    source_meta = source_sh.fetch_sheet_metadata()
    source_props_by_title = {s["properties"]["title"]: s["properties"] for s in source_meta["sheets"]}
    source_order = [s["properties"]["title"] for s in source_meta["sheets"]]

    # _prune_dropped_sheets already has to fetch RESULT's metadata to check for anything to
    # prune, and returns it when nothing was pruned -- reused directly instead of this
    # function fetching the exact same metadata again with a second round trip.
    result_meta = _prune_dropped_sheets(result_sh, drop_sheets) or result_sh.fetch_sheet_metadata()
    result_order = [s["properties"]["title"] for s in result_meta["sheets"]]
    result_titles = set(result_order)

    # keep_sheets: every SOURCE sheet not dropped, in SOURCE's own order, PLUS any sheet RESULT
    # already has that SOURCE no longer carries at all (and that isn't itself dropped) --
    # e.g. an active raw-source tab the template stopped keeping a copy of once this script
    # started owning its content completely every run (see ensure_raw_source_sheet in
    # build_aggregate_workbook.py). Without this, such a sheet would be invisible to `wb`
    # entirely (missing from keep_sheets means never wired up below) even though RESULT
    # genuinely has it -- and a caller reaching for it via ensure_raw_source_sheet would then
    # try to create a duplicate and fail, since Sheets rejects two sheets sharing a title.
    # A sheet neither SOURCE nor RESULT currently has stays correctly absent from wb -- that's
    # what lets ensure_raw_source_sheet tell "RESULT already has this" apart from "this needs
    # to be created fresh" (a genuinely new RESULT bootstrapped after SOURCE stopped carrying
    # a copy at all).
    keep_sheets = [t for t in source_order if t not in drop_sheets]
    keep_sheets += [t for t in result_order if t not in drop_sheets and t not in source_props_by_title]

    missing = [t for t in keep_sheets if t not in result_titles]

    if missing:
        _bootstrap_sheets(source_sh, result_sh, source_props_by_title, keep_sheets, missing)
        result_meta = result_sh.fetch_sheet_metadata()

    result_props_by_title = {s["properties"]["title"]: s["properties"] for s in result_meta["sheets"]}
    result_merges_by_title = {s["properties"]["title"]: s.get("merges", []) for s in result_meta["sheets"]}

    wb = CompatWorkbook(gc, result_sh)
    wb._named_range_ids = {
        nr["name"]: nr["namedRangeId"] for nr in result_meta.get("namedRanges", [])
    }

    for title in keep_sheets:
        props = result_props_by_title[title]
        gs_ws = gspread.Worksheet(result_sh, props, result_sh.id, result_sh.client)
        cws = CompatWorksheet(wb, gs_ws, props["sheetId"])
        cws._seed_merges(result_merges_by_title.get(title, []))
        wb._sheets[title] = cws
        wb._order.append(title)

    # Independent of whether any sheet was missing -- a prior run can have copied sheets but
    # died before finishing named ranges (as happened once during development: >60 individual
    # addNamedRange calls in a minute tripped the API's per-minute write quota). Idempotent:
    # _bootstrap_named_ranges only ever requests names RESULT doesn't already have.
    _bootstrap_named_ranges(source_meta, wb, keep_sheets)

    # Must run after named ranges exist and before the real seed-read below, so the seeded
    # cache reflects the repaired (not the copy-corrupted) formula text.
    if missing:
        _repair_broken_named_range_refs(source_sh, result_sh, missing)
        _repair_ghost_sheet_refs(source_sh, result_sh, missing)
        legit_names = {nr["name"] for nr in source_meta.get("namedRanges", [])}
        _cleanup_shadow_named_ranges(result_sh, legit_names)
        wb._named_range_ids = {
            name: rid for name, rid in wb._named_range_ids.items() if name in legit_names
        }

    log.info("Fetching current contents of %d sheet(s)...", len(keep_sheets))
    value_ranges = result_sh.values_batch_get(
        [f"'{t}'" for t in keep_sheets], params={"valueRenderOption": "FORMULA"}
    ).get("valueRanges", [])
    for title, vr in zip(keep_sheets, value_ranges):
        wb._sheets[title]._seed_values(vr.get("values", []))

    return wb


def _prune_dropped_sheets(result_sh, drop_sheets):
    """Deletes any sheet in RESULT whose title is in drop_sheets, plus any named range that
    pointed at one of those sheets (deleteSheet does not auto-clean named ranges the way the
    Sheets UI does) -- both fired in a single batch_update. Returns the metadata this already
    had to fetch to check for matches, so the caller can reuse it instead of fetching the same
    thing again -- but only when nothing was pruned (None otherwise): a prune invalidates that
    metadata (sheets/named ranges it lists no longer exist), so the caller must re-fetch."""
    meta = result_sh.fetch_sheet_metadata()
    to_remove = [s for s in meta["sheets"] if s["properties"]["title"] in drop_sheets]
    if not to_remove:
        return meta
    removed_ids = {s["properties"]["sheetId"] for s in to_remove}
    requests = [{"deleteSheet": {"sheetId": sid}} for sid in removed_ids]
    requests += [
        {"deleteNamedRange": {"namedRangeId": nr["namedRangeId"]}}
        for nr in meta.get("namedRanges", [])
        if nr.get("range", {}).get("sheetId") in removed_ids
    ]
    result_sh.batch_update({"requests": requests})
    log.info(
        "Pruned %d sheet(s) now in drop_sheets but still present in RESULT: %s",
        len(to_remove), ", ".join(s["properties"]["title"] for s in to_remove),
    )


def _bootstrap_sheets(source_sh, result_sh, source_props_by_title, keep_sheets, missing):
    log.info("Bootstrapping %d missing sheet(s) from template: %s", len(missing), ", ".join(missing))
    rename_requests = []
    for title in missing:
        src_ws = gspread.Worksheet(source_sh, source_props_by_title[title], source_sh.id, source_sh.client)
        resp = src_ws.copy_to(result_sh.id)
        rename_requests.append({
            "updateSheetProperties": {
                "properties": {"sheetId": resp["sheetId"], "title": title},
                "fields": "title",
            }
        })
    result_sh.batch_update({"requests": rename_requests})
    log.info("Copied and renamed %d sheet(s)", len(missing))

    try:
        post_meta = result_sh.fetch_sheet_metadata()
        props_by_title = {s["properties"]["title"]: s["properties"] for s in post_meta["sheets"]}
        ordered = [
            gspread.Worksheet(result_sh, props_by_title[t], result_sh.id, result_sh.client)
            for t in keep_sheets if t in props_by_title
        ]
        result_sh.reorder_worksheets(ordered)
    except Exception:
        log.warning("Sheet reorder failed (cosmetic only, continuing)", exc_info=True)

    if "Sheet1" not in keep_sheets:
        try:
            sample = result_sh.values_get("'Sheet1'!A1:Z50").get("values", [])
        except APIError:
            sample = None
        if sample is not None:
            if not sample:
                result_sh.del_worksheet(result_sh.worksheet("Sheet1"))
                log.info("Removed default placeholder 'Sheet1'")
            else:
                log.warning("'Sheet1' has content -- leaving it in place, not deleting")


def _bootstrap_named_ranges(source_meta, wb, keep_sheets):
    """Recreates every SOURCE named range targeting a kept sheet that RESULT doesn't already
    have. Builds and sends all addNamedRange requests in a couple of big batch_update calls
    (not one per name) -- calling set_defined_name() per name here blew through the Sheets
    API's per-minute write-request quota at ~100 names. Source and result sheets have
    identical dimensions right after copy_to, so each source GridRange is reused as-is with
    only its sheetId swapped to the result sheet's id -- no A1 round-trip needed."""
    keep_set = set(keep_sheets)
    sheet_title_by_source_id = {
        s["properties"]["sheetId"]: s["properties"]["title"] for s in source_meta["sheets"]
    }
    existing = set(wb._named_range_ids)

    requests, names_in_order = [], []
    for nr in source_meta.get("namedRanges", []):
        rng = nr.get("range", {})
        title = sheet_title_by_source_id.get(rng.get("sheetId"))
        if title not in keep_set or nr["name"] in existing:
            continue
        result_range = dict(rng)
        result_range["sheetId"] = wb[title].sheet_id
        requests.append({"addNamedRange": {"namedRange": {"name": nr["name"], "range": result_range}}})
        names_in_order.append(nr["name"])

    CHUNK = 300
    for i in range(0, len(requests), CHUNK):
        batch = requests[i : i + CHUNK]
        resp = wb._sh.batch_update({"requests": batch})
        for name, reply in zip(names_in_order[i : i + CHUNK], resp["replies"]):
            wb._named_range_ids[name] = reply["addNamedRange"]["namedRange"]["namedRangeId"]
    log.info("Bootstrapped %d named range(s)", len(requests))


def _repair_broken_named_range_refs(source_sh, result_sh, copied_titles):
    """Google Sheets' cross-spreadsheet copyTo (confirmed empirically, not documented)
    corrupts any formula that references a workbook-scoped named range by its bare name --
    e.g. "=VLOOKUP(A2,TeamPOS,2,FALSE)" comes out the other side as
    "=VLOOKUP(A2,#REF!,2,FALSE)", baked into the formula text. Formulas using an explicit
    Sheet!Range reference are unaffected. Creating the named range in the destination
    afterward (_bootstrap_named_ranges) does NOT self-heal an already-corrupted cell -- the
    #REF! is now the formula's own text, not a broken lookup.

    Repairs by diffing each freshly-copied sheet's formulas against the template's and
    re-pasting the template's original formula text into any cell that now reads "#REF!" in
    the copy but not in the template. That text is fresh (not itself copied), so it resolves
    correctly once a same-named named range exists in the destination -- exactly how every
    other formula this script writes already works.

    Runs the scan-and-fix in a short retry loop: on a large sheet (confirmed on the ~1500-row
    CVals/Vorp and FanPts/Vorp) the corruption doesn't appear to be fully settled by the time
    copyTo's response comes back -- a diff taken immediately after copying missed cells that a
    few seconds later did read as corrupted. Re-scanning after fixing catches anything that
    only became visibly broken after the first pass, and stops as soon as a pass finds
    nothing left to fix."""
    if not copied_titles:
        return
    total_fixed = 0
    for attempt in range(1, 6):
        fixed = _scan_and_fix_ref_errors_once(source_sh, result_sh, copied_titles)
        total_fixed += fixed
        if fixed == 0:
            break
        log.info("Repair pass %d: fixed %d cell(s), re-scanning...", attempt, fixed)
        time.sleep(3)
    log.info("Repaired %d formula cell(s) corrupted by cross-spreadsheet copy (total)", total_fixed)


def _scan_and_fix_ref_errors_once(source_sh, result_sh, copied_titles):
    ranges = [f"'{t}'" for t in copied_titles]
    src_grids = source_sh.values_batch_get(ranges, params={"valueRenderOption": "FORMULA"}).get("valueRanges", [])
    dst_grids = result_sh.values_batch_get(ranges, params={"valueRenderOption": "FORMULA"}).get("valueRanges", [])

    data = []
    for title, src_vr, dst_vr in zip(copied_titles, src_grids, dst_grids):
        src_rows = src_vr.get("values", [])
        dst_rows = dst_vr.get("values", [])
        for r, dst_row in enumerate(dst_rows, start=1):
            src_row = src_rows[r - 1] if r - 1 < len(src_rows) else []
            for c, dst_val in enumerate(dst_row, start=1):
                if not (isinstance(dst_val, str) and "#REF!" in dst_val):
                    continue
                src_val = src_row[c - 1] if c - 1 < len(src_row) else None
                if src_val is None or "#REF!" in src_val or src_val == dst_val:
                    continue  # not a copy artifact -- already broken in the template, or unchanged
                data.append({"range": _a1_range(title, r, c, r, c), "values": [[src_val]]})

    if not data:
        return 0
    CHUNK = 2000
    for i in range(0, len(data), CHUNK):
        result_sh.values_batch_update(
            {"valueInputOption": "USER_ENTERED", "data": data[i : i + CHUNK]}
        )
    return len(data)


_DIRECT_SHEET_REF = re.compile(r"(?:'[^']+'|[A-Za-z_][A-Za-z0-9_]*)!")


def _repair_ghost_sheet_refs(source_sh, result_sh, copied_titles):
    """Cross-spreadsheet copyTo can also corrupt a formula that references another sheet
    directly by name (e.g. "=SUM(Settings!A1:A10)"), even though the formula's own text comes
    through completely untouched -- confirmed empirically: the cell evaluates to #REF! with
    the underlying detail "Unresolved sheet name 'Settings'", meaning copyTo preserved a stale
    internal sheet-id binding instead of relinking it to the destination's own same-named
    sheet. This is a different failure mode than _repair_broken_named_range_refs (which
    catches named-range corruption baked into the formula TEXT as a literal "#REF!") -- here
    the text is fine and only a live evaluation reveals the break, so this proactively
    re-pastes every direct cross-sheet-reference formula's own text (never named-range lookups
    or INDIRECT(), neither of which are affected) to force Sheets to reparse and rebind it
    fresh, rather than reactively scanning for damage that isn't visible in the text. Blind
    and unconditional per matching cell -- a formula that was already fine is unaffected by
    being re-pasted verbatim."""
    ranges = [f"'{t}'" for t in copied_titles]
    grids = source_sh.values_batch_get(ranges, params={"valueRenderOption": "FORMULA"}).get("valueRanges", [])

    data = []
    for title, vr in zip(copied_titles, grids):
        for r, row in enumerate(vr.get("values", []), start=1):
            for c, v in enumerate(row, start=1):
                if not (isinstance(v, str) and v.startswith("=") and "INDIRECT(" not in v.upper()):
                    continue
                if _DIRECT_SHEET_REF.search(v):
                    data.append({"range": _a1_range(title, r, c, r, c), "values": [[v]]})

    if not data:
        return 0
    CHUNK = 2000
    for i in range(0, len(data), CHUNK):
        result_sh.values_batch_update(
            {"valueInputOption": "USER_ENTERED", "data": data[i : i + CHUNK]}
        )
    log.info("Re-pasted %d formula cell(s) with direct cross-sheet references (ghost-reference repair)", len(data))
    return len(data)


def _cleanup_shadow_named_ranges(result_sh, legit_names):
    """copyTo also spontaneously creates extra named ranges with synthetic sheet-qualified
    names (e.g. "'ADPYahoo'!ADPYahoo") as a side effect of copying a formula that referenced a
    name it couldn't yet resolve in the destination -- confirmed empirically, undocumented.
    They're functionally inert once _repair_broken_named_range_refs restores the real formula
    text (nothing references them), but clutter the spreadsheet's named-range list. Deletes
    any named range in the result whose name doesn't match one of the template's own."""
    meta = result_sh.fetch_sheet_metadata()
    shadow = [nr for nr in meta.get("namedRanges", []) if nr["name"] not in legit_names]
    if not shadow:
        return
    requests = [{"deleteNamedRange": {"namedRangeId": nr["namedRangeId"]}} for nr in shadow]
    CHUNK = 300
    for i in range(0, len(requests), CHUNK):
        result_sh.batch_update({"requests": requests[i : i + CHUNK]})
    log.info("Removed %d shadow named range(s) created as a copyTo side effect", len(shadow))


# ---------------------------------------------------------------------------
# Named ranges (replaces openpyxl's wb.defined_names[name].attr_text = new_ref)
# ---------------------------------------------------------------------------

_SIMPLE_SHEET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_sheet(title):
    if _SIMPLE_SHEET_NAME.match(title):
        return title
    return "'" + title.replace("'", "''") + "'"


def _split_ref(ref):
    sheet_part, range_part = ref.split("!", 1)
    if sheet_part.startswith("'") and sheet_part.endswith("'"):
        sheet_part = sheet_part[1:-1].replace("''", "'")
    return sheet_part, range_part


def set_defined_name(wb, name, new_ref):
    set_defined_names(wb, {name: new_ref})


def set_defined_names(wb, mapping):
    """Like set_defined_name, but for many names at once, in as few real API round trips as
    possible -- one batch_update covers the whole mapping (chunked at 300 requests, same limit
    _bootstrap_named_ranges already chunks at) instead of one per name. Firing set_defined_name
    in a loop (confirmed empirically: 24 calls across 8 sources' 3 named ranges each, on top of
    the ~22 already scattered through build_aggregate_workbook.py) is exactly the kind of load
    that trips the Sheets API's per-minute write quota -- see _bootstrap_named_ranges' own
    docstring, which hit the same wall at >60 addNamedRange calls in a minute -- and the
    resulting throttling/backoff is what actually made a run slow, not the request work itself."""
    requests = []
    for name, new_ref in mapping.items():
        sheet_title, range_part = _split_ref(new_ref)
        sheet_id = wb[sheet_title].sheet_id
        grid_range = a1_range_to_grid_range(range_part.replace("$", ""), sheet_id)

        existing_id = wb._named_range_ids.get(name)
        if existing_id:
            requests.append({
                "updateNamedRange": {
                    "namedRange": {"namedRangeId": existing_id, "name": name, "range": grid_range},
                    "fields": "range",
                }
            })
        else:
            requests.append({"addNamedRange": {"namedRange": {"name": name, "range": grid_range}}})
            add_names_in_order.append(name)

    CHUNK = 300
    for i in range(0, len(requests), CHUNK):
        batch = requests[i : i + CHUNK]
        resp = wb._sh.batch_update({"requests": batch})
        for req, reply in zip(batch, resp["replies"]):
            if "addNamedRange" in req:
                name = req["addNamedRange"]["namedRange"]["name"]
                wb._named_range_ids[name] = reply["addNamedRange"]["namedRange"]["namedRangeId"]
