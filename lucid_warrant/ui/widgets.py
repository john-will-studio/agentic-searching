"""Shared widgets: tables with empty states, structured inspectors, chips, dialogs."""

from __future__ import annotations

import calendar
import os
import re
import sys
import time
from pathlib import Path
import tkinter as tk
from datetime import date, timedelta
from tkinter import ttk

from . import msg as messagebox

from ..dates import MAX_YEAR, MIN_YEAR, DateLimitError, parse_day
from ..library import suggest_template_id
from ..util import from_iso
from . import theme
from .geometry import frame_insets, work_area


# --- small helpers --------------------------------------------------------------

TIME_ZONE = {"value": "utc"}  # from the ui.time_zone preference (theme.apply sets it)


def set_time_zone(value: str) -> None:
    TIME_ZONE["value"] = "local" if value == "local" else "utc"


def shown_time(moment):
    """A datetime in the zone times are shown in: UTC unless the analyst chose their local time."""
    from datetime import timezone

    return moment.astimezone() if TIME_ZONE["value"] == "local" else moment.astimezone(timezone.utc)


def zone_label(moment) -> str:
    """The zone a shown time is in, kept short. macOS names it "EDT"; Windows says "Eastern Daylight Time", which
    widened every time column and the status bar, so a long name is shown as the UTC offset instead (UTC-04:00)."""
    name = moment.strftime("%Z")
    if " " not in name:
        return name
    minutes = int(moment.utcoffset().total_seconds() // 60)
    return f"UTC{'+' if minutes >= 0 else '-'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def local_time(iso: str | None) -> str:
    """A timestamp for display, in UTC by default (ui.time_zone); anything unparsable is shown as-is rather than
    breaking a view."""
    if not iso:
        return ""
    try:
        moment = shown_time(from_iso(iso))
    except (TypeError, ValueError):
        return f"{iso} (invalid time)"
    return moment.strftime("%Y-%m-%d %H:%M ") + ("UTC" if TIME_ZONE["value"] == "utc" else zone_label(moment))


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:48] or "term"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def lint_badge(report: dict | None) -> str:
    if not report:
        return "not recorded"
    if not report.get("passed"):
        return "✗ failed"
    if "findings" in report:
        warnings = sum(f["level"] == "warning" for f in report["findings"])
    elif "warnings" in report:
        warnings = report["warnings"]
    elif "codes" in report:
        warnings = sum(str(code).startswith("W") for code in report["codes"])
    else:
        return report.get("summary") or "passed"
    return f"⚠ {plural(warnings, 'warning')}" if warnings else "✓ passed"


def scrolled(parent, widget_cls, *, horizontal: bool = False, **kwargs):
    """widget_cls inside a frame with scrollbars. Returns (frame, widget)."""
    frame = ttk.Frame(parent)
    if widget_cls in (tk.Text, tk.Listbox):
        colors = theme.text_widget_colors()
        if widget_cls is tk.Listbox:
            colors.pop("insertbackground")
        kwargs = {**colors, **kwargs}
    widget = widget_cls(frame, **kwargs)
    vertical = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
    widget.configure(yscrollcommand=vertical.set)
    widget.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    if horizontal:
        bar = ttk.Scrollbar(frame, orient="horizontal", command=widget.xview)
        widget.configure(xscrollcommand=bar.set)
        bar.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, widget


WHEEL_PIXELS = 48  # one detented mouse-wheel notch (Windows/Linux)
MAC_WHEEL_PIXELS = 6  # per unit of macOS wheel delta, which arrives as many small, accelerating events


def split_panes(panes: ttk.PanedWindow, fraction: float = 0.5) -> None:
    """Put the divider of a two-pane window at `fraction` of its width the first time it has a size. After
    that it's the user's: dragging the divider sticks, and resizing the window keeps the panes' weights."""
    state = {"done": False}

    def first(_event=None):
        if not state["done"] and panes.winfo_width() > 1:
            panes.sashpos(0, int(panes.winfo_width() * fraction))
            state["done"] = True

    panes.bind("<Configure>", first, add="+")


def on_resize(widget: tk.Misc, callback, add: str | None = "+") -> None:
    """Bind <Configure> for real size changes only. Scrolling a page moves its widgets, and on macOS each move
    sends <Configure> to every widget inside; re-wrapping or re-fitting on each of those slows scrolling down."""
    last = {}

    def handler(event):
        if event.widget is not widget:
            return None
        size = (event.width, event.height)
        if last.get("size") == size:
            return None
        last["size"] = size
        return callback(event)

    widget.bind("<Configure>", handler, add=add)


READ_WIDTH = 960  # maximum readable width for explanatory prose; tables remain fluid


def autohide_scrollbar(bar: ttk.Scrollbar, content: tk.Misc):
    """A yscrollcommand that shows `bar` (to the right of `content`) only when there's something to scroll: a
    scrollbar beside text that fits looked broken. The bar sits in a fixed-width slot that never leaves the
    layout; only the bar inside it comes and goes. (Packing and unpacking the bar itself changed the layout, a
    dialog sized to its content then flipped between needing and not needing it, and the window froze.)"""
    slot = tk.Frame(bar.master, width=bar.winfo_reqwidth(), bd=0, highlightthickness=0, bg=theme.PALETTE["bg"])
    slot.pack(side="right", fill="y", before=content)
    slot.pack_propagate(False)
    state = {"shown": False}

    def set_view(first, last):
        bar.set(first, last)
        needed = float(first) > 0.001 or float(last) < 0.999
        if needed == state["shown"]:
            return
        state["shown"] = needed
        if needed:
            bar.place(in_=slot, relx=0, rely=0, relwidth=1, relheight=1)
            bar.lift(slot)
        else:
            bar.place_forget()

    return set_view


def responsive_wrap(frame, labels, padding=0):
    """One resize owner per label, capped by both its container and readable width."""
    labels = [label for label in labels if not getattr(label, "_responsive_wrap", False)]
    for label in labels:
        label._responsive_wrap = True

    def fit(_event=None):
        width = frame.winfo_width() - padding - 4
        if width > 0:
            for label in labels:
                if label.winfo_exists():
                    label.configure(wraplength=max(1, min(READ_WIDTH, width)))

    if labels:
        on_resize(frame, fit)
        frame.after_idle(fit)


REPEAT_PAIR_MS = 30  # X11 auto-repeat sends release+press pairs this close together; a person can't


def bind_decision_keys(widget, actions: dict) -> None:
    """Bind review keys (e.g. {"A": approve, "R": reject, "P": pending}) so each physical press decides once: a
    held key's auto-repeat is ignored however long it's held (Windows and macOS repeat presses without a release;
    X11 sends release+press pairs, recognized by how close together they are). Leaving the widget resets it."""
    held: dict[str, object] = {}  # key -> True while down, or a pending "it's really up" timer after a release

    def press(key):
        state = held.get(key)
        if state is not None:
            if state is not True:  # a release was pending: this press is auto-repeat, not a new press
                widget.after_cancel(state)
                held[key] = True
            return "break"
        held[key] = True
        actions[key]()
        return "break"

    def release(key):
        if held.get(key) is True:
            held[key] = widget.after(REPEAT_PAIR_MS, lambda: held.pop(key, None))

    for key in actions:
        for keysym in (key.lower(), key.upper()):
            widget.bind(f"<KeyPress-{keysym}>", lambda _e, k=key: press(k))
            widget.bind(f"<KeyRelease-{keysym}>", lambda _e, k=key: release(k))
    widget.bind("<FocusOut>", lambda _e: held.clear(), add="+")


def confirm_bulk(parent, count: int, what: str) -> bool:
    """Approving several rows at once is a bigger decision than one: say how many, once."""
    if count <= 1:
        return True
    return messagebox.askyesno("Include in warrant", f"Include {count} selected {what} in the warrant?", parent=parent)


class SearchPicker(ttk.Combobox):
    """A dropdown you can type into to narrow it: "ger", "DE" or "turk" filters the list to matching entries
    (anywhere in the text, any case); Down opens it. Enter or leaving the field resolves an exact entry, an exact
    code (the text before " — ") or a single match; anything else reverts, so the value is always one of the
    choices (or blank, when blank is allowed). Works the same on Windows and macOS, where a read-only
    dropdown can't be searched."""

    def __init__(self, master, choices: list[str], textvariable: tk.StringVar, *, allow_blank: bool = True, **kw):
        super().__init__(master, textvariable=textvariable, values=list(choices), **kw)
        self.all_choices = [c for c in choices if c]
        self.allow_blank = allow_blank
        self.var = textvariable
        self._last_good = textvariable.get()
        self.bind("<KeyRelease>", self._filter, add="+")
        self.bind("<Return>", lambda _e: self.resolve(), add="+")
        self.bind("<FocusOut>", lambda _e: self.resolve(), add="+")
        self.bind("<<ComboboxSelected>>", lambda _e: self._accept(self.var.get()), add="+")

    def matches(self, text: str) -> list[str]:
        t = text.strip().casefold()
        return [c for c in self.all_choices if t in c.casefold()] if t else list(self.all_choices)

    def _filter(self, event=None):
        if event is not None and event.keysym in ("Down", "Up", "Return", "Escape", "Tab", "ISO_Left_Tab"):
            return
        self.configure(values=([""] if self.allow_blank else []) + self.matches(self.var.get()))

    def _accept(self, value: str):
        self._last_good = value
        self.var.set(value)
        self.configure(values=([""] if self.allow_blank else []) + self.all_choices)

    def resolve(self) -> str:
        text = self.var.get().strip()
        if text in self.all_choices or (not text and self.allow_blank):
            self._accept(text)
            return text
        by_code = [c for c in self.all_choices if c.split(" ")[0].casefold() == text.casefold()]
        found = by_code or self.matches(text)
        if len(found) == 1:
            self._accept(found[0])
            return found[0]
        self._accept(self._last_good)  # not one choice: back to what it was
        return self._last_good


class ScrollableFrame(ttk.Frame):
    _last_pointer_press = 0.0  # shared: when the mouse was last pressed, and on which widget
    _last_pressed = ""

    """Scrolls vertically as a whole; content goes in .body.

    Handles mouse wheels (<MouseWheel>, X11 <Button-4/5>) and, on Tk 9, trackpads
    (<TouchpadScroll>) anywhere over the content, not just over the scrollbar.
    Lists and text boxes inside keep their own scrolling while they have more to show.
    """

    def __init__(self, master, max_width: int | None = None):
        self.max_width = max_width
        super().__init__(master)
        # One scroll unit = one pixel, so trackpad deltas map directly.
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=theme.PALETTE["bg"], yscrollincrement=1)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=autohide_scrollbar(self.bar, self.canvas))  # shown only when needed
        self.body = ttk.Frame(self.canvas)
        window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        on_resize(self.body, lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")), add=None)
        def fit(event):
            # On a wide screen the page keeps a readable width and sits in the middle, rather than stretching.
            width = min(event.width, self.max_width) if self.max_width else event.width
            self.canvas.itemconfigure(window, width=width)
            self.canvas.coords(window, max((event.width - width) // 2, 0), 0)

        self.canvas.bind("<Configure>", fit)
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self.bind_all("<Button-4>", self._on_wheel, add="+")
        self.bind_all("<Button-5>", self._on_wheel, add="+")
        try:
            self.bind_all("<TouchpadScroll>", self._on_touchpad, add="+")
        except tk.TclError:  # <TouchpadScroll> doesn't exist before Tk 9
            pass
        self.bind_all("<FocusIn>", self._reveal_focus, add="+")
        self.bind_all("<ButtonPress>", self._note_pointer_press, add="+")

    @staticmethod
    def _note_pointer_press(event=None):
        ScrollableFrame._last_pointer_press = time.monotonic()
        ScrollableFrame._last_pressed = str(getattr(event, "widget", ""))

    def _inside(self, widget) -> bool:
        """The page's content, or the page's own background (the margins beside centered content on a wide
        screen), so scrolling works wherever the pointer is over the page."""
        return isinstance(widget, tk.Misc) and (widget is self.canvas or str(widget).startswith(str(self.canvas) + "."))

    def _owns(self, widget) -> bool:
        if not self._inside(widget):
            return False
        if isinstance(widget, ttk.Scrollbar):
            return False  # a nested scrollbar scrolls its own widget
        if isinstance(widget, (ttk.Treeview, tk.Text, tk.Listbox)):
            first, last = widget.yview()
            return first <= 0.0 and last >= 1.0  # only when it has nothing more to show
        return True

    def guard_value_widgets(self):
        """Comboboxes and spinboxes change their value on scroll. Inside a scrolling page that's a
        trap (scrolling could change the authorization), so scroll the page instead and swallow it."""
        def visit(widget):
            for child in widget.winfo_children():
                if isinstance(child, (ttk.Combobox, ttk.Spinbox)):
                    child.bind("<MouseWheel>", lambda e: self._scroll_wheel(e) or "break")
                    child.bind("<Button-4>", lambda e: self._scroll_wheel(e) or "break")
                    child.bind("<Button-5>", lambda e: self._scroll_wheel(e) or "break")
                    try:
                        child.bind("<TouchpadScroll>", lambda e: self._scroll_touchpad(e) or "break")
                    except tk.TclError:
                        pass
                visit(child)
        visit(self.body)

    def _on_wheel(self, event):
        if self._owns(event.widget):
            self._scroll_wheel(event)

    def _on_touchpad(self, event):
        if self._owns(event.widget):
            self._scroll_touchpad(event)

    def _scroll_wheel(self, event):
        if getattr(event, "num", None) in (4, 5):
            pixels = -WHEEL_PIXELS if event.num == 4 else WHEEL_PIXELS
        elif os.name == "nt":
            pixels = -int(event.delta / 120 * WHEEL_PIXELS)
        else:  # macOS: proportional to the delta, so acceleration feels continuous rather than stepped
            pixels = -int(event.delta * MAC_WHEEL_PIXELS) or (-1 if event.delta > 0 else 1)
        self._queue_scroll(pixels)

    def _scroll_touchpad(self, event):
        _dx, dy = (int(v) for v in self.tk.splitlist(self.tk.call("tk::PreciseScrollDeltas", event.delta)))
        self._queue_scroll(-dy)

    def _queue_scroll(self, pixels: int) -> None:
        """Add up the scroll events that arrive together and redraw once for all of them. A trackpad sends a
        stream of small events; redrawing the page for each one queues them up and makes scrolling lag."""
        if not pixels:
            return
        self._pending_scroll = getattr(self, "_pending_scroll", 0) + pixels
        if not getattr(self, "_scroll_scheduled", False):
            self._scroll_scheduled = True
            self.after_idle(self._flush_scroll)

    def _flush_scroll(self) -> None:
        pixels, self._pending_scroll, self._scroll_scheduled = getattr(self, "_pending_scroll", 0), 0, False
        if pixels:
            try:
                self.canvas.yview_scroll(pixels, "units")
            except tk.TclError:
                pass  # the page closed in the meantime

    def scroll_to(self, widget, margin: int = 12) -> None:
        """Scroll so `widget` sits at the top of the visible page."""
        self.update_idletasks()
        top = widget.winfo_rooty() - self.body.winfo_rooty()
        region = self.canvas.bbox("all")
        total = max((region[3] - region[1]) if region else self.body.winfo_height(), 1)
        self.canvas.yview_moveto(max(top - margin, 0) / total)

    def _reveal_focus(self, event):
        """Keep the keyboard-focused control visible when tabbing through a long page.

        Only for keyboard focus: a mouse click also moves focus, and scrolling then would pull the
        control out from under the pointer and lose the click."""
        widget = event.widget
        if not self._inside(widget):
            return
        try:
            focused = widget.focus_get()
        except (KeyError, tk.TclError):  # focus is in a widget tkinter doesn't know (e.g. a dialog's internals)
            return
        if focused is not widget:
            return  # Tk also sends FocusIn to ancestors of the focused control; only the control itself counts
        clicked = str(widget) == ScrollableFrame._last_pressed or str(widget).startswith(ScrollableFrame._last_pressed + ".")
        if clicked and time.monotonic() - ScrollableFrame._last_pointer_press < 0.5:
            return
        try:
            top = widget.winfo_rooty() - self.body.winfo_rooty()
            bottom = top + widget.winfo_height()
            view_top = self.canvas.canvasy(0)
            view_height = self.canvas.winfo_height()
            region = self.canvas.bbox("all")
            total = max((region[3] - region[1]) if region else self.body.winfo_height(), 1)
            if top < view_top:
                self.canvas.yview_moveto(max(top - 12, 0) / total)
            elif bottom > view_top + view_height:
                self.canvas.yview_moveto(max(bottom + 12 - view_height, 0) / total)
        except tk.TclError:
            pass

    def to_top(self):
        self.canvas.yview_moveto(0)


# --- tables ---------------------------------------------------------------------------

class Table(ttk.Frame):
    """A Treeview with proportional columns, scrollbars and an explanatory empty state.

    columns: [(id, title, weight, min_width, anchor)]
    """

    def __init__(self, master, columns, *, height: int = 10, selectmode: str = "browse", horizontal: bool = True,
                 fit_content=()):
        """fit_content: columns that must never clip (IDs, times, statuses). They're sized to their longest
        value; if the table is too narrow for them, it scrolls sideways instead of truncating."""
        super().__init__(master)
        self.columns = columns
        # fit_content: column ids, or {column id: max width} to cap long free text (names)
        self.fit_content = dict(fit_content) if isinstance(fit_content, dict) else {c: None for c in fit_content}
        self.min_widths = {c[0]: c[3] for c in columns}
        box, self.tree = scrolled(self, ttk.Treeview, horizontal=horizontal, columns=[c[0] for c in columns],
                                  show="headings", height=height, selectmode=selectmode)
        box.pack(fill="both", expand=True)
        for cid, title, _weight, min_width, anchor in columns:
            self.tree.heading(cid, text=title, anchor="w")
            self.tree.column(cid, minwidth=min_width, width=min_width, anchor=anchor, stretch=True)
        self.empty_label = ttk.Label(self.tree, style="Muted.TLabel", justify="center", wraplength=420)
        self.empty_text = ""
        on_resize(self.tree, self._fit)
        self.tree.bind("<Map>", self._fit, add="+")  # tables on tabs not yet shown get sized when they appear
        self.tree.bind("<FocusIn>", self._ensure_item_focus, add="+")

    def _fit(self, event=None):
        """Give every column its minimum, then share what's left by weight. Narrower than the
        minimums: keep the minimums and let the horizontal scrollbar take over."""
        width = self.tree.winfo_width()
        if width <= 1:  # not laid out yet: at least never clip the fit-to-content columns
            for cid in self.min_widths:
                self.tree.column(cid, width=self.min_widths[cid], minwidth=self.min_widths[cid])
            return
        minimum = sum(self.min_widths.values())
        spare = max(width - minimum - 4, 0)
        total = sum(c[2] for c in self.columns) or 1
        for cid, _title, weight, _min, _anchor in self.columns:
            # minwidth too: Tk squeezes stretchable columns back to their minwidth when it re-lays out the table
            self.tree.column(cid, width=self.min_widths[cid] + int(spare * weight / total), minwidth=self.min_widths[cid])

    def autosize(self):
        """Grow fit_content columns to their widest value (header included)."""
        if not self.fit_content:
            return
        from tkinter import font as tkfont

        style = ttk.Style()

        def font_of(element: str, fallback: str):
            name = style.lookup(element, "font") or fallback
            try:
                return tkfont.nametofont(name)
            except tk.TclError:  # a theme font that isn't a registered named font
                return tkfont.Font(font=name) if name else tkfont.nametofont(fallback)

        body = font_of("Treeview", "TkDefaultFont")
        heading = font_of("Treeview.Heading", "TkHeadingFont")
        rows = self.tree.get_children()[:500]
        for cid, title, _weight, min_width, _anchor in self.columns:
            if cid in self.fit_content:
                widest = max([heading.measure(title)] + [body.measure(str(self.tree.set(r, cid))) for r in rows])
                cap = self.fit_content[cid]
                self.min_widths[cid] = max(min_width, min(widest + 28, cap) if cap else widest + 28)
        self._fit()

    def _ensure_item_focus(self, _event=None):
        """Arrow keys need an item with focus; give it the selected row, or the first."""
        if self.tree.focus() and self.tree.exists(self.tree.focus()):
            return
        target = self.selected() or next(iter(self.tree.get_children()), None)
        if target:
            self.tree.focus(target)

    def clear(self):
        self._kept_view = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())

    def restore_view(self):
        """After a refresh re-filled the rows, put the list back where the user had scrolled it."""
        self.tree.update_idletasks()
        self.tree.yview_moveto(getattr(self, "_kept_view", 0.0))

    def set_empty(self, text: str):
        self.empty_text = text
        self.update_empty()

    def update_empty(self):
        self.autosize()
        if self.tree.get_children() or not self.empty_text:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text=self.empty_text)
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")

    def selected(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def reselect(self, iid: str | None, *, see: bool = True) -> bool:
        if iid and self.tree.exists(iid):
            if self.tree.selection() != (iid,):
                self.tree.selection_set(iid)
            self.tree.focus(iid)
            if see:
                self.tree.see(iid)
            return True
        if self.tree.selection():
            self.tree.selection_remove(*self.tree.selection())
        return False


# --- structured inspector -------------------------------------------------------------

TONES = {"info": ("info_bg", "info"), "warn": ("warn_bg", "warn"), "danger": ("danger_bg", "danger"),
         "agent": ("agent_bg", "agent_border"), "ok": ("surface", "ok")}


class Inspector(ttk.Frame):
    """A details pane built from sections and label/value fields, with an action bar at the bottom."""

    def __init__(self, master, empty_text: str):
        super().__init__(master)
        bar = ttk.Frame(self, padding=(12, 8))
        bar.pack(side="bottom", fill="x")
        self.actions = ttk.Frame(bar)  # buttons, wrapping onto more rows when the pane is narrow
        self.actions.pack(fill="x")
        on_resize(self.actions, self._flow_actions, add=None)
        self.notes = ttk.Frame(bar)  # an explanation under the buttons, so they never crowd each other out
        self.notes.pack(fill="x")
        self._note = None
        on_resize(bar, self._wrap_note, add=None)
        ttk.Separator(self).pack(side="bottom", fill="x")
        self.scroll = ScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True)
        self.body = self.scroll.body
        self.body.columnconfigure(1, weight=1)
        self.empty_text = empty_text
        self.key = None
        self._wrapping: list[tuple[tk.Widget, int]] = []
        on_resize(self.body, self._rewrap)
        self.show_empty()

    def _rewrap(self, _event=None):
        width = self.body.winfo_width()
        for widget, margin in self._wrapping:
            try:
                widget.configure(wraplength=max(200, width - margin))
            except tk.TclError:
                pass

    def begin(self, key=None):
        """Start rebuilding. With the same key as last time (a live refresh of the same record),
        the scroll position is kept; a different record starts at the top."""
        same = key is not None and key == self.key
        position = self.scroll.canvas.yview()[0] if same else 0.0
        self.key = key
        for child in list(self.body.winfo_children()) + list(self.actions.winfo_children()) + list(self.notes.winfo_children()):
            child.destroy()
        self._wrapping = []
        self._note = None
        self.row = 0
        self.after_idle(lambda: self.scroll.canvas.yview_moveto(position))

    def show_empty(self, text: str | None = None):
        self.begin()
        self.key = None
        label = ttk.Label(self.body, text=text or self.empty_text, style="Muted.TLabel", justify="left")
        label.grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=16)
        self._wrapping.append((label, 40))

    def _next(self) -> int:
        self.row += 1
        return self.row

    def title(self, text: str, subtitle: str | None = None):
        label = ttk.Label(self.body, text=text, style="Title.TLabel", justify="left")
        label.grid(row=self._next(), column=0, columnspan=3, sticky="w", padx=16, pady=(14, 0))
        self._wrapping.append((label, 40))
        if subtitle:
            sub = ttk.Label(self.body, text=subtitle, style="Muted.TLabel", justify="left")
            sub.grid(row=self._next(), column=0, columnspan=3, sticky="w", padx=16)
            self._wrapping.append((sub, 40))

    def chips(self, items: list[tuple[str, str]]):
        """Status chips: [(text, tone)] where tone is ok/info/warn/danger/agent."""
        row = ttk.Frame(self.body)
        row.grid(row=self._next(), column=0, columnspan=3, sticky="w", padx=16, pady=(8, 0))
        for i, (text, tone) in enumerate(items):
            make_chip(row, text, tone).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 6), pady=2)

    def section(self, text: str):
        ttk.Label(self.body, text=text, style="Heading.TLabel").grid(
            row=self._next(), column=0, columnspan=3, sticky="w", padx=16, pady=(18, 2))
        ttk.Separator(self.body).grid(row=self._next(), column=0, columnspan=3, sticky="ew", padx=16, pady=(0, 6))

    def field(self, label: str, value, *, mono: bool = False, copy: bool = False, style: str | None = None):
        r = self._next()
        ttk.Label(self.body, text=label, style="Muted.TLabel", wraplength=200, justify="left").grid(
            row=r, column=0, sticky="nw", padx=(16, 12), pady=2)
        value = "" if value is None else str(value)
        widget = ttk.Label(self.body, text=value, justify="left", font="LW.Mono" if mono else "LW.Body",
                           style=style or "TLabel")
        widget.grid(row=r, column=1, sticky="w", pady=2)
        self._wrapping.append((widget, 260))
        if copy and value:
            ttk.Button(self.body, text="Copy", command=lambda: self._copy(value)).grid(row=r, column=2, sticky="e", padx=16)

    def para(self, text: str, *, style: str | None = None, font: str = "LW.Body"):
        label = ttk.Label(self.body, text=text, justify="left", style=style or "TLabel", font=font)
        label.grid(row=self._next(), column=0, columnspan=3, sticky="w", padx=16, pady=2)
        self._wrapping.append((label, 40))

    def callout(self, heading: str, text: str, tone: str = "info"):
        bg_key, fg_key = TONES[tone]
        p = theme.PALETTE
        frame = tk.Frame(self.body, bg=p[bg_key], highlightthickness=0)
        frame.grid(row=self._next(), column=0, columnspan=3, sticky="ew", padx=16, pady=6)
        tk.Frame(frame, bg=p[fg_key], width=4).pack(side="left", fill="y")
        inner = tk.Frame(frame, bg=p[bg_key])
        inner.pack(side="left", fill="both", expand=True, padx=10, pady=8)
        tk.Label(inner, text=heading, bg=p[bg_key], fg=p[fg_key], font="LW.Strong", anchor="w", justify="left").pack(fill="x")
        body = tk.Label(inner, text=text, bg=p[bg_key], fg=p["text"], font="LW.Body", anchor="w", justify="left")
        body.pack(fill="x")
        self._wrapping.append((body, 80))

    def code(self, text: str, max_lines: int = 18):
        lines = text.count("\n") + 1
        box, widget = scrolled(self.body, tk.Text, horizontal=True, wrap="none", font="LW.Mono",
                               height=min(lines, max_lines), background=theme.PALETTE["code_bg"])
        widget.insert("1.0", text)
        widget.configure(state="disabled")
        box.grid(row=self._next(), column=0, columnspan=3, sticky="ew", padx=16, pady=4)

    def link(self, text: str, command):
        """A navigation link: a real button, so it's reachable with Tab and activated with Space/Return."""
        button = ttk.Button(self.body, text=text, command=command)
        button.grid(row=self._next(), column=0, columnspan=3, sticky="w", padx=16, pady=3)

    def action(self, text: str, command, *, accent: bool = False, enabled: bool = True, side: str = "left"):
        button = ttk.Button(self.actions, text=text, command=command, style="Accent.TButton" if accent else "TButton",
                            state="normal" if enabled else "disabled")
        button._side = side
        self._flow_actions()
        return button

    def _flow_actions(self, _event=None):
        """Lay the action buttons out left to right, wrapping onto another row when they don't fit,
        so none is ever clipped in a narrow pane. Buttons added with side="right" go last."""
        buttons = [b for b in self.actions.winfo_children() if isinstance(b, ttk.Button)]
        buttons = [b for b in buttons if getattr(b, "_side", "left") != "right"] + \
                  [b for b in buttons if getattr(b, "_side", "left") == "right"]
        width = self.actions.winfo_width()
        if width <= 1:
            width = 10_000  # not laid out yet: one row now, re-flowed on <Configure>
        row = col = x = 0
        for b in buttons:
            need = b.winfo_reqwidth() + 8
            if col and x + need > width:
                row, col, x = row + 1, 0, 0
            b.grid(row=row, column=col, sticky="w", padx=(0, 8), pady=(0, 4))
            col, x = col + 1, x + need

    def action_note(self, text: str):
        self._note = ttk.Label(self.notes, text=text, style="Small.TLabel", justify="left")
        self._note.pack(anchor="w", pady=(6, 0))
        self._wrap_note()

    def _wrap_note(self, _event=None):
        if self._note is not None and self._note.winfo_exists():
            self._note.configure(wraplength=max(200, self.notes.winfo_width() - 8))

    def _copy(self, value: str):
        self.clipboard_clear()
        self.clipboard_append(value)


def make_chip(parent, text: str, tone: str = "info") -> tk.Label:
    bg_key, fg_key = TONES.get(tone, TONES["info"])
    p = theme.PALETTE
    return tk.Label(parent, text=f" {text} ", bg=p[bg_key], fg=p[fg_key], font="LW.Small", padx=6, pady=2)


# --- parameter 2 chips ----------------------------------------------------------------

class CheckChips(ttk.Frame):
    """Toggle buttons for parameter 2. Keyboard: Tab to focus, Space to toggle."""

    PER_ROW = 5

    def __init__(self, master, on_change=None):
        super().__init__(master)
        self.on_change = on_change
        self.vars: dict[str, tk.BooleanVar] = {}

    def set_options(self, options, selected=None):
        keep = set(selected if selected is not None else self.values())
        for child in self.winfo_children():
            child.destroy()
        self.vars = {}
        for i, option in enumerate(options):
            var = tk.BooleanVar(value=option.value in keep)
            ttk.Checkbutton(self, text=option.value, variable=var, style="Toggle.TButton",
                            command=self._changed).grid(row=i // self.PER_ROW, column=i % self.PER_ROW, padx=(0, 6), pady=3, sticky="w")
            self.vars[option.value] = var
        self._changed()

    def _changed(self):
        if self.on_change:
            self.on_change()

    def values(self) -> list[str]:
        return sorted(v for v, var in self.vars.items() if var.get())


# --- dialogs ----------------------------------------------------------------------------

class _Dialog(tk.Toplevel):
    def __init__(self, master, title: str):
        super().__init__(master)
        self.title(title)
        self.configure(bg=theme.PALETTE["bg"])
        if master.winfo_viewable():
            self.transient(master)
        self.result = None
        self.anchor = None  # a widget to open beside (the button that opened it), instead of centered
        self.frame = ttk.Frame(self, padding=16)
        self.frame.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda _e: self.destroy())

    def place_on_screen(self) -> None:
        """Just below the anchor (above it if there's no room), else centered over the window that opened it,
        else centered on the screen. Windows otherwise opens new windows near the top-left of the screen."""
        self.withdraw()
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        owner = self.master.winfo_toplevel()
        # Keep clear of the taskbar (Windows) or the menu bar and Dock (macOS, which otherwise moves the window
        # itself, onto the button that opened it). wm maxsize is the usable height, less a title bar.
        bottom = max(40, sh - owner.wm_maxsize()[1])
        above = False
        if self.anchor is not None and self.anchor.winfo_viewable():
            x = self.anchor.winfo_rootx()
            y = self.anchor.winfo_rooty() + self.anchor.winfo_height() + 4
            if y + h > sh - bottom:
                y, above = self.anchor.winfo_rooty() - h - 4, True
        elif owner.winfo_viewable():
            x = owner.winfo_rootx() + (owner.winfo_width() - w) // 2
            y = owner.winfo_rooty() + (owner.winfo_height() - h) // 2
        else:
            x, y = (sw - w) // 2, (sh - h) // 3
        x, y = min(max(x, 0), max(sw - w, 0)), min(max(y, 0), max(sh - h - bottom, 0))
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        if above:  # the title bar adds height above the contents: move up so it doesn't cover the anchor
            self.update_idletasks()
            self.geometry(f"+{x}+{max(y - (self.winfo_rooty() - y), 0)}")

    def run(self):
        self.place_on_screen()
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window()
        return self.result




class MessageDialog(_Dialog):
    """A themed, parent-centered message with messagebox-compatible answers."""

    def __init__(self, master, title, message, *, kind="info", buttons="ok", default=None, detail=None):
        super().__init__(master, title)
        self.resizable(False, False)
        choices = {"ok": (("ok", "OK", "ok"),),
                   "yesno": (("yes", "Yes", True), ("no", "No", False)),
                   "okcancel": (("ok", "OK", True), ("cancel", "Cancel", False))}[buttons]
        self.result = choices[-1][2]  # Escape and the window's close button cancel.
        default = default or choices[0][0]
        self.default_result = next(value for name, _label, value in choices if name == default)
        style = {"info": "Link.TLabel", "warning": "Warn.TLabel",
                 "error": "Danger.TLabel", "question": "Strong.TLabel"}[kind]
        ttk.Label(self.frame, text={"info": "Information", "warning": "Warning", "error": "Error",
                                    "question": "Please confirm"}[kind], style=style).pack(anchor="w")
        self.page = ScrollableFrame(self.frame)
        self.page.pack(fill="both", expand=True)
        self.page.canvas.configure(width=500, height=1)
        self.message = ttk.Label(self.page.body, text=str(message), wraplength=480, justify="left")
        self.message.pack(fill="x", pady=(8, 0))
        if detail:
            ttk.Label(self.page.body, text=detail, wraplength=480, justify="left",
                      style="Muted.TLabel").pack(fill="x", pady=(8, 0))
        row = ttk.Frame(self.frame)
        row.pack(side="bottom", anchor="e", pady=(18, 0), before=self.page)
        self.buttons = {}
        for name, label, value in choices:
            button = ttk.Button(row, text=label, command=lambda result=value: self._finish(result),
                                style="Accent.TButton" if name == default else "TButton")
            button.pack(side="left", padx=(8, 0))
            self.buttons[name] = button
            # Handle this before the TButton class binding, including during startup.
            for sequence in ("<Return>", "<KP_Enter>"):
                button.bind(sequence, self._activate)
        self.default_button = self.buttons[default]
        self.bind("<Return>", self._activate)
        self.bind("<KP_Enter>", self._activate)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def place_on_screen(self):
        self.withdraw()
        left, top, right, bottom = work_area(self.master)
        dl, dt, dr, db = frame_insets(self)
        width = min(540, right - left - dl - dr)
        for label in self.page.body.winfo_children():
            label.configure(wraplength=min(480, max(1, width - 60)))
        # Measure while unmapped; only the reading area yields space to the footer.
        self.update_idletasks()
        chrome = self.winfo_reqheight() - self.page.winfo_reqheight()
        height = min(chrome + self.page.body.winfo_reqheight(), bottom - top - dt - db)
        owner = self.master.winfo_toplevel()
        x = owner.winfo_rootx() + (owner.winfo_width() - width) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - height) // 2
        x = max(left, min(x, right - width - dl - dr))
        y = max(top, min(y, bottom - height - dt - db))
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.deiconify()

    def _activate(self, _event=None):
        focused = self.focus_get()
        button = focused if focused in self.buttons.values() else self.default_button
        button.invoke()
        return "break"

    def _finish(self, result):
        self.result = result
        self.destroy()

    def run(self):
        previous_grab, previous_focus = self.grab_current(), self.focus_get()
        try:
            self.place_on_screen()
            if not self.winfo_viewable():
                self.wait_visibility()
            self.grab_set()
            self.default_button.focus_set()
            self.wait_window()
            return self.result
        finally:
            # Validation and confirmations often open on top of another modal dialog.
            if previous_grab is not None and previous_grab.winfo_exists():
                previous_grab.grab_set()
            if previous_focus is not None and previous_focus.winfo_exists():
                previous_focus.focus_set()


class WorkspaceDialog(_Dialog):
    """Choose where the warrant directory lives: create a new one, or open an existing one."""

    def __init__(self, master, *, initial: Path, intro: bool = True):
        super().__init__(master, "Choose your warrant directory" if intro else "Open or create a project")
        f = self.frame
        ttk.Label(f, text="Where should Lucid Warrant keep its records?", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(f, text="The warrant directory holds your signing key, signed warrants, the agent's inbox and the "
                          "record of every search. Choose a folder you back up. Pick a project folder (where your "
                          "agent works) and its lucid-warrant folder is opened, or created inside it; an existing "
                          "warrant directory or an empty folder is used as it is.", wraplength=520, justify="left",
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 12))
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.path = tk.StringVar(value=str(initial))
        ttk.Entry(row, textvariable=self.path, width=56).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self._browse).pack(side="left", padx=(8, 0))
        self.note = ttk.Label(f, style="Small.TLabel", wraplength=520, justify="left")
        self.note.pack(anchor="w", pady=(8, 0))
        self.path.trace_add("write", lambda *_: self._describe())
        self._describe()
        buttons = ttk.Frame(f)
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text="Continue", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.bind("<Return>", lambda _e: self._ok())

    def chosen(self) -> Path:
        """The warrant directory that will be used (see workspace.for_folder)."""
        from ..workspace import for_folder

        return for_folder(Path(self.path.get().strip()))

    def _describe(self):
        from ..workspace import is_workspace

        try:
            target = self.chosen()
        except OSError:
            self.note.configure(text="That folder can't be read.")
            return
        if is_workspace(target):
            text = f"Opens the existing warrant directory {target}."
        else:
            text = f"Creates a new warrant directory at {target}."
        self.note.configure(text=text)

    def _browse(self):
        from tkinter import filedialog

        start = Path(self.path.get()).expanduser()
        while not start.exists() and start != start.parent:
            start = start.parent
        folder = filedialog.askdirectory(parent=self, initialdir=str(start), mustexist=False,
                                         title="Choose a folder for the warrant directory")
        if folder:
            self.path.set(folder)

    def _ok(self):
        if not self.path.get().strip():
            messagebox.showerror(self.title(), "Choose a folder.", parent=self)
            return
        try:
            self.result = self.chosen()
        except OSError as exc:
            messagebox.showerror(self.title(), f"That folder can't be used: {exc}", parent=self)
            return
        self.destroy()

    @classmethod
    def ask(cls, master, **kwargs):
        return cls(master, **kwargs).run()


class TextDialog(_Dialog):
    """Ask for one line of text, in the app's theme. (Tk's simpledialog uses classic buttons, which macOS draws
    with its own white background while the theme sets light text for dark schemes: unreadable.)"""

    def __init__(self, master, title: str, prompt: str, initialvalue: str = ""):
        super().__init__(master, title)
        f = self.frame
        ttk.Label(f, text=prompt, wraplength=460, justify="left").pack(anchor="w")
        self.value = tk.StringVar(value=initialvalue or "")
        entry = ttk.Entry(f, textvariable=self.value, width=48)
        entry.pack(fill="x", pady=(6, 12))
        buttons = ttk.Frame(f)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="OK", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.bind("<Return>", lambda _e: self._ok())
        entry.focus_set()
        entry.select_range(0, "end")

    def _ok(self):
        self.result = self.value.get()
        self.destroy()

    @classmethod
    def ask(cls, master, title: str, prompt: str, initialvalue: str = ""):
        return cls(master, title, prompt, initialvalue).run()


REASSESS_UNITS = {"minutes": 1, "hours": 60, "days": 1440}


def format_wait(minutes) -> str:
    if minutes is None:
        return "never"
    for unit, size in (("day", 1440), ("hour", 60), ("minute", 1)):
        if minutes % size == 0:
            n = minutes // size
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return f"{minutes} minutes"


class RejectDialog(_Dialog):
    """Reject hit terms: a reason the agent will see, and when it may nominate them again (or never)."""

    def __init__(self, master, literals: list[str], *, reason: str = "", minutes: int | None = 30 * 1440):
        super().__init__(master, "Reject " + ("hit term" if len(literals) == 1 else f"{len(literals)} hit terms"))
        f = self.frame
        shown = ", ".join(f"\u201c{x}\u201d" for x in literals[:6]) + (" …" if len(literals) > 6 else "")
        ttk.Label(f, text=f"Reject {shown}", style="Strong.TLabel", wraplength=480, justify="left").pack(anchor="w")
        ttk.Label(f, text="Reason, for the agent (it sees this in its status):", justify="left").pack(anchor="w", pady=(10, 2))
        self.reason = tk.StringVar(value=reason)
        entry = ttk.Entry(f, textvariable=self.reason, width=56)
        entry.pack(fill="x")
        ttk.Label(f, text="The agent may nominate it again:").pack(anchor="w", pady=(12, 2))
        self.mode = tk.StringVar(value="never" if minutes is None else "after")
        row = ttk.Frame(f)
        row.pack(anchor="w")
        ttk.Radiobutton(row, text="After", value="after", variable=self.mode).pack(side="left")
        unit, amount = "days", 30
        if minutes:
            unit = next(u for u in ("days", "hours", "minutes") if minutes % REASSESS_UNITS[u] == 0)
            amount = minutes // REASSESS_UNITS[unit]
        self.amount = tk.StringVar(value=str(amount))
        ttk.Spinbox(row, from_=1, to=999, textvariable=self.amount, width=5).pack(side="left", padx=6)
        self.unit = tk.StringVar(value=unit)
        ttk.Combobox(row, textvariable=self.unit, values=list(REASSESS_UNITS), state="readonly", width=8).pack(side="left")
        ttk.Radiobutton(f, text="Never (rejected until you undo it)", value="never", variable=self.mode).pack(anchor="w", pady=(4, 0))
        ttk.Label(f, text="Until then, nominations of this term are held back. After that, it comes back for review "
                          "if it's nominated again, with this reason attached.", style="Small.TLabel",
                  wraplength=480, justify="left").pack(anchor="w", pady=(10, 0))
        buttons = ttk.Frame(f)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Reject", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.bind("<Return>", lambda _e: self._ok())
        entry.focus_set()

    def _ok(self):
        from ..review import MAX_REJECT_REASON

        reason = " ".join(self.reason.get().split())
        if len(reason) > MAX_REJECT_REASON:
            messagebox.showerror(self.title(), f"Keep the reason to {MAX_REJECT_REASON} characters or fewer.", parent=self)
            return
        minutes = None
        if self.mode.get() == "after":
            try:
                amount = int(self.amount.get())
                if not 1 <= amount <= 999:
                    raise ValueError
            except ValueError:
                messagebox.showerror(self.title(), "Enter a whole number from 1 to 999.", parent=self)
                return
            minutes = amount * REASSESS_UNITS[self.unit.get()]
        self.result = {"reason": reason, "minutes": minutes}
        self.destroy()

    @classmethod
    def ask(cls, master, literals, **kwargs):
        return cls(master, literals, **kwargs).run()


def askstring(title: str, prompt: str, *, parent, initialvalue: str = ""):
    """Drop-in for simpledialog.askstring: the text, or None if cancelled."""
    return TextDialog.ask(parent, title, prompt, initialvalue)


class PassphraseDialog(_Dialog):
    def __init__(self, master, title: str, summary: str, confirm: bool = False, document=None):
        super().__init__(master, title)
        self.confirm = confirm
        f = self.frame
        if document:
            from .document import render_text

            box, text = scrolled(f, tk.Text, width=96, height=34)
            render_text(text, document)
            box.pack(fill="both", expand=True, pady=(0, 12))
            self.geometry("")
        elif summary:
            ttk.Label(f, text="You are signing:", style="Heading.TLabel").pack(anchor="w")
            box, text = scrolled(f, tk.Text, width=100, height=min(24, summary.count("\n") + 2), wrap="word",
                                 font="LW.Mono")
            text.insert("1.0", summary)
            text.configure(state="disabled")
            box.pack(fill="both", expand=True, pady=(4, 12))
        ttk.Label(f, text="Passphrase").pack(anchor="w")
        self.first = ttk.Entry(f, show="•", width=40)
        self.first.pack(anchor="w", pady=(2, 0))
        if confirm:
            ttk.Label(f, text="Repeat passphrase").pack(anchor="w", pady=(8, 0))
            self.second = ttk.Entry(f, show="•", width=40)
            self.second.pack(anchor="w", pady=(2, 0))
        buttons = ttk.Frame(f)
        buttons.pack(anchor="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Sign" if (summary or document) and not confirm else "OK", style="Accent.TButton",
                   command=self._ok).pack(side="right", padx=8)
        self.bind("<Return>", lambda _e: self._ok())
        self.first.focus_set()

    def _ok(self):
        value = self.first.get()
        if self.confirm and value != self.second.get():
            messagebox.showerror("Passphrase", "The passphrases differ.", parent=self)
            return
        self.result = value
        self.destroy()

    @classmethod
    def ask(cls, master, title: str, summary: str = "", confirm: bool = False, document=None):
        return cls(master, title, summary, confirm, document).run()


class MonthCalendar(tk.Frame):
    """A clickable month grid that follows the theme palette. Arrow keys move, Enter picks."""

    def __init__(self, master, on_select):
        p = theme.PALETTE
        super().__init__(master, bg=p["surface"], highlightthickness=1, highlightbackground=p["border"],
                         highlightcolor=p["accent"], padx=8, pady=6, takefocus=1)
        self.on_select = on_select
        self.selected: date | None = None
        self.cursor = date.today()
        self.range: tuple[date | None, date | None] = (None, None)
        self.year, self.month = self.cursor.year, self.cursor.month
        header = tk.Frame(self, bg=p["surface"])
        header.grid(row=0, column=0, columnspan=7, sticky="ew", pady=(0, 4))
        for text, months, side in (("«", -12, "left"), ("‹", -1, "left"), ("»", 12, "right"), ("›", 1, "right")):
            nav = tk.Label(header, text=text, bg=p["surface"], fg=p["accent"], cursor="hand2", font="LW.Strong", padx=6)
            nav.pack(side=side)
            nav.bind("<Button-1>", lambda _e, m=months: self.shift(m))
        self.title_label = tk.Label(header, bg=p["surface"], fg=p["text"], font="LW.Strong")
        self.title_label.pack(side="left", expand=True)
        for col, name in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
            tk.Label(self, text=name, bg=p["surface"], fg=p["muted"], font="LW.Small").grid(row=1, column=col)
        self.cells = []
        for i in range(42):
            cell = tk.Label(self, width=3, cursor="hand2", font="LW.Body")
            cell.grid(row=2 + i // 7, column=i % 7, padx=1, pady=1)
            cell.bind("<Button-1>", lambda _e, i=i: self._click(i))
            self.cells.append(cell)
        self.days: list[date] = []
        for key, delta in (("<Left>", -1), ("<Right>", 1), ("<Up>", -7), ("<Down>", 7)):
            self.bind(key, lambda _e, d=delta: self._move(d))
        self.bind("<Return>", lambda _e: self._pick(self.cursor))
        self.bind("<space>", lambda _e: self._pick(self.cursor))
        self.bind("<FocusIn>", lambda _e: self.draw())
        self.bind("<FocusOut>", lambda _e: self.draw())
        self.draw()

    def shift(self, months: int):
        index = self.year * 12 + self.month - 1 + months
        if MIN_YEAR <= index // 12 <= MAX_YEAR:
            self.year, self.month = index // 12, index % 12 + 1
            self.draw()

    def show(self, day: date):
        self.year, self.month, self.cursor = day.year, day.month, day
        self.draw()

    def _move(self, delta: int):
        target = self.cursor + timedelta(days=delta)
        if MIN_YEAR <= target.year <= MAX_YEAR:
            self.show(target)

    def draw(self):
        p = theme.PALETTE
        self.title_label.configure(text=f"{calendar.month_name[self.month]} {self.year}")
        weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(self.year, self.month)
        days = [d for week in weeks for d in week]
        days += [days[-1] + timedelta(days=n + 1) for n in range(42 - len(days))]
        self.days = days
        low, high = self.range
        focused = self.focus_get() is self
        for cell, day in zip(self.cells, days):
            if day == self.selected:
                bg, fg = p["accent"], p["accent_text"]
            elif low and high and low <= day <= high:
                bg, fg = p["accent_soft"], p["text"]
            else:
                bg, fg = p["surface"], p["text"] if day.month == self.month else p["muted"]
            relief = "solid" if focused and day == self.cursor else "flat"
            cell.configure(text=str(day.day), bg=bg, fg=fg, relief=relief, bd=1 if relief == "solid" else 0,
                           font="LW.Strong" if day == date.today() else "LW.Body")

    def _click(self, index: int):
        self._pick(self.days[index])

    def _pick(self, day: date):
        self.selected = self.cursor = day
        if day.month != self.month:
            self.show(day)
        self.on_select(day)


class DateRangeDialog(_Dialog):
    def __init__(self, master):
        super().__init__(master, "Block a date range")
        self.resizable(False, False)
        self.start: date | None = None
        self.end: date | None = None
        f = self.frame
        ttk.Label(f, text="Pick the first and last day to block (UTC calendar days; both ends blocked). "
                  "Click a day, or Tab into a calendar and use the arrow keys and Enter.",
                  style="Muted.TLabel", wraplength=560).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.start_text, self.end_text = tk.StringVar(), tk.StringVar()
        for col, (label, var, which) in enumerate((("From", self.start_text, "start"), ("To", self.end_text, "end"))):
            box = ttk.Frame(f)
            box.grid(row=1, column=col, sticky="w", padx=(0 if col == 0 else 16, 0))
            ttk.Label(box, text=label).pack(side="left")
            entry = ttk.Entry(box, textvariable=var, width=12)
            entry.pack(side="left", padx=6)
            entry.bind("<Return>", lambda _e, w=which: self._typed(w))
            entry.bind("<FocusOut>", lambda _e, w=which: self._typed(w, quiet=True))
            ttk.Label(box, text="YYYY-MM-DD", style="Small.TLabel").pack(side="left")
        self.from_cal = MonthCalendar(f, on_select=lambda d: self._pick("start", d))
        self.from_cal.grid(row=2, column=0, sticky="nw", pady=8)
        self.to_cal = MonthCalendar(f, on_select=lambda d: self._pick("end", d))
        self.to_cal.grid(row=2, column=1, sticky="nw", pady=8, padx=(16, 0))
        bottom = ttk.Frame(f)
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.summary = ttk.Label(bottom, text="Pick a From date.")
        self.summary.pack(side="left")
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Add", style="Accent.TButton", command=self._ok).pack(side="right", padx=8)

    def _pick(self, which: str, day: date):
        if which == "start":
            self.start = day
            if self.end is None or self.end < day:
                self.end = day
                self.to_cal.show(day)
        else:
            self.end = day
            if self.start is None or self.start > day:
                self.start = day
                self.from_cal.show(day)
        self._update()

    def _typed(self, which: str, quiet: bool = False):
        text = (self.start_text if which == "start" else self.end_text).get().strip()
        if not text:
            return
        try:
            day = parse_day(text)
        except DateLimitError as exc:
            if not quiet:
                messagebox.showerror("Block a date range", str(exc), parent=self)
            return
        (self.from_cal if which == "start" else self.to_cal).show(day)
        self._pick(which, day)

    def _update(self):
        self.start_text.set(self.start.isoformat() if self.start else "")
        self.end_text.set(self.end.isoformat() if self.end else "")
        for cal, chosen in ((self.from_cal, self.start), (self.to_cal, self.end)):
            cal.selected = chosen
            cal.range = (self.start, self.end)
            cal.draw()
        if self.start and self.end:
            days = (self.end - self.start).days + 1
            self.summary.configure(text=f"Blocking {self.start.isoformat()} to {self.end.isoformat()} ({plural(days, 'day')})")

    def _ok(self):
        start_text, end_text = self.start_text.get().strip(), self.end_text.get().strip()
        try:
            start = parse_day(start_text) if start_text else None
            end = parse_day(end_text) if end_text else start
        except DateLimitError as exc:
            messagebox.showerror("Block a date range", str(exc), parent=self)
            return
        if start is None:
            messagebox.showerror("Block a date range", "Pick a From date.", parent=self)
            return
        if end < start:
            messagebox.showerror("Block a date range", "The To date is before the From date.", parent=self)
            return
        self.result = {"start": start.isoformat(), "end": end.isoformat()}
        self.destroy()

    @classmethod
    def ask(cls, master, anchor=None):
        dialog = cls(master)
        dialog.anchor = anchor
        return dialog.run()


class PromoteDialog(_Dialog):
    """Name and describe a template before adding it to the library. The id is derived from the name."""

    def __init__(self, master, library: dict, skeleton: str, plain: str, shape: str, default_name: str = ""):
        super().__init__(master, "Add template to library")
        self.library = library
        f = self.frame
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="This changes what the agent may do", style="Heading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Before: one approved run of one query with fixed terms.\n"
                          "After: a reusable template the agent may run with the approved terms of any active warrant, without asking.",
                  wraplength=620, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))
        for r, (label, value, mono) in enumerate((("What it does", plain, False), ("Lucene", shape, True), ("Skeleton", skeleton, True)), start=2):
            ttk.Label(f, text=label, style="Muted.TLabel").grid(row=r, column=0, sticky="nw", pady=2)
            ttk.Label(f, text=value, wraplength=560, justify="left", font="LW.Mono" if mono else "LW.Body").grid(
                row=r, column=1, sticky="w", padx=8, pady=2)
        ttk.Separator(f).grid(row=5, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(f, text="Name").grid(row=6, column=0, sticky="w")
        self.name = tk.StringVar(value=default_name)
        entry = ttk.Entry(f, textvariable=self.name, width=52)
        entry.grid(row=6, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Generated from the query's structure; edit it if you like.", style="Small.TLabel").grid(
            row=7, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Description").grid(row=8, column=0, sticky="nw", pady=(10, 0))
        self.description = tk.Text(f, width=62, height=3, wrap="word", font="LW.Body", **theme.text_widget_colors())
        self.description.insert("1.0", plain)
        self.description.grid(row=8, column=1, sticky="w", padx=8, pady=(10, 0))
        ttk.Label(f, text="Analyst-maintained; pre-filled with the generated description.", style="Small.TLabel").grid(
            row=9, column=1, sticky="w", padx=8)
        self.id_label = ttk.Label(f, style="Small.TLabel")
        self.id_label.grid(row=10, column=1, sticky="w", padx=8, pady=(8, 0))
        self.name.trace_add("write", lambda *_: self._update_id())
        self._update_id()
        buttons = ttk.Frame(f)
        buttons.grid(row=11, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Continue to signature…", style="Accent.TButton", command=self._ok).pack(side="right", padx=8)
        entry.focus_set()
        entry.select_range(0, "end")

    def _update_id(self):
        name = self.name.get().strip()
        self.id_label.configure(text=f"The agent will refer to it as: {suggest_template_id(self.library, name)}" if name else "")

    def _ok(self):
        name = self.name.get().strip()
        description = self.description.get("1.0", "end").strip()
        if not name or not description:
            messagebox.showerror("Add template", "Give it a name and a description.", parent=self)
            return
        self.result = (name, description, suggest_template_id(self.library, name))
        self.destroy()

    @classmethod
    def ask(cls, master, library, skeleton, plain, shape, default_name=""):
        return cls(master, library, skeleton, plain, shape, default_name).run()


class GrowTable(ttk.Frame):
    """A small table with headers and one row per item that grows instead of scrolling.

    columns: [(id, title, width)]. Rows are lists of values. An empty table shows one
    placeholder row so the headers never float alone.
    """

    def __init__(self, master, columns, *, placeholder: str = "None", on_activate=None,
                 fit_column: str | None = None):
        super().__init__(master)
        self.fit_column = fit_column
        if fit_column is not None:
            on_resize(self, lambda _e: self._refit())
        self.columns = columns
        self.placeholder = placeholder
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings", height=1, selectmode="extended")
        for cid, title, width in columns:
            self.tree.heading(cid, text=title, anchor="w")
            self.tree.column(cid, width=width, minwidth=width, stretch=False, anchor="w")
        self.tree.pack(fill="x")
        self.tree.tag_configure("placeholder", foreground=theme.PALETTE["muted"])
        if on_activate:
            self.tree.bind("<Double-1>", lambda _e: on_activate(self.selected_indexes()))
            self.tree.bind("<Return>", lambda _e: on_activate(self.selected_indexes()))
        self.tip: tk.Toplevel | None = None
        self.tree.bind("<Motion>", self._hover, add="+")
        self.tree.bind("<Leave>", lambda _e: self._hide_tip(), add="+")
        # Re-fit when the page (or, outside a page, the window) changes size. Bind to that widget itself, never
        # to the toplevel: a toplevel binding runs for every widget inside it, on every scroll step.
        edge, parent = self.winfo_toplevel(), master
        while parent is not None:
            if isinstance(parent, tk.Canvas):
                edge = parent
                break
            parent = parent.master
        if isinstance(edge, tk.Canvas):
            on_resize(edge, lambda _e: self._refit())
        else:
            edge.bind("<Configure>", lambda e: self._refit() if e.widget is edge else None, add="+")
        self.bind("<Map>", lambda _e: self._refit(), add="+")
        self.bind("<Unmap>", lambda _e: self._hide_tip(), add="+")
        self.bind("<Destroy>", lambda e: self._hide_tip() if e.widget is self else None, add="+")
        self.set_rows([])

    def _refit(self):
        if self.winfo_exists():
            self.after_idle(lambda: self._fit_columns() if self.winfo_exists() else None)

    def set_rows(self, rows: list[list]):
        self._hide_tip()  # it may be showing a row that's about to go
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(rows):
            self.tree.insert("", "end", iid=str(i), values=row)
        if not rows:
            self.tree.insert("", "end", iid="placeholder", values=[self.placeholder] + [""] * (len(self.columns) - 1),
                             tags=("placeholder",))
        self.tree.configure(height=max(1, len(rows)))
        self._fit_columns()

    def _fit_columns(self):
        """Each column as wide as its widest value or header (at least its configured width)."""
        from tkinter import font as tkfont

        style = ttk.Style()
        try:
            body = tkfont.nametofont(style.lookup("Treeview", "font") or "TkDefaultFont")
        except tk.TclError:
            body = tkfont.Font(font=style.lookup("Treeview", "font") or "TkDefaultFont")
        needs = []
        for cid, title, width in self.columns:
            values = [str(self.tree.set(item, cid)) for item in self.tree.get_children()]
            needs.append(max(width, max([body.measure(title)] + [body.measure(v) for v in values]) + 24))
        # Columns get what they need, up to the width actually available; the last (free-text) column takes
        # the rest. Anything still too long shows in full in a tooltip.
        # Room from the table's left edge to the right edge of the visible page (the scrolling canvas, or the window).
        edge = self.winfo_toplevel()
        parent = self.master
        while parent is not None:
            if isinstance(parent, tk.Canvas):
                edge = parent
                break
            parent = parent.master
        available = (edge.winfo_rootx() + edge.winfo_width() - self.winfo_rootx() - 12) if self.winfo_ismapped() else 0
        if self.fit_column is not None and available > 0:
            # Keep every column reachable. First take space from the nominated free-text column, then
            # share any remaining shortage across the others. Full cell values remain in hover tooltips.
            available = min(available, self.master.winfo_width() - 4)
            widths = [min(n, 480) for n in needs]
            floors = [body.measure(title) + 24 for _cid, title, _width in self.columns]
            flexible = next(i for i, (cid, _title, _width) in enumerate(self.columns) if cid == self.fit_column)
            shortage = max(0, sum(widths) - available)
            take = min(shortage, max(0, widths[flexible] - floors[flexible]))
            widths[flexible] -= take
            shortage -= take
            room = sum(max(0, w - floor) for w, floor in zip(widths, floors))
            if shortage and room:
                fraction = min(1, shortage / room)
                widths = [w - int((max(0, w - floor) * fraction) + 0.999)
                          for w, floor in zip(widths, floors)]
            for (cid, _title, _width), width in zip(self.columns, widths):
                self.tree.column(cid, width=width, minwidth=width)
            return
        for i, (cid, _title, width) in enumerate(self.columns):
            cap = 480
            if i == len(self.columns) - 1 and available > 0:
                cap = max(width, available - sum(min(n, 480) for n in needs[:-1]))
            final = min(needs[i], cap) if i == len(self.columns) - 1 else min(needs[i], 480)
            self.tree.column(cid, width=final, minwidth=final)

    def _hover(self, event):
        from tkinter import font as tkfont

        row, column = self.tree.identify_row(event.y), self.tree.identify_column(event.x)
        if not row or not column or row == "placeholder":
            return self._hide_tip()
        cid = self.columns[int(column[1:]) - 1][0]
        text = str(self.tree.set(row, cid))
        try:
            body = tkfont.nametofont(ttk.Style().lookup("Treeview", "font") or "TkDefaultFont")
        except tk.TclError:
            body = tkfont.nametofont("TkDefaultFont")
        if body.measure(text) + 20 <= int(self.tree.column(cid, "width")):
            return self._hide_tip()
        if self.tip is None or not self.tip.winfo_exists():
            self.tip = tk.Toplevel(self)
            self.tip.wm_overrideredirect(True)
            tk.Label(self.tip, bg=theme.PALETTE["surface"], fg=theme.PALETTE["text"], relief="solid", borderwidth=1,
                     wraplength=520, justify="left", padx=6, pady=3).pack()
        label = self.tip.winfo_children()[0]
        label.configure(text=text)
        self.tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 16}")

    def _hide_tip(self):
        tip, self.tip = getattr(self, "tip", None), None
        try:
            if tip is not None and tip.winfo_exists():
                tip.destroy()
        except tk.TclError:
            pass

    def selected_indexes(self) -> list[int]:
        return sorted(int(i) for i in self.tree.selection() if i != "placeholder")


class CountryDialog(_Dialog):
    """Pick a country by typing its code or name, and give a reason (required for exclusions, optional for inclusions)."""

    def __init__(self, master, countries: dict, title: str, *, reason: str | None = None,
                 reason_label: str = "Reason", taken=frozenset(), reason_required: bool = True):
        self.reason_required = reason_required
        super().__init__(master, title)
        self.countries, self.taken = countries, set(taken)
        f = self.frame
        ttk.Label(f, text="Type a two-letter code or a country name").pack(anchor="w")
        self.query = tk.StringVar()
        entry = ttk.Entry(f, textvariable=self.query, width=40)
        entry.pack(anchor="w", pady=(2, 6))
        box, self.list = scrolled(f, tk.Listbox, height=10, width=44, activestyle="none", font="LW.Body")
        box.pack(fill="both", expand=True)
        self.reason = None
        if reason is not None:
            ttk.Label(f, text=reason_label).pack(anchor="w", pady=(10, 0))
            self.reason = tk.StringVar(value=reason)
            ttk.Entry(f, textvariable=self.reason, width=40).pack(anchor="w", pady=(2, 0))
        buttons = ttk.Frame(f)
        buttons.pack(anchor="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Add", style="Accent.TButton", command=self._ok).pack(side="right", padx=8)
        self.query.trace_add("write", lambda *_: self._filter())
        self.list.bind("<Double-1>", lambda _e: self._ok())
        self.bind("<Return>", lambda _e: self._ok())
        entry.bind("<Down>", lambda _e: (self.list.focus_set(), self.list.selection_set(0)))
        self._filter()
        entry.focus_set()

    def _filter(self):
        needle = self.query.get().strip().casefold()
        self.codes = [code for code, name in sorted(self.countries.items(), key=lambda kv: kv[1])
                      if code not in self.taken and (not needle or needle == code.casefold() or needle in name.casefold())]
        exact = [c for c in self.codes if c.casefold() == needle]
        self.codes = exact + [c for c in self.codes if c not in exact]
        self.list.delete(0, "end")
        for code in self.codes:
            self.list.insert("end", f"{code}  {self.countries[code]}")
        if self.codes:
            self.list.selection_set(0)

    def _ok(self):
        picked = self.list.curselection()
        if not picked:
            messagebox.showerror(self.title(), "Choose a country from the list.", parent=self)
            return
        code = self.codes[picked[0]]
        if self.reason is not None:
            reason = " ".join(self.reason.get().split())
            if not reason and self.reason_required:
                messagebox.showerror(self.title(), "Give a reason.", parent=self)
                return
            from ..scope import MAX_REASON

            if len(reason) > MAX_REASON:
                messagebox.showerror(self.title(), f"Keep the reason to {MAX_REASON} characters or fewer.", parent=self)
                return
            self.result = {"code": code, "reason": reason}
        else:
            self.result = {"code": code}
        self.destroy()

    @classmethod
    def ask(cls, master, countries, title, **kwargs):
        return cls(master, countries, title, **kwargs).run()


def text_table(headers: list[str], rows: list[list[str]]) -> str:
    """A small aligned table in plain text (for monospaced boxes): text left, numbers right."""
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    lines = []
    for n, row in enumerate([headers, *rows]):
        cells = [str(c).ljust(widths[i]) if i == 0 else str(c).rjust(widths[i]) for i, c in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def flash(widget: tk.Misc, text: str, x: int, y: int, ms: int = 900) -> tk.Toplevel:
    """A small label near the pointer that disappears by itself (e.g. "Copied!")."""
    tip = tk.Toplevel(widget)
    tip.wm_overrideredirect(True)
    tk.Label(tip, text=text, bg=theme.PALETTE["accent"], fg=theme.PALETTE["accent_text"], padx=8, pady=2,
             font="LW.Small").pack()
    tip.wm_geometry(f"+{x + 12}+{y - 28}")
    timer = tip.after(ms, tip.destroy)
    # If the owner goes first, cancel the timer so it can't fire into a destroyed widget.
    tip.bind("<Destroy>", lambda e: tip.after_cancel(timer) if e.widget is tip else None)
    return tip


def copy_text(widget: tk.Misc, text: str) -> None:
    widget.clipboard_clear()
    widget.clipboard_append(text)


# Modifier bits that mean "selecting", per platform. Shift (0x1) and Control (0x4) everywhere; macOS: Command is
# Mod1 (0x8) and Option Mod2 (0x10); Windows: Alt is 0x20000 and Mod1 (0x8) is NumLock, which must not block copying;
# X11: Alt is Mod1 (0x8), and Mod2 (0x10) is usually NumLock.
def select_modifiers(platform: str = sys.platform) -> int:
    return 0x1 | 0x4 | (0x8 | 0x10 if platform == "darwin" else 0x20000 if platform == "win32" else 0x8)


SELECT_MODIFIERS = select_modifiers()


def click_to_copy(tree: ttk.Treeview, notify=None, *, skip_columns=(), empty=("", "—")) -> None:
    """A plain click on a cell copies its text (and still selects the row). Shift/Ctrl/Cmd-clicks only select."""

    def on_click(event):
        if event.state & SELECT_MODIFIERS:
            return  # extending or changing a selection, not copying
        if tree.identify_region(event.x, event.y) != "cell":
            return
        row, column = tree.identify_row(event.y), tree.identify_column(event.x)
        if not row or not column:
            return
        # "#N" counts the columns as displayed, which may be a reordered subset (displaycolumns).
        shown = tree.cget("displaycolumns")
        shown = tree.cget("columns") if not shown or shown[0] == "#all" else shown
        cid = shown[int(column[1:]) - 1]
        text = str(tree.set(row, cid))
        if cid in skip_columns or text in empty:
            return
        copy_text(tree, text)
        flash(tree, "Copied!", event.x_root, event.y_root)
        if notify:
            notify(f"Copied \u201c{text}\u201d")

    tree.bind("<ButtonRelease-1>", on_click, add="+")
