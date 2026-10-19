"""Warrants: every warrant, including expired, revoked and superseded, with a read-only scope view."""

from __future__ import annotations

from .term_dialog import term_label, matching

import json
import tkinter as tk
from tkinter import ttk

from . import msg as messagebox

from ..executor import term_usage
from ..util import to_iso, utc_now
from ..warrant import list_warrants, revoke_warrant
from .describe import duration_until, scope_fields
from . import theme
from .widgets import Inspector, Table, local_time, plural, split_panes

STATUS_TONE = {"active": "ok", "expired": "warn", "revoked": "danger", "superseded": "info", "INVALID": "danger"}


RECORD_ONLY = ("The agent can't run anything under this warrant. It's kept as a record so past runs can be traced "
               "to the authorization they used.")


def status_message(w: dict, d: dict, used_this_hour: int = 0) -> tuple[str, str, str]:
    """(heading, text, tone): one message per status, worded the same way for every warrant. An active warrant
    whose hourly limit is used up says so: it's still valid, but nothing can run right now."""
    status = w["status"]
    limit = d.get("max_queries_per_hour")
    if status == "active" and limit and used_this_hour >= limit:
        return ("Active · hourly limit reached", f"All {limit} searches allowed in the last hour have been used, so the "
                f"agent can't search right now; searches resume as the hour rolls over. The warrant stays valid until "
                f"{local_time(d['expires_at'])} ({duration_until(d, utc_now())}).", "warn")
    if status == "active":
        return ("Active", f"The agent can run searches under this warrant until {local_time(d['expires_at'])} "
                          f"({duration_until(d, utc_now())}).", "ok")
    if status == "expired":
        return ("Expired · record only", f"Expired {local_time(d['expires_at'])}. {RECORD_ONLY} Renew… signs a "
                                         "replacement with the same terms.", "warn")
    if status == "superseded":
        return ("Replaced · record only", f"Replaced by {w.get('superseded_by', 'a newer warrant')} when it was edited. "
                                          f"{RECORD_ONLY}", "info")
    if status == "revoked":
        return ("Revoked · record only", f"Revoked before it expired. {RECORD_ONLY}", "danger")
    return (status.capitalize(), RECORD_ONLY, "info")


class WarrantsTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=(12, 10))
        self.app = app
        self.warrants: dict[str, dict] = {}
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="New warrant", style="Accent.TButton", command=app.new_warrant).pack(side="left")
        self.include_archived = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Include revoked and superseded", variable=self.include_archived,
                        command=self.refresh).pack(side="left", padx=16)
        ttk.Label(bar, text="Search").pack(side="left", padx=(8, 0))
        self.search = tk.StringVar()
        ttk.Entry(bar, textvariable=self.search, width=30).pack(side="left", padx=6)
        ttk.Label(bar, text="ID, purpose or hit term", style="Small.TLabel").pack(side="left")
        self.search.trace_add("write", lambda *_: self.refresh())
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        self.inspector = Inspector(panes, "Select a warrant to see exactly what it allows.")
        panes.add(left, weight=1)
        panes.add(self.inspector, weight=1)
        split_panes(panes)  # table and details start at 50/50
        self.table = Table(left, [
            ("id", "Warrant ID", 3, 200, "w"), ("status", "Status", 2, 100, "w"), ("expires", "Expires", 3, 150, "w"),
            ("terms", "Terms", 1, 55, "e"), ("purpose", "Purpose", 5, 160, "w"),
        ], height=18, fit_content={"id": None, "status": None, "expires": None, "terms": None, "purpose": 420})
        self.table.pack(fill="both", expand=True)
        self.table.set_empty("No warrants yet. Use New warrant to compose one.")
        self.table.tree.bind("<<TreeviewSelect>>", lambda _e: self.show())
        self.table.tree.bind("<Double-1>", lambda _e: self.edit())
        p = theme.PALETTE
        self.table.tree.tag_configure("archived", foreground=p["muted"])

    def refresh(self):
        kept = self.table.selected()
        rows = list_warrants(self.app.paths, self.app.public_key, include_archived=self.include_archived.get())
        self.warrants = {w["warrant_id"]: w for w in rows}
        needle = self.search.get().strip().lower()
        if needle:
            def haystack(w):
                d = w["doc"] or {}
                return " ".join([w["warrant_id"], d.get("purpose", ""), *(t["literal"] for t in d.get("terms", []))]).lower()
            rows = [w for w in rows if needle in haystack(w)]
        self.table.clear()
        for w in rows:
            d = w["doc"]
            status = f"superseded" if w["status"] == "superseded" else w["status"]
            self.table.tree.insert("", "end", iid=w["warrant_id"],
                                   tags=("archived",) if w["status"] in ("revoked", "superseded") else (),
                                   values=(w["warrant_id"], status, local_time(d["expires_at"]) if d else "",
                                           len(d["terms"]) if d else "",
                                           " ".join(d["purpose"].split()) if d else w.get("error", "")))
        if needle:
            empty = f"No warrants match \"{needle}\"."
        elif self.include_archived.get():
            empty = "No warrants yet. Use New warrant to compose one."
        else:
            empty = "No active or expired warrants. Tick Include revoked and superseded to see the records."
        self.table.set_empty(empty)
        self.table.restore_view()
        if not self.table.reselect(kept, see=False):
            active = next((w["warrant_id"] for w in rows if w["status"] == "active"), None)
            if not self.table.reselect(active):
                self.inspector.show_empty()

    def select(self, warrant_id: str):
        if warrant_id not in self.warrants or self.search.get():
            self.include_archived.set(True)
            self.search.set("")  # refreshes
            self.refresh()
        self.table.reselect(warrant_id)
        self.show()

    def show(self):
        wid = self.table.selected()
        w = self.warrants.get(wid or "")
        view = self.inspector
        if not w:
            view.show_empty()
            return
        signature = (wid, w["status"], w.get("superseded_by"), json.dumps(w.get("doc"), sort_keys=True),
                     self.app.paths.audit_log.stat().st_mtime_ns if self.app.paths.audit_log.exists() else 0,
                     int(utc_now().timestamp() // 60))  # the minute: expiry and the rolling hour change with time
        if signature == getattr(self, "_shown", None) and view.key == wid:
            return
        self._shown = signature
        view.begin(key=wid)
        d = w["doc"]
        if not d:
            view.title(wid, "Failed verification")
            view.callout("Can't be trusted", w.get("error", ""), "danger")
            return
        status = w["status"]
        view.title(wid, f"Warrant · {status}" + (f" · expires {duration_until(d, utc_now())}" if status == "active" else ""))
        chips = [(status.capitalize(), STATUS_TONE.get(status, "info"))]
        if w.get("superseded_by"):
            chips.append((f"Replaced by {w['superseded_by']}", "info"))
        if d.get("supersedes"):
            chips.append((f"Replaces {d['supersedes']}", "info"))
        view.chips(chips)
        used = 0
        if d.get("max_queries_per_hour") and status == "active":
            from ..executor import queries_in_last_hour

            used = queries_in_last_hour(self.app.paths, wid)
        view.callout(*status_message(w, d, used))
        view.section("What it allows")
        scope_fields(view, d, self.app.rbac, self.app.settings)
        if status == "active":
            view.para("The agent may combine the included terms below with any enabled library template, under these "
                      "parameters and limits, until expiry. New syntax needs a one-time approval.", style="Muted.TLabel")
        else:
            view.para("While it was active, the agent could combine the included terms below with any enabled library "
                      "template, under these parameters and limits. New syntax needed a one-time approval.",
                      style="Muted.TLabel")
        if d.get("max_queries_per_hour") and status == "active":
            view.field("Used in the last hour", f"{used} of {d['max_queries_per_hour']} searches",
                       style="Warn.TLabel" if used >= d["max_queries_per_hour"] else "TLabel")
        view.section("Purpose")
        view.para(d["purpose"])
        view.para("Recorded justification; not enforced.", style="Small.TLabel")
        view.section("Certifications")
        certs = d.get("certifications")
        if certs:
            for c in certs:
                view.para("\u2611 " + c["text"])
            view.para(f"Accepted {local_time(certs[0].get('accepted_at'))}" if certs[0].get("accepted_at") else "",
                      style="Small.TLabel")
        else:
            view.para("None recorded: signed before certifications existed.", style="Muted.TLabel")
        context = d.get("config_context")
        view.field("Configuration", f"{context['values']['organization']['name']} rev. {context['values']['organization']['revision']} "
                   f"(hash {context['sha256'][:16]}…)" if context else "Not captured (signed before configuration snapshots)")
        usage = term_usage(self.app.paths, wid)
        view.section(f"{self.app.settings['terms']['hit_term']['plural'].capitalize()} and usage")
        for t in d["terms"]:
            u = usage.get(t["term_id"], {})
            last = f", last {local_time(u['last_used'])}" if u.get("last_used") else ""
            view.para(term_label(t), font="LW.Strong")
            view.para(matching(t), style="Muted.TLabel")
            client = f"{self.app.settings['terms']['client_id']['singular']} {t['client_id']} · " if t.get("client_id") else ""
            view.para(f"{client}{t['term_id']} · {t['source']} · used in {plural(u.get('queries', 0), 'search')} "
                      f"({u.get('records', 0)} results in those searches, which may also match other terms), "
                      f"{plural(u.get('refused', 0), 'blocked attempt')}{last}", style="Muted.TLabel")
        approved = {t["term_id"] for t in d["terms"]}
        for term_id, u in sorted(usage.items()):
            if term_id not in approved:
                view.field(term_id, f"NOT APPROVED · {plural(u['refused'], 'blocked attempt')}", style="Danger.TLabel")
        view.section("Record")
        view.field("Warrant ID", wid, mono=True, copy=True)
        view.field("Signed", local_time(d["created_at"]))
        view.field("Expires", local_time(d["expires_at"]))
        if status == "active":
            view.action("Edit…", self.edit, accent=True)
            view.action("Revoke…", self.revoke)
        elif status == "expired":
            view.action("Renew…", self.edit, accent=True)
        elif w.get("superseded_by"):
            view.action(f"Open {w['superseded_by']}", lambda: self.select(w["superseded_by"]))
        view.action("Print / Save as PDF…", lambda: self.print_warrant(wid))
        view.action("Runs under this warrant  →", lambda: self.app.show_runs(warrant=wid))

    def print_warrant(self, warrant_id: str):
        """Export the signed warrant as a formatted HTML document and open it for printing or saving as PDF."""
        import webbrowser

        w = self.warrants.get(warrant_id)
        if not w or not w["doc"]:
            return
        html = self.export_html(w)
        path = self.app.paths.data / "exports" / f"{warrant_id}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, "utf-8")
        webbrowser.open(path.resolve().as_uri())
        self.app.notify(f"Opened {path.name} in your browser: use Print, then Save as PDF")

    def export_html(self, w: dict) -> str:
        from ..scope import countries
        from ..signing import key_id
        from .document import render_html, warrant_blocks

        d, settings = w["doc"], self.app.settings
        try:
            p1 = {o.value: o.label for o in self.app.rbac.parameter_1_options()}
            p2 = {o.value: o.label for o in self.app.rbac.parameter_2_options(d["parameter_1"])}
        except Exception:  # noqa: BLE001 - labels are decoration
            p1, p2 = {}, {}
        context = (d.get("config_context") or {}).get("values", settings)  # the wording it was signed under
        status = {"active": "ACTIVE", "expired": "EXPIRED", "revoked": "REVOKED",
                  "superseded": f"SUPERSEDED BY {w.get('superseded_by', '')}"}.get(w["status"], w["status"].upper())
        previous = next((r["doc"] for r in list_warrants(self.app.paths, self.app.public_key, include_archived=True)
                         if r["warrant_id"] == d.get("supersedes") and r.get("doc")), {}) if d.get("supersedes") else {}
        old_terms = previous.get("terms", [])
        old_literals = {t["literal"] for t in old_terms}
        literals = {t["literal"] for t in d["terms"]}
        blocks = warrant_blocks(
            d, labels={"p1": p1, "p2": p2}, countries=countries(self.app.paths),
            library_line="the organization's signed template library", status=f"SIGNED · {status}",
            replaces=d.get("supersedes"), sources=context.get("source_labels"),
            param_names=(context["parameters"]["parameter_1"]["label"], context["parameters"]["parameter_2"]["label"]),
            terms=context.get("terms"), old_terms=old_terms, old_places=previous.get("places", []),
            new_literals=literals - old_literals if previous else set(), removed=sorted(old_literals - literals),
            signature={"key_id": key_id(self.app.public_key),
                       "status": "Ed25519 signature verified against this key when this copy was printed"})
        from ..executor import audit_problems, payload_record, read_audit, run_term_ids
        from ..summaries import read_summary
        from .document import clean_run, runs_addendum

        printed_at = local_time(to_iso(utc_now()))
        runs = []
        for entry in read_audit(self.app.paths, limit=None):
            if entry.get("warrant_id") != d["warrant_id"]:
                continue
            record, problem = payload_record(self.app.paths, entry.get("run_id"))
            runs.append(clean_run(entry, record, problem, run_term_ids(self.app.paths, entry),
                                  read_summary(self.app.paths, entry.get("run_id"))))
        # The footer closes the warrant itself, before the addendum, so it never sits alone on a final page.
        blocks.append(("footer", f"Printed {printed_at} from {self.app.settings['organization']['name']} · Lucid Warrant. "
                                 f"The signed record is {self.app.paths.warrants / (d['warrant_id'] + '.json')} "
                                 "(or its archive)."))
        blocks += runs_addendum(d, runs, printed_at=printed_at, terms=context.get("terms"),
                                log_problems=audit_problems(self.app.paths))
        return render_html(blocks)

    def edit(self):
        w = self.warrants.get(self.table.selected() or "")
        if not w or not w["doc"] or w["status"] not in ("active", "expired"):
            return
        self.app.edit_warrant(w["warrant_id"], w["doc"], expired=w["status"] == "expired")

    def revoke(self):
        wid = self.table.selected()
        w = self.warrants.get(wid or "")
        if not w or w["status"] not in ("active", "expired"):
            return
        if messagebox.askyesno(
            "Revoke warrant",
            f"Revoke {wid}?\n\nThe agent can't start new searches under it, and one-time approvals tied to it stop "
            "working. A search already in progress is not stopped. The warrant stays listed as a record.",
            parent=self,
        ):
            revoke_warrant(self.app.paths, wid)
            self.app.notify(f"Revoked {wid}")
            self.app.refresh_all()
