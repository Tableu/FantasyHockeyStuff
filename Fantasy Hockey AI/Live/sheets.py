"""Read-only tksheet tables for the Live windows (draft_gui.py, plan_gui.py).

    bind_header_clicks(sheet, on_column)   a click on a header calls on_column(index); a drag
                                           across it (resizing a column) does not
    bind_row_clicks(sheet, on_row)         a click on a row calls on_row(index)
    Table(parent, columns, ...)            a sheet of rows, each coloured by its tags, with an
                                           optional sort on a header click and action on a row click
"""

import tkinter.font as tkfont

from tksheet import Sheet

TABLE_FONT = ("Segoe UI", 9, "normal")
HEADER_FONT = ("Segoe UI", 9, "bold")
TEXT_KEYS = {"player", "add", "drop", "note", "slot", "after"}     # left-aligned; the rest centred
# Columns whose text wraps onto more lines, the row growing to fit, rather than running past the
# cell's edge: an add or a drop can be a long name with a status and a start day, or a week plan's
# whole schedule. Re-wrapped when a column is resized.
WRAP_KEYS = {"add", "drop"}
ROW_HEIGHT = 22
CELL_PADDING = 12               # pixels of a column's width the text may not use


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


def bind_row_clicks(sheet, on_row) -> None:
    """Call on_row(row index) when a row of the table is clicked (pressed and released on it)."""
    press = {}

    def down(event):
        press["at"] = (sheet.identify_row(event) if sheet.identify_region(event) == "table"
                       else None)

    def up(event):
        start, press["at"] = press.get("at"), None
        if start is None or sheet.identify_region(event) != "table" or sheet.identify_row(event) != start:
            return
        on_row(start)

    sheet.bind("<ButtonPress-1>", down, add="+")
    sheet.bind("<ButtonRelease-1>", up, add="+")


class Table:
    """A read-only sheet: `columns` is [(key, heading, width)]; `styles` maps a row tag to its
    {"bg": ..., "fg": ...}. `set_rows([(values, tags)])` redraws it; a row takes the background
    of its first tag that has one and the foreground likewise. With `on_sort`, a header click
    calls on_sort(key), and `set_rows(..., sort_key=, descending=)` marks that header."""

    def __init__(self, parent, columns, styles, on_sort=None, on_row_click=None):
        self.frame = parent
        self.keys = [c[0] for c in columns]
        self.headings = [c[1] for c in columns]
        self.styles = styles
        # A blank heading stays blank: tksheet otherwise shows the column's letter ("H").
        self.sheet = Sheet(parent, show_row_index=False, show_top_left=False, font=TABLE_FONT,
                           header_font=HEADER_FONT, default_row_height=ROW_HEIGHT, table_bg="white",
                           show_default_header_for_empty=False)
        self.font = tkfont.Font(root=parent, font=TABLE_FONT)
        self.wrapped = [i for i, k in enumerate(self.keys) if k in WRAP_KEYS]
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
        if on_row_click is not None:
            bind_row_clicks(self.sheet, lambda i: on_row_click(i) if i < len(self.rows) else None)
        if self.wrapped:
            self.sheet.extra_bindings("column_width_resize", lambda event: self._rewrap())
        self.sheet.pack(fill="both", expand=True)
        self.rows = []

    def _header_clicked(self, index):
        if index < len(self.keys):
            self.on_sort(self.keys[index])

    def _wrap(self, text, width) -> list:
        """`text` broken at spaces into lines no wider than `width` pixels. A continuation line keeps
        the first line's indent (the week plans' ranked options); a word wider than the cell stays
        whole on its own line."""
        text = str(text)
        if not text or self.font.measure(text) <= width:
            return [text]
        indent = text[:len(text) - len(text.lstrip(" "))]
        lines, line = [], ""
        for word in text.split():
            candidate = indent + word if not line else f"{line} {word}"
            if not line or self.font.measure(candidate) <= width:
                line = candidate
            else:
                lines.append(line)
                line = indent + word
        lines.append(line)
        return lines

    def _display(self):
        """The rows as shown: wrapped columns broken onto lines at their current widths, and each
        row's height, enough for its tallest cell."""
        if not self.wrapped or not self.rows:
            return self.rows, None
        widths = self.sheet.get_column_widths()
        line = self.font.metrics("linespace")
        shown, heights = [], []
        for values in self.rows:
            values, tallest = list(values), 1
            for i in self.wrapped:
                if i < len(values) and i < len(widths):
                    lines = self._wrap(values[i], max(widths[i] - CELL_PADDING, 20))
                    values[i] = "\n".join(lines)
                    tallest = max(tallest, len(lines))
            shown.append(values)
            heights.append(ROW_HEIGHT if tallest == 1 else tallest * line + 8)
        return shown, heights

    def _rewrap(self) -> None:
        """A column was resized: wrap the text again at the new widths."""
        shown, heights = self._display()
        self.sheet.set_sheet_data(shown, reset_col_positions=False, redraw=False)
        if heights is not None:
            self.sheet.set_row_heights(heights)
        self.sheet.refresh()

    def set_rows(self, rows, sort_key=None, descending=False) -> None:
        # `self.rows` keeps the text unwrapped: it is what a row click or a copy is about.
        self.rows = [[("" if v is None else v) for v in values] for values, _ in rows]
        sheet = self.sheet
        sheet.dehighlight_all(redraw=False)
        shown, heights = self._display()
        sheet.set_sheet_data(shown, reset_col_positions=False, redraw=False)
        if heights is not None:
            sheet.set_row_heights(heights)
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
