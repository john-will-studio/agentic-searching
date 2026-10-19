"""The tutorial: what Lucid Warrant is for, then each section, one page at a time with Previous and Next.

On first launch it starts before the main window: the first page offers the light/dark theme and color scheme, so
the rest is seen in the analyst's choice, and the analyst creates a signing key on the passphrase page; the tour of the tabs continues once the window is up. Later it opens from the Tutorial button and shows each
tab (and each part of Compose) behind the page that describes it, outlined in gold, with the tutorial beside the
outline and an arrow pointing at it when there's room.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, font as tkfont

from . import msg as messagebox

from .. import org_config
from ..signing import MIN_PASSPHRASE_LENGTH, keys_exist
from . import theme
from .widgets import autohide_scrollbar
from .geometry import frame_insets, work_area

# Each page: (title, text, where to point: None, a tab attribute on the app, or ("compose", section)).
PAGES = [
    ("What Lucid Warrant does",
     "Lucid Warrant lets an analyst approve the scope of an agent's searches. Searches through Lucid Warrant "
     "enforce the signed authorization, included hit terms, geographic constraints and limits.\n\nYou review the "
     "purpose; the software cannot check whether a search serves that purpose.\n\nSigning with your passphrase "
     "approves repeated searches under the signed template library, within those constraints. Each search "
     "attempt through Lucid Warrant is recorded.", None),
    ("Your warrant directory",
     "Everything this deployment knows lives in one folder: the signed warrants, the agent's inbox, the record of "
     "every search and your signing key.\n\nYours is {workspace}. Back it up like any important record.\n\nTo keep a "
     "separate project apart, create another warrant directory from About.", None),
    ("Your signing key and passphrase",
     "Your signature is what gives a warrant force, and it is the one thing the agent can't produce.\n\nThe agent can "
     "write files, and a confused or overeager one might draft a warrant of its own or quietly widen an existing one. "
     "It would get nowhere: every search is checked against your signature, and any warrant you didn't sign, or that "
     "changed after you signed it, is refused.\n\nThe key behind that signature is locked with a passphrase only you "
     "know and never stored, so it also stops anyone who finds the computer unlocked. Use at least {min_length} "
     "characters.\n\nThere is no recovery: lose it, and you create a new key and re-sign the template library.", "key"),
    ("Compose: hit terms",
     "Hit terms are the words the agent may search for. Approve a term as a pattern to let * match any characters "
     "and ? one character within a word, after at least three ordinary characters (for example PROJECT*). They arrive from two places: a nomination script that "
     "reads your case or client systems, and the agent itself, which can propose terms with a reason.\n\nEach row shows "
     "where a term came from and, where relevant, the client it concerns. Press A to include, R to reject (you'll say "
     "why, and when the agent may try again) and P to set it aside; the same keys work for places.\n\nSeveral selected "
     "rows ask once before they're included, and a held key decides only one row. Nothing takes effect until you sign.",
     ("compose", "terms")),
    ("Compose: authorization",
     "The two parameters come from your organization's entitlement system, never from the agent. The first is the "
     "authority under which the agent searches; the second, which categories of records it may see.\n\nYou can grant "
     "only what you yourself are entitled to grant.", ("compose", "authorization")),
    ("Compose: purpose",
     "The purpose is the agent's brief: it directs the work and travels with every search the agent sends. Software "
     "can't police intent, so this is where you do.\n\nRead the agent's draft, make it say exactly what the work is for, "
     "and certify that it meets your organization's needs.", ("compose", "purpose")),
    ("Compose: geographic constraint",
     "Data-protection law follows the data. Limit searches to the countries you answer for, or exclude those that "
     "must not be searched, each with a reason.\n\nThe constraint is applied to every search, outside the agent's reach, "
     "and returned country codes are checked against it. The service supplies those codes; the software does not "
     "independently locate every record.\n\nPlaces narrow searches further to circles around "
     "particular sites, each with its own signed radius (100 m unless you change it); the converter turns MGRS, UTM "
     "or degrees-minutes-seconds into lat, lon and shows evidence from the bundled map. Import list… reads GeoJSON, KML "
     "or CSV.\n\nInclude a place after comparing its coordinate and country with your source. The country must agree "
     "with the bundled map, whose borders are approximate.\n\nMissing or unusable record coordinates are withheld "
     "when places constrain the search.", ("compose", "geography")),
    ("Compose: limits and signing",
     "Limits keep a warrant proportionate: when it expires, how many records one search may return, how many searches "
     "the agent may run in an hour, how far back it may look and which dates are off limits. Certify that they are "
     "narrowly drawn.\n\nReview and sign then shows the warrant as a document and asks for your passphrase.",
     ("compose", "limits")),
    ("Warrants",
     "Every warrant you sign, current or retired, is kept as a record. Select one to see what it allows, how often "
     "each term has been used and how close the agent is to its hourly limit.\n\nFrom here you can edit, renew or revoke "
     "it, print it with a page for every search run under it, or go straight to those runs.", "warrants_tab"),
    ("Templates",
     "The agent doesn't write free-form queries. It fits included terms into templates from a signed library: one "
     "term, both terms, either term, one but not another. Each template is described in plain English beside its "
     "Lucene.\n\nDisabling one takes effect at once, and that change is signed too.", "templates"),
    ("Syntax requests",
     "When the templates fall short, the agent can ask for something new. Each request arrives with a plain-English "
     "account of what it does, the exact query and the agent's reasoning, marked as its own words.\n\nApprove it for one "
     "run, reject it, or, once it has proved itself, promote it into the library.", "approvals"),
    ("Runs",
     "This is the audit trail: every search the agent attempted, sent or blocked, with the warrant it ran under, the "
     "terms it used, the checks it passed and the exact payload it sent.\n\nBlocked attempts are "
     "kept as well; they are often the most telling.", "runs"),
    ("Working with the agent",
     "Run lucid-warrant init in the folder where your agent works: it creates the warrant directory and gives the "
     "agent a short guide to working under a warrant.\n\nThe agent proposes terms and a purpose, waits for your "
     "signature, then searches through the lucid-warrant command, which accepts only what you signed.\n\nAppearance… "
     "changes colors and text size, About… switches projects, and this tutorial is a click away.", None),
]
KEY_PAGE = 2
ARROWS = {"left": "◀", "right": "▶", "top": "▲", "bottom": "▼"}  # the tutorial's side facing the outline
FIRST_TOUR_PAGE = 3


class SectionBox:
    """A gold outline around the part of the window a tutorial page describes: four thin bars placed over the
    container, so nothing under them is covered and the outline scrolls with the page."""

    THICKNESS = 3
    TAG = "LWSectionBox"

    def __init__(self):
        self.bars: list[tk.Frame] = []
        self.container = self.view = None
        self.rect = None

    def show(self, container, rect, view=None) -> None:
        """rect() gives (x, y, width, height) inside container; it's asked again whenever the container resizes.
        view is the visible area the container scrolls in (Compose's canvas), if it scrolls."""
        self.hide()
        self.container, self.rect, self.view = container, rect, view or container
        self.bars = [tk.Frame(container, bg=theme.HIGHLIGHT[theme.MODE], bd=0, highlightthickness=0) for _ in range(4)]
        container.bind_class(self.TAG, "<Configure>", lambda _e: self.layout())
        container.bindtags(container.bindtags() + (self.TAG,))
        self.layout()

    def layout(self) -> None:
        if not self.bars or not self.container.winfo_exists():
            return
        x, y, w, h = self.rect()
        t = self.THICKNESS
        edges = ((x, y, w, t), (x, y + h - t, w, t), (x, y, t, h), (x + w - t, y, t, h))
        for bar, (bx, by, bw, bh) in zip(self.bars, edges):
            bar.place(x=bx, y=by, width=max(bw, t), height=max(bh, t), bordermode="outside")  # ignore the padding
            bar.lift()

    def screen_rect(self) -> tuple[int, int, int, int] | None:
        """The outline's visible part on screen (clipped to the scrolled view it's in), or None if it's hidden."""
        if not self.bars or not self.container.winfo_exists():
            return None
        x, y, w, h = self.rect()
        left, top = self.container.winfo_rootx() + x, self.container.winfo_rooty() + y
        right, bottom = left + w, top + h
        view = self.view
        left = max(left, view.winfo_rootx())
        right = min(right, view.winfo_rootx() + view.winfo_width())
        top = max(top, view.winfo_rooty())
        bottom = min(bottom, view.winfo_rooty() + view.winfo_height())
        return (left, top, right - left, bottom - top) if bottom > top and right > left else None

    def hide(self) -> None:
        for bar in self.bars:
            bar.destroy()
        self.bars = []
        if self.container is not None and self.container.winfo_exists():
            self.container.bindtags(tuple(t for t in self.container.bindtags() if t != self.TAG))
        self.container = None


class Tutorial(tk.Toplevel):
    """One page at a time. phase="intro" (first launch, before the main window) stops after the key page;
    phase="tour" (and later openings) covers everything and shows each section as it goes."""

    def __init__(self, app, start: int = 0, phase: str = "tour"):
        super().__init__(app)
        self.withdraw()  # parent layout may flush while the first page is being prepared
        self.app, self.phase, self.index = app, phase, start
        self.finished = False
        self.title("Lucid Warrant tutorial")
        self.configure(bg=theme.PALETTE["surface"], highlightthickness=1,
                       highlightbackground=theme.HIGHLIGHT[theme.MODE], highlightcolor=theme.HIGHLIGHT[theme.MODE])
        self.resizable(False, False)
        if app.winfo_viewable():
            self.transient(app)
        self.box = SectionBox()
        self.placed = None  # which side of the tutorial faces the outline, if it's beside one
        self.arrow = tk.Label(self, font="LW.Title", bd=0)
        self.frame = frame = ttk.Frame(self, padding=22, style="Tutorial.TFrame")
        frame.pack(fill="both", expand=True)
        self.counter = tk.Label(frame, font="LW.Small", bg=theme.PALETTE["accent_soft"],
                                fg=theme.PALETTE["text"], padx=8, pady=3, bd=0)
        self.counter.pack(anchor="w")
        self.heading = ttk.Label(frame, style="Tutorial.Title.TLabel", wraplength=620, justify="left")
        self.heading.pack(anchor="w", pady=(2, 10))
        # Pixel bounds keep the line length steady as the named font grows. Only the
        # reading area scrolls; navigation and first-launch controls stay in view.
        self.reading = ttk.Frame(frame, width=640, height=200, style="Tutorial.TFrame")
        self.reading.pack(fill="x")
        self.reading.pack_propagate(False)
        self.scrollbar = ttk.Scrollbar(self.reading, orient="vertical")
        self.text = tk.Text(self.reading, font="LW.Tutorial", wrap="word", width=1, height=1,
                            spacing1=2, spacing2=4, spacing3=8, padx=0, pady=0,
                            **theme.text_widget_colors())
        self.text.pack(side="left", fill="both", expand=True)
        self.text.configure(highlightthickness=0, state="disabled",
                            yscrollcommand=autohide_scrollbar(self.scrollbar, self.text))  # shown only when needed
        self.scrollbar.configure(command=self.text.yview)
        self.extra = ttk.Frame(frame, style="Tutorial.TFrame")
        self.extra.pack(anchor="w", fill="x", pady=(12, 0))
        buttons = ttk.Frame(frame, style="Tutorial.TFrame")
        buttons.pack(fill="x", pady=(18, 0))
        self.close_button = ttk.Button(buttons, text="Close", command=self.close)
        self.close_button.pack(side="left")
        self.next_button = ttk.Button(buttons, text="Next", style="Accent.TButton", command=self.next)
        self.next_button.pack(side="right")
        self.prev_button = ttk.Button(buttons, text="Previous", command=self.previous)
        self.prev_button.pack(side="right", padx=8)
        self.bind("<Right>", lambda _e: self.next())
        self.bind("<Left>", lambda _e: self.previous())
        self.bind("<Escape>", lambda _e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<<ThemeChanged>>", self._theme_changed, "+")
        self._pending_place = None
        self._pending_theme = None
        self._monitor_poll = None
        self._watch_bindings = []
        self._last_bounds = None
        self.show()
        self.deiconify()
        self._monitor_poll = self.after(500, self._check_monitor)

    # --- pages ---------------------------------------------------------------------------

    def last_index(self) -> int:
        return KEY_PAGE if self.phase == "intro" else len(PAGES) - 1

    def show(self):
        title, text, target = PAGES[self.index]
        terms = self.app.settings["terms"]["hit_term"]
        for old, new in (("Hit terms", terms["plural"]), ("hit terms", terms["plural"].lower())):
            title, text = title.replace(old, new), text.replace(old, new)
        # Finish any parent scroll/reflow before changing the displayed page. No
        # event flush is allowed between these content changes and final geometry.
        self._cancel_place()
        self._point_at(target)
        # Canvas scrolling positions its embedded frame at idle. Settle it while
        # the complete previous tutorial page is still displayed.
        self.app.update_idletasks()
        self.counter.configure(text=f"{self.index + 1} of {len(PAGES)}")
        self.heading.configure(text=title)
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        # A paragraph gets extra space through its tag, without an entire blank line
        # at the largest text size.
        self.text.tag_configure("paragraph", spacing3=16)
        paragraphs = text.format(workspace=self.app.paths.data.parent, min_length=MIN_PASSPHRASE_LENGTH).split("\n\n")
        for index, paragraph in enumerate(paragraphs):
            self.text.insert("end", paragraph + ("\n" if index < len(paragraphs) - 1 else ""), "paragraph")
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)
        for child in self.extra.winfo_children():
            child.destroy()
        if target == "key":
            self._key_controls()
        elif self.phase == "intro" and self.index == 0:
            self._appearance_controls()
        self.prev_button.configure(state="normal" if self.index > 0 else "disabled")
        at_end = self.index >= self.last_index()
        self.next_button.configure(text="Finish" if at_end and self.phase != "intro" else "Next",
                                   state="disabled" if target == "key" and not keys_exist(self.app.paths) else "normal")
        self._watch_bounds()
        self._place()

    def _cancel_place(self):
        if self._pending_place is not None:
            self.after_cancel(self._pending_place)
            self._pending_place = None

    def _watch_bounds(self):
        self._cancel_place()
        for widget, binding in self._watch_bindings:
            if widget.winfo_exists():
                widget.unbind("<Configure>", binding)
        self._watch_bindings = []
        for widget in {self.app, self.box.container, self.box.view, *self.box.bars} - {None}:
            binding = widget.bind("<Configure>", self._bounds_changed, "+")
            self._watch_bindings.append((widget, binding))

    def _bounds_changed(self, _event=None):
        if _event is not None and _event.widget not in {w for w, _b in self._watch_bindings}:
            return
        # Debounce a burst of main-window, canvas and outline Configure events.
        self._cancel_place()
        self._pending_place = self.after(30, self._place_if_moved)

    def _check_monitor(self):
        # Dock/taskbar or monitor changes need not send Tk a Configure event.
        # Poll bounds only; unchanged bounds never lay out or move the window.
        if self._pending_place is None:
            self._place_if_moved()
        self._monitor_poll = self.after(500, self._check_monitor)

    def _bounds(self):
        app = self.app
        return (app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height(),
                self.box.screen_rect(), work_area(app))

    def _place_if_moved(self):
        self._pending_place = None
        if self.winfo_exists() and self._bounds() != self._last_bounds:
            self._place()

    def _key_controls(self):
        if keys_exist(self.app.paths):
            ttk.Label(self.extra, text=f"✓ Your signing key is in {self.app.paths.keys}.", style="Tutorial.Ok.TLabel",
                      wraplength=620, justify="left").pack(anchor="w")
            return
        ttk.Button(self.extra, text="Create my signing key…", style="Accent.TButton",
                   command=self._create_key).pack(anchor="w")
        ttk.Label(self.extra, text="Next becomes available once the key exists.", style="Tutorial.Small.TLabel").pack(
            anchor="w", pady=(6, 0))

    def _appearance_controls(self):
        """First launch only: light/dark and the color scheme, applied and saved as they're chosen."""
        ui = self.app.settings["ui"]
        ttk.Label(self.extra, text="How it looks", style="Tutorial.Strong.TLabel").pack(anchor="w")
        modes = ttk.Frame(self.extra, style="Tutorial.TFrame")
        modes.pack(anchor="w", pady=(4, 0))
        self.mode_choice = tk.StringVar(value=ui["theme_mode"])
        for value, label in (("system", "Follow system"), ("light", "Light"), ("dark", "Dark")):
            ttk.Radiobutton(modes, text=label, value=value, variable=self.mode_choice,
                            style="Tutorial.TRadiobutton",
                            command=self._apply_appearance).pack(side="left", padx=(0, 12))
        schemes = {sid: scheme.get("label", sid) for sid, scheme in org_config.schemes(self.app.settings).items()
                   if not scheme.get("hidden") and sid != "custom"}
        self.scheme_ids = {label: sid for sid, label in schemes.items()}
        row = ttk.Frame(self.extra, style="Tutorial.TFrame")
        row.pack(anchor="w", pady=(6, 0))
        ttk.Label(row, text="Color scheme", style="Tutorial.TLabel").pack(side="left", padx=(0, 8))
        self.scheme_choice = ttk.Combobox(row, state="readonly", width=16, values=list(schemes.values()))
        self.scheme_choice.set(schemes.get(ui["scheme"], ui["scheme"]))
        self.scheme_choice.pack(side="left")
        self.scheme_choice.bind("<<ComboboxSelected>>", lambda _e: self._apply_appearance())
        ttk.Label(self.extra, text="Change these any time under Appearance….", style="Tutorial.Small.TLabel").pack(
            anchor="w", pady=(6, 0))

    def _apply_appearance(self):
        changes = {"theme_mode": self.mode_choice.get(),
                   "scheme": self.scheme_ids.get(self.scheme_choice.get(), self.app.settings["ui"]["scheme"])}
        self.app.settings["ui"] = {**self.app.settings["ui"], **changes}
        theme.apply(self.app, self.app.settings["ui"], org_config.schemes(self.app.settings))
        try:
            org_config.save_user_ui(self.app.paths, changes, getattr(self.app, "org_config_path", None))
        except org_config.ConfigError as exc:
            messagebox.showerror("Appearance", str(exc), parent=self)

    def _theme_changed(self, event):
        if event.widget is self:
            if self._pending_theme is not None:
                self.after_cancel(self._pending_theme)
            self._pending_theme = self.after_idle(self._refresh_theme)

    def _refresh_theme(self):
        self._pending_theme = None
        if not self.winfo_exists():
            return
        self.configure(bg=theme.PALETTE["surface"], highlightbackground=theme.HIGHLIGHT[theme.MODE],
                       highlightcolor=theme.HIGHLIGHT[theme.MODE])
        for bar in self.box.bars:
            bar.configure(bg=theme.HIGHLIGHT[theme.MODE])
        self._place()

    def _create_key(self):
        if self.app.create_signing_key(parent=self):
            self.show()

    def _point_at(self, target):
        """Open the tab (and part of Compose) this page describes, behind the tutorial, and outline it."""
        self.box.hide()
        notebook = getattr(self.app, "notebook", None)
        if notebook is None or target in (None, "key"):
            return
        try:
            if isinstance(target, tuple):
                compose = self.app.compose
                notebook.select(compose)
                self.app.update_idletasks()
                section = compose.sections[target[1]]
                compose.page.scroll_to(section)
                self.box.show(section.master, lambda: self._section_rect(compose, target[1]), compose.page.canvas)
            else:
                tab = getattr(self.app, target)
                notebook.select(tab)
                self.app.update_idletasks()
                self.box.show(tab, lambda: (0, 0, tab.winfo_width(), tab.winfo_height()))
        except (AttributeError, KeyError, tk.TclError):
            pass

    @staticmethod
    def _section_rect(compose, name) -> tuple[int, int, int, int]:
        """From the middle of the gap above this section's heading to the middle of the gap above the next one."""
        names = list(compose.sections)
        section = compose.sections[name]
        body = section.master
        top = max(section.winfo_y() - 11, 0)
        later = names[names.index(name) + 1:]
        bottom = compose.sections[later[0]].winfo_y() - 11 if later else body.winfo_height() - 4
        return 4, top, body.winfo_width() - 8, bottom - top

    def _set_arrow(self, side: str | None) -> None:
        self.arrow.pack_forget()
        self.frame.pack_forget()
        if side:
            self.arrow.configure(text=ARROWS[side], fg=theme.HIGHLIGHT[theme.MODE], bg=theme.PALETTE["surface"])
            self.arrow.pack(side=side, padx=(6, 0) if side == "left" else (0, 6) if side == "right" else 0)
        self.frame.pack(fill="both", expand=True)

    def _usable_area(self) -> tuple[int, int]:
        """Vertical bounds retained for callers; placement uses all four work edges."""
        _left, top, _right, bottom = work_area(self.app)
        return top, bottom

    @staticmethod
    def _packed_size(widget):
        """Measure packed content synchronously, without running Tk's redraw queue.

        Tk updates a label's requested size immediately, but propagates frame sizes
        at idle. Sum the pack requests here so page text and geometry commit together.
        """
        children = widget.pack_slaves()
        if not children or not widget.pack_propagate():
            return widget.winfo_reqwidth(), widget.winfo_reqheight()
        width = height = used_w = used_h = 0
        for child in children:
            w, h = Tutorial._packed_size(child)
            info = child.pack_info()
            def pad(key):
                values = info[key] if isinstance(info[key], tuple) else widget.tk.splitlist(str(info[key]))
                pixels = [widget.winfo_pixels(v) for v in values]
                return sum(pixels) if len(pixels) > 1 else 2 * pixels[0]
            w += pad("padx") + pad("ipadx")
            h += pad("pady") + pad("ipady")
            if info["side"] in ("left", "right"):
                width = max(width, used_w + w)
                height = max(height, used_h + h)
                used_w += w
            else:
                width = max(width, used_w + w)
                height = max(height, used_h + h)
                used_h += h
        padding = widget.tk.splitlist(widget.cget("padding")) if isinstance(widget, ttk.Frame) else ()
        if padding:
            p = [widget.winfo_pixels(v) for v in padding]
            p = (p * 4)[:4] if len(p) < 3 else p + p[1:2] if len(p) == 3 else p
            width += p[0] + p[2]
            height += p[1] + p[3]
        return width, height

    def _measure(self) -> dict:
        """Font metrics never change or flush the displayed arrow. Text can scroll."""
        font = tkfont.nametofont("LW.Title")
        # The label has its normal one-pixel horizontal and vertical padding.
        arrow_w = max(font.measure(glyph) for glyph in ARROWS.values()) + 2 + 6
        arrow_h = font.metrics("linespace") + 2
        width, height = self._packed_size(self.frame)
        text_font = tkfont.nametofont("LW.Tutorial")
        content = 0
        line_width = max(1, int(self.reading.cget("width")) - self.scrollbar.winfo_reqwidth())
        for paragraph in self.text.get("1.0", "end-1c").splitlines():
            lines, used = 1, 0
            for word in paragraph.split():
                size = text_font.measure(word + " ")
                if used and used + size > line_width:
                    lines, used = lines + 1, 0
                used += size
            content += lines * (text_font.metrics("linespace") + 4) + 18
        return {"frame_w": width + 2,
                "chrome_h": height - self.reading.winfo_reqheight() + 2,
                "content": max(100, content), "arrow_w": arrow_w, "arrow_h": arrow_h}

    @staticmethod
    def _size_for(m: dict, side: str | None, room_h: int) -> tuple[int, int, int] | None:
        """(width, height, reading height) with the arrow on `side`, at most `room_h` tall; None if even a short
        reading area won't fit."""
        extra_w = m["arrow_w"] if side in ("left", "right") else 0
        extra_h = m["arrow_h"] if side in ("top", "bottom") else 0
        reading = min(m["content"], room_h - m["chrome_h"] - extra_h)
        if reading < 100:
            return None
        height = m["chrome_h"] + reading + extra_h
        if side in ("left", "right"):
            height = max(height, m["arrow_h"])
        return m["frame_w"] + extra_w, height, reading

    def _place(self):
        """Beside the outlined section with an arrow pointing at it, the reading area shortened to fit the room on
        that side if needed; otherwise (no side has room) near the main window's bottom-right corner without an
        arrow (or centered, before the main window exists). Always inside the usable screen area. Worked out
        first, then laid out once: trying each side on the real window was drawn each time (a flicker)."""
        app, gap = self.app, 8
        target = self.box.screen_rect() if app.winfo_viewable() else None
        left, top, right, bottom = work_area(app)
        dl, dt, dr, db = frame_insets(self)
        bar = dt + db
        # Reserve horizontal frame borders too; all candidates use outer bounds.
        m = self._measure()
        max_width = right - left - dl - dr
        if m["frame_w"] > max_width:
            shrink = m["frame_w"] - max_width
            self.reading.configure(width=max(100, int(self.reading.cget("width")) - shrink))
            self.heading.configure(wraplength=max(100, max_width - 48))
            m = self._measure()
        m["frame_w"] += dl + dr
        choice = None
        if target:
            bx, by, bw, bh = target
            for side in ("left", "right", "top", "bottom"):  # the tutorial's side facing the outline
                if side in ("left", "right"):
                    room_h = bottom - top - bar
                else:
                    room_h = (bottom - (by + bh + gap) - bar) if side == "top" else (by - gap - top - bar)
                size = self._size_for(m, side, room_h)
                if size is None:
                    continue
                tw, h, reading = size
                th = h + bar  # outer height: a geometry position is the frame's top, title bar included
                if side in ("left", "right"):
                    x = bx + bw + gap if side == "left" else bx - gap - tw
                    y = min(max(by + (bh - th) // 2, top), bottom - th)
                    fits = left <= x and x + tw <= right
                else:
                    x = min(max(bx + bw - tw - 24, left), right - tw)
                    y = by + bh + gap if side == "top" else by - gap - th
                    fits = top <= y and y + th <= bottom and left <= x and x + tw <= right
                if fits:
                    choice = (side, x, y, reading)
                    break
        if choice is None:  # not beside the outline: no arrow pointing at nothing
            tw, h, reading = self._size_for(m, None, bottom - top - bar) or (m["frame_w"], 0, 100)
            th = m["chrome_h"] + reading + bar
            if app.winfo_viewable():
                x = app.winfo_rootx() + app.winfo_width() - tw - 24
                y = app.winfo_rooty() + app.winfo_height() - th - 70
            else:
                x, y = left + (right - left - tw) // 2, top + (bottom - top - th) // 3
            x = min(max(x, left), max(right - tw, left))
            y = min(max(y, top), max(bottom - th, top))
            choice = (None, x, y, reading)
        side, x, y, reading = choice
        self._set_arrow(side)
        self.reading.configure(height=reading)
        size = self._size_for(m, side, bottom - top - bar)
        width = size[0] - dl - dr
        height = m["chrome_h"] + reading + (m["arrow_h"] if side in ("top", "bottom") else 0)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.placed = side
        self._placed_for = target
        self._last_bounds = self._bounds()

    # --- navigation ------------------------------------------------------------------------

    def next(self):
        if str(self.next_button.cget("state")) == "disabled":
            return
        if self.index >= self.last_index():
            self.finished = True
            self.destroy()
            return
        self.index += 1
        self.show()

    def previous(self):
        if self.index > 0:
            self.index -= 1
            self.show()

    def destroy(self):
        self._cancel_place()
        for callback in (self._pending_theme, self._monitor_poll):
            if callback is not None:
                self.after_cancel(callback)
        for widget, binding in self._watch_bindings:
            if widget.winfo_exists():
                widget.unbind("<Configure>", binding)
        self._watch_bindings = []
        self.box.hide()
        super().destroy()

    def close(self):
        if self.phase == "intro" and not keys_exist(self.app.paths):
            if not messagebox.askyesno("Tutorial", "Skip the tutorial? You'll still be asked to create a signing key.",
                                       parent=self):
                return
        self.destroy()

    def run(self):
        """Modal: wait until it's closed. Returns True if the reader reached the end."""
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window()
        return self.finished
