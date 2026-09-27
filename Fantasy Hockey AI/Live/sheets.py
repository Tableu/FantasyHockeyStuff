"""Read-only tksheet tables for the Live windows (draft_gui.py, plan_gui.py).

    bind_header_clicks(sheet, on_column)   a click on a header calls on_column(index); a drag
                                           across it (resizing a column) does not
    Table(parent, columns, ...)            a sheet of rows, each coloured by its tags, with an
                                           optional sort on a header click
"""

from tksheet import Sheet

TABLE_FONT = ("Segoe UI", 9, "normal")
HEADER_FONT = ("Segoe UI", 9, "bold")
TEXT_KEYS = {"player", "add", "drop", "note", "slot", "after"}     # left-aligned; the rest centred


def bind_header_clicks(sheet, on_column) -> None:
    """Call on_column(column index) when a header is clicked -- pressed and released on the same
    column without moving -- so a drag that resizes a column does not also sort by it. Tk sends a
    second quick click as a double-click, not a press, so that counts as a click too."""
    press = {}

    def down(event):
        press["at"] = ((event.x_root, sheet.identify_column(event))
                       if sheet.identify_region(event) == "header" else None)

    def up(event):
        start, press["at"] = press.get("at"), None
        if (start is None or start[1] is None or sheet.identify_region(event) != "header"
                or abs(event.x_root - start[0]) > 3 or sheet.identify_column(event) != start[1]):
            return
        on_column(start[1])

    sheet.bind("<ButtonPress-1>", down, add="+")
    sheet.bind("<ButtonRelease-1>", up, add="+")
    sheet.bind("<Double-Button-1>", down, add="+")


class Table:
    """A read-only sheet: `columns` is [(key, heading, width)]; `styles` maps a row tag to its
    {"bg": ..., "fg": ...}. `set_rows([(values, tags)])` redraws it; a row takes the background
    of its first tag that has one and the foreground likewise. With `on_sort`, a header click
    calls on_sort(key), and `set_rows(..., sort_key=, descending=)` marks that header."""

    def __init__(self, parent, columns, styles, on_sort=None):
        self.frame = parent
        self.keys = [c[0] for c in columns]
        self.headings = [c[1] for c in columns]
        self.styles = styles
        self.sheet = Sheet(parent, show_row_index=False, show_top_left=False, font=TABLE_FONT,
                           header_font=HEADER_FONT, default_row_height=22, table_bg="white")
        self.sheet.enable_bindings("single_select", "row_select", "column_width_resize",
                                   "arrowkeys", "copy")
        self.sheet.set_sheet_data([], reset_col_positions=True, redraw=False)
        self.sheet.headers(self.headings, redraw=False)
        self.sheet.set_column_widths([c[2] for c in columns])
        self.sheet.align_columns({i: "w" if k in TEXT_KEYS else "center" for i, k in enumerate(self.keys)},
                                 align_header=True, redraw=False)
        self.on_sort = on_sort
        if on_sort is not None:
            bind_header_clicks(self.sheet, self._header_clicked)
        self.sheet.pack(fill="both", expand=True)
        self.rows = []

    def _header_clicked(self, index):
        if index < len(self.keys):
            self.on_sort(self.keys[index])

    def set_rows(self, rows, sort_key=None, descending=False) -> None:
        self.rows = [[("" if v is None else v) for v in values] for values, _ in rows]
        sheet = self.sheet
        sheet.dehighlight_all(redraw=False)
        sheet.set_sheet_data(self.rows, reset_col_positions=False, redraw=False)
        looks = {}
        for i, (_, tags) in enumerate(rows):
            styles = [self.styles[t] for t in tags if t in self.styles]
            bg = next((s["bg"] for s in styles if "bg" in s), None)
            fg = next((s["fg"] for s in styles if "fg" in s), None)
            if bg or fg:
                looks.setdefault((bg, fg), []).append(i)
        for (bg, fg), indexes in looks.items():
            sheet.highlight_rows(indexes, bg=bg, fg=fg, redraw=False)
        sheet.headers([h + ((" ▼" if descending else " ▲") if k == sort_key else "")
                       for k, h in zip(self.keys, self.headings)], redraw=False)
        sheet.refresh()
