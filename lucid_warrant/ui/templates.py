"""Templates: the signed library, with search, status filter, structure, checks and usage."""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk

from ..executor import template_usage
from ..library import describe_template, load_library, set_template_enabled
from ..lint import lint
from . import theme
from .widgets import Inspector, Table, lint_badge, local_time, plural


class TemplatesTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=(12, 10))
        self.app = app
        self.library = None
        self.usage: dict[str, dict] = {}
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Search").pack(side="left")
        self.search = tk.StringVar()
        ttk.Entry(bar, textvariable=self.search, width=28).pack(side="left", padx=(6, 16))
        self.status_filter = tk.StringVar(value="All")
        for name in ("All", "Enabled", "Disabled"):
            ttk.Radiobutton(bar, text=name, value=name, variable=self.status_filter, style="Toggle.TButton",
                            command=self.render).pack(side="left", padx=(0, 4))
        self.header = ttk.Label(self, style="Muted.TLabel")  # its own line: it's long and mustn't be clipped
        self.header.pack(fill="x", pady=(0, 6))
        self.search.trace_add("write", lambda *_: self.render())

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        self.inspector = Inspector(panes, "Select a template to see what it does, its Lucene, its checks and how it's been used.")
        panes.add(left, weight=3)
        panes.add(self.inspector, weight=4)
        self.table = Table(left, [
            ("name", "Template", 5, 180, "w"), ("status", "Status", 2, 90, "w"), ("lint", "Checks", 2, 100, "w"),
            ("runs", "Successful runs", 2, 110, "e"), ("last", "Last used", 3, 140, "w"),
        ], height=18, fit_content={"name": 360, "status": None, "lint": None, "runs": None, "last": None})
        self.table.pack(fill="both", expand=True)
        self.table.tree.bind("<<TreeviewSelect>>", lambda _e: self.show())
        p = theme.PALETTE
        self.table.tree.tag_configure("disabled", foreground=p["muted"])

    def refresh(self):
        try:
            self.library = load_library(self.app.paths, self.app.public_key)
        except Exception as exc:  # noqa: BLE001
            self.library = None
            self.header.configure(text=f"Library NOT usable: {exc}", style="Danger.TLabel")
            self.app.report_problem("library", f"Template library unusable: {exc}")
            self.table.clear()
            self.inspector.show_empty("The template library failed verification. The agent can't run library templates.")
            return
        self.app.report_problem("library", None)
        self.usage = template_usage(self.app.paths)
        enabled = sum(t.get("enabled", True) for t in self.library["templates"])
        self.header.configure(style="Muted.TLabel", text=f"Library v{self.library['version']} · signature valid · "
                              f"{enabled} of {len(self.library['templates'])} enabled · updated {local_time(self.library['updated_at'])}")
        self.render()

    def _lint(self, template):
        return lint(template["skeleton"], None, default_field=self.library["default_field"],
                    allowed_fields=self.library["allowed_fields"], stage="library").to_dict()

    def render(self):
        if not self.library:
            return
        kept = self.table.selected()
        self.table.clear()
        needle = self.search.get().strip().lower()
        wanted = self.status_filter.get()
        for t in self.library["templates"]:
            enabled = t.get("enabled", True)
            if wanted == "Enabled" and not enabled or wanted == "Disabled" and enabled:
                continue
            info = describe_template(self.library, t)
            haystack = " ".join([t["name"], t["template_id"], t["skeleton"], info["plain_english"], t["description"]]).lower()
            if needle and needle not in haystack:
                continue
            u = self.usage.get(t["template_id"], {})
            self.table.tree.insert("", "end", iid=t["template_id"], tags=() if enabled else ("disabled",), values=(
                t["name"], "Enabled" if enabled else "Disabled", lint_badge(self._lint(t)), u.get("queries", 0),
                local_time(u["last_used"]) if u.get("last_used") else "never"))
        self.table.set_empty("No templates match." if needle or wanted != "All" else "The library is empty.")
        self.table.restore_view()
        if not self.table.reselect(kept, see=False):
            self.inspector.show_empty()

    def select(self, template_id: str):
        self.search.set("")
        self.status_filter.set("All")
        self.render()
        self.table.reselect(template_id)
        self.show()

    def show(self):
        tid = self.table.selected()
        template = next((t for t in (self.library or {}).get("templates", []) if t["template_id"] == tid), None)
        view = self.inspector
        if not template:
            view.show_empty()
            return
        info = describe_template(self.library, template)
        report = self._lint(template)
        u = self.usage.get(tid, {})
        signature = (tid, self.library["version"], json.dumps(u, sort_keys=True))
        if signature == getattr(self, "_shown", None) and view.key == tid:
            return
        self._shown = signature
        view.begin(key=tid)
        view.title(info["name"], "Library template")
        view.chips([("Enabled" if info["enabled"] else "Disabled", "ok" if info["enabled"] else "danger"),
                    (lint_badge(report), "ok" if report["passed"] else "danger")]
                   + ([("Expands matching (proximity)", "warn")] if info["expands_matching"] else []))
        view.section("What it does")
        view.para(info["plain_english"], font="LW.Strong")
        view.para("Generated from the template. [slot] is where an approved hit term goes.", style="Small.TLabel")
        view.section("Syntax")
        view.field("Lucene", "")
        view.code(info["lucene_shape"])
        view.field("Skeleton", info["skeleton"], mono=True)
        view.field("Slots", ", ".join(info["slots"]))
        view.section("Checks")
        view.field("Structure lint", lint_badge(report))
        for f in report["findings"]:
            view.field(f"  {f['level']} {f['code']}", f["message"], style="Warn.TLabel")
        view.para("Signature valid means the library is unchanged since you signed it. It isn't a guarantee the "
                  "template matches as intended on the real index; test it there. Each run is also linted when fired.",
                  style="Small.TLabel")
        view.section("Usage")
        view.field("Successful runs", u.get("queries", 0))
        view.field("Records returned", f"{u.get('records', 0):,} (cumulative, may repeat)")
        view.field("Last used", local_time(u["last_used"]) if u.get("last_used") else "never")
        view.link("Runs using this template  →", lambda: self.app.show_runs(template=tid))
        view.section("Record")
        view.field("Description", info["description"])
        view.para("Analyst-maintained description.", style="Small.TLabel")
        view.field("Agent refers to it as", tid, mono=True, copy=True)
        view.field("Origin", f"{template.get('origin', '')}, added {local_time(template.get('added_at'))}")
        verb = "Disable…" if info["enabled"] else "Enable…"
        view.action(verb, lambda: self.toggle(template, info), accent=not info["enabled"])
        view.action_note("Disabling blocks future runs of this template; nothing else changes."
                         if info["enabled"] else "Enabling lets the agent run it again with approved terms.")

    def toggle(self, template, info):
        enable = not info["enabled"]
        verb = "ENABLE" if enable else "DISABLE"
        impact = ("The agent will be able to run it again with the approved terms of any active warrant."
                  if enable else
                  "Blocks future runs of this template under every warrant. One-time approvals are separate and "
                  "unaffected. Runs already made stay in the history.")
        summary = (f"{verb} TEMPLATE (library v{self.library['version']} -> v{self.library['version'] + 1})\n\n"
                   f"{info['name']}\n{info['plain_english']}\n{info['lucene_shape']}\n\nIMPACT: {impact}")
        reviewed = self.library["version"]
        done = self.app.sign(f"{verb.title()} template", summary, lambda key: set_template_enabled(
            self.app.paths, key, template["template_id"], enable, expected_version=reviewed))
        if done:
            self.app.notify(f"{'Enabled' if enable else 'Disabled'} {info['name']} · library v{done['version']}")
            self.app.refresh_all()
