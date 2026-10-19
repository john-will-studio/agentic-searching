"""Runs: the global execution history, filterable, with IDs, checks and the exact payload sent."""

from __future__ import annotations

from .term_dialog import term_label

import json
import tkinter as tk
from tkinter import ttk

from ..executor import load_payload, payload_path, read_audit
from ..library import load_library
from ..syntax import SyntaxRejected, parse_skeleton, suggest_name
from .widgets import Inspector, Table, lint_badge, local_time, plural, split_panes, text_table

OUTCOMES = {"ok": "Completed", "denied": "Blocked before sending", "error": "Execution failed"}
PAGE = 500
ALL = "All"


class RunsTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=(12, 10))
        self.app = app
        self.entries: dict[str, dict] = {}
        self.names: dict[str, str] = {}
        self.term_names = {}
        self.limit = PAGE
        self.contextual = False  # filters applied by a "View runs…" link, cleared on plain tab entry
        self.arriving_by_link = False  # consumed by the next tab-change event, whenever it arrives
        self._quiet = False
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Warrant").pack(side="left")
        self.warrant = tk.StringVar(value=ALL)
        self.warrant_box = ttk.Combobox(bar, textvariable=self.warrant, state="readonly", width=28)
        self.warrant_box.pack(side="left", padx=(6, 14))
        ttk.Label(bar, text="Outcome").pack(side="left")
        self.outcome = tk.StringVar(value=ALL)
        ttk.Combobox(bar, textvariable=self.outcome, state="readonly", width=22,
                     values=[ALL, *OUTCOMES.values()]).pack(side="left", padx=(6, 14))
        ttk.Label(bar, text="Search").pack(side="left")
        self.search = tk.StringVar()
        ttk.Entry(bar, textvariable=self.search, width=26).pack(side="left", padx=(6, 6))
        self.template_filter: str | None = None
        for var in (self.warrant, self.outcome, self.search):
            var.trace_add("write", lambda *_: None if self._quiet else self.render())
        # Active filters are spelled out in their own row, with one click to show everything.
        self.banner = ttk.Frame(self)
        self.banner_label = ttk.Label(self.banner, style="Warn.TLabel")
        self.banner_label.pack(side="left")
        ttk.Button(self.banner, text="Show all runs", command=self.show_all).pack(side="left", padx=10)

        panes = self.panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, side="bottom")
        left = ttk.Frame(panes)
        self.inspector = Inspector(panes, "Select a run to see its IDs, its checks and the exact payload that was sent.")
        panes.add(left, weight=1)
        panes.add(self.inspector, weight=1)
        split_panes(panes)  # table and details start at 50/50
        self.table = Table(left, [
            ("time", "Time", 3, 140, "w"), ("warrant", self.app.settings["terms"]["warrant"]["singular"], 3, 170, "w"),
            ("outcome", "Outcome", 3, 150, "w"), ("via", "Template / approval", 4, 170, "w"),
            ("terms", self.app.settings["terms"]["hit_term"]["plural"], 4, 140, "w"), ("records", "Records", 1.4, 70, "e"), ("lint", "Syntax checks", 2, 110, "w"),
        ], height=18, fit_content={"time": None, "warrant": None, "outcome": None, "via": 320, "terms": 360, "records": None, "lint": None})
        self.table.pack(fill="both", expand=True)
        self.table.tree.bind("<<TreeviewSelect>>", lambda _e: self.show())
        from . import theme

        self.table.tree.tag_configure("denied", foreground=theme.PALETTE["danger"])
        self.table.tree.tag_configure("error", foreground=theme.PALETTE["warn"])
        foot = ttk.Frame(left)
        foot.pack(fill="x", pady=(6, 0), side="bottom", before=self.table)  # reserve its row before the table expands
        self.count_label = ttk.Label(foot, style="Muted.TLabel")
        self.count_label.pack(side="left")
        self.more_button = ttk.Button(foot, text="Show more", command=self.show_more)

    def refresh(self):
        audit = read_audit(self.app.paths, limit=None)
        self.entries = {}
        for i, e in enumerate(audit):
            self.entries[e.get("run_id") or f"legacy-{i}"] = e
        try:
            library = load_library(self.app.paths, self.app.public_key)
            self.names = {t["template_id"]: t["name"] for t in library["templates"]}
        except Exception:  # noqa: BLE001
            self.names = {}
        warrants = sorted({e.get("warrant_id") for e in audit if e.get("warrant_id")}, reverse=True)
        from ..warrant import list_warrants

        self.term_names = {r["warrant_id"]: {t["term_id"]: term_label(t) for t in r["doc"]["terms"]}
                           for r in list_warrants(self.app.paths, self.app.public_key, include_archived=True)
                           if r.get("doc")}
        self.warrant_box.configure(values=[ALL, *warrants])
        self.render()

    def _results(self, view, e, saved):
        """What came back (a profile, not a list: there may be hundreds of thousands) and what the agent made of it,
        ahead of the payload, which is for when results look wrong."""
        from ..results import describe_lines, pivot_caption, pivot_table, results_path
        from ..summaries import read_summary

        prof = (saved or {}).get("profile")
        if prof is None:
            return  # recorded before results were profiled
        view.section("What came back")
        for label, value in describe_lines(prof):
            view.field(label, value)
        if prof.get("count"):
            headers, rows = pivot_table(prof, self.app.settings["terms"]["hit_term"]["singular"])
            view.para(pivot_caption(prof, self.app.settings["terms"]["hit_term"]["singular"]), style="Strong.TLabel")
            view.code(text_table(headers, rows), max_lines=14)
        else:
            view.para("No records matched within the warrant's scope.", style="Muted.TLabel")
        path = results_path(self.app.paths, e["run_id"])
        if path.exists():
            command = f"lucid-warrant results {e['run_id']} --format parquet --out {e['run_id']}.parquet"
            view.field("Full results", command, mono=True, copy=True)
            view.para(f"Saved in {path}. Export as parquet, csv or jsonl to investigate individual records.",
                      style="Small.TLabel")
        summary = read_summary(self.app.paths, e.get("run_id"))
        if summary:
            when = f" · {local_time(summary['at'])}" if summary.get("at") else ""
            view.callout(f"Agent's summary — its words, not verified{when}", summary["text"], "agent")
        else:
            view.para("The agent hasn't summarized this run.", style="Muted.TLabel")

    def _via(self, e) -> str:
        if e.get("kind") == "onetime":
            return f"One-time {e.get('onetime_id', '')}"
        if e.get("kind") == "syntax_request":
            return f"Syntax request: {e.get('skeleton', '')}"
        return self.names.get(e.get("template_id"), e.get("template_id", ""))

    @staticmethod
    def _term_ids(e):
        return e.get("term_ids") or sorted(set((e.get("bindings") or {}).values()))

    def _terms(self, e) -> str:
        names = self.term_names.get(e.get("warrant_id"), {})
        ids = self._term_ids(e)
        shown = [names[tid] for tid in ids if tid in names]
        missing = len(ids) - len(shown)
        if missing:
            shown.append(f"{missing} unavailable (see details)")
        return ", ".join(shown)

    def render(self):
        kept = self.table.selected()
        self.table.clear()
        wanted_w, wanted_o, needle = self.warrant.get(), self.outcome.get(), self.search.get().strip().lower()
        matches = []
        for iid, e in reversed(list(self.entries.items())):
            if wanted_w != ALL and e.get("warrant_id") != wanted_w:
                continue
            if wanted_o != ALL and OUTCOMES.get(e.get("outcome")) != wanted_o:
                continue
            if self.template_filter and e.get("template_id") != self.template_filter:
                continue
            if needle:
                haystack = " ".join(str(v) for v in (iid, e.get("service_query_id"), self._via(e), self._terms(e), ", ".join(self._term_ids(e)),
                                                     e.get("warrant_id"), e.get("reason"))).lower()
                if needle not in haystack:
                    continue
            matches.append((iid, e))
        for iid, e in matches[: self.limit]:
            self.table.tree.insert("", "end", iid=iid, tags=(e.get("outcome"),), values=(
                local_time(e["ts"]), e.get("warrant_id") or "—", OUTCOMES.get(e.get("outcome"), e.get("outcome")),
                self._via(e), self._terms(e),
                e.get("count", "") if e.get("outcome") == "ok" else "", lint_badge(e.get("lint")) if e.get("lint") else "—"))
        total = len(self.entries)
        shown = min(len(matches), self.limit)
        active = []
        if wanted_w != ALL:
            active.append(f"warrant {wanted_w}")
        if self.template_filter:
            active.append(f"template \"{self.names.get(self.template_filter, '?')}\" ({self.template_filter})")
        if wanted_o != ALL:
            active.append(f"outcome {wanted_o}")
        if needle:
            active.append(f"text \"{needle}\"")
        filtered = bool(active)
        if active:
            hidden = total - len(matches)
            self.banner_label.configure(text=f"Filtered by {' · '.join(active)}  —  {plural(hidden, 'run')} hidden")
            # Packed ahead of the expanding results pane, or it would get no height at all.
            self.banner.pack(fill="x", pady=(0, 8), before=self.panes)
        else:
            self.banner.pack_forget()
        self.count_label.configure(text=f"Showing {shown:,} of {len(matches):,} {'matching ' if filtered else ''}runs"
                                        f"{f' ({total:,} in total)' if filtered else ''}, newest first. Full history is kept.")
        if len(matches) > self.limit:
            self.more_button.pack(side="right")
        else:
            self.more_button.pack_forget()
        self.table.set_empty(f"No runs match these filters ({plural(total, 'run')} in total). Use Show all runs above."
                             if filtered else "No runs yet. Runs appear here as the agent searches.")
        self.table.restore_view()
        if not self.table.reselect(kept, see=False):
            self.inspector.show_empty()

    def show_more(self):
        self.limit += PAGE
        self.render()

    def clear_filters(self):
        """Reset every filter without rendering in between."""
        self._quiet = True
        try:
            self.template_filter = None
            self.contextual = False
            self.limit = PAGE
            self.warrant.set(ALL)
            self.outcome.set(ALL)
            self.search.set("")
        finally:
            self._quiet = False

    def show_all(self):
        self.clear_filters()
        self.render()

    def filter_by(self, warrant: str | None = None, template: str | None = None):
        self.clear_filters()
        self._quiet = True
        if warrant:
            if warrant not in self.warrant_box.cget("values"):
                self.warrant_box.configure(values=[*self.warrant_box.cget("values"), warrant])
            self.warrant.set(warrant)
        self.template_filter = template
        self.contextual = True
        self._quiet = False
        self.render()

    def entered_from_tab(self):
        """Called on every switch to Runs. A link sets arriving_by_link first, so its own filter is kept;
        opening Runs from its tab drops filters a link applied earlier."""
        if self.arriving_by_link:
            self.arriving_by_link = False
            return
        if self.contextual:
            self.show_all()

    def select(self, run_id: str):
        self.clear_filters()
        newest_first = list(reversed(list(self.entries)))
        if run_id in newest_first:
            self.limit = max(PAGE, newest_first.index(run_id) + 1)
        self.render()
        if self.table.reselect(run_id):
            self.show()
        else:
            self.inspector.show_empty(f"Run {run_id} isn't in the history.")

    def show(self):
        iid = self.table.selected()
        e = self.entries.get(iid or "")
        view = self.inspector
        if not e:
            view.show_empty()
            return
        from ..summaries import summary_path

        try:
            summary_stamp = summary_path(self.app.paths, e.get("run_id")).stat().st_mtime_ns if e.get("run_id") else None
        except (OSError, ValueError):
            summary_stamp = None
        signature = (iid, json.dumps(e, sort_keys=True), summary_stamp)
        if signature == getattr(self, "_shown", None) and view.key == iid:
            return  # live refresh of the same, unchanged run: leave the pane as the user has it
        self._shown = signature
        outcome = OUTCOMES.get(e.get("outcome"), e.get("outcome"))
        view.begin(key=iid)
        view.title(outcome, local_time(e["ts"]))
        tone = {"ok": "ok", "denied": "danger", "error": "warn"}.get(e.get("outcome"), "info")
        chips = [(outcome, tone)]
        if e.get("lint"):
            chips.append((f"Checks at firing: {lint_badge(e['lint'])}", "ok" if e["lint"].get("passed") else "danger"))
        view.chips(chips)
        if e.get("outcome") == "denied":
            view.callout("Blocked before sending", f"Nothing went to the search service.\nReason: {e.get('reason', '')}", "danger")
        if e.get("withheld"):
            from .document import withheld_text

            view.callout("Withheld from the agent", withheld_text(e["withheld"], e.get("withheld_detail")) + ". Fewer "
                         "results than the service returned: this isn't evidence that nothing else matched.", "warn")
        if e.get("storage_problem"):
            view.callout("Record incomplete", f"The search was sent, but {e['storage_problem']}. The audit entry above "
                         "is complete; check the service's own logs for the rest.", "danger")
        elif e.get("outcome") == "error":
            view.callout("Execution failed", "Sending was attempted and failed. Whether the search service received "
                         f"the request is unknown.\n{e.get('reason', '')}", "warn")
        view.section("Identifiers")
        view.field("Run ID", e.get("run_id") or "(legacy record, before run IDs)", mono=True, copy=bool(e.get("run_id")))
        view.field("Service query ID", e.get("service_query_id") or "—", mono=True, copy=bool(e.get("service_query_id")))
        view.field("Warrant", e.get("warrant_id", ""), mono=True, copy=True)
        if e.get("warrant_id"):
            view.link("Open warrant  →", lambda: self.app.show_warrant(e["warrant_id"]))
        view.section("What ran")
        if e.get("kind") == "onetime":
            view.field("One-time approval", e.get("onetime_id", ""), mono=True)
            view.link("Open approval  →", lambda: self.app.show_approval("ot:" + e["onetime_id"]))
        else:
            view.field("Template", self._via(e))
            if e.get("template_id") in self.names:
                view.link("Open template  →", lambda: self.app.show_template(e["template_id"]))
        view.field(self.app.settings["terms"]["hit_term"]["plural"], self._terms(e))
        view.field("Internal IDs", ", ".join(self._term_ids(e)), mono=True, copy=True)
        if e.get("outcome") == "ok":
            view.field("Records returned", e.get("count", 0))
        saved = load_payload(self.app.paths, e["run_id"]) if e.get("run_id") else None
        if e.get("outcome") == "ok":
            self._results(view, e, saved)
        report = (saved or {}).get("lint")
        if report or e.get("lint"):
            view.section("Checks at firing")
            if report:
                view.field("Result", lint_badge(report))
                for f in report.get("findings", []):
                    view.field(f"  {f['level']} {f['code']}", f["message"],
                               style="Danger.TLabel" if f["level"] == "error" else "Warn.TLabel")
            else:
                view.field("Result", e["lint"].get("summary", ""))
        view.section("Attempted payload" if e.get("outcome") == "error" else "Payload sent")
        if e.get("outcome") == "denied":
            view.para("None. The run was blocked before anything was sent.", style="Muted.TLabel")
            if e.get("query"):
                view.field("Query it would have sent", "")
                view.code(e["query"])
        elif saved:
            view.field("Sent", local_time(saved["sent_at"]))
            view.code(json.dumps(saved["payload"], indent=2, ensure_ascii=False), max_lines=24)
            if saved.get("request_sent"):
                view.para("Actual HTTP request (credentials hidden)", style="Strong.TLabel")
                view.code(json.dumps(saved["request_sent"], indent=2, ensure_ascii=False), max_lines=24)
        elif e.get("run_id"):
            view.callout("Payload unavailable", "The saved payload record is missing or unreadable:\n"
                         f"{payload_path(self.app.paths, e['run_id'])}", "danger")
            if e.get("query"):
                view.code(e["query"])
        else:
            view.para("Legacy record: this run predates payload records.", style="Muted.TLabel")
            if e.get("query"):
                view.code(e["query"])
