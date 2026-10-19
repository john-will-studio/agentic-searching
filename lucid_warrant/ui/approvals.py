"""Approvals: every syntax request through its lifecycle, in one list.

needs review → (approve) → ready → (run) → used → (optionally) promoted
             → (reject)  → rejected
A ready approval becomes unavailable if it expires or its warrant is revoked
or superseded. Eligibility, execution and library relationship are shown as
separate facts.
"""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk

from . import msg as messagebox

from ..executor import ledger_problems, read_audit, used_onetime_ids
from ..library import load_library, promote_template
from ..onetime import (
    approve_request,
    lint_request,
    list_onetime,
    list_requests,
    preview,
    reject_request,
    warrant_digest,
)
from ..syntax import SyntaxRejected, explain, parse_skeleton, render, slots, suggest_name
from ..util import check_id, from_iso, utc_now
from ..warrant import list_warrants, resolve_bindings
from .describe import scope_fields
from .widgets import Inspector, PromoteDialog, Table, lint_badge, local_time, plural

FILTERS = ["Needs review", "Ready", "Used", "Rejected", "Unavailable", "All"]
MAX_AGENT_TEXT = 600


def _name(item: dict) -> str:
    try:
        return suggest_name(parse_skeleton(item["skeleton"]))
    except (SyntaxRejected, KeyError, TypeError):
        return "(unreadable syntax)"


def _bounded(text: str) -> str:
    """Agent text, flattened and capped so it can't impersonate the app's own layout."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= MAX_AGENT_TEXT else flat[:MAX_AGENT_TEXT] + " … [truncated]"


class ApprovalsTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=(12, 10))
        self.app = app
        self.items: dict[str, dict] = {}
        self.reviewed: dict[str, tuple] = {}
        self.ledger_broken = False

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        self.filter = tk.StringVar(value="Needs review")
        self.filter_buttons = {}
        for name in FILTERS:
            button = ttk.Radiobutton(bar, text=name, value=name, variable=self.filter, style="Toggle.TButton",
                                     command=self.render)
            button.pack(side="left", padx=(0, 4))
            self.filter_buttons[name] = button

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        self.inspector = Inspector(panes, "Select a request or approval to see what it would do, the checks it passed, "
                                          "and the agent's reasoning.")
        panes.add(left, weight=3)
        panes.add(self.inspector, weight=4)
        self.table = Table(left, [
            ("name", "Name", 4, 180, "w"), ("status", "Status", 4, 170, "w"), ("lint", "Checks", 2, 100, "w"),
            ("when", "When", 3, 130, "w"), ("library", "Library", 2, 100, "w"),
        ], height=18, fit_content={"name": 340, "status": None, "lint": None, "when": None, "library": 320})
        self.table.pack(fill="both", expand=True)
        self.table.tree.bind("<<TreeviewSelect>>", lambda _e: self.show())
        self._sash_set = False

        def first_map(_event=None):
            if not self._sash_set and panes.winfo_width() > 1:
                panes.sashpos(0, int(panes.winfo_width() * 0.45))
                self._sash_set = True
        panes.bind("<Configure>", first_map, add="+")

    # --- data --------------------------------------------------------------------------

    def refresh(self):
        paths, pub = self.app.paths, self.app.public_key
        self.warrants = {w["warrant_id"]: w for w in list_warrants(paths, pub, include_archived=True)}
        try:
            self.library = load_library(paths, pub)
            promoted = {t.get("origin", "").removeprefix("promoted from "): t for t in self.library["templates"]}
        except Exception:  # noqa: BLE001
            self.library, promoted = None, {}
        attempts: dict[str, list] = {}
        for entry in read_audit(paths, limit=None):
            if entry.get("kind") == "onetime":
                attempts.setdefault(entry.get("onetime_id"), []).append(entry)
        used = used_onetime_ids(paths)
        self.ledger_broken = bool(ledger_problems(paths))
        items: dict[str, dict] = {}

        for r in list_requests(paths):
            if r.get("malformed"):
                items["req:" + r["file"].removesuffix(".json")] = {"kind": "request", "doc": r, "stage": "Needs review",
                                             "status": "Malformed request file", "lint": None, "when": ""}
                continue
            try:
                relint = lint_request(paths, pub, r["warrant_id"], r["skeleton"], r["bindings"], "review").to_dict()
            except Exception as exc:  # noqa: BLE001 - shown in the inspector
                relint = {"passed": False, "summary": f"cannot check: {exc}", "findings": []}
            mismatch = bool(r.get("lint")) and r["lint"].get("passed") != relint["passed"]
            status = "Needs review" if relint["passed"] else "Needs review · checks failed"
            if mismatch:
                status += " · recorded result differs"
            stage = "Needs review"
            parent = self.warrants.get(r["warrant_id"])
            if not parent or parent["status"] != "active":
                # Its warrant can no longer be used, so approving it would be pointless: the agent must re-request.
                state = parent["status"] if parent else "missing"
                stage = "Unavailable"
                status = f"Unavailable · warrant {state}" + (
                    f"; re-request under {parent['superseded_by']}" if parent and parent.get("superseded_by") else "")
            items["req:" + r["request_id"]] = {"kind": "request", "doc": r, "stage": stage, "status": status,
                                               "lint": relint, "mismatch": mismatch, "when": r.get("submitted_at")}

        rejected_dir = paths.syntax_requests / "rejected"
        for path in sorted(rejected_dir.glob("req-*.json")) if rejected_dir.exists() else ():
            try:
                r = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                r = None
            if not isinstance(r, dict):  # a rejected file may be malformed; it's still a record
                r = {"malformed": True}
            # Keyed by file name, never by the ID inside the file, so records can't collide.
            r = {**r, "request_id": path.stem, "file": path.name}
            when = r.get("submitted_at") if isinstance(r.get("submitted_at"), str) else None
            items["rej:" + path.stem] = {"kind": "rejected", "doc": r, "stage": "Rejected", "status": "Rejected",
                                         "lint": None, "when": when}

        now = utc_now()
        for o in list_onetime(paths, pub):
            oid = o["onetime_id"]
            tries = attempts.get(oid, [])
            blocked = sum(e["outcome"] == "denied" for e in tries)
            if "invalid" in o:
                stage, status = "Unavailable", "Invalid signature"
            elif oid in used:
                last = next((e for e in reversed(tries) if e["outcome"] in ("ok", "error")), None)
                stage = "Used"
                if last and last["outcome"] == "ok":
                    status = f"Used · completed ({plural(last.get('count', 0), 'record')})"
                elif last:
                    status = "Used · service error"
                else:
                    status = "Used · outcome unknown"
            else:
                parent = self.warrants.get(o["warrant_id"])
                if not parent or parent["status"] == "INVALID":
                    stage, status = "Unavailable", "Unavailable · warrant missing"
                elif parent["status"] in ("superseded", "revoked"):
                    stage, status = "Unavailable", f"Unavailable · warrant {parent['status']}"
                elif parent["status"] == "expired" or now >= from_iso(o["expires_at"]):
                    stage, status = "Unavailable", "Expired"
                else:
                    blocker = self._blocker(o, parent["doc"])
                    stage, status = ("Unavailable", f"Unavailable · {blocker}") if blocker else ("Ready", "Ready to run once")
                if blocked:
                    status += f" · {plural(blocked, 'blocked attempt')}"
            items["ot:" + oid] = {"kind": "onetime", "doc": o, "stage": stage, "status": status, "lint": o.get("lint"),
                                  "when": o.get("approved_at"), "attempts": tries, "promoted": promoted.get(oid)}
        self.items = items
        counts = {name: sum(i["stage"] == name for i in items.values()) for name in FILTERS}
        counts["All"] = len(items)
        for name, button in self.filter_buttons.items():
            button.configure(text=f"{name} ({counts[name]})")
        self.needs_review = counts["Needs review"]
        self.render()

    def _blocker(self, o: dict, warrant: dict) -> str | None:
        """The first reason the executor would refuse this approval right now, mirroring its checks."""
        try:
            self.app.rbac.check(warrant["parameter_1"], warrant["parameter_2"])
        except Exception as exc:  # noqa: BLE001
            return f"entitlement no longer valid ({exc})"
        if not self.library:
            return "template library unusable"
        try:
            from ..syntax import compile_query

            current = compile_query(o["skeleton"], o["literals"], default_field=self.library["default_field"],
                                    allowed_fields=self.library["allowed_fields"]).query
        except Exception as exc:  # noqa: BLE001
            return f"can't be rendered any more ({exc})"
        if current != o["query"]:
            return "no longer renders as signed (library fields changed)"
        try:
            literals = resolve_bindings(warrant, o["bindings"])
        except Exception:  # noqa: BLE001
            literals = None
        if literals != o["literals"]:
            return "its warrant no longer approves these terms"
        if self.ledger_broken:
            return "single-use ledger unreadable"
        return None

    def render(self):
        kept = self.table.selected()
        self.table.clear()
        wanted = self.filter.get()
        rows = [(iid, i) for iid, i in self.items.items() if wanted == "All" or i["stage"] == wanted]
        rows.sort(key=lambda r: r[1].get("when") or "", reverse=True)
        for iid, item in rows:
            library = f"template: {item['promoted']['template_id']}" if item.get("promoted") else (
                "not promoted" if item["kind"] == "onetime" else "")
            checks = lint_badge(item["lint"]) if item["kind"] != "rejected" else ""
            self.table.tree.insert("", "end", iid=iid, values=(
                _name(item["doc"]), item["status"], checks, local_time(item["when"]), library))
        self.table.set_empty({
            "Needs review": "Nothing needs review. New syntax requests from the agent appear here automatically.",
            "Ready": "No approved queries are waiting to run.",
            "Used": "No one-time approvals have been used yet.",
            "Rejected": "No rejected requests.",
            "Unavailable": "Nothing is unavailable.",
        }.get(wanted, "No syntax requests yet."))
        self.table.restore_view()
        if not self.table.reselect(kept, see=False):
            self.inspector.show_empty()

    def select(self, iid: str):
        if iid in self.items:
            self.filter.set(self.items[iid]["stage"])
            self.render()
            self.table.reselect(iid)
            self.show()

    # --- inspector -------------------------------------------------------------------

    def show(self):
        iid = self.table.selected()
        if not iid or iid not in self.items:
            self.inspector.show_empty()
            return
        item = self.items[iid]
        parent = self.warrants.get(item["doc"].get("warrant_id", ""), {})
        signature = (iid, item["status"], item.get("when"), len(item.get("attempts", [])),
                     (item.get("promoted") or {}).get("template_id"), json.dumps(item["doc"], sort_keys=True, default=str),
                     (self.library or {}).get("version"), parent.get("status"),
                     json.dumps(parent.get("doc"), sort_keys=True, default=str))
        if signature == getattr(self, "_shown", None) and self.inspector.key == iid:
            return  # live refresh of the same, unchanged item: leave the pane as the user has it
        self._shown = signature
        {"request": self._show_request, "onetime": self._show_onetime, "rejected": self._show_rejected}[item["kind"]](iid, item)

    def _meaning_and_syntax(self, view, skeleton, literals):
        library = self.library
        if not library:
            view.callout("Template library unavailable", "Can't render this query until the library verifies.", "danger")
            return None
        try:
            node = parse_skeleton(skeleton, library["allowed_fields"])
        except SyntaxRejected as exc:
            view.callout("Unsupported syntax", str(exc), "danger")
            return None
        view.section("What it does")
        view.para(explain(node, literals, library["default_field"]), font="LW.Strong")
        view.para("Generated from the query itself, not written by the agent.", style="Small.TLabel")
        return node

    def _syntax(self, view, skeleton, node, literals):
        if node is None:
            return
        view.section("Syntax")
        view.field("Skeleton", skeleton, mono=True)
        view.field("Lucene", "")
        view.code(render(node, literals, self.library["default_field"]))

    def _checks(self, view, report, title):
        view.field(title, lint_badge(report) if report else "not recorded")
        for f in (report or {}).get("findings", []):
            view.field(f"  {f['level']} {f['code']}", f["message"], style="Danger.TLabel" if f["level"] == "error" else "Warn.TLabel")

    def _show_request(self, iid, item):
        view, r = self.inspector, item["doc"]
        view.begin(key=iid)
        if r.get("malformed"):
            view.title("Malformed request file", r["file"])
            view.callout("Can't be reviewed", r.get("problem", "unreadable"), "danger")
            view.action("Reject", lambda: self.reject(r["request_id"], file=r["file"]))
            return
        try:
            item["lint"] = lint_request(self.app.paths, self.app.public_key, r["warrant_id"], r["skeleton"],
                                        r["bindings"], "review").to_dict()
        except Exception as exc:  # noqa: BLE001
            item["lint"] = {"passed": False, "summary": f"cannot check: {exc}", "findings": [], "checked_at": None}
        if item["stage"] == "Unavailable":
            view.title(_name(r), "Syntax request from the agent · unavailable")
            view.chips([("Unavailable", "danger"), (lint_badge(item["lint"]), "ok" if item["lint"]["passed"] else "danger")])
            view.callout("Can't be approved", item["status"].removeprefix("Unavailable · ").capitalize()
                         + ". A request is tied to the warrant it was made under; the agent must make it again under "
                           "an active warrant.", "danger")
        else:
            view.title(_name(r), "Syntax request from the agent · needs your review")
            view.chips([("Needs review", "warn"), (lint_badge(item["lint"]), "ok" if item["lint"]["passed"] else "danger")])
        warrant = self.warrants.get(r["warrant_id"], {})
        approvable = item["lint"]["passed"]
        try:
            _, compiled = preview(self.app.paths, self.app.public_key, r["warrant_id"], r["skeleton"], r["bindings"])
            literals = {slot: next(t["literal"] for t in warrant["doc"]["terms"] if t["term_id"] == tid)
                        for slot, tid in r["bindings"].items()}
        except Exception as exc:  # noqa: BLE001
            view.callout("Can't be approved", str(exc), "danger")
            compiled, literals, approvable = None, None, False
        node = self._meaning_and_syntax(view, r["skeleton"], literals) if literals else None
        if literals:
            view.section("Hit terms it uses")
            for slot, tid in r["bindings"].items():
                view.field(slot, f"\"{literals[slot]}\"   ({tid})")
        if warrant.get("doc"):
            view.section(f"Would run under {r['warrant_id']}")
            view.field("Warrant status", warrant["status"])
            scope_fields(view, warrant["doc"], self.app.rbac, self.app.settings)
        view.section("Checks")
        self._checks(view, item["lint"], f"Re-checked {local_time(item['lint'].get('checked_at')) or 'now'}")
        self._checks(view, r.get("lint"), "Recorded at nomination")
        if item.get("mismatch"):
            view.callout("Recorded result differs", "The result stored in the request file doesn't match the re-check. "
                         "Trust the re-check; the file is writable by the agent.", "danger")
        view.callout("Agent's rationale — its words, not verified", _bounded(r.get("rationale")) or "(none given)", "agent")
        self._syntax(view, r["skeleton"], node, literals or {})
        view.section("Record")
        view.field("Request ID", r["request_id"], mono=True, copy=True)
        view.field("Submitted", local_time(r.get("submitted_at")))
        if compiled is not None and approvable:
            self.reviewed[r["request_id"]] = (compiled.query, self.library["version"] if self.library else None,
                                              warrant_digest(warrant["doc"]))
        view.action("Approve for one run…", lambda: self.approve(r, item), accent=True, enabled=approvable)
        view.action("Reject", lambda: self.reject(r["request_id"], file=r.get("file")))
        view.action_note("Approving allows exactly one run. If it works you can promote it to the library.")

    def _show_onetime(self, iid, item):
        view, o = self.inspector, item["doc"]
        view.begin(key=iid)
        if "invalid" in o:
            view.title("Invalid one-time approval", o["onetime_id"])
            view.callout("Signature check failed", o["invalid"], "danger")
            return
        view.title(_name(o), "One-time approval")
        tone = {"Ready": "ok", "Used": "info", "Unavailable": "danger"}[item["stage"]]
        chips = [(item["status"], tone)]
        chips.append((f"Template: {item['promoted']['name']}", "info") if item.get("promoted") else ("Not promoted", "info"))
        view.chips(chips)
        view.section("What it does")
        view.para(explain(parse_skeleton(o["skeleton"]), o["literals"], self.library["default_field"]) if self.library
                  else o.get("structure", ""), font="LW.Strong")
        view.para("Generated from the signed query.", style="Small.TLabel")
        view.section("Hit terms it uses")
        for slot, literal in o["literals"].items():
            view.field(slot, f"\"{literal}\"   ({o['bindings'][slot]})")
        view.section("Execution")
        if not item["attempts"]:
            view.para("Not attempted yet." if item["stage"] == "Ready" else "No attempts recorded.", style="Muted.TLabel")
        for e in reversed(item["attempts"]):
            outcome = {"ok": f"Completed · {plural(e.get('count', 0), 'record')}", "denied": "Blocked before sending",
                       "error": "Service error"}.get(e["outcome"], e["outcome"])
            if e.get("run_id"):
                view.link(f"{local_time(e['ts'])} · {outcome} · {e['run_id']}  →  view run",
                          lambda rid=e["run_id"]: self.app.show_run(rid))
            else:
                view.field(local_time(e["ts"]), outcome)
        warrant = self.warrants.get(o["warrant_id"], {})
        view.section(f"Warrant {o['warrant_id']}")
        view.field("Status", warrant.get("status", "missing")
                   + (f" (replaced by {warrant['superseded_by']})" if warrant.get("superseded_by") else ""))
        view.link("Open warrant  →", lambda: self.app.show_warrant(o["warrant_id"]))
        view.section("Checks")
        self._checks(view, o.get("lint"), "Lint at approval (signed)")
        view.para("Every run is also checked when fired; see each run's details.", style="Small.TLabel")
        view.section("Signed query")
        view.field("Skeleton", o["skeleton"], mono=True)
        view.field("Structure", o.get("structure", ""))
        view.field("Lucene (as signed)", "")
        view.code(o["query"])
        view.para("This exact query is what runs. Date limits from the warrant are added when it's sent.", style="Small.TLabel")
        view.section("Record")
        view.field("Approval ID", o["onetime_id"], mono=True, copy=True)
        view.field("From request", o.get("request_id", ""), mono=True)
        view.field("Approved", local_time(o.get("approved_at")))
        view.field("Expires", local_time(o.get("expires_at")))
        if item.get("promoted"):
            view.action("Open template", lambda: self.app.show_template(item["promoted"]["template_id"]))
        else:
            view.action("Promote to library…", lambda: self.promote(o, item), accent=True, enabled=item["stage"] == "Used")
            view.action_note("Promotion makes this syntax reusable under every active warrant."
                             if item["stage"] == "Used" else "Available after the approval has been used.")

    def _show_rejected(self, iid, item):
        view, r = self.inspector, item["doc"]
        view.begin(key=iid)
        view.title(_name(r) if not r.get("malformed") else "Malformed request file", "Rejected syntax request")
        view.chips([("Rejected", "danger")])
        view.callout("Agent's rationale — its words, not verified", _bounded(r.get("rationale")) or "(none given)", "agent")
        view.section("Syntax")
        view.field("Skeleton", str(r.get("skeleton", "")), mono=True)
        view.section("Record")
        view.field("Request ID", r["request_id"], mono=True, copy=True)
        view.field("Submitted", local_time(r.get("submitted_at")))

    # --- actions -------------------------------------------------------------------------

    def approve(self, request, item):
        rid = request["request_id"]
        if rid not in self.reviewed:
            return
        query, version, digest = self.reviewed[rid]
        warrant = self.warrants.get(request["warrant_id"], {}).get("doc", {})
        try:
            node = parse_skeleton(request["skeleton"], self.library["allowed_fields"])
            literals = {slot: next(t["literal"] for t in warrant["terms"] if t["term_id"] == tid)
                        for slot, tid in request["bindings"].items()}
            meaning = explain(node, literals, self.library["default_field"])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Approve", str(exc), parent=self)
            return
        summary = (
            f"ONE-TIME APPROVAL for request {rid}\n\n"
            f"What it does: {meaning}\n"
            f"Lucene:       {query}\n\n"
            f"Runs once under {request['warrant_id']} (authorization {warrant.get('parameter_1')}, "
            f"visibility {', '.join(warrant.get('parameter_2', []))}), with that warrant's limits and date rules.\n"
            f"Checks: {lint_badge(item['lint'])}\n\n"
            "If the query or the template library changes before you sign, signing is refused."
        )
        result = self.app.sign("Approve one-time syntax", summary, lambda key: approve_request(
            self.app.paths, key, request, reviewed_query=query, reviewed_library_version=version,
            reviewed_warrant_digest=digest))
        if result:
            self.app.notify(f"Approved {result['onetime_id']} for one run")
            self.app.refresh_all()
            self.select("ot:" + result["onetime_id"])

    def reject(self, request_id, file=None):
        if messagebox.askyesno("Reject", f"Reject request {request_id}? The agent will have to ask again.", parent=self):
            try:
                reject_request(self.app.paths, request_id, file=file)
            except ValueError as exc:
                messagebox.showerror("Reject", str(exc), parent=self)
                return
            self.app.refresh_all()

    def promote(self, o, item):
        if not self.library:
            return
        node = parse_skeleton(o["skeleton"], self.library["allowed_fields"])
        default_field = self.library["default_field"]
        plain = explain(node, None, default_field)
        shape = render(node, {slot: "{" + slot + "}" for slot in slots(node)}, default_field)
        completed = [e for e in item["attempts"] if e["outcome"] == "ok"]
        if not completed and not messagebox.askyesno("Promote", "This query didn't complete successfully. Promote it anyway?", parent=self):
            return
        answer = PromoteDialog.ask(self, self.library, o["skeleton"], plain, shape, suggest_name(node))
        if not answer:
            return
        name, description, template_id = answer
        summary = (
            f"ADD TEMPLATE TO LIBRARY (v{self.library['version']} -> v{self.library['version'] + 1})\n\n"
            f"Name:            {name}\nDescription:     {description}\nWhat it does:    {plain}\n"
            f"Lucene:          {shape}\nAgent refers to: {template_id}\nOrigin:          promoted from {o['onetime_id']}\n\n"
            "CHANGE IN AUTHORITY: before, one approved run of one query with fixed terms. After, the agent may run "
            "this template with the approved terms of ANY active warrant, without asking. You can disable it later."
        )
        reviewed = self.library["version"]
        done = self.app.sign("Promote template", summary, lambda key: promote_template(
            self.app.paths, key, skeleton=o["skeleton"], template_id=template_id, name=name, description=description,
            origin=f"promoted from {o['onetime_id']}", expected_version=reviewed))
        if done:
            self.app.notify(f"Template library is now v{done['version']}")
            self.app.refresh_all()
