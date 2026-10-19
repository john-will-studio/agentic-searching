"""Compose: the nomination queue and the warrant composer (new, edit, renew).

Decisions take effect when the warrant is signed: included terms go into it,
rejected terms are remembered and hidden. Until then everything can be undone.
"""

from __future__ import annotations

import sys
import tkinter as tk
from datetime import timedelta
from tkinter import ttk

from . import msg as messagebox

from ..dates import MAX_LOOKBACK_DAYS, DateLimitError, normalize_ranges
from ..dates import describe as describe_dates
from ..library import load_library
from ..review import clear_agent_note, held_rejections, read_agent_note, is_held, load_rejected, merge, read_inbox, reject, unique_id, unreject, warranted_terms
from ..util import from_iso, to_iso, utc_now
from ..warrant import (
    DEFAULT_MAX_ROWS,
    MAX_QUERIES_PER_HOUR,
    MAX_ROWS,
    MAX_TTL_DAYS,
    WarrantError,
    build_warrant,
    format_duration,
    normalize_client_id,
    replace_warrant,
    save_warrant,
)
from . import theme
from ..org_config import authority_hash, authority_values, config_context, field_mismatches
from ..scope import MAX_REASON, countries, include_rows
from .document import source_label, warrant_blocks
from .term_dialog import TermDialog, matching
from .widgets import (
    responsive_wrap,
    READ_WIDTH,
    bind_decision_keys,
    confirm_bulk,
    CheckChips,
    CountryDialog,
    DateRangeDialog,
    GrowTable,
    RejectDialog,
    ScrollableFrame,
    Table,
    askstring,
    click_to_copy,
    copy_text,
    format_wait,
    local_time,
    plural,
    scrolled,
    slugify,
)

PAGE_WIDTH = 1720  # the page grows with the window up to this, so tables use a wide screen
TEXT_WIDTH = 1180  # the wrap width explanatory labels are created with (see _fit_text)
STATUS = {"pending": "● Pending", "approved": "✓ Included", "rejected": "✗ Rejected"}
STATUS_EDIT = STATUS
P1_PLACEHOLDER = "Select…"


# The review keys, shown on the buttons as they appear on the keyboard (lowercase works too).
DECISION_KEYS = (("A", "approved"), ("R", "rejected"), ("P", "pending"))


class ComposeTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.settings = app.settings
        self.terms: list[dict] = []
        self.hidden_rows: dict[str, dict] = {}
        self.agent_purpose = ""
        self.editing: str | None = None
        self.blocked: list[dict] = []

        # Sticky footer: what will be signed, always visible.
        footer = ttk.Frame(self, padding=(16, 10))
        footer.pack(side="bottom", fill="x")
        ttk.Separator(self).pack(side="bottom", fill="x")
        self.sign_button = ttk.Button(footer, text="Review and sign…", style="Accent.TButton", command=self.sign)
        self.sign_button.pack(side="right")
        self.footer_text = ttk.Label(footer, justify="left", wraplength=TEXT_WIDTH)
        self.footer_text.pack(side="left", fill="x", expand=True)

        self.page = ScrollableFrame(self, max_width=PAGE_WIDTH)
        self.page.pack(fill="both", expand=True)
        body = ttk.Frame(self.page.body, padding=(16, 8, 16, 16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        # Edit banner
        self.edit_banner = ttk.Frame(body)
        self.edit_label = ttk.Label(self.edit_banner, style="Warn.TLabel", font="LW.Heading")
        self.edit_label.pack(side="left")
        ttk.Button(self.edit_banner, text="Cancel edit", command=self.cancel_edit).pack(side="left", padx=12)
        self.edit_note = ttk.Label(self.edit_banner, style="Muted.TLabel")
        self.edit_note.pack(side="left")

        # 1. Hit terms --------------------------------------------------------------
        header = ttk.Frame(body)
        header.grid(row=1, column=0, sticky="ew", pady=(8, 2))
        # A message from the agent (`propose --note`): a request or a report that isn't a term or the purpose.
        p = theme.PALETTE
        self.agent_box = tk.Frame(header, bg=p["agent_bg"], highlightthickness=1, highlightbackground=p["agent_border"])
        inner = tk.Frame(self.agent_box, bg=p["agent_bg"])
        inner.pack(fill="x", padx=10, pady=8)
        top = tk.Frame(inner, bg=p["agent_bg"])
        top.pack(fill="x")
        self.agent_heading = tk.Label(top, bg=p["agent_bg"], fg=p["text"], font="LW.Strong", anchor="w")
        self.agent_heading.pack(side="left")
        ttk.Button(top, text="Dismiss", command=self.dismiss_agent_message).pack(side="right")
        self.agent_text = tk.Label(inner, bg=p["agent_bg"], fg=p["text"], font="LW.Body", justify="left", anchor="w",
                                   wraplength=TEXT_WIDTH - 40)
        self.agent_text.pack(fill="x", pady=(4, 0))
        self._agent_message = None
        # Shown when a newer signed organization configuration is in force than the one this draft started with.
        self.config_banner = tk.Frame(header, bg=p["warn_bg"], highlightthickness=1, highlightbackground=p["warn"])
        ttk.Button(self.config_banner, text="Start new draft", command=lambda: self.app.adopt_new_config()).pack(
            side="right", padx=10, pady=8)
        tk.Label(self.config_banner, bg=p["warn_bg"], fg=p["text"], font="LW.Body", justify="left", anchor="w",
                 wraplength=TEXT_WIDTH - 200,
                 text="The organization configuration changed (new defaults, limits or wording may apply). This draft "
                      "was started under the previous one and can't be signed. Start a new draft to use the "
                      "configuration in force; pending nominations stay in the inbox.").pack(
            side="left", fill="x", padx=10, pady=8)
        self.config_outdated = False
        head = ttk.Frame(header)
        head.pack(fill="x")
        self._head = head
        ttk.Label(head, text=self.settings["terms"]["hit_term"]["plural"], style="Heading.TLabel").pack(side="left")
        self.sections = {"terms": header}  # where each part of the page starts (the tutorial scrolls to them)
        self.counts = ttk.Label(head, style="Muted.TLabel")
        self.counts.pack(side="left", padx=12)
        self.show_hidden = tk.BooleanVar(value=False)
        ttk.Checkbutton(head, text="Show hidden", variable=self.show_hidden, command=self.render_terms).pack(side="right")

        copy_key = "Cmd+C" if sys.platform == "darwin" else "Ctrl+C"
        ttk.Label(header, text=f"Click a cell to copy it · {copy_key} copies the selected rows", style="Small.TLabel").pack(
            anchor="w")  # its own line, so it never competes with the heading row for width
        notes = ttk.Frame(header)  # second line, so live notes never crowd out the heading row
        notes.pack(fill="x")
        self.inbox_note = ttk.Label(notes, style="Ok.TLabel")
        self.inbox_note.pack(side="left")
        self.problem_note = ttk.Label(notes, style="Warn.TLabel", cursor="hand2")
        self.problem_note.pack(side="left", padx=12)
        self.problem_note.bind("<Button-1>", lambda _e: self.show_inbox_problems())
        self.inbox_problems: list[str] = []

        self.table = Table(body, [
            ("status", "Status", 2, 150, "w"), ("literal", self.settings["terms"]["hit_term"]["singular"], 3, 160, "w"),
            ("client_id", self.settings["terms"]["client_id"]["singular"], 1.4, 110, "w"), ("source", "Source", 1.2, 90, "w"),
            ("provenance", "Provenance / reason", 5, 200, "w"), ("term_id", "Term ID", 2, 120, "w"),
            ("matching", "Matching", 3, 170, "w"),
        ], height=8, selectmode="extended", fit_content={"status": None, "literal": 320, "client_id": 200, "source": None,
                                                         "term_id": None, "provenance": 520, "matching": 380})
        self.table.grid(row=2, column=0, sticky="nsew")
        self.tree = self.table.tree
        self.tree.configure(displaycolumns=("status", "literal", "matching", "client_id", "source", "provenance"))
        p = theme.PALETTE
        self.tree.tag_configure("approved", foreground=p["ok"])
        self.tree.tag_configure("pending", foreground=p["warn"])
        self.tree.tag_configure("rejected", foreground=p["muted"])
        self.tree.tag_configure("hidden", foreground=p["muted"])
        # One decision per physical press (a held key doesn't run down the list), with or without Shift / Caps Lock.
        bind_decision_keys(self.tree, {key: (lambda d=decision: self.decide(d)) for key, decision in DECISION_KEYS})
        click_to_copy(self.tree, self.app.notify, skip_columns=("status",))
        for sequence in ("<Control-c>", "<Command-c>"):
            try:
                self.tree.bind(sequence, lambda _e: self.copy_selected())
            except tk.TclError:  # <Command-…> only exists on macOS
                pass

        actions = ttk.Frame(body)
        actions.grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Button(actions, text="✓ Include in warrant  (A)", command=lambda: self.decide("approved")).pack(side="left")
        ttk.Button(actions, text=f"✗ Reject {self.settings['terms']['hit_term']['singular'].lower()}  (R)", command=lambda: self.decide("rejected")).pack(side="left", padx=6)
        ttk.Button(actions, text="Return to pending  (P)", command=lambda: self.decide("pending")).pack(side="left")
        ttk.Button(actions, text=f"Add {self.settings['terms']['hit_term']['singular'].lower()}…", command=self.add_term).pack(side="left", padx=(24, 6))
        ttk.Button(actions, text=f"Edit {self.settings['terms']['hit_term']['singular'].lower()}…", command=self.edit_literal).pack(side="left")

        ttk.Button(actions, text="Details…", command=self.term_details).pack(side="left", padx=6)

        # 2. Authorization ----------------------------------------------------------
        self.sections["authorization"] = ttk.Label(
            body, text=f"{self.settings['parameters']['parameter_1']['label']} and "
                       f"{self.settings['parameters']['parameter_2']['label'].lower()}", style="Heading.TLabel")
        self.sections["authorization"].grid(row=4, column=0, sticky="w", pady=(22, 6))
        auth = ttk.Frame(body)
        auth.grid(row=5, column=0, sticky="ew")
        params = self.settings["parameters"]
        ttk.Label(auth, text=f"{params['parameter_1']['label']} (parameter 1)").grid(row=0, column=0, sticky="w", pady=4)
        self.p1_display = tk.StringVar(value=P1_PLACEHOLDER)
        self.p1_box = ttk.Combobox(auth, textvariable=self.p1_display, state="readonly", width=44)
        self.p1_box.grid(row=0, column=1, sticky="w", padx=12)
        self.p1_box.bind("<<ComboboxSelected>>", lambda _e: self.parameter_1_changed())
        ttk.Label(auth, text=f"{params['parameter_2']['label']} (parameter 2)").grid(row=1, column=0, sticky="nw", pady=(10, 4))
        self.chips = CheckChips(auth, on_change=self._p2_changed)
        self.chips.grid(row=1, column=1, sticky="w", padx=12, pady=(8, 0))
        self.p2_hint = ttk.Label(auth, style="Muted.TLabel", wraplength=TEXT_WIDTH - 200, justify="left")
        self.p2_hint.grid(row=2, column=1, sticky="w", padx=12, pady=(2, 0))

        # 3. Purpose ----------------------------------------------------------------
        self.sections["purpose"] = ttk.Label(body, text="Purpose", style="Heading.TLabel")
        self.sections["purpose"].grid(row=6, column=0, sticky="w", pady=(22, 2))
        ttk.Label(body, text="The agent's brief: it directs the agent's work and is sent with every search. Review it for "
                  "compliance and intent; it isn't checked automatically (the terms, templates, authorization, "
                  "geographic constraint and limits are).", style="Muted.TLabel", wraplength=TEXT_WIDTH, justify="left").grid(
            row=7, column=0, sticky="w")
        notes = ttk.Frame(body)
        notes.grid(row=8, column=0, sticky="ew", pady=(4, 2))
        self.agent_note = ttk.Label(notes, style="Warn.TLabel")
        self.agent_note.pack(anchor="w")
        box, self.purpose = scrolled(body, tk.Text, height=6, wrap="word", font="LW.Body")
        box.grid(row=9, column=0, sticky="ew")
        self.purpose.bind("<<Modified>>", self._purpose_modified)
        self.cert_purpose = tk.BooleanVar(value=False)
        self.cert_purpose.trace_add("write", lambda *_a: self._show_purpose_origin())
        self._certification(body, 10, self.cert_purpose, self.settings["certification"]["purpose"])

        # 4. Geographic constraint ---------------------------------------------------
        self.sections["geography"] = ttk.Label(body, text="Geographic constraint", style="Heading.TLabel")
        self.sections["geography"].grid(row=11, column=0, sticky="w", pady=(22, 2))
        ttk.Label(body, text="Records carry a two-letter country code. Limit searches to the countries you're responsible "
                  "for, and exclude countries that must not be searched.", style="Muted.TLabel", wraplength=TEXT_WIDTH,
                  justify="left").grid(row=12, column=0, sticky="w", pady=(0, 6))
        scope = ttk.Frame(body)
        scope.grid(row=13, column=0, sticky="ew")
        # Labels sit above their tables, so the tables (and long exclusion reasons) get the full page width.
        ttk.Label(scope, text="Included countries", style="Strong.TLabel").grid(row=0, column=0, sticky="w", pady=(4, 2))
        include = ttk.Frame(scope)
        include.grid(row=1, column=0, sticky="w")
        self.country_mode = tk.StringVar(value="all")
        ttk.Radiobutton(include, text="All countries, except the exclusions below", value="all",
                        variable=self.country_mode, command=self._country_mode_changed).pack(anchor="w")
        ttk.Radiobutton(include, text="Only these countries:", value="only", variable=self.country_mode,
                        command=self._country_mode_changed).pack(anchor="w", pady=(2, 4))
        self.include_rows: list[dict] = []  # {"code", "reason"}; the reason is optional
        self.include_table = GrowTable(include, [("code", "Code", 56), ("country", "Country", 150),
                                                 ("reason", self.settings["country_scope"]["reason_label"], 220)],
                                       placeholder="No countries chosen", on_activate=lambda idx: self.edit_inclusion())
        self.include_table.pack(anchor="w")
        row = ttk.Frame(include)
        row.pack(anchor="w", pady=(4, 0))
        self.include_add = ttk.Button(row, text="Add country…", command=self.add_included_country)
        self.include_add.pack(side="left")
        self.include_edit = ttk.Button(row, text="Edit reason…", command=self.edit_inclusion)
        self.include_edit.pack(side="left", padx=(6, 0))
        self.include_remove = ttk.Button(row, text="Remove", command=self.remove_included_country)
        self.include_remove.pack(side="left", padx=6)

        ttk.Label(scope, text="Excluded countries", style="Strong.TLabel").grid(row=2, column=0, sticky="w", pady=(14, 2))
        exclude = ttk.Frame(scope)
        exclude.grid(row=3, column=0, sticky="w")
        self.exclude_rows: list[dict] = []
        self.exclude_table = GrowTable(exclude, [("code", "Code", 56), ("country", "Country", 150),
                                                 ("reason", self.settings["country_scope"]["reason_label"], 220)],
                                       placeholder="None excluded", on_activate=lambda idx: self.edit_exclusion())
        self.exclude_table.pack(anchor="w")
        row = ttk.Frame(exclude)
        row.pack(anchor="w", pady=(4, 0))
        ttk.Button(row, text="Add exclusion…", command=self.add_exclusion).pack(side="left")
        ttk.Button(row, text="Edit reason…", command=self.edit_exclusion).pack(side="left", padx=6)
        ttk.Button(row, text="Remove", command=self.remove_exclusion).pack(side="left")
        ttk.Label(exclude, text="Excluded countries are never searched, whatever else is chosen. The starting list "
                                "comes from your organization's configuration: it's a default to check for this case, "
                                "not a legal determination.", style="Small.TLabel", wraplength=900,
                  justify="left").pack(anchor="w", pady=(4, 0))
        from .places_panel import PlacesPanel

        self.places_panel = PlacesPanel(scope, self)
        self.places_panel.grid(row=4, column=0, sticky="ew")

        # 5. Limits ----------------------------------------------------------------
        self.sections["limits"] = ttk.Label(body, text="Limits", style="Heading.TLabel")
        self.sections["limits"].grid(row=14, column=0, sticky="w", pady=(22, 6))
        limits = ttk.Frame(body)
        limits.grid(row=15, column=0, sticky="ew")
        ttk.Label(limits, text="Expires in").grid(row=0, column=0, sticky="w", pady=4)
        expiry = ttk.Frame(limits)
        expiry.grid(row=0, column=1, sticky="w", padx=12)
        self.ttl_days, self.ttl_hours, self.ttl_minutes = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for var, top, unit, step in ((self.ttl_days, MAX_TTL_DAYS, "days", 1), (self.ttl_hours, 23, "hours", 1),
                                     (self.ttl_minutes, 59, "min", 5)):
            ttk.Spinbox(expiry, from_=0, to=top, increment=step, textvariable=var, width=4).pack(side="left", padx=(0, 4))
            ttk.Label(expiry, text=unit).pack(side="left", padx=(0, 12))
        self.expiry_preview = ttk.Label(expiry, style="Muted.TLabel")
        self.expiry_preview.pack(side="left")

        ttk.Label(limits, text="Max records per search").grid(row=1, column=0, sticky="w", pady=4)
        rows = ttk.Frame(limits)
        rows.grid(row=1, column=1, sticky="w", padx=12)
        self.rows = tk.StringVar()
        ttk.Spinbox(rows, from_=1, to=MAX_ROWS, increment=1000, textvariable=self.rows, width=10).pack(side="left")
        ttk.Label(rows, text=f"1 to {MAX_ROWS:,} · per search, not a total across searches", style="Muted.TLabel").pack(side="left", padx=10)

        ttk.Label(limits, text="Max searches per hour").grid(row=2, column=0, sticky="w", pady=4)
        rate = ttk.Frame(limits)
        rate.grid(row=2, column=1, sticky="w", padx=12)
        self.rate = tk.StringVar()
        ttk.Spinbox(rate, from_=1, to=MAX_QUERIES_PER_HOUR, increment=5, textvariable=self.rate, width=10).pack(side="left")
        ttk.Label(rate, text=f"1 to {MAX_QUERIES_PER_HOUR:,} · searches the agent may send in any rolling hour",
                  style="Muted.TLabel").pack(side="left", padx=10)
        # What the two limits allow together, so a per-search cap isn't mistaken for a total.
        self.limits_effect = ttk.Label(limits, style="Strong.TLabel")
        self.limits_effect.grid(row=2, column=2, sticky="w")

        ttk.Label(limits, text="Search window").grid(row=3, column=0, sticky="nw", pady=4)
        window = ttk.Frame(limits)
        window.grid(row=3, column=1, sticky="w", padx=12)
        self.window_mode = tk.StringVar(value="limited")
        self.lookback = tk.StringVar()
        first = ttk.Frame(window)
        first.pack(anchor="w")
        ttk.Radiobutton(first, text="Last", value="limited", variable=self.window_mode).pack(side="left")
        ttk.Spinbox(first, from_=1, to=MAX_LOOKBACK_DAYS, increment=30, textvariable=self.lookback, width=7).pack(side="left", padx=6)
        ttk.Label(first, text="days, up to the time of each search").pack(side="left")
        ttk.Radiobutton(window, text="No date restriction", value="none", variable=self.window_mode).pack(anchor="w", pady=(2, 0))

        ttk.Label(limits, text="Blocked date ranges").grid(row=4, column=0, sticky="nw", pady=(10, 4))
        blocked = ttk.Frame(limits)
        blocked.grid(row=4, column=1, sticky="w", padx=12, pady=(10, 0))
        self.blocked: list[dict] = []
        self.blocked_table = GrowTable(blocked, [("from", "From (UTC)", 150), ("to", "To (UTC, inclusive)", 170)],
                                       placeholder="No blocked dates")
        self.blocked_table.pack(anchor="w")
        row = ttk.Frame(blocked)
        row.pack(anchor="w", pady=(4, 0))
        self.add_range_button = ttk.Button(row, text="Add range…", command=self.add_blocked_range)
        self.add_range_button.pack(side="left")
        ttk.Button(row, text="Remove", command=self.remove_blocked_range).pack(side="left", padx=6)
        ttk.Label(blocked, text="No search may return records from these dates.", style="Small.TLabel").pack(anchor="w", pady=(4, 0))

        self.cert_limits = tk.BooleanVar(value=False)
        self._certification(body, 16, self.cert_limits, self.settings["certification"]["limits"])

        for var in (self.p1_display, self.cert_purpose, self.cert_limits):
            var.trace_add("write", lambda *_: self.update_footer())
        for var in (self.ttl_days, self.ttl_hours, self.ttl_minutes, self.rows, self.rate, self.lookback, self.window_mode):
            var.trace_add("write", lambda *_: self._limits_changed())
        responsive_wrap(self, [self.footer_text], padding=256)

        self.load_rbac_options()
        self._apply_defaults()
        self.merge_inbox()
        self.page.guard_value_widgets()
        self._fit_text(body)

    # --- authorization ----------------------------------------------------------------

    def load_rbac_options(self):
        try:
            self.p1_options = {o.value: o for o in self.app.rbac.parameter_1_options()}
        except (OSError, KeyError, ValueError) as exc:
            messagebox.showerror("Entitlements", f"Could not load entitlements: {exc}", parent=self)
            self.p1_options = {}
        self.p1_labels = {f"{v} — {o.label}" if o.label != v else v: v for v, o in self.p1_options.items()}
        self.p1_box.configure(values=list(self.p1_labels))
        if self.p1 not in self.p1_options:
            self.p1_display.set(P1_PLACEHOLDER)
        self.parameter_1_changed()

    def refresh_rbac(self):
        """Re-read entitlements, keeping choices that are still offered and saying what was dropped."""
        before_p1, before_p2 = self.p1, self.chips.values()
        self.p1_options = {o.value: o for o in self.app.rbac.parameter_1_options()}
        self.p1_labels = {f"{v} — {o.label}" if o.label != v else v: v for v, o in self.p1_options.items()}
        self.p1_box.configure(values=list(self.p1_labels))
        if before_p1 and before_p1 not in self.p1_options:
            self.p1_display.set(P1_PLACEHOLDER)
            self.parameter_1_changed(selected=[])
            self.app.notify(f"Search authorization {before_p1} is no longer available to you; choose again.")
            return
        if before_p1:
            self.set_p1(before_p1)
            self.parameter_1_changed(selected=before_p2)
            dropped = sorted(set(before_p2) - set(self.chips.values()))
            if dropped:
                self.app.notify(f"No longer entitled to: {', '.join(dropped)}")

    @property
    def p1(self) -> str:
        return self.p1_labels.get(self.p1_display.get(), "")

    def set_p1(self, value: str):
        label = next((k for k, v in self.p1_labels.items() if v == value), None)
        self.p1_display.set(label or P1_PLACEHOLDER)

    def parameter_1_changed(self, selected=None):
        if not self.p1:
            self.chips.set_options([], selected=[])
            self.p2_hint.configure(text="Choose a search authorization to see the visibility attributes you're entitled to.")
            return
        self.p2_options = {o.value: o for o in self.app.rbac.parameter_2_options(self.p1)}
        self.chips.set_options(list(self.p2_options.values()), selected=selected)

    def _p2_changed(self):
        if self.p1:
            self.p2_hint.configure(text="" if self.chips.values() else "Select one or more.")
        self.update_footer()

    # --- certifications ----------------------------------------------------------------

    def _certification(self, parent, row, var, text):
        """A required certification: a checkbox with its full statement beside it."""
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        box = ttk.Checkbutton(frame, variable=var)
        box.pack(side="left", anchor="n", pady=(2, 0))
        label = ttk.Label(frame, text=text, justify="left", wraplength=880, cursor="hand2")
        label.pack(side="left", fill="x", expand=True)
        label.bind("<Button-1>", lambda _e: var.set(not var.get()))
        responsive_wrap(frame, [label], padding=36)

    def _uncertify(self, which: str):
        var = self.cert_purpose if which == "purpose" else self.cert_limits
        if getattr(self, "_loading", False) or not var.get():
            return
        var.set(False)
        self.app.notify("Changed after certifying: please review and certify again.")

    def _limits_changed(self):
        self._uncertify("limits")
        self.update_footer()

    # --- defaults ------------------------------------------------------------------------

    def _apply_defaults(self):
        """Fill limits and scope from the organization's defaults (settings)."""
        d, scope = self.settings["defaults"], self.settings["country_scope"]
        self._loading = True
        try:
            days, rest = divmod(d["expiry_minutes"], 1440)
            self.ttl_days.set(str(days))
            self.ttl_hours.set(str(rest // 60))
            self.ttl_minutes.set(str(rest % 60))
            self.rows.set(str(d["max_rows_per_run"]))
            self.rate.set(str(d["max_queries_per_hour"]))
            self.window_mode.set("limited" if d["lookback_days"] else "none")
            self.lookback.set(str(d["lookback_days"] or 30))
            self.blocked = [dict(r) for r in d["blocked_ranges"]]
            self.include_rows = include_rows(scope["include"]) if scope["mode"] == "include" else []
            self.country_mode.set("only" if scope["mode"] == "include" else "all")
            self.exclude_rows = [dict(r) for r in scope["exclude"]]
            self.cert_purpose.set(False)
            self.cert_limits.set(False)
        finally:
            self._loading = False
        self._render_blocked()
        self._render_scope()

    # --- geographic constraint -------------------------------------------------------------

    def _fit_text(self, body) -> None:
        """Explanatory labels wrap to the page as it is: never wider than the page (at 1024 px they were cut off)
        and never wider than READ_WIDTH (on a wide screen long lines are hard to read)."""
        def visit(widget):
            for child in widget.winfo_children():
                if isinstance(child, (ttk.Label, tk.Label)) and int(str(child.cget("wraplength")) or 0) > 0:
                    responsive_wrap(widget, [child], padding=36)
                visit(child)

        visit(body)

    def config_changed(self, outdated: bool) -> None:
        """A newer signed configuration is in force: this draft can't be signed under the old one (see sign())."""
        self.config_outdated = outdated
        if outdated:
            self.config_banner.pack(fill="x", pady=(0, 10), before=self._head)
        else:
            self.config_banner.pack_forget()
        self.update_footer()

    def current_scope(self) -> dict:
        """The country constraint as it stands in the form (what a place's country is checked against)."""
        return {"include": self.include_rows if self.country_mode.get() == "only" else [], "exclude": self.exclude_rows}

    def _country_name(self, code):
        return countries(self.app.paths).get(code, "unknown code")

    def _render_scope(self):
        only = self.country_mode.get() == "only"
        self.include_table.set_rows([[r["code"], self._country_name(r["code"]), r["reason"] or "—"]
                                     for r in self.include_rows] if only else [])
        self.include_table.placeholder = "No countries chosen" if only else "All countries"
        if not only:
            self.include_table.set_rows([])
        for button in (self.include_add, self.include_edit, self.include_remove):
            button.configure(state="normal" if only else "disabled")
        self.exclude_table.set_rows([[r["code"], self._country_name(r["code"]), r["reason"]] for r in self.exclude_rows])
        if hasattr(self, "places_panel"):
            self.places_panel.recheck()  # a place whose country is no longer permitted can't stay approved
        self._limits_changed()

    def _country_mode_changed(self):
        self._render_scope()

    def _taken_codes(self) -> set[str]:
        return {r["code"] for r in self.include_rows} | {r["code"] for r in self.exclude_rows}

    def add_included_country(self):
        result = CountryDialog.ask(self, countries(self.app.paths), "Include a country",
                                   reason="", reason_label=f"{self.settings['country_scope']['reason_label']} (optional, "
                                                           "for example \u201cHome country of client CL-100482\u201d)",
                                   reason_required=False, taken=self._taken_codes())
        if result:
            self.include_rows = sorted([*self.include_rows, {"code": result["code"], "reason": result.get("reason", "")}],
                                       key=lambda r: r["code"])
            self._render_scope()

    def edit_inclusion(self):
        indexes = self.include_table.selected_indexes()
        if len(indexes) != 1:
            messagebox.showinfo("Edit reason", "Select one included country.", parent=self)
            return
        self._edit_reason(self.include_rows[indexes[0]], "including", required=False)

    def remove_included_country(self):
        for index in reversed(self.include_table.selected_indexes()):
            del self.include_rows[index]
        self._render_scope()

    def add_exclusion(self):
        result = CountryDialog.ask(self, countries(self.app.paths), "Exclude a country",
                                   reason=self.settings["country_scope"]["default_reason"],
                                   reason_label=self.settings["country_scope"]["reason_label"],
                                   taken=self._taken_codes())
        if result:
            self.exclude_rows = sorted([*self.exclude_rows, result], key=lambda r: r["code"])
            self._render_scope()

    def edit_exclusion(self):
        indexes = self.exclude_table.selected_indexes()
        if len(indexes) != 1:
            messagebox.showinfo("Edit reason", "Select one excluded country.", parent=self)
            return
        self._edit_reason(self.exclude_rows[indexes[0]], "excluding", required=True)

    def _edit_reason(self, row: dict, verb: str, *, required: bool) -> None:
        reason = askstring("Edit reason", f"{self.settings['country_scope']['reason_label']} for {verb} "
                                        f"{row['code']} ({self._country_name(row['code'])})"
                                        + ("" if required else " (optional)") + ":",
                                        initialvalue=row["reason"], parent=self)
        if reason is None:
            return
        reason = " ".join(reason.split())
        if not reason and required:
            messagebox.showerror("Edit reason", "Give a reason.", parent=self)
            return
        if len(reason) > MAX_REASON:
            messagebox.showerror("Edit reason", f"Keep the reason to {MAX_REASON} characters or fewer.", parent=self)
            return
        row["reason"] = reason
        self._render_scope()

    def remove_exclusion(self):
        for index in reversed(self.exclude_table.selected_indexes()):
            del self.exclude_rows[index]
        self._render_scope()

    # --- date limits ----------------------------------------------------------------

    def add_blocked_range(self):
        result = DateRangeDialog.ask(self, anchor=self.add_range_button)
        if result is None:
            return
        try:
            self.blocked = normalize_ranges([*self.blocked, result])
        except DateLimitError as exc:
            messagebox.showerror("Blocked date range", str(exc), parent=self)
            return
        self._render_blocked()

    def remove_blocked_range(self):
        for index in reversed(self.blocked_table.selected_indexes()):
            del self.blocked[index]
        self._render_blocked()

    def _render_blocked(self):
        self.blocked_table.set_rows([[r["start"], r["end"]] for r in self.blocked])
        self._limits_changed()

    # --- queue --------------------------------------------------------------------------

    def merge_inbox(self):
        candidates, purpose, problems = read_inbox(self.app.paths)
        self._show_agent_message(read_agent_note(self.app.paths))
        # Exact approval of PROJECT* does not cover a proposal to use PROJECT* as a pattern (or vice versa).
        held = set(held_rejections(self.app.paths))
        added = []
        for mode in ("exact", "pattern"):
            covered = warranted_terms(self.app.paths, self.app.public_key, match=mode)
            added += merge(self.terms, [t for t in candidates if t.get("match", "exact") == mode],
                           held | set(covered), load_rejected(self.app.paths))
        places_added = self.places_panel.merge_proposal(self.app.paths)
        current = self.purpose.get("1.0", "end").strip()
        if purpose and purpose != self.agent_purpose and current in ("", self.agent_purpose) and not self.editing:
            self._set_purpose(purpose, agent=True)
            self._uncertify("purpose")  # the text the certification covered just changed
        if purpose:
            self.agent_purpose = purpose
        keep = self.tree.selection()
        if added or places_added:
            what = [plural(len(added), "new nomination")] if added else []
            what += [plural(places_added, "new place")] if places_added else []
            # Stays until the analyst acts on the review tables (a timed note was easy to miss).
            self.inbox_note.configure(text=" and ".join(what) + " arrived: review them below")
        self.inbox_problems = problems
        self.problem_note.configure(
            text=f"⚠ {plural(len(problems), 'inbox entry', 'inbox entries')} skipped (details)" if problems else "")
        self.render_terms(keep)

    def inbox_changed(self):
        self.merge_inbox()

    def _show_agent_message(self, message):
        if message == self._agent_message:
            return
        self._agent_message = message
        if not message:
            self.agent_box.pack_forget()
            return
        when = f" · received {local_time(message['at'])}" if message.get("at") else ""
        self.agent_heading.configure(text=f"Message from the agent{when}")
        self.agent_text.configure(text=message["text"])
        self.agent_box.pack(fill="x", pady=(0, 10), before=self._head)

    def dismiss_agent_message(self):
        """Clear the agent's note from the inbox once read (the agent can send a new one)."""
        clear_agent_note(self.app.paths)
        self._show_agent_message(None)

    def show_inbox_problems(self):
        if self.inbox_problems:
            messagebox.showwarning("Inbox entries skipped",
                                   "These nominations couldn't be used. Fix them in the inbox files and they'll be picked "
                                   "up automatically:\n\n" + "\n".join(self.inbox_problems), parent=self)

    def _set_purpose(self, text: str, agent: bool = False, note: str = ""):
        self.purpose.delete("1.0", "end")
        self.purpose.insert("1.0", text)
        self.purpose.edit_modified(False)
        self.agent_note.configure(text="Agent's draft: review and edit before signing." if agent else note)

    def purpose_origin(self) -> str:
        """Where the purpose text came from: "agent" (the agent's draft as proposed), "agent-edited" (still largely
        the agent's draft), or "analyst" (written, or substantially rewritten, by the analyst). Small edits don't
        turn an agent's draft into the analyst's own words."""
        import difflib

        text = self.purpose.get("1.0", "end").strip()
        if not self.agent_purpose or not text:
            return "analyst"
        if text == self.agent_purpose:
            return "agent"
        similar = difflib.SequenceMatcher(None, self.agent_purpose, text).ratio()
        return "agent-edited" if similar >= 0.5 else "analyst"

    def _show_purpose_origin(self):
        origin = self.purpose_origin()
        certified = self.cert_purpose.get()
        text = {"agent": "Agent's draft: review and edit before signing.",
                "agent-edited": "Agent's draft, edited by you: still largely the agent's words. Review it as a whole."}
        if origin in text and certified:
            text = {k: "Agent's draft" + (", edited and" if k == "agent-edited" else "") + " reviewed and certified by you."
                    for k in text}
        self.agent_note.configure(text=text.get(origin, ""))

    def _purpose_modified(self, _event=None):
        if self.purpose.edit_modified():
            self.purpose.edit_modified(False)
            self._uncertify("purpose")
            self._show_purpose_origin()
            self.update_footer()

    def render_terms(self, select=()):
        self.table.clear()
        for i, t in enumerate(self.terms):
            labels = STATUS_EDIT if t.get("from_warrant") else STATUS
            status = labels[t["decision"]]
            if t["decision"] == "rejected" and "reassess_minutes" in t:
                status += f" · renominate: {format_wait(t['reassess_minutes'])}"
            provenance = t["provenance"]
            if t["decision"] == "pending" and t.get("rejected_before"):
                status += " (rejected before)"
                before = t["rejected_before"]
                provenance += f"  [rejected {local_time(before['at'])}" + (f": {before['reason']}]" if before["reason"] else "]")
            self.tree.insert("", "end", iid=f"q{i}", tags=(t["decision"],),
                             values=(status, t["literal"], t.get("client_id") or "—",
                                     source_label(t["source"], self.settings["source_labels"]), provenance, t["term_id"], matching(t)))
        rejected = load_rejected(self.app.paths)
        warranted = warranted_terms(self.app.paths, self.app.public_key)
        in_queue = {t["literal"] for t in self.terms}
        hidden_w = [lit for lit in warranted if lit not in in_queue]
        hidden_r = [lit for lit in rejected if lit not in in_queue]
        self.hidden_rows = {}
        if self.show_hidden.get():
            for literal in sorted(hidden_w):
                info = warranted[literal]
                t = info["term"]
                iid = _hidden_iid("w", literal)
                self.hidden_rows[iid] = {"kind": "warranted", "term": t}
                self.tree.insert("", "end", iid=iid, tags=("hidden",), values=(
                    f"In {info['warrant_id']}", literal, t.get("client_id") or "—", t["source"], t.get("provenance", ""),
                    t["term_id"], matching(t)))
            for literal in sorted(hidden_r):
                e = rejected[literal]
                iid = _hidden_iid("r", literal)
                self.hidden_rows[iid] = {"kind": "rejected", "term": e}
                until = ("never" if not e.get("renominate_after") else
                         f"after {local_time(e['renominate_after'])}" if is_held(e) else "now")
                self.tree.insert("", "end", iid=iid, tags=("hidden",), values=(
                    f"✗ Rejected earlier · renominate {until}", literal, e.get("client_id") or "—", e.get("source", ""), e.get("provenance", ""),
                    e.get("term_id", ""), matching(e)))
        self.table.set_empty(
            "No terms awaiting review."
            + (f"\n{plural(len(hidden_w), 'term')} covered by active warrants · {len(hidden_r)} rejected earlier."
               if hidden_w or hidden_r else "")
            + ("\nTick Show hidden to see them." if hidden_w or hidden_r else "\nNew nominations appear here automatically.")
        )
        select = [s for s in select if self.tree.exists(s)]
        if select:
            self.tree.selection_set(select)
        n = {d: sum(t["decision"] == d for t in self.terms) for d in STATUS}
        self.counts.configure(text=f"{n['approved']} included · {n['rejected']} rejected · {n['pending']} pending"
                                   f"   ·   hidden: {len(hidden_w)} in active warrants, {len(hidden_r)} rejected earlier")
        self.update_footer()

    def decide(self, decision: str):
        selection = list(self.tree.selection())
        if not selection:
            return
        self.inbox_note.configure(text="")  # the arrivals are being reviewed
        if decision == "approved" and not confirm_bulk(self, len(selection),
                                                       self.settings["terms"]["hit_term"]["plural"].lower()):
            return
        if decision == "rejected":
            queued = [self.terms[int(i[1:])] for i in selection if i.startswith("q")]
            if queued:
                first = queued[0]
                answer = RejectDialog.ask(self, [t["literal"] for t in queued], reason=first.get("reject_reason", ""),
                                          **({"minutes": first["reassess_minutes"]} if "reassess_minutes" in first else {}))
                if answer is None:
                    return  # cancelled: nothing changes
                for t in queued:
                    t["reject_reason"], t["reassess_minutes"] = answer["reason"], answer["minutes"]
        self._uncertify("purpose")  # it certifies the approved hit terms
        order = list(self.tree.get_children())
        position = order.index(selection[-1])
        for iid in selection:
            if iid.startswith("q"):
                self.terms[int(iid[1:])]["decision"] = decision
                continue
            row = self.hidden_rows[iid]
            if decision == "rejected":
                continue
            term = row["term"]
            self.terms.append({
                "unreject_on_sign": row["kind"] == "rejected",
                "term_id": unique_id(term.get("term_id") or "h-" + slugify(term["literal"]), {t["term_id"] for t in self.terms}),
                "literal": term["literal"], "source": term.get("source") or "human",
                "provenance": term.get("provenance", ""), "client_id": term.get("client_id", ""), "decision": decision,
                **({"match": "pattern"} if term.get("match") == "pattern" else {}),
            })
        self.render_terms()
        rows = self.tree.get_children()
        if rows:
            nxt = rows[min(position + (1 if len(selection) == 1 else 0), len(rows) - 1)]
            self.tree.selection_set(nxt)
            self.tree.focus(nxt)
            self.tree.see(nxt)

    def _old_items(self, old_id: str | None, field: str) -> list:
        """The terms or places of the warrant being replaced, for the signing document's list of changes."""
        if not old_id:
            return []
        try:
            from ..warrant import load_warrant

            return load_warrant(self.app.paths, self.app.public_key, old_id).get(field) or []
        except Exception:  # noqa: BLE001 - shown as decoration only; signing doesn't depend on it
            return []

    def _confirm_orphans(self, old_id: str) -> bool:
        """Syntax requests and one-time approvals are tied to one warrant. Say which ones a replacement will strand."""
        from ..executor import used_onetime_ids
        from ..onetime import list_onetime, list_requests

        try:
            requests = [r for r in list_requests(self.app.paths) if r.get("warrant_id") == old_id]
            used = used_onetime_ids(self.app.paths)
            approvals = [o for o in list_onetime(self.app.paths, self.app.public_key)
                         if o.get("warrant_id") == old_id and o.get("onetime_id") not in used]
        except Exception:  # noqa: BLE001 - informational only
            return True
        if not requests and not approvals:
            return True
        parts = ([plural(len(requests), "syntax request") + " awaiting review"] if requests else []) + (
            [plural(len(approvals), "unused one-time approval")] if approvals else [])
        return messagebox.askyesno(
            "Replace warrant",
            f"Replacing {old_id} makes {' and '.join(parts)} unavailable: each is tied to the warrant it was made "
            "under. The agent will have to request them again under the new warrant.\n\nContinue?", parent=self)

    def copy_selected(self):
        """Copy the selected rows' hit terms and client IDs, one per line, tab-separated (pastes as two columns)."""
        rows = [i for i in self.tree.selection() if self.tree.exists(i)]
        if not rows:
            return "break"
        lines = []
        for iid in rows:
            client = self.tree.set(iid, "client_id")
            lines.append(self.tree.set(iid, "literal") + "\t" + ("" if client == "—" else client))  # always two columns
        copy_text(self, "\n".join(lines))
        self.app.notify(f"Copied {plural(len(lines), self.settings['terms']['hit_term']['singular'].lower())} "
                        f"with {self.settings['terms']['client_id']['plural']}")
        return "break"

    def term_details(self):
        from .widgets import Inspector, _Dialog

        selected = self.tree.selection()
        if len(selected) != 1:
            messagebox.showinfo("Details", "Select one row.", parent=self)
            return
        dialog = _Dialog(self, self.tree.set(selected[0], "literal"))
        view = Inspector(dialog.frame, "")
        view.pack(fill="both", expand=True)
        view.begin()
        for column, label in (("literal", self.settings["terms"]["hit_term"]["singular"]),
                              ("matching", "Matching"), ("term_id", "Internal ID"), ("provenance", "Provenance / reason")):
            view.field(label, self.tree.set(selected[0], column), copy=True)
        ttk.Button(dialog.frame, text="Close", command=dialog.destroy).pack(anchor="e")
        dialog.geometry("640x360")
        dialog.run()

    def add_term(self):
        result = TermDialog.ask(self, f"Add {self.settings['terms']['hit_term']['singular'].lower()}",
                                self.settings['terms']['hit_term']['singular'])
        if result is None:
            return
        literal = result["literal"]
        if any(t["literal"] == literal for t in self.terms):
            messagebox.showerror(f"Add {self.settings['terms']['hit_term']['singular'].lower()}", f"That {self.settings['terms']['hit_term']['singular'].lower()} is already in the list.", parent=self)
            return
        client_label = self.settings["terms"]["client_id"]["singular"]
        while True:
            client_id = askstring(f"Add {self.settings['terms']['hit_term']['singular'].lower()}", f"{client_label} associated with it (optional):",
                                               parent=self)
            if client_id is None:
                return
            try:
                client_id = normalize_client_id(client_id)
                break
            except ValueError as exc:
                messagebox.showerror(f"Add {self.settings['terms']['hit_term']['singular'].lower()}", str(exc), parent=self)
        reason = askstring(f"Add {self.settings['terms']['hit_term']['singular'].lower()}", "Why include it? (optional)", parent=self) or ""
        self._uncertify("purpose")
        self.terms.append({
            "unreject_on_sign": literal in load_rejected(self.app.paths),
            "term_id": unique_id("h-" + slugify(literal), {t["term_id"] for t in self.terms}),
            **result, "source": "human", "client_id": client_id,
            "provenance": f"human: {reason}" if reason else "added by human", "decision": "approved",
        })
        self.render_terms((f"q{len(self.terms) - 1}",))

    def edit_literal(self):
        selection = self.tree.selection()
        if len(selection) != 1 or not selection[0].startswith("q"):
            messagebox.showinfo(f"Edit {self.settings['terms']['hit_term']['singular'].lower()}", f"Select one {self.settings['terms']['hit_term']['singular'].lower()} in the queue.", parent=self)
            return
        term = self.terms[int(selection[0][1:])]
        result = TermDialog.ask(self, f"Edit {self.settings['terms']['hit_term']['singular'].lower()}",
                                self.settings['terms']['hit_term']['singular'], term)
        if result is None or (result["literal"], result["match"]) == (term["literal"], term.get("match", "exact")):
            return
        if any(t is not term and t["literal"] == result["literal"] for t in self.terms):
            messagebox.showerror(f"Edit {self.settings['terms']['hit_term']['singular'].lower()}",
                                 "That text is already in the list.", parent=self)
            return
        self._uncertify("purpose")
        term["provenance"] = (f"{term['provenance']} [edited by human; was {term['literal']!r} "
                              f"({term.get('match', 'exact')})]")
        if term["source"] in ("script", "agent"):
            term["source"] = "human-edited"
        term.update(result)
        self.render_terms(selection)

    # --- edit / renew -----------------------------------------------------------------

    def refresh_warrants(self):
        if self.show_hidden.get():
            self.render_terms(self.tree.selection())

    def start_edit(self, warrant_id: str, doc: dict, expired: bool):
        if self.editing == warrant_id:
            return
        if self.editing:
            self.cancel_edit()
        self.editing = warrant_id
        literals = {t["literal"] for t in doc["terms"]}
        self.terms = [{**t, "decision": "approved", "from_warrant": True} for t in doc["terms"]] + [
            t for t in self.terms if t["literal"] not in literals]
        self.set_p1(doc["parameter_1"])
        self.parameter_1_changed(selected=doc["parameter_2"])
        self._set_purpose(doc["purpose"], note=f"From {warrant_id}. Edit if the task has changed.")
        # A purpose the agent drafted stays the agent's draft in the replacement, unless it's rewritten.
        self.agent_purpose = doc["purpose"] if doc.get("purpose_origin") in ("agent", "agent-edited") else ""
        self._loading = True
        self.rows.set(str(doc["max_rows_per_run"]))
        self.rate.set(str(doc.get("max_queries_per_hour") or self.settings["defaults"]["max_queries_per_hour"]))
        lookback = doc.get("max_lookback_days", 0)
        self.window_mode.set("limited" if lookback else "none")
        self.lookback.set(str(lookback or self.settings["defaults"]["lookback_days"] or 30))
        self.blocked = list(doc.get("blocked_date_ranges", []))
        scope = doc.get("country_scope")
        if scope is not None:  # older warrants have no country scope: keep the defaults for the replacement
            self.include_rows = include_rows(scope.get("include"))
            self.country_mode.set("only" if self.include_rows else "all")
            self.exclude_rows = [dict(r) for r in scope.get("exclude", [])]
        self.places_panel.load_signed(doc.get("places"))
        self.cert_purpose.set(False)
        self.cert_limits.set(False)
        self._loading = False
        self._render_blocked()
        self._render_scope()
        if not expired:
            remaining = max(1, int((from_iso(doc["expires_at"]) - utc_now()).total_seconds() // 60) + 1)
            days, rest = divmod(remaining, 1440)
            self.ttl_days.set(str(min(days, MAX_TTL_DAYS)))
            self.ttl_hours.set(str(rest // 60))
            self.ttl_minutes.set(str(rest % 60))
        self.edit_label.configure(text=f"{'Renewing expired' if expired else 'Editing'} {warrant_id}")
        self.edit_note.configure(text="Signing replaces it. Kept terms keep their IDs. R removes a term or place; P leaves it out for now.")
        self.edit_banner.grid(row=0, column=0, sticky="ew", pady=(4, 8))
        self.page.to_top()
        self.render_terms()

    def cancel_edit(self):
        if not self.editing:
            return
        self.terms = [{k: v for k, v in t.items() if k != "from_warrant"} for t in self.terms if not t.get("from_warrant")]
        self.places_panel.rows = [r for r in self.places_panel.rows if not r.get("from_warrant")]
        self.places_panel.render()
        self._clear_settings()
        self.merge_inbox()

    def _clear_settings(self):
        self.editing = None
        self.edit_banner.grid_forget()
        self.p1_display.set(P1_PLACEHOLDER)
        self.parameter_1_changed(selected=[])
        self._set_purpose("")
        self.agent_purpose = ""
        self._apply_defaults()

    def reset_form(self):
        self.terms = [{k: v for k, v in t.items() if k not in ("from_warrant", "unreject_on_sign")}
                      for t in self.terms if t["decision"] == "pending"]
        self.places_panel.keep_pending()
        self._clear_settings()
        self.merge_inbox()

    # --- validation, footer, signing -------------------------------------------------

    def _limits(self):
        """(ttl_minutes, rows, lookback_days, problems)."""
        problems = []
        try:
            days, hours, minutes = (int(v.get()) for v in (self.ttl_days, self.ttl_hours, self.ttl_minutes))
            if min(days, hours, minutes) < 0 or hours > 23 or minutes > 59:
                raise ValueError
            ttl = days * 1440 + hours * 60 + minutes
            if not 1 <= ttl <= MAX_TTL_DAYS * 1440:
                problems.append(f"expiry must be 1 minute to {MAX_TTL_DAYS} days")
        except ValueError:
            ttl = None
            problems.append("expiry needs whole numbers (0–23 hours, 0–59 minutes)")
        try:
            rows = int(self.rows.get().replace(",", ""))
            if not 1 <= rows <= MAX_ROWS:
                problems.append(f"max rows must be 1 to {MAX_ROWS:,}")
        except ValueError:
            rows = None
            problems.append("max rows must be a whole number")
        try:
            self.rate_value = int(self.rate.get().replace(",", ""))
            if not 1 <= self.rate_value <= MAX_QUERIES_PER_HOUR:
                problems.append(f"max searches per hour must be 1 to {MAX_QUERIES_PER_HOUR:,}")
        except ValueError:
            self.rate_value = None
            problems.append("max searches per hour must be a whole number")
        if self.window_mode.get() == "none":
            lookback = 0
        else:
            try:
                lookback = int(self.lookback.get())
                if not 1 <= lookback <= MAX_LOOKBACK_DAYS:
                    problems.append(f"search window must be 1 to {MAX_LOOKBACK_DAYS:,} days")
            except ValueError:
                lookback = None
                problems.append("search window must be a whole number of days")
        return ttl, rows, lookback, problems

    def update_footer(self):
        if not hasattr(self, "footer_text"):
            return
        ttl, rows, lookback, problems = self._limits()
        if hasattr(self, "limits_effect"):
            self.limits_effect.configure(text=f"Together: up to {rows * self.rate_value:,} records an hour"
                                         if rows and self.rate_value else "")
        valid_ttl = ttl and not any(p.startswith("expiry") for p in problems)
        self.expiry_preview.configure(
            text=f"→ expires {local_time(to_iso(utc_now() + timedelta(minutes=ttl)))}" if valid_ttl else "")
        included = [t for t in self.terms if t["decision"] == "approved"]
        if not self.purpose.get("1.0", "end").strip():
            problems.append("write a purpose")
        if self.country_mode.get() == "only" and not self.include_rows:
            problems.append("add at least one country to search, or choose All countries")
        if hasattr(self, "places_panel"):
            problems += self.places_panel.problems()
        if getattr(self, "config_outdated", False):
            problems.insert(0, "start a new draft: the organization configuration changed")
        if getattr(self.app, "field_mismatch", None):
            problems.append("fix the configuration: its field names don't match the template library (see About)")
        if not self.cert_purpose.get():
            problems.append(f"certify the {self.settings['terms']['hit_term']['plural'].lower()} and purpose")
        if not self.cert_limits.get():
            problems.append("certify the limits")
        if not included:
            problems.insert(0, f"include at least one {self.settings['terms']['hit_term']['singular'].lower()}")
        if not self.p1:
            problems.insert(0, "choose a search authorization")
        elif not self.chips.values():
            problems.insert(0, "choose at least one visibility attribute")
        # Only say what's missing; the full warrant is shown in the signing document.
        if problems:
            self.footer_text.configure(text="Still needed: " + "; ".join(problems) + ".", style="Warn.TLabel")
            self.sign_button.configure(state="disabled")
            return
        self.footer_text.configure(style="Muted.TLabel", text="Ready. Review and sign opens the warrant document.")
        self.sign_button.configure(state="normal")

    def sign(self):
        app = self.app
        current = getattr(app, "verified_settings", self.settings)
        if authority_hash(current) != authority_hash(self.settings):
            messagebox.showerror("Sign warrant", "The organization configuration changed while you were composing. "
                                 "Start a new draft (the button at the top of Compose) so the warrant is signed under "
                                 "the configuration in force.", parent=self)
            return
        if not (self.cert_purpose.get() and self.cert_limits.get()):  # checked here too, not only by the button
            self.update_footer()
            return
        ttl, rows, lookback, problems = self._limits()
        if problems or not self.p1:
            self.update_footer()
            return
        old_id = self.editing
        approved = [t for t in self.terms if t["decision"] == "approved"]
        rejected = [t for t in self.terms if t["decision"] == "rejected"]
        pending = [t for t in self.terms if t["decision"] == "pending"]
        kept = [t for t in approved if t.get("from_warrant")]
        taken = {t["term_id"] for t in kept}
        combined = list(kept)
        for t in approved:
            if not t.get("from_warrant"):
                combined.append({**t, "term_id": unique_id(t["term_id"], taken)})
                taken.add(combined[-1]["term_id"])
        settings = self.settings["certification"]
        try:
            doc = build_warrant(
                parameter_1=self.p1, parameter_2=self.chips.values(), purpose=self.purpose.get("1.0", "end").strip(),
                terms=combined, ttl_minutes=ttl, max_rows=rows, rbac=app.rbac, lookback_days=lookback,
                blocked_ranges=self.blocked, supersedes=old_id, paths=app.paths,
                country_include=self.include_rows if self.country_mode.get() == "only" else [],
                country_exclude=self.exclude_rows,
                certifications=[{"id": "purpose", "text": settings["purpose"]}, {"id": "limits", "text": settings["limits"]}],
                config_context=config_context(self.settings), max_queries_per_hour=self.rate_value,
                places=self.places_panel.for_warrant(), purpose_origin=self.purpose_origin(),
            )
        except WarrantError as exc:
            messagebox.showerror("Sign warrant", str(exc), parent=self)
            return
        try:
            library = load_library(app.paths, app.public_key)
            mismatch = field_mismatches(authority_values(self.settings), library)
            if mismatch:  # re-checked now, not trusted from the last refresh
                messagebox.showerror("Sign warrant", "The configuration and the template library disagree on field names:\n\n"
                                     + "\n".join(mismatch), parent=self)
                return
            enabled = sum(t.get("enabled", True) for t in library["templates"])
            library_line = f"the signed template library (version {library['version']}, {enabled} enabled templates, and any added later)"
        except Exception as exc:  # noqa: BLE001 - shown to the user
            library_line = f"the template library (currently unusable: {exc})"
        p2_labels = getattr(self, "p2_options", {})
        blocks = warrant_blocks(
            doc, countries=countries(app.paths), library_line=library_line, replaces=old_id,
            labels={"p1": {v: o.label for v, o in self.p1_options.items()}, "p2": {v: o.label for v, o in p2_labels.items()}},
            sources=self.settings["source_labels"],
            param_names=(self.settings["parameters"]["parameter_1"]["label"], self.settings["parameters"]["parameter_2"]["label"]),
            terms=self.settings["terms"],
            new_literals={t["literal"] for t in approved if not t.get("from_warrant")} if old_id else frozenset(),
            removed=[t["literal"] for t in rejected + pending if t.get("from_warrant")],
            old_places=self._old_items(old_id, "places"),
            old_terms=self._old_items(old_id, "terms"),
        )

        if old_id and not self._confirm_orphans(old_id):
            return

        def save(key):
            if old_id:
                replace_warrant(app.paths, old_id, doc, key)
            else:
                save_warrant(app.paths, doc, key)
            for term in rejected:
                reject(app.paths, term)
            self.places_panel.record_rejections(app.paths)
            for term in self.terms:
                if term.get("unreject_on_sign") and term["decision"] != "rejected":
                    unreject(app.paths, term["literal"])
            return doc

        if app.sign("Sign warrant", "", save, document=blocks):
            app.notify(f"Signed {doc['warrant_id']}" + (f" (replaces {old_id})" if old_id else ""))
            app.refresh_all()
            self.reset_form()


def _hidden_iid(kind: str, literal: str) -> str:
    """Row ID for a hidden term, stable across refreshes (keyed by the term, not its position)."""
    import hashlib

    return f"{kind}{hashlib.sha256(literal.encode()).hexdigest()[:16]}"
