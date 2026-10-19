"""Places in Compose's geographic constraint: a coordinate converter, and the places to limit searches to.

Places are nominated like hit terms (by the agent with `propose --place`, or added here) and reviewed one by one.
A place can't be approved until the analyst has chosen the country it's in, that country is permitted by the
warrant's countries, and the point is inside it (places.check_place). Each place's radius is signed with it.
"""

from __future__ import annotations

import json
import re
import time
import tkinter as tk
from tkinter import ttk

from . import msg as messagebox

from .. import geo
from ..places import (DEFAULT_RADIUS_KM, MAX_LABEL, MAX_REASON, PlaceError, check_place, duplicate_of,
                      permitted_country)
from ..scope import countries
from . import theme
from .widgets import (GrowTable, SearchPicker, _Dialog, bind_decision_keys, confirm_bulk, copy_text, on_resize,
                      responsive_wrap)

STATUS_WRAP = 210  # px: the import review's status text wraps here, and its column is at least this wide
STATUS = {"pending": "● Pending", "approved": "✓ Included", "rejected": "✕ Rejected"}
CONVERSION_WARNING = "Check this conversion yourself before using it: a misread coordinate puts the circle elsewhere."


def _flow_controls(frame, controls):
    """Wrap controls using their measured widths (or explicit pixel widths for editable fields)."""
    def flow(_event=None):
        available = frame.winfo_width()
        if available <= 1:
            return
        x = y = height = 0
        for widget, pixels in controls:
            width = min(pixels or widget.winfo_reqwidth(), available)
            if x and x + width > available:
                x, y, height = 0, y + height + 6, 0
            h = widget.winfo_reqheight()
            widget.place(x=x, y=y, width=width, height=h)
            x += width + 6
            height = max(height, h)
        frame.configure(height=y + height)

    on_resize(frame, flow)
    frame.after_idle(flow)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "place"


def country_choices(scope: dict, known: dict) -> dict[str, str]:
    """Every country as "DE — Germany", in code order, the ones this warrant doesn't permit marked with why
    ("excluded: GDPR", or "not included"), so a file's country is always findable and the reason is visible."""
    excluded = {r["code"]: r.get("reason", "") for r in (scope or {}).get("exclude") or []}
    out = {}
    for code in sorted(known):
        label = f"{code} — {known[code]}"
        if code in excluded:
            label += f"  (excluded: {excluded[code]})" if excluded[code] else "  (excluded)"
        elif not permitted_country(scope, code):
            label += "  (not included)"
        out[label] = code
    return out


def choice_for(choices: dict[str, str], code: str) -> str:
    """The list entry for a code (or the code itself, when it isn't a known country)."""
    return next((label for label, c in choices.items() if c == code), code)


def convert(text: str, form: str) -> tuple[geo.Coordinate | None, str]:
    """(coordinate, one line to show) for the converter: lat, lon, how it was read, and which country it's in."""
    try:
        c = geo.parse_coordinate(text, form)
    except geo.CoordinateError as exc:
        return None, f"✕ {exc}"
    how = f"{c.form}, {c.detail}" if c.detail else c.form
    band = f"  ·  {c.note}" if c.note and not c.detail else ""  # e.g. how a UTM band letter was read
    return c, f"{text.strip()} → {c.text()}   ·   {how}   ·   {geo.where_is(c.lat, c.lon)}{band}"


class PlaceDialog(_Dialog):
    """Add or edit a place: label, coordinate (any form, shown converted), radius, country and reason. It won't
    close with a place that couldn't be approved: the point must be inside the chosen, permitted country."""

    def __init__(self, master, *, place: dict | None, scope: dict, max_radius_km: float, known: dict, title: str,
                 confirm: str = ""):
        super().__init__(master, title)
        self.scope, self.max_radius_km, self.known = scope, max_radius_km, known
        place = place or {}
        self.base = place
        f = self.frame
        if confirm:  # the country (or point) came from a file, the agent or the map: check it against the source
            ttk.Label(f, text=confirm, style="Warn.TLabel", wraplength=520, justify="left").pack(anchor="w", pady=(0, 6))
        self.label = tk.StringVar(value=place.get("label", ""))
        # An existing place opens on its stored lat, lon (read as lat, lon), never on its original text: re-reading
        # that text could interpret it differently (a lon, lat proposal would flip). The original is shown below.
        stored = "lat" in place and "lon" in place
        self.coordinate = tk.StringVar(value=geo.format_latlon(place["lat"], place["lon"]) if stored
                                       else place.get("given_as", ""))
        self.form = tk.StringVar(value=geo.FORMS["latlon"] if stored else geo.FORMS["auto"])
        self.radius = tk.StringVar(value=f"{place.get('radius_km', DEFAULT_RADIUS_KM):g}")
        self.country_names = country_choices(scope, known)
        chosen = place.get("country", "")
        self.country = tk.StringVar(value=choice_for(self.country_names, chosen) if chosen else "")
        self.reason = tk.StringVar(value=place.get("reason", ""))

        def row(text, widget, note=None):
            ttk.Label(f, text=text).pack(anchor="w", pady=(10, 2))
            widget.pack(anchor="w", fill="x")
            if note:
                ttk.Label(f, text=note, style="Small.TLabel", wraplength=520, justify="left").pack(anchor="w")

        row("Label", ttk.Entry(f, textvariable=self.label, width=50))
        coord = ttk.Frame(f)
        ttk.Combobox(coord, textvariable=self.form, values=list(geo.FORMS.values()), state="readonly",
                     width=20).pack(side="left")
        entry = ttk.Entry(coord, textvariable=self.coordinate, width=40)
        entry.pack(side="left", padx=(6, 0), fill="x", expand=True)
        row("Coordinate (lat, lon, MGRS, UTM, or degrees-minutes-seconds)", coord)
        self.preview = ttk.Label(f, style="Muted.TLabel", wraplength=520, justify="left")
        self.preview.pack(anchor="w", pady=(4, 0))
        ttk.Label(f, text=CONVERSION_WARNING, style="Warn.TLabel", wraplength=520, justify="left").pack(anchor="w")
        row("Maximum search radius (km)", ttk.Entry(f, textvariable=self.radius, width=10),
            f"Signed with this place: every search uses exactly this circle. 0.1 km is 100 m; at most "
            f"{max_radius_km:g} km.")
        self.precision_note = ttk.Label(f, style="Warn.TLabel", wraplength=520, justify="left")
        self.precision_note.pack(anchor="w")
        ttk.Label(f, text=geo.MAP_LIMITATION, style="Small.TLabel", wraplength=520,
                  justify="left").pack(anchor="w")
        hint = place.get("country_suggested")
        self.country_picker = SearchPicker(f, list(self.country_names), self.country, width=48)
        row("Country it's in (type to search, e.g. \"ger\" or \"DE\")", self.country_picker,
            "Choose it yourself; the point is checked against that country's border. Countries this warrant "
            "doesn't permit are marked; a place there can't be included until you include it (or stop excluding it)."
            + (f" The agent suggested {hint}." if hint else ""))
        row("Reason", ttk.Entry(f, textvariable=self.reason, width=60))
        buttons = ttk.Frame(f)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="OK", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        for var in (self.coordinate, self.form, self.radius, self.country):
            var.trace_add("write", lambda *_a: self._update_preview())
        self._update_preview()
        self.place_id = place.get("place_id")
        entry.focus_set()

    def _form_key(self) -> str:
        return next((k for k, v in geo.FORMS.items() if v == self.form.get()), "auto")

    def _update_preview(self):
        _c, message = convert(self.coordinate.get(), self._form_key()) if self.coordinate.get().strip() else (None, "")
        if _c:
            evidence = geo.input_evidence(_c, self.coordinate.get(), self.base)
            message = f"{_c.text()} · {geo.evidence_text(evidence)} · {geo.where_is(_c.lat, _c.lon)}"
            code = self.country_names.get(self.country.get(), self.country.get().split(" ")[0])
            if code and geo.in_country(code, _c.lat, _c.lon):
                message += " · Country agrees with the bundled map"
            try:
                warning = geo.precision_warning({**evidence, "radius_km": float(self.radius.get())})
            except ValueError:
                warning = ""
        else:
            warning = ""
        self.preview.configure(text=message)
        self.precision_note.configure(text=warning)

    def _ok(self):
        self.country_picker.resolve()
        c, message = convert(self.coordinate.get(), self._form_key())
        if c is None:
            messagebox.showerror(self.title(), message.lstrip("✕ "), parent=self)
            return
        try:
            radius = float(self.radius.get())
        except ValueError:
            messagebox.showerror(self.title(), "The radius must be a number of kilometres (0.1 is 100 m).", parent=self)
            return
        place = {**self.base, "label": " ".join(self.label.get().split()), "lat": round(c.lat, 6),
                 "lon": round(c.lon, 6), "radius_km": round(radius, 3), "country": self.country_names.get(self.country.get(), self.country.get().split(" ")[0]),
                 "reason": " ".join(self.reason.get().split()),
                 **geo.input_evidence(c, self.coordinate.get(), self.base), "source": self.base.get("source", "human"), "country_confirmed": True,
                 "place_id": self.place_id or f"p-{_slug(self.label.get())}"}
        try:
            # Validate the other fields before asking about precision. The temporary key is
            # never saved; only the explicit acknowledgment below is kept with the place.
            check_place({**place, "radius_km": radius, "precision_acknowledged": geo.precision_key(place)},
                        self.scope, self.max_radius_km)
        except PlaceError as exc:
            messagebox.showerror(self.title(), str(exc), parent=self)
            return
        if geo.precision_needs_review(place):
            if not messagebox.askyesno("Acknowledge coordinate precision",
                                      geo.precision_warning(place) + "\n\nUse this circle with that limitation?",
                                      parent=self):
                return
            place["precision_acknowledged"] = geo.precision_key(place)
        self.result = place
        self.destroy()

    @classmethod
    def ask(cls, master, **kwargs):
        return cls(master, **kwargs).run()


NONE = "(none)"
FIELD_LABELS = {"coordinate": "Coordinate (one column)", "lat": "Latitude", "lon": "Longitude", "label": "Label",
                "radius": "Radius", "country": "Country", "reason": "Reason"}
DROP_TEXT = "Drag a CSV, KML, KMZ or GeoJSON file here"


def _load_tkdnd(toplevel, attempts: int = 5) -> None:
    """Load the tkdnd extension, retrying a failed load. On Windows, Tcl's package scan sometimes can't open
    tkdnd's pkgIndex.tcl ("no such file or directory", though it exists: a file briefly held by an on-access
    scanner), and drag and drop silently went missing in about one launch in five; the next attempt works."""
    from tkinterdnd2 import TkinterDnD

    for attempt in range(attempts):
        try:
            TkinterDnD._require(toplevel)
            return
        except RuntimeError as exc:  # "Unable to load tkdnd library." (not "Platform not supported.")
            if "Unable to load" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))  # at most a second in all, and only when loading failed


def _enable_drop(widget, on_file) -> bool:
    """Accept files dropped on `widget` (tkdnd, via tkinterdnd2). False when drag and drop isn't available."""
    try:
        import tkinterdnd2

        _load_tkdnd(widget.winfo_toplevel())
        widget.drop_target_register(tkinterdnd2.DND_FILES)
    except Exception:  # noqa: BLE001 - no tkdnd for this platform: Browse still works
        return False

    def dropped(event):
        files = widget.tk.splitlist(event.data)
        if files:
            on_file(files[0])
        return event.action

    widget.dnd_bind("<<Drop>>", dropped)
    return True


class ImportChooser(_Dialog):
    """The first step of Import list: drop a file or browse for one, with what the importer reads best. A file
    that can't be read is reported here, with why, and the dialog stays open for another."""

    def __init__(self, master, *, max_radius_km: float):
        from ..place_import import ACCEPTED_VALUES, BEST_FORMAT, FILE_TYPES, LIMITS

        super().__init__(master, "Import places")
        self.max_radius_km = max_radius_km
        f = self.frame
        ttk.Label(f, text="Import a list of places", style="Heading.TLabel").pack(anchor="w")
        p = theme.PALETTE
        self.drop = tk.Canvas(f, width=620, height=130, highlightthickness=0, bg=p["surface"])
        self.drop.pack(anchor="w", pady=(10, 0))
        self.drop.create_rectangle(4, 4, 616, 126, dash=(6, 4), outline=p["muted"], width=2)
        self.drop_label = self.drop.create_text(310, 50, text=DROP_TEXT, fill=p["text"], font="LW.Strong")
        self.drop.create_text(310, 78, text="or", fill=p["muted"], font="LW.Small")
        browse = ttk.Button(self.drop, text="Browse for a file…", command=self.browse)
        self.drop.create_window(310, 104, window=browse)
        self.can_drop = _enable_drop(self.drop, lambda path: self.choose(path))
        if not self.can_drop:
            self.drop.itemconfigure(self.drop_label, text="Choose a CSV, KML, KMZ or GeoJSON file")
        self.error = ttk.Label(f, style="Danger.TLabel", wraplength=620, justify="left")
        self.error.pack(anchor="w", pady=(8, 0))
        for title, body in (("Best format", BEST_FORMAT), ("Accepted values", ACCEPTED_VALUES),
                            ("Accepted files", f"{FILE_TYPES}. {LIMITS}")):
            ttk.Label(f, text=title, style="Strong.TLabel").pack(anchor="w", pady=(10, 2))
            ttk.Label(f, text=body, style="Small.TLabel" if title != "Best format" else "TLabel",
                      font="LW.Mono" if title == "Best format" else None, wraplength=620,
                      justify="left").pack(anchor="w")
        ttk.Label(f, text="Next you'll check each place and fill in anything the file doesn't have. Every place "
                          "arrives pending, to be included with A once its country is confirmed.",
                  style="Muted.TLabel", wraplength=620, justify="left").pack(anchor="w", pady=(12, 0))
        buttons = ttk.Frame(f)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")

    def browse(self):
        from tkinter import filedialog

        chosen = filedialog.askopenfilename(parent=self, title="Import places", filetypes=[
            ("Places", "*.csv *.geojson *.json *.kml *.kmz *.txt"), ("All files", "*.*")])
        if chosen:
            self.choose(chosen)

    def choose(self, path):
        """Read the file; keep it if it gives places, otherwise say why it can't be used."""
        from pathlib import Path

        from ..place_import import ImportError_, read_places

        path = Path(path)
        try:
            read_places(path, max_radius_km=self.max_radius_km)
        except (ImportError_, OSError) as exc:
            self.error.configure(text=f"{path.name} can't be imported: {exc}. See the best format below.")
            return
        self.result = path
        self.destroy()

    @classmethod
    def ask(cls, master, **kwargs):
        return cls(master, **kwargs).run()


class ImportReview(_Dialog):
    """The second step: one row per place read from the file, every cell editable. Blank or wrong cells are
    flagged; blanks can be filled one by one or all at once. Only complete rows can be imported."""

    MAX_SHOWN = 100

    def __init__(self, master, path, *, scope: dict, max_radius_km: float, room: int, known: dict,
                 existing: list[dict] | None = None):
        from ..place_import import read_places

        super().__init__(master, f"Import places · {path.name}")
        self.path, self.scope, self.max_radius_km, self.room, self.read = path, scope, max_radius_km, room, read_places
        self.known = known
        self.existing = list(existing or [])  # places already in Compose (not rejected)
        self.data = read_places(path, max_radius_km=max_radius_km)
        from .widgets import ScrollableFrame

        self.page = ScrollableFrame(self.frame)
        self.page.pack(fill="both", expand=True)
        self.page.canvas.configure(height=500, width=900)
        f = self.page.body
        self.header = ttk.Label(f, style="Strong.TLabel")
        self.header.pack(anchor="w")
        map_note = ttk.Label(f, text=geo.MAP_LIMITATION, style="Small.TLabel", wraplength=700, justify="left")
        map_note.pack(anchor="w")
        mapping = ttk.Frame(f)
        mapping.pack(fill="x", pady=(8, 0))
        for column in range(4):
            mapping.columnconfigure(column, weight=1, uniform="mapping")
        self.vars = {}
        for i, (key, label) in enumerate(FIELD_LABELS.items()):
            cell = ttk.Frame(mapping)
            cell.grid(row=i // 4, column=i % 4, sticky="ew", padx=(0, 14), pady=2)
            ttk.Label(cell, text=label, style="Small.TLabel").pack(anchor="w")
            var = tk.StringVar(value=self.data.mapping.get(key) or NONE)
            box = ttk.Combobox(cell, textvariable=var, values=[NONE] + self.data.columns, state="readonly", width=1)
            box.pack(fill="x")
            box.bind("<<ComboboxSelected>>", lambda _e: self.reread())
            self.vars[key] = var
        cell = ttk.Frame(mapping)
        cell.grid(row=1, column=3, sticky="ew", pady=2)
        ttk.Label(cell, text="Radius unit in the file", style="Small.TLabel").pack(anchor="w")
        self.unit = tk.StringVar(value=self.data.radius_unit)
        unit = ttk.Combobox(cell, textvariable=self.unit, values=["km", "m"], state="readonly", width=6)
        unit.pack(anchor="w")
        unit.bind("<<ComboboxSelected>>", lambda _e: self.reread())
        cell = ttk.Frame(mapping)
        cell.grid(row=2, column=0, sticky="ew", pady=2)
        ttk.Label(cell, text="Coordinate column is", style="Small.TLabel").pack(anchor="w")
        self.coordinate_form = tk.StringVar(value=geo.FORMS.get(self.data.coordinate_form, geo.FORMS["auto"]))
        form = ttk.Combobox(cell, textvariable=self.coordinate_form, values=list(geo.FORMS.values()),
                            state="readonly", width=1)
        form.pack(fill="x")
        form.bind("<<ComboboxSelected>>", lambda _e: self.reread())
        self.unit_note = ttk.Label(f, style="Warn.TLabel", wraplength=1000, justify="left")
        self.unit_note.pack(anchor="w")
        mapping_note = ttk.Label(f, text="Changing a column reads the file again (edits below are replaced).",
                                 style="Small.TLabel", justify="left")
        mapping_note.pack(anchor="w")

        fill = ttk.Frame(f)
        fill.pack(fill="x", pady=(10, 0))
        fill_label = ttk.Label(fill, text="Fill blanks:")
        self.fill_radius = tk.StringVar(value=f"{DEFAULT_RADIUS_KM:g}")
        radius_entry = ttk.Entry(fill, textvariable=self.fill_radius)
        radius_button = ttk.Button(fill, text="radius (km)", command=lambda: self.fill("radius", self.fill_radius.get()))
        self.choices = country_choices(scope, known)
        self.choice_list = [""] + list(self.choices)
        self.fill_country = tk.StringVar()
        country_entry = SearchPicker(fill, self.choice_list, self.fill_country)
        country_button = ttk.Button(fill, text="country", command=lambda: self.fill("country", self.fill_country.get()))
        self.fill_reason = tk.StringVar(value=f"Imported from {path.name}")
        reason_entry = ttk.Entry(fill, textvariable=self.fill_reason)
        reason_button = ttk.Button(fill, text="reason", command=lambda: self.fill("reason", self.fill_reason.get()))
        _flow_controls(fill, [(fill_label, None), (radius_entry, 70), (radius_button, None),
                              (country_entry, 210), (country_button, None), (reason_entry, 220), (reason_button, None)])

        self.grid_frame = ttk.Frame(f)
        self.grid_frame.pack(fill="x", pady=(10, 0))
        self.skipped = ttk.Label(f, style="Small.TLabel", wraplength=1040, justify="left")
        self.skipped.pack(anchor="w", pady=(6, 0))
        self.summary = ttk.Label(f, wraplength=1040, justify="left")
        self.summary.pack(anchor="w", pady=(4, 0))
        buttons = ttk.Frame(self.frame)
        buttons.pack(fill="x", pady=(12, 0))
        self.ok = ttk.Button(buttons, text="Import", style="Accent.TButton", command=self._ok)
        self.ok.pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.rows: list[dict] = []
        self.build_rows()
        # Mapping, fill controls and rows share the viewport at every text size.
        # Only the actions are reserved, so large text cannot squeeze rows out.
        buttons.pack_configure(side="bottom", before=self.page)
        responsive_wrap(f, [self.header, map_note, self.unit_note, mapping_note, self.skipped, self.summary], padding=32)
        on_resize(self.grid_frame, lambda _e: self._wrap_evidence())
        owner = master.winfo_toplevel()
        self.geometry(f"{min(1100, owner.winfo_width())}x{min(720, owner.winfo_height())}")

    def _wrap_evidence(self):
        width = self.grid_frame.winfo_width() - self.grid_frame.grid_bbox(column=0, row=0)[2] - 12
        if width > 1:
            for row in self.rows:
                row["evidence"].configure(wraplength=width)

    # --- rows ------------------------------------------------------------------------------------------------

    EDITABLE = ("label", "coordinate", "radius", "country", "reason")

    def _edited_rows(self) -> int:
        return sum(1 for r in self.rows if any(r[k].get() != r["initial"][k] for k in self.EDITABLE))

    def _choices_now(self) -> dict:
        return {"vars": {k: v.get() for k, v in self.vars.items()}, "unit": self.unit.get(),
                "form": self.coordinate_form.get()}

    def reread(self):
        from ..place_import import ImportError_

        edited = self._edited_rows()
        if edited and not messagebox.askyesno(
                "Import places", f"Reading the file again replaces your edits in {edited} row"
                                 f"{'s' if edited != 1 else ''}. Continue?", parent=self):
            previous = getattr(self, "_choices", None)  # put the column choices back as they were
            if previous:
                for k, v in previous["vars"].items():
                    self.vars[k].set(v)
                self.unit.set(previous["unit"])
                self.coordinate_form.set(previous["form"])
            return
        mapping = {k: (None if v.get() == NONE else v.get()) for k, v in self.vars.items()}
        form = next((k for k, v in geo.FORMS.items() if v == self.coordinate_form.get()), "auto")
        try:
            self.data = self.read(self.path, mapping=mapping, radius_unit=self.unit.get(), coordinate_form=form,
                                  max_radius_km=self.max_radius_km, require_places=False)
        except (ImportError_, OSError) as exc:
            self.summary.configure(text=str(exc), style="Danger.TLabel")
            return
        self.build_rows()

    def build_rows(self):
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.rows = []
        # Status right after the tick box, so what's wrong with a row is always in view (not past the edge).
        headings = ("Import", "Status", "Label", "Lat, lon", "Radius (km)", "Country", "Reason")
        for c, text in enumerate(headings):
            ttk.Label(self.grid_frame, text=text, style="Strong.TLabel").grid(row=0, column=c, sticky="w", padx=4)
        # The status column is sized in pixels, the same as the status text's wrap width. A width in characters
        # depends on the font: with Windows' Segoe UI 8 pt, 28 characters were 168 px, so every line wrapped at
        # 210 px lost its last 42 px ("same coordinates as 'NY offi").
        self.grid_frame.columnconfigure(1, minsize=STATUS_WRAP + 8)
        # Entries stretch into pixel-sized columns; width=1 only removes their character-based request.
        for column, pixels in ((2, 110), (3, 145), (4, 90), (5, 130), (6, 130)):
            self.grid_frame.columnconfigure(column, minsize=pixels, weight=1)
        places = self.data.places[: self.MAX_SHOWN]
        for i, p in enumerate(places, start=1):
            row = {"include": tk.BooleanVar(value=i <= self.room),
                   "label": tk.StringVar(value=p.get("label", "")),
                   "coordinate": tk.StringVar(value=geo.format_latlon(p["lat"], p["lon"])),
                   "radius": tk.StringVar(value=f"{p['radius_km']:g}" if "radius_km" in p else ""),
                   "country": tk.StringVar(value=choice_for(self.choices, p["country"]) if p.get("country") else ""),
                   "reason": tk.StringVar(value=p.get("reason", "")), "original": p}
            grid_row = 2 * i - 1
            row["evidence"] = ttk.Label(self.grid_frame, text=geo.evidence_text(p), style="Small.TLabel",
                                         wraplength=700, justify="left")
            row["evidence"].grid(row=grid_row + 1, column=1, columnspan=6, sticky="w", padx=4, pady=(0, 8))
            ttk.Checkbutton(self.grid_frame, variable=row["include"]).grid(row=grid_row, column=0, padx=4)
            row["status"] = ttk.Label(self.grid_frame, style="Small.TLabel", wraplength=STATUS_WRAP, justify="left")
            row["status"].grid(row=grid_row, column=1, sticky="w", padx=4)
            for c, key in enumerate(("label", "coordinate", "radius"), start=2):
                ttk.Entry(self.grid_frame, textvariable=row[key], width=1).grid(
                    row=grid_row, column=c, sticky="ew", padx=4, pady=1)
            SearchPicker(self.grid_frame, self.choice_list, row["country"], width=1).grid(row=grid_row, column=5, sticky="ew", padx=4)
            ttk.Entry(self.grid_frame, textvariable=row["reason"], width=1).grid(row=grid_row, column=6, sticky="ew", padx=4)
            row["initial"] = {k: row[k].get() for k in self.EDITABLE}
            for key in ("include", "label", "coordinate", "radius", "country", "reason"):
                row[key].trace_add("write", lambda *_a, r=row: self.check(r))
            self.rows.append(row)
            self.check(row, summary=False)
        self.page.guard_value_widgets()
        self._choices = self._choices_now()
        kind = self.data.kind
        self.header.configure(text=f"{self.path.name} · {kind} · {len(self.data.places)} places read"
                                   + (f" (the first {self.MAX_SHOWN} are shown)" if len(self.data.places) > self.MAX_SHOWN else ""))
        shown = "; ".join(f"{w}: {what}" for w, what in self.data.problems[:6])
        more = f" (and {len(self.data.problems) - 6} more)" if len(self.data.problems) > 6 else ""
        self.skipped.configure(text=f"Not imported from the file: {shown}{more}" if self.data.problems else "")
        notes = list(self.data.warnings)
        if not self.data.unit_stated:
            notes.append(f"The file doesn't say whether its radius column ({self.data.mapping.get('radius')}) is in km "
                         "or m: km is assumed. Check it and set the unit above.")
        self.unit_note.configure(text="\n".join(notes))
        self._wrap_evidence()
        self.summarize()

    def place_from(self, row) -> tuple[dict | None, str]:
        """(place, problem) for one row; problem is "" when it can be imported."""
        label = " ".join(row["label"].get().split())
        if not label:
            return None, "⚠ label is blank"
        try:
            c = geo.parse_coordinate(row["coordinate"].get())
        except geo.CoordinateError as exc:
            return None, f"✕ {exc}"
        if not row["radius"].get().strip():
            return None, "⚠ radius is blank"
        try:
            radius = float(row["radius"].get())
        except ValueError:
            return None, "✕ radius must be a number of km"
        if not 0.01 <= radius <= self.max_radius_km:
            return None, f"✕ radius must be 0.01 to {self.max_radius_km:g} km"
        reason = " ".join(row["reason"].get().split())
        if not reason:
            return None, "⚠ reason is blank"
        place = {"label": label[:MAX_LABEL], "lat": round(c.lat, 6), "lon": round(c.lon, 6), "radius_km": radius,
                 "country": self.choices.get(row["country"].get(), row["country"].get().split(" ")[0]),
                 "reason": reason[:MAX_REASON],
                 **geo.input_evidence(c, row["coordinate"].get(), row["original"])}
        earlier = [p for r in self.rows[:self.rows.index(row)] if r["include"].get()
                   for p in [self._basic(r)] if p] if row in self.rows else []
        duplicate = duplicate_of(place, self.existing + earlier)
        if duplicate:
            return None, "⚠ " + duplicate.split(": ", 1)[-1]
        precision = geo.precision_warning(place)
        if place["country"]:
            try:
                # Country evidence and precision are independent; show both when both need attention.
                check_place({**place, "place_id": "p-check", "source": "human",
                             "precision_acknowledged": geo.precision_key(place)}, self.scope, self.max_radius_km)
            except PlaceError as exc:
                detail = str(exc).removeprefix(label + ": ")
                code = place["country"].upper()
                if re.fullmatch(r"[A-Z]{2}", code) and not permitted_country(self.scope, code):
                    excluded = next((r for r in self.scope.get("exclude", []) if r["code"] == code), None)
                    why = ("excluded: " + excluded["reason"] if excluded and excluded.get("reason") else
                           "excluded" if excluded else "not included")
                    detail = f"{code} isn't permitted ({why})"
                elif re.fullmatch(r"[A-Z]{2}", code) and not geo.in_country(code, c.lat, c.lon):
                    found = geo.country_at(c.lat, c.lon)
                    detail = (f"in {geo.country_name(found)}, not {geo.country_name(code)} ({code})" if found else
                              "at sea: not inside any country")
                return place, "⚠ " + detail + ("\n" + precision if precision else "")
            country = "Country agrees with the bundled map"
        else:
            country = "· country to choose when reviewing"
        if precision:
            return place, "⚠ " + precision + "\n" + country + ". Confirm when including this place."
        return place, "" if place["country"] else country

    def _basic(self, row) -> dict | None:
        """Just the label and point of a row, for comparing rows with each other."""
        label = " ".join(row["label"].get().split())
        try:
            c = geo.parse_coordinate(row["coordinate"].get())
        except geo.CoordinateError:
            return None
        return {"label": label, "lat": c.lat, "lon": c.lon} if label else None

    def check(self, row, summary=True):
        # One row's edit can make another row a duplicate, or stop it being one: refresh them all.
        for r in self.rows if summary else [row]:
            _place, problem = self.place_from(r)
            style = ("Danger.TLabel" if problem.startswith("✕") else "Warn.TLabel" if problem.startswith("⚠")
                     else "Ok.TLabel" if not problem else "Small.TLabel")
            r["status"].configure(text=problem or "Country agrees with the bundled map", style=style)
            evidence = geo.evidence_text(_place or r["original"])
            if _place and problem.startswith("⚠"):
                try:
                    check_place({**_place, "place_id": "p-check", "source": "human",
                                 "precision_acknowledged": geo.precision_key(_place)}, self.scope, self.max_radius_km)
                except PlaceError as exc:
                    evidence += " · " + str(exc)
            r["evidence"].configure(text=evidence)
        if summary:
            self._wrap_evidence()
            self.summarize()

    def fill(self, key: str, value: str):
        """Put `value` into every blank cell of that column."""
        for row in self.rows:
            if not row[key].get().strip():
                row[key].set(value)

    def summarize(self):
        chosen = [r for r in self.rows if r["include"].get()]
        blocked = [r for r in chosen if self.place_from(r)[0] is None]
        too_many = len(chosen) > self.room
        if too_many:
            text, style = f"{len(chosen)} rows are ticked, but only {self.room} more places fit in a warrant.", "Danger.TLabel"
        elif blocked:
            text = (f"{len(blocked)} ticked rows need fixing: a missing label, coordinate, radius or reason, or a "
                    "duplicate (edit or untick them).")
            style = "Warn.TLabel"
        else:
            text = (f"{len(chosen)} places will arrive Pending. Include each with A after reviewing its coordinate and country. "
                    "The country must agree with the bundled map.")
            style = "TLabel"
        self.summary.configure(text=text, style=style)
        self.ok.configure(text=f"Import {len(chosen)} places",
                          state="normal" if chosen and not blocked and not too_many else "disabled")

    def _ok(self):
        places = []
        for row in self.rows:
            if row["include"].get():
                place, _problem = self.place_from(row)
                if place is None:
                    return
                places.append(place)
        self.result = places
        self.destroy()

    @classmethod
    def ask(cls, master, path, **kwargs):
        from ..place_import import ImportError_

        try:
            dialog = cls(master, path, **kwargs)
        except (ImportError_, OSError) as exc:  # the file was moved, deleted or changed after it was chosen
            messagebox.showerror("Import places", f"{path.name} can't be read any more: {exc}. Choose it again.",
                                 parent=master)
            return None
        return dialog.run()


class PlacesPanel(ttk.Frame):
    """The converter and the places table, inside Compose's geographic constraint."""

    def __init__(self, master, compose):
        super().__init__(master)
        self.compose = compose
        # Share the geographic section's available width; an unweighted grid column keeps its requested width.
        master.columnconfigure(0, weight=1)
        self.rows: list[dict] = []  # each place, with "decision": pending / approved / rejected
        self._approved_key = "[]"  # the approved places last rendered: a change needs the limits certified again
        self._history: list[list[dict]] = []  # earlier states of the table, for Undo
        ttk.Label(self, text="Places", style="Strong.TLabel").pack(anchor="w", pady=(14, 2))
        description = ttk.Label(self, text="Limit every search to circles around specific sites. Each place's radius is its signed "
                             "maximum (100 m unless you change it). A place can't be included until you've chosen the "
                             "country it's in and the point is inside that country.", style="Small.TLabel",
                  wraplength=600, justify="left")
        description.pack(anchor="w")

        converter = ttk.Frame(self)
        converter.pack(anchor="w", fill="x", pady=(8, 0))
        title = ttk.Label(converter, text="Coordinate converter")
        self.form = tk.StringVar(value=geo.FORMS["auto"])
        form = ttk.Combobox(converter, textvariable=self.form, values=list(geo.FORMS.values()), state="readonly")
        self.coordinate = tk.StringVar()
        entry = ttk.Entry(converter, textvariable=self.coordinate)
        entry.bind("<Return>", lambda _e: self.convert())
        convert_button = ttk.Button(converter, text="Convert", command=self.convert)
        self.copy_button = ttk.Button(converter, text="Copy lat, lon", command=self.copy_converted, state="disabled")
        self.add_converted = ttk.Button(converter, text="Add as place…", command=self.add_from_converter,
                                        state="disabled")
        _flow_controls(converter, [(title, None), (form, 200), (entry, 340), (convert_button, None),
                                   (self.copy_button, None), (self.add_converted, None)])
        self.result = ttk.Label(self, style="Muted.TLabel", font="LW.Mono", justify="left", wraplength=1000)
        self.result.pack(anchor="w", pady=(4, 0))
        map_note = ttk.Label(self, text=geo.MAP_LIMITATION, style="Small.TLabel", justify="left")
        map_note.pack(anchor="w")
        self.warning = ttk.Label(self, style="Warn.TLabel", wraplength=1100, justify="left")
        self.warning.pack(anchor="w")
        self.converted: geo.Coordinate | None = None
        self.converted_input = ""

        self.table = GrowTable(self, [("status", "Status", 100), ("label", "Label", 150), ("latlon", "Lat, lon", 170),
                                      ("radius", "Radius", 70), ("country", "Country", 150),
                                      ("reason", "Reason", 200), ("source", "Source", 80)],
                               placeholder="No places: searches aren't limited to sites",
                               on_activate=lambda idx: self.edit(), fit_column="reason")
        self.table.pack(fill="x", pady=(8, 0))
        self.pending_note = ttk.Label(self, style="Warn.TLabel", justify="left")
        self.pending_note.pack(anchor="w")
        responsive_wrap(self, [description, self.result, map_note, self.warning, self.pending_note])
        tree = self.table.tree
        p = theme.PALETTE
        tree.tag_configure("approved", foreground=p["ok"])
        tree.tag_configure("pending", foreground=p["warn"])
        tree.tag_configure("rejected", foreground=p["muted"])
        # The same review keys and buttons as hit terms.
        from .compose import DECISION_KEYS

        actions = {"approved": self.approve, "rejected": self.reject, "pending": self.set_pending}
        bind_decision_keys(tree, {key: (lambda d=decision: self.decide(d)) for key, decision in DECISION_KEYS})
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="✓ Include in warrant  (A)", command=lambda: self.decide("approved"))
        ttk.Button(buttons, text="✗ Reject place  (R)", command=lambda: self.decide("rejected"))
        ttk.Button(buttons, text="Return to pending  (P)", command=lambda: self.decide("pending"))
        ttk.Button(buttons, text="Add place…", command=self.add)
        ttk.Button(buttons, text="Import list…", command=self.import_list)
        ttk.Button(buttons, text="Edit place…", command=self.edit)
        ttk.Button(buttons, text="Remove", command=self.remove)
        self.undo_button = ttk.Button(buttons, text="Undo", command=self.undo, state="disabled")
        _flow_controls(buttons, [(button, None) for button in buttons.winfo_children()])
        for sequence in ("<Control-z>", "<Command-z>"):
            try:
                tree.bind(sequence, lambda _e: self.undo())
            except tk.TclError:  # <Command-…> only exists on macOS
                pass
        self._actions = actions

    # --- converter -------------------------------------------------------------------------------------------

    def _form_key(self) -> str:
        return next((k for k, v in geo.FORMS.items() if v == self.form.get()), "auto")

    def convert(self):
        self.converted_input = self.coordinate.get()
        self.converted, message = convert(self.converted_input, self._form_key())
        self.result.configure(text=message, style="Muted.TLabel" if self.converted else "Danger.TLabel")
        self.warning.configure(text=CONVERSION_WARNING if self.converted else "")
        state = "normal" if self.converted else "disabled"
        self.copy_button.configure(state=state)
        self.add_converted.configure(state=state)

    def copy_converted(self):
        if self.converted:
            copy_text(self, self.converted.text())
            self.compose.app.notify("Copied " + self.converted.text())

    def add_from_converter(self):
        if self.converted:
            self.add({**geo.input_evidence(self.converted, self.converted_input),
                      "lat": round(self.converted.lat, 6), "lon": round(self.converted.lon, 6)})

    # --- places ----------------------------------------------------------------------------------------------

    def _context(self) -> dict:
        compose = self.compose
        return {"scope": compose.current_scope(), "max_radius_km": compose.settings["geography"]["max_radius_km"],
                "known": countries(compose.app.paths)}

    def _selected(self) -> list[int]:
        return self.table.selected_indexes()

    def add(self, prefill: dict | None = None):
        place = PlaceDialog.ask(self, place=prefill, title="Add a place", **self._context())
        if place:
            self._remember()
            taken = {r["place_id"] for r in self.rows}
            base, n = place["place_id"], 2
            while place["place_id"] in taken:
                place["place_id"], n = f"{base}-{n}", n + 1
            self.rows.append({**place, "decision": "approved"})  # added by the analyst, already checked
            self.render()

    def _remember(self):
        """Keep the table as it is now, so the next change can be undone."""
        import copy

        self._history = (self._history + [copy.deepcopy(self.rows)])[-20:]
        self.undo_button.configure(state="normal")

    def undo(self):
        """Put back the table as it was before the last change (decision, edit, add, import or remove)."""
        if not self._history:
            return
        self.rows = self._history.pop()
        self.undo_button.configure(state="normal" if self._history else "disabled")
        self.render()
        self.compose.app.notify("Undone")

    def import_list(self, path=None):
        """Places from a GeoJSON, KML/KMZ or CSV file: choose (or drop) the file, check and complete each row, and
        they arrive pending, to be reviewed like the rest."""
        from pathlib import Path

        from ..places import MAX_PLACES

        context = self._context()
        if path is None:
            path = ImportChooser.ask(self, max_radius_km=context["max_radius_km"])
            if not path:
                return
        path = Path(path)
        room = MAX_PLACES - len([r for r in self.rows if r["decision"] != "rejected"])
        existing = [r for r in self.rows if r["decision"] != "rejected"]
        places = ImportReview.ask(self, path, room=room, existing=existing, **context)
        if not places:
            return
        self._remember()
        taken = {r["place_id"] for r in self.rows}
        for p in places:
            place_id, n = f"p-{_slug(p['label'])}", 2
            while place_id in taken:
                place_id, n = f"p-{_slug(p['label'])}-{n}", n + 1
            taken.add(place_id)
            self.rows.append({"place_id": place_id, "label": p["label"], "lat": p["lat"], "lon": p["lon"],
                              "radius_km": p["radius_km"], "country": p.get("country", ""), "reason": p["reason"],
                              **{k: p.get(k) for k in geo.EVIDENCE_FIELDS}, "source": "human", "decision": "pending",
                              "origin": f"Imported: {path.name}", "country_confirmed": False})
        self.render()
        self.compose.app.notify(f"Imported {len(places)} places from {path.name}; review each with A / R / P")

    def edit(self):
        indexes = self._selected()
        if len(indexes) != 1:
            messagebox.showinfo("Edit place", "Select one place.", parent=self)
            return
        row = self.rows[indexes[0]]
        place = PlaceDialog.ask(self, place=row, title="Edit place", **self._context())
        if place:
            # Editing corrects a place; it isn't a decision. The place keeps its status (a pending one still needs A),
            # except that an approved place that now duplicates another goes back to pending.
            self._remember()
            edited = {**row, **place}
            duplicate = duplicate_of(edited, self._approved(besides=indexes[0]))
            if duplicate:
                if edited["decision"] == "approved":
                    messagebox.showerror("Edit place", duplicate + ". Change it, or remove one of them.", parent=self)
                    edited["decision"] = "pending"
                edited["held"] = duplicate.split(": ", 1)[-1]
            else:
                edited.pop("held", None)
            self.rows[indexes[0]] = edited
            self.render()

    def _approved(self, besides: int | None = None) -> list[dict]:
        return [r for i, r in enumerate(self.rows) if r["decision"] == "approved" and i != besides]

    def approve(self):
        context = self._context()
        self._remember()
        for index in self._selected():
            row = self.rows[index]
            if (row.get("country") and not row.get("country_confirmed")) or geo.precision_needs_review(row):
                origin = row.get("origin") or ("the agent's proposal" if row.get("source") == "agent" else "a file")
                confirmation = (geo.precision_warning(row) if geo.precision_needs_review(row) else
                                f"The country was filled in from {origin}, not chosen by you. Compare the coordinate "
                                f"(given as {row.get('given_as') or geo.format_latlon(row['lat'], row['lon'])!r}) and "
                                "the country with your source, then press OK to confirm.")
                place = PlaceDialog.ask(
                    self, place=row, title="Confirm where this place is", **context,
                    confirm=confirmation)
                if not place:
                    continue
                row = self.rows[index] = {**row, **place}
            try:
                check_place(row, context["scope"], context["max_radius_km"])
            except PlaceError:
                # Not approvable as it stands (most often: no country chosen yet). Open it to be fixed; the dialog
                # checks again, and nothing that fails is kept on the row.
                title = "Choose the country, then include" if not row.get("country") else "Fix the place, then include"
                place = PlaceDialog.ask(self, place=row, title=title, **context)
                if not place:
                    continue
                try:
                    check_place(place, context["scope"], context["max_radius_km"])
                except PlaceError as exc:
                    messagebox.showerror("Include place", str(exc), parent=self)
                    continue
                row = self.rows[index] = {**row, **place}
            duplicate = duplicate_of(row, self._approved(besides=index))
            if duplicate:
                messagebox.showerror("Include place", duplicate + ". Edit or remove one of them first.", parent=self)
                continue
            row["decision"] = "approved"
            row.pop("held", None)
        self.render()

    def reject(self):
        selected = self._selected()
        if any(self.rows[i].get("source") == "agent" for i in selected):
            from .widgets import askstring

            reason = askstring("Reject place", "Why reject it? The agent sees this in its status. Once you sign, a "
                                               "rejected agent place isn't offered for review again.", parent=self)
            if reason is None:
                return  # cancelled: nothing changes
        else:
            reason = ""
        self._remember()
        for index in selected:
            self.rows[index]["decision"] = "rejected"
            self.rows[index]["reject_reason"] = " ".join(reason.split())[:200]
        self.render()

    def set_pending(self):
        self._remember()
        for index in self._selected():
            self.rows[index]["decision"] = "pending"
        self.render()

    def decide(self, decision: str):
        """A, R or P: decide the selected places, then move to the next row, as the hit-term table does."""
        selection = self._selected()
        if not selection:
            return
        if decision == "approved" and not confirm_bulk(self, len(selection), "places"):
            return
        position = selection[-1]
        self._actions[decision]()
        unresolved = [i for i in selection if self.rows[i]["decision"] != decision]
        if unresolved:
            # Some weren't decided (a confirmation was cancelled or failed): stay on exactly those rows.
            self.table.tree.selection_set([str(i) for i in unresolved])
            self.table.tree.see(str(unresolved[0]))
            return
        rows = self.table.tree.get_children()
        if rows:
            nxt = rows[min(position + (1 if len(selection) == 1 else 0), len(rows) - 1)]
            self.table.tree.selection_set(nxt)
            self.table.tree.focus(nxt)

    def remove(self):
        self._remember()
        for index in reversed(self._selected()):
            del self.rows[index]
        self.render()

    def recheck(self) -> int:
        """After the countries change: an approved place that no longer passes (its country isn't permitted any
        more) goes back to pending, with the reason shown, so the table never says Approved for a place that
        couldn't be signed. It isn't re-approved automatically if the country comes back; press A."""
        context = self._context()
        moved = 0
        for r in self.rows:
            if r["decision"] != "approved":
                continue
            try:
                check_place(r, context["scope"], context["max_radius_km"])
            except PlaceError as exc:
                r["decision"], r["held"] = "pending", str(exc).split(": ", 1)[-1]
                moved += 1
        if moved:
            self.render()
        return moved

    def render(self):
        known = countries(self.compose.app.paths)
        rows = []
        for r in self.rows:
            if r.get("country"):
                country = f"{r['country']} — {known.get(r['country'], '?')}"
            else:
                country = "not chosen" + (f" (agent suggests {r['country_suggested']})" if r.get("country_suggested")
                                          else "")
            held = r.get("held") if r["decision"] == "pending" else None
            if r["decision"] == "pending" and not held:
                duplicate = duplicate_of(r, self._approved())
                held = duplicate.split(": ", 1)[-1] if duplicate else None
            status = STATUS[r["decision"]] + (" — " + held if held else "")
            rows.append([status, r["label"], geo.format_latlon(r["lat"], r["lon"]),
                         f"{r['radius_km'] * 1000:g} m" if r["radius_km"] < 1 else f"{r['radius_km']:g} km", country, r.get("reason", ""),
                         r.get("origin") or ("Agent proposal" if r.get("source") == "agent" else "Analyst")])
        self.table.set_rows(rows)
        pending = sum(1 for r in self.rows if r["decision"] == "pending")
        approved_count = sum(1 for r in self.rows if r["decision"] == "approved")
        self.pending_note.configure(text=(
            f"{pending} pending place{'s' if pending != 1 else ''} won't limit searches until included (A)."
            + ("" if approved_count else " With no included places, searches aren't limited to sites.")) if pending else "")
        # The approved places are part of the certified limits: any change to them needs certifying again.
        approved = json.dumps(self.for_warrant(), sort_keys=True)
        if approved != getattr(self, "_approved_key", None):
            if getattr(self, "_approved_key", None) is not None:
                self.compose._uncertify("limits")
            self._approved_key = approved
        for i, r in enumerate(self.rows):
            self.table.tree.item(str(i), tags=(r["decision"],))
        self.compose.update_footer()

    # --- with Compose ----------------------------------------------------------------------------------------

    def record_rejections(self, paths) -> None:
        """At signing (as with hit terms): rejected agent places stay rejected, with their reason, so the
        proposal doesn't bring them back for review; the agent sees the reason in its status."""
        from ..places import record_place_rejections
        from ..util import to_iso, utc_now

        record_place_rejections(paths, self.rows, to_iso(utc_now()))

    def merge_proposal(self, paths) -> int:
        """Agent-proposed places not seen yet arrive as pending. Returns how many arrived."""
        try:
            doc = json.loads(paths.proposal.read_text("utf-8")) if paths.proposal.exists() else {}
        except (OSError, ValueError):
            return 0
        known_labels = {r["label"] for r in self.rows}
        taken = {r["place_id"] for r in self.rows}
        from ..places import load_rejected_places, place_key

        rejected = load_rejected_places(paths)
        added = 0
        for p in (doc.get("places") or []) if isinstance(doc, dict) else []:
            if not isinstance(p, dict) or not isinstance(p.get("label"), str) or p["label"] in known_labels:
                continue
            try:
                lat, lon, radius = float(p["lat"]), float(p["lon"]), float(p.get("radius_km", DEFAULT_RADIUS_KM))
            except (KeyError, TypeError, ValueError):
                continue
            if place_key({"label": p["label"], "lat": lat, "lon": lon}) in rejected:
                continue  # rejected at an earlier signing
            place_id = f"p-{_slug(p['label'])}"
            n = 2
            while place_id in taken:
                place_id, n = f"p-{_slug(p['label'])}-{n}", n + 1
            taken.add(place_id)
            self.rows.append({"place_id": place_id, "label": p["label"][:MAX_LABEL], "lat": lat, "lon": lon,
                              "radius_km": radius, "reason": str(p.get("reason", ""))[:MAX_REASON], "source": "agent",
                              "country": "", "country_suggested": str(p.get("country_suggested", ""))[:2],
                              "given_as": str(p.get("given_as", ""))[:80], "decision": "pending"})
            added += 1
        if added:
            self.render()
        return added

    def load_signed(self, places: list[dict]):
        self.rows = [{**p, "decision": "approved", "from_warrant": True} for p in places or []]
        self._approved_key = None  # loading a warrant to edit isn't a change to certify
        self.render()

    def keep_pending(self):
        self.rows = [r for r in self.rows if r["decision"] == "pending"]
        self.render()

    def for_warrant(self) -> list[dict]:
        return [{k: r[k] for k in ("place_id", "label", "lat", "lon", "radius_km", "country", "reason", "source",
                                  *geo.EVIDENCE_FIELDS) if k in r}
                for r in self.rows if r["decision"] == "approved"]

    def problems(self) -> list[str]:
        """Approved places that are no longer valid (e.g. their country stopped being permitted)."""
        context = self._context()
        out = []
        approved = []
        for r in self.rows:
            if r["decision"] == "approved":
                try:
                    check_place(r, context["scope"], context["max_radius_km"])
                except PlaceError as exc:
                    out.append(f"fix place {exc}")
                duplicate = duplicate_of(r, approved)
                if duplicate:
                    out.append(f"fix place {duplicate}")
                approved.append(r)
        return out
