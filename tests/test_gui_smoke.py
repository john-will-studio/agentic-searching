"""GUI smoke tests: drive the real Tk widgets with real clicks and keys.

Only modal dialogs are patched. Every click checks the widget is still under the
pointer between press and release (a moved widget means a lost click), and any
exception raised inside a Tk callback fails the test.

Each test runs in its own Python process (see run_isolated below): Tk 9 on macOS
can crash when a second Tk interpreter is created in the same process, and the
app only ever creates one.
"""

from __future__ import annotations

import json
from pathlib import Path
import os
import time
from datetime import timedelta
import tkinter as tk
from tkinter import ttk
import traceback
from tkinter import simpledialog

from lucid_warrant.ui import msg as messagebox
from unittest import mock

import pytest

from tests.conftest import sign_org_file

from lucid_warrant import signing
from lucid_warrant.config import SEED_DIR, Paths
from lucid_warrant.executor import read_audit, run_template
from lucid_warrant.library import load_library, sign_seed_library
from lucid_warrant.onetime import list_onetime, list_requests, submit_request
from lucid_warrant.rbac import get_rbac
from lucid_warrant.review import load_rejected
from lucid_warrant.signing import generate_keypair, load_private_key, load_public_key
from lucid_warrant.util import utc_now
from lucid_warrant.warrant import build_warrant, list_warrants, revoke_warrant, save_warrant

PASSPHRASE = "smoke passphrase"

CHILD = os.environ.get("LW_GUI_CHILD") == "1"
pytestmark = pytest.mark.gui


# --- harness --------------------------------------------------------------------------

class Harness:
    def __init__(self, paths: Paths):
        from lucid_warrant.ui import app as appmod
        from lucid_warrant.ui import widgets

        self.paths = paths
        self.errors: list[str] = []
        self.prompts: list[str] = []
        self.answers: dict[str, list] = {"askstring": [], "askyesno": [], "range": [], "promote": [], "reject": []}
        self.shown: list[tuple[str, str]] = []
        self.documents: list = []
        self.patches = [
            mock.patch.object(appmod.App, "_unlock_or_setup", return_value=True),
            mock.patch.object(widgets.PassphraseDialog, "ask", side_effect=self._passphrase),
            mock.patch.object(widgets.DateRangeDialog, "ask", side_effect=lambda m, **_k: self.answers["range"].pop(0)),
            mock.patch.object(widgets.PromoteDialog, "ask", side_effect=self._promote),
            mock.patch.object(simpledialog, "askstring", side_effect=lambda *a, **k: self.answers["askstring"].pop(0)),
            mock.patch.object(widgets.TextDialog, "ask", side_effect=lambda *a, **k: self.answers["askstring"].pop(0)
                              if self.answers["askstring"] else ""),  # unanswered: left blank, as an analyst might
            mock.patch.object(widgets.RejectDialog, "ask", side_effect=lambda *a, **k: self.answers["reject"].pop(0)
                              if self.answers["reject"] else {"reason": "", "minutes": 30 * 1440}),
            mock.patch.object(messagebox, "askyesno", side_effect=lambda *a, **k: self.answers["askyesno"].pop(0)
                              if self.answers["askyesno"] else True),
        ]
        for name in ("showinfo", "showerror", "showwarning", "askokcancel"):
            self.patches.append(mock.patch.object(messagebox, name, side_effect=lambda *a, n=name, **k: self.shown.append(
                (n, a[1] if len(a) > 1 else "")) or True))
        for p in self.patches:
            p.start()
        self._old_report = tk.Tk.report_callback_exception
        tk.Tk.report_callback_exception = lambda _self, *exc: self.errors.append("".join(traceback.format_exception(*exc)))
        self.app = appmod.App(paths)
        self.app.geometry("1200x720")
        self.pump()

    def _passphrase(self, master, title, summary="", confirm=False, document=None):
        from lucid_warrant.ui.document import render_html

        self.prompts.append(render_html(document) if document else summary)
        self.documents.append(document)
        return PASSPHRASE

    def _promote(self, master, library, skeleton, plain, shape, default_name=""):
        from lucid_warrant.library import suggest_template_id

        name = self.answers["promote"].pop(0) if self.answers["promote"] else default_name
        return name, plain, suggest_template_id(library, name)

    def close(self):
        try:
            self.app.destroy()
        finally:
            tk.Tk.report_callback_exception = self._old_report
            for p in self.patches:
                p.stop()

    def pump(self, seconds: float = 0.0):
        end = time.monotonic() + seconds
        while True:
            self.app.update()
            if time.monotonic() >= end:
                break
            time.sleep(0.02)

    def pump_until(self, condition, seconds: float = 2.0) -> bool:
        """Pump events until condition() holds (or the time runs out). For settling layout, where a fixed pause is
        enough on one machine but not another (Windows relays out more slowly than macOS)."""
        end = time.monotonic() + seconds
        while not condition():
            if time.monotonic() >= end:
                return False
            self.pump(0.02)
        return True

    def click(self, widget):
        """A real click: press, check the widget hasn't moved, release."""
        self.pump()
        assert widget.winfo_ismapped(), f"{widget} isn't visible"
        assert str(widget.cget("state")) != "disabled", f"{widget.cget('text')!r} is disabled"
        x, y = widget.winfo_width() // 2, widget.winfo_height() // 2
        before = (widget.winfo_rootx(), widget.winfo_rooty())
        widget.event_generate("<ButtonPress-1>", x=x, y=y)
        self.pump()
        after = (widget.winfo_rootx(), widget.winfo_rooty())
        assert before == after, f"{widget.cget('text')!r} moved under the pointer during the click ({before} -> {after})"
        widget.event_generate("<ButtonRelease-1>", x=x, y=y)
        self.pump()

    def key(self, widget, keysym: str):
        """Press and release a key, as a person would (the review keys act once per physical press)."""
        from lucid_warrant.ui import widgets

        widget.focus_force()
        self.pump()
        widget.event_generate(f"<KeyPress-{keysym}>")
        self.pump()
        widget.event_generate(f"<KeyRelease-{keysym}>")
        self.pump((widgets.REPEAT_PAIR_MS + 20) / 1000)

    def tab(self) -> str:
        return self.app.notebook.tab(self.app.notebook.select(), "text").strip()


def find(parent, cls=ttk.Button, text: str = "", *, mapped: bool = True):
    """The first descendant of class cls whose text contains `text`."""
    parent.update()  # draw anything created since the last event loop pass
    stack = [parent]
    while stack:
        widget = stack.pop(0)
        for child in widget.winfo_children():
            stack.append(child)
            if isinstance(child, cls) and text in str(child.cget("text") if "text" in child.keys() else ""):
                if not mapped or child.winfo_ismapped():
                    return child
    raise AssertionError(f"no {cls.__name__} with text containing {text!r} under {parent}")


def texts(parent) -> str:
    out, stack = [], [parent]
    while stack:
        widget = stack.pop()
        stack.extend(widget.winfo_children())
        if "text" in widget.keys():
            out.append(str(widget.cget("text")))
    return "\n".join(out)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(signing, "SCRYPT_N", signing.MIN_SCRYPT_N)
    monkeypatch.setenv("LUCID_WARRANT_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.delenv("LUCID_WARRANT_ORG_CONFIG", raising=False)
    p = Paths(keys=tmp_path / "keys", data=tmp_path / "data", seed=SEED_DIR,
              corpus_file=Path(__file__).parent / "fixtures" / "mock_corpus_fixed.json")
    monkeypatch.setenv("LUCID_WARRANT_KEYS", str(p.keys))
    monkeypatch.setenv("LUCID_WARRANT_DATA", str(p.data))
    generate_keypair(p, PASSPHRASE)
    sign_seed_library(p, load_private_key(p, PASSPHRASE))
    p.inbox.mkdir(parents=True)
    p.nominations.write_text(json.dumps({"terms": [
        {"term_id": "t-cedar", "literal": "PROJECT_CEDAR", "provenance": "intake", "client_id": "CL-100482"},
        {"term_id": "t-north", "literal": "SUPPLIER_NORTH", "provenance": "intake"},
        {"term_id": "t-alias", "literal": "NORTH_SUPPLY_CO", "provenance": "vendor master"},
        {"term_id": "t-test", "literal": "TEST_DATA", "provenance": "exclusion"},
    ]}))
    p.proposal.write_text(json.dumps({"purpose": "Find records for DEMO-42.",
                                      "terms": [{"literal": "CEDAR_PHASE_TWO", "reason": "invoice title"}]}))
    return p


@pytest.fixture
def gui(paths):
    if not CHILD:  # the parent process only dispatches; the child runs the real GUI
        yield None
        return
    h = Harness(paths)
    yield h
    errors = list(h.errors)
    h.close()
    assert not errors, "Tk callback exceptions:\n" + "\n".join(errors)


def key_for(paths):
    return load_private_key(paths, PASSPHRASE)


def make_warrant(paths, terms=("PROJECT_CEDAR", "SUPPLIER_NORTH"), **kw):
    doc = build_warrant(parameter_1=kw.pop("p1", "0012"), parameter_2=kw.pop("p2", ["ATTR_1000", "ATTR_1200"]),
                        purpose=kw.pop("purpose", "Smoke test purpose"), ttl_minutes=kw.pop("ttl", 60),
                        max_rows=kw.pop("rows", 100), rbac=get_rbac(paths),
                        terms=[{"term_id": "t-" + t.lower().replace("_", "-"), "literal": t, "source": "script"} for t in terms],
                        **kw)
    save_warrant(paths, doc, key_for(paths))
    return doc


def certify(h: Harness):
    c = h.app.compose
    c.cert_purpose.set(True)
    c.cert_limits.set(True)
    h.pump()


def sign_from_compose(h: Harness, p1_index: int = 1, attrs=("ATTR_1000",)):
    c = h.app.compose
    c.p1_display.set(c.p1_box.cget("values")[p1_index])
    c.parameter_1_changed()
    for attr in attrs:
        c.chips.vars[attr].set(True)
    c._p2_changed()
    certify(h)
    h.click(c.sign_button)


# --- shell ----------------------------------------------------------------------------

def test_tabs_order_and_shortcuts(gui):
    assert [gui.app.notebook.tab(t, "text").strip() for t in gui.app.notebook.tabs()] == [
        "Compose", "Warrants", "Templates", "Syntax requests", "Runs"]
    target = gui.app.notebook  # shortcuts are delivered to the focused widget; make sure it's ours
    target.focus_force()
    gui.pump()
    target.event_generate("<Control-Key-5>")
    gui.pump()
    assert gui.tab() == "Runs"
    target.focus_force()
    target.event_generate("<Control-Key-2>")
    gui.pump()
    assert gui.tab() == "Warrants"
    assert "PRACTICE MODE" in texts(gui.app)


# --- compose --------------------------------------------------------------------------

def test_compose_review_keys_and_sign(gui, paths):
    c = gui.app.compose
    assert str(c.sign_button.cget("state")) == "disabled"
    assert "choose a search authorization" in c.footer_text.cget("text")
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    gui.key(c.tree, "a")
    gui.key(c.tree, "r")  # NORTH_SUPPLY_CO
    assert [t["decision"] for t in c.terms[:3]] == ["approved", "approved", "rejected"]
    assert any(t["literal"] == "NORTH_SUPPLY_CO" for t in c.terms), "a rejection must stay visible until signing"
    sign_from_compose(gui)
    (w,) = list_warrants(paths, load_public_key(paths))
    assert [t["literal"] for t in w["doc"]["terms"]] == ["PROJECT_CEDAR", "SUPPLIER_NORTH"]
    assert "NORTH_SUPPLY_CO" in load_rejected(paths)
    assert c.p1 == "" and all(t["decision"] == "pending" for t in c.terms)
    assert {t["literal"] for t in c.terms} == {"TEST_DATA", "CEDAR_PHASE_TWO"}
    assert "NORTH_SUPPLY_CO" not in gui.prompts[-1], "terms left out of the warrant aren't listed in it"
    assert "Search Warrant" in gui.prompts[-1] and "I. Authorization" in gui.prompts[-1]


def test_compose_buttons_add_edit_hidden_undo(gui, paths):
    c = gui.app.compose
    from lucid_warrant.ui.term_dialog import TermDialog

    gui.answers["askstring"] += ["CL-9", "seen in rec-9"]
    with mock.patch.object(TermDialog, "ask", return_value={"literal": "NEW_TERM_Ω", "match": "exact"}):
        gui.click(find(c, text="Add hit term"))
    assert c.terms[-1]["literal"] == "NEW_TERM_Ω" and c.terms[-1]["decision"] == "approved"
    assert c.terms[-1]["client_id"] == "CL-9"
    c.tree.selection_set(f"q{len(c.terms) - 1}")
    with mock.patch.object(TermDialog, "ask", return_value={"literal": "NEW_TERM_Ω2", "match": "exact"}):
        gui.click(find(c, text="Edit hit term"))
    assert c.terms[-1]["literal"] == "NEW_TERM_Ω2" and c.terms[-1]["source"] == "human"
    c.tree.selection_set("q3")
    gui.click(find(c, text="Reject hit term"))
    sign_from_compose(gui)
    show = find(c, ttk.Checkbutton, "Show hidden")
    gui.click(show)
    rejected_rows = [i for i in c.tree.get_children() if i.startswith("r")]
    assert rejected_rows, "Show hidden lists rejected terms"
    c.tree.selection_set(rejected_rows[0])
    gui.click(find(c, text="Return to pending"))
    assert load_rejected(paths), "undoing a rejection takes effect at the next signature"


def test_compose_limits_and_readiness(gui, paths):
    c = gui.app.compose
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    c.purpose.delete("1.0", "end")
    c.update_footer()
    c.p1_display.set(c.p1_box.cget("values")[0]); c.parameter_1_changed(); c.chips.vars["ATTR_1000"].set(True); c._p2_changed()
    assert "write a purpose" in c.footer_text.cget("text") and str(c.sign_button.cget("state")) == "disabled"
    c.purpose.insert("1.0", "Purpose")
    c._purpose_modified()
    c.ttl_hours.set("99")
    assert "expiry" in c.footer_text.cget("text") and c.expiry_preview.cget("text") == ""
    c.ttl_hours.set("2")
    c.rows.set("600000")
    assert "500,000" in c.footer_text.cget("text")
    c.rows.set("20000")
    gui.click(find(c, ttk.Radiobutton, "No date restriction"))
    gui.answers["range"].append({"start": "2026-01-01", "end": "2026-01-31"})
    gui.click(find(c, text="Add range"))
    assert c.blocked == [{"start": "2026-01-01", "end": "2026-01-31"}]
    certify(gui)
    gui.click(c.sign_button)
    (w,) = list_warrants(paths, load_public_key(paths))
    assert w["doc"]["max_lookback_days"] == 0 and w["doc"]["blocked_date_ranges"] == [{"start": "2026-01-01", "end": "2026-01-31"}]


def test_visibility_chips_keyboard_and_scroll_guard(gui):
    c = gui.app.compose
    c.set_p1("0012")
    c.parameter_1_changed()
    gui.pump()
    chip = find(c.chips, ttk.Checkbutton, "ATTR_1200")
    gui.key(chip, "space")
    assert "ATTR_1200" in c.chips.values(), "Space toggles a visibility attribute"
    before = c.p1
    position = c.page.canvas.yview()[0]
    c.p1_box.event_generate("<MouseWheel>", delta=-3, x=5, y=5)
    gui.pump()
    assert c.p1 == before, "scrolling over the authorization dropdown must not change it"
    assert c.page.canvas.yview()[0] > position, "it scrolls the page instead"


def test_live_nominations_and_inbox_problems(gui, paths):
    c = gui.app.compose
    proposal = json.loads(paths.proposal.read_text())
    proposal["terms"] += [{"literal": "LIVE_TERM", "reason": "x"}, {"literal": "", "reason": "bad"}]
    paths.proposal.write_text(json.dumps(proposal))
    gui.pump(2.5)
    assert any(t["literal"] == "LIVE_TERM" for t in c.terms)
    assert "skipped" in c.problem_note.cget("text")


# --- warrants --------------------------------------------------------------------------

def test_warrant_view_runs_link_keeps_filter(gui, paths):
    """User report: first click only scrolled the pane; second opened Runs without the filter."""
    a, b = make_warrant(paths), make_warrant(paths, terms=("NORTH_SUPPLY_CO",))
    run_template(paths, warrant_id=a["warrant_id"], template_id="single-term", bindings={"term": "t-project-cedar"})
    run_template(paths, warrant_id=b["warrant_id"], template_id="single-term", bindings={"term": "t-north-supply-co"})
    gui.app.refresh_all()
    gui.app.show_warrant(a["warrant_id"])
    view = gui.app.warrants_tab.inspector
    gui.pump()
    view.scroll.canvas.yview_moveto(1.0)
    gui.pump()
    gui.click(find(view.actions, text="Runs under this warrant"))
    runs = gui.app.runs
    assert gui.tab() == "Runs"
    assert runs.warrant.get() == a["warrant_id"]
    assert len(runs.table.tree.get_children()) == 1
    assert a["warrant_id"] in runs.banner_label.cget("text")
    gui.click(find(runs.banner, text="Show all runs"))
    assert len(runs.table.tree.get_children()) == 2


def test_warrant_actions_by_status(gui, paths):
    active = make_warrant(paths)
    gui.app.refresh_all()
    gui.app.show_warrant(active["warrant_id"])
    view = gui.app.warrants_tab.inspector
    gui.click(find(view.actions, text="Edit"))
    assert gui.tab() == "Compose" and gui.app.compose.editing == active["warrant_id"]
    gui.click(find(gui.app.compose, text="Cancel edit"))
    assert gui.app.compose.editing is None
    gui.app.show_warrant(active["warrant_id"])
    gui.answers["askyesno"].append(True)
    gui.click(find(view.actions, text="Revoke"))
    assert gui.app.warrants_tab.warrants[active["warrant_id"]]["status"] == "revoked"
    gui.app.show_warrant(active["warrant_id"])
    buttons = [b.cget("text") for b in view.actions.winfo_children() if isinstance(b, ttk.Button)]
    assert "record only" in texts(view) and buttons == ["Print / Save as PDF…", "Runs under this warrant  →"], \
        "a record can be printed and its runs viewed, but not edited or revoked"


def test_edit_then_new_warrant_asks(gui, paths):
    active = make_warrant(paths)
    gui.app.refresh_all()
    gui.app.show_warrant(active["warrant_id"])
    gui.click(find(gui.app.warrants_tab.inspector.actions, text="Edit"))
    gui.app.show_warrant(active["warrant_id"])
    gui.answers["askyesno"].append(True)
    gui.click(find(gui.app.warrants_tab, text="New warrant"))
    assert gui.app.compose.editing is None and gui.tab() == "Compose"


def test_replacement_and_renew(gui, paths):
    old = make_warrant(paths)
    expired = make_warrant(paths, terms=("NORTH_SUPPLY_CO",), ttl=1, now=utc_now() - timedelta(hours=1))
    gui.app.refresh_all()
    gui.app.show_warrant(old["warrant_id"])
    gui.click(find(gui.app.warrants_tab.inspector.actions, text="Edit"))
    assert str(gui.app.compose.sign_button.cget("state")) == "disabled", "an edit must be certified again"
    certify(gui)
    gui.click(gui.app.compose.sign_button)
    statuses = {w["warrant_id"]: w["status"] for w in list_warrants(paths, load_public_key(paths), include_archived=True)}
    assert statuses[old["warrant_id"]] == "superseded"
    gui.app.show_warrant(old["warrant_id"])
    assert "Replaced by" in texts(gui.app.warrants_tab.inspector)
    gui.app.show_warrant(expired["warrant_id"])
    gui.click(find(gui.app.warrants_tab.inspector.actions, text="Renew"))
    assert "Renewing expired" in gui.app.compose.edit_label.cget("text")
    certify(gui)
    gui.click(gui.app.compose.sign_button)
    assert any(w["status"] == "active" and w["doc"]["terms"][0]["literal"] == "NORTH_SUPPLY_CO"
               for w in list_warrants(paths, load_public_key(paths)))


# --- templates --------------------------------------------------------------------------

def test_templates_filter_disable_enable_and_runs_link(gui, paths):
    w = make_warrant(paths)
    run_template(paths, warrant_id=w["warrant_id"], template_id="all-of-two",
                 bindings={"a": "t-project-cedar", "b": "t-supplier-north"})
    gui.app.refresh_all()
    tp = gui.app.templates
    gui.app.show_template("all-of-two")
    tp.search.set("either")
    assert set(tp.table.tree.get_children()) == {"any-of-two", "required-plus-alternatives"}
    tp.search.set("")
    gui.app.show_template("all-of-two")
    gui.click(find(tp.inspector.actions, text="Disable"))
    assert not next(t for t in load_library(paths, load_public_key(paths))["templates"] if t["template_id"] == "all-of-two")["enabled"]
    gui.click(find(tp, ttk.Radiobutton, "Disabled"))
    assert list(tp.table.tree.get_children()) == ["all-of-two"]
    gui.app.show_template("all-of-two")
    gui.click(find(tp.inspector.body, text="Runs using this template"))
    assert gui.tab() == "Runs" and len(gui.app.runs.table.tree.get_children()) == 1
    gui.app.show_template("all-of-two")
    gui.click(find(tp.inspector.actions, text="Enable"))
    assert next(t for t in load_library(paths, load_public_key(paths))["templates"] if t["template_id"] == "all-of-two")["enabled"]


# --- syntax requests -------------------------------------------------------------------

def _request(paths, warrant, skeleton="({a} OR {b}) AND NOT {c}", **bindings):
    bindings = bindings or {"a": "t-project-cedar", "b": "t-supplier-north", "c": "t-north-supply-co"}
    return submit_request(paths, load_public_key(paths), warrant_id=warrant["warrant_id"], skeleton=skeleton,
                          bindings=bindings, rationale="Line\nFAKE: APPROVED BY SECURITY")


def test_syntax_request_lifecycle(gui, paths):
    from lucid_warrant.executor import run_onetime

    w = make_warrant(paths, terms=("PROJECT_CEDAR", "SUPPLIER_NORTH", "NORTH_SUPPLY_CO"))
    req = _request(paths, w)
    gui.pump(2.5)
    assert gui.app.notebook.tab(gui.app.approvals, "text").strip() == "Syntax requests (1)"
    ap = gui.app.approvals
    gui.app.show_approval("req:" + req["request_id"])
    assert "FAKE: APPROVED BY SECURITY" in texts(ap.inspector)  # shown, but flattened inside the agent box
    gui.click(find(ap.inspector.actions, text="Approve for one run"))
    (ot,) = list_onetime(paths, load_public_key(paths))
    assert ap.table.selected() == "ot:" + ot["onetime_id"] and ap.filter.get() == "Ready"
    assert str(find(ap.inspector.actions, text="Promote").cget("state")) == "disabled"
    result = run_onetime(paths, onetime_id=ot["onetime_id"])
    gui.pump(2.5)
    gui.app.show_approval("ot:" + ot["onetime_id"])
    gui.click(find(ap.inspector.body, text="view run"))
    assert gui.tab() == "Runs" and gui.app.runs.table.selected() == result["run_id"]
    gui.app.show_approval("ot:" + ot["onetime_id"])
    gui.click(find(ap.inspector.actions, text="Promote"))
    assert any(t.get("origin") == f"promoted from {ot['onetime_id']}" for t in load_library(paths, load_public_key(paths))["templates"])
    gui.app.show_approval("ot:" + ot["onetime_id"])
    gui.click(find(ap.inspector.actions, text="Open template"))
    assert gui.tab() == "Templates"


def test_syntax_request_reject_and_unavailable(gui, paths):
    w = make_warrant(paths, terms=("PROJECT_CEDAR", "SUPPLIER_NORTH", "NORTH_SUPPLY_CO"))
    reject_me, approve_me = _request(paths, w), _request(paths, w, skeleton="{a} AND {b}",
                                                          a="t-project-cedar", b="t-supplier-north")
    gui.app.refresh_all()
    ap = gui.app.approvals
    gui.app.show_approval("req:" + reject_me["request_id"])
    gui.answers["askyesno"].append(True)
    gui.click(find(ap.inspector.actions, text="Reject"))
    assert [r["request_id"] for r in list_requests(paths)] == [approve_me["request_id"]]
    gui.app.show_approval("req:" + approve_me["request_id"])
    gui.click(find(ap.inspector.actions, text="Approve for one run"))
    revoke_warrant(paths, w["warrant_id"])
    gui.pump(2.5)
    (ot,) = list_onetime(paths, load_public_key(paths))
    assert ap.items["ot:" + ot["onetime_id"]]["status"] == "Unavailable · warrant revoked"


def test_tampered_request_lint_flagged(gui, paths):
    w = make_warrant(paths, terms=("PROJECT_CEDAR", "SUPPLIER_NORTH", "NORTH_SUPPLY_CO"))
    req = _request(paths, w)
    path = paths.syntax_requests / f"{req['request_id']}.json"
    doc = json.loads(path.read_text())
    doc["lint"]["passed"] = False
    path.write_text(json.dumps(doc))
    gui.app.refresh_all()
    assert "recorded result differs" in gui.app.approvals.items["req:" + req["request_id"]]["status"]


# --- runs ---------------------------------------------------------------------------------

def test_runs_filters_paging_and_old_run_link(gui, paths):
    w = make_warrant(paths)
    first = None
    for i in range(505):
        result = run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": "t-project-cedar"})
        first = first or result["run_id"]
    gui.app.refresh_all()
    runs = gui.app.runs
    gui.app.notebook.select(runs)
    gui.pump()
    assert len(runs.table.tree.get_children()) == 500 and runs.more_button.winfo_ismapped()
    gui.click(runs.more_button)
    assert len(runs.table.tree.get_children()) == 505
    runs.outcome.set("Blocked before sending")
    assert not runs.table.tree.get_children() and "Show all runs" in texts(runs)
    gui.app.show_run(first)
    assert runs.table.selected() == first, "an old run beyond the first page is still selected by its link"


def test_run_payload_unavailable_and_copy(gui, paths):
    w = make_warrant(paths)
    result = run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": "t-project-cedar"})
    (paths.state / "payloads" / f"{result['run_id']}.json").unlink()
    gui.app.refresh_all()
    gui.app.show_run(result["run_id"])
    view = gui.app.runs.inspector
    assert "Payload unavailable" in texts(view)
    gui.click(find(view.body, text="Copy"))
    assert gui.app.clipboard_get() == result["run_id"]


def test_inspector_keeps_scroll_on_live_refresh(gui, paths):
    w = make_warrant(paths)
    result = run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": "t-project-cedar"})
    gui.app.refresh_all()
    gui.app.show_run(result["run_id"])
    view = gui.app.runs.inspector
    gui.pump()
    view.scroll.canvas.yview_moveto(0.5)
    gui.pump()
    position = view.scroll.canvas.yview()[0]
    run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": "t-supplier-north"})
    gui.pump(2.5)
    assert gui.app.runs.table.selected() == result["run_id"]
    assert view.scroll.canvas.yview()[0] == pytest.approx(position, abs=0.02)


def test_torn_audit_line_reported_not_fatal(gui, paths):
    w = make_warrant(paths)
    run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": "t-project-cedar"})
    with open(paths.audit_log, "a") as fh:
        fh.write('{"ts": "2026')
    gui.pump(2.5)
    assert len(gui.app.runs.table.tree.get_children()) == 1
    assert "unreadable audit line" in gui.app.problem_label.cget("text")


def test_empty_states_explain_themselves(gui):
    assert "No warrants yet" in gui.app.warrants_tab.table.empty_label.cget("text")
    assert "No runs yet" in gui.app.runs.table.empty_label.cget("text")
    assert "Nothing needs review" in gui.app.approvals.table.empty_label.cget("text")


# --- regressions for Codex's GUI findings -----------------------------------------------

def test_f3_return_activates_links(gui, paths):
    a = make_warrant(paths)
    gui.app.refresh_all()
    gui.app.show_warrant(a["warrant_id"])
    link = find(gui.app.warrants_tab.inspector.actions, text="Runs under this warrant")
    gui.key(link, "Return")
    assert gui.tab() == "Runs" and gui.app.runs.warrant.get() == a["warrant_id"]


def test_f4_selection_survives_refresh_with_show_hidden(gui, paths):
    c = gui.app.compose
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    sign_from_compose(gui)
    gui.click(find(c, ttk.Checkbutton, "Show hidden"))
    target = c.tree.get_children()[-1]
    c.tree.selection_set(target)
    gui.app.event_generate("<F5>")  # live refresh, forced
    gui.app.refresh_all()
    gui.pump()
    assert c.tree.selection() == (target,)


def test_f13_runs_list_scroll_survives_live_append(gui, paths):
    w = make_warrant(paths)
    for _ in range(120):
        run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": "t-project-cedar"})
    gui.app.refresh_all()
    runs = gui.app.runs
    gui.app.notebook.select(runs)
    gui.pump()
    top = runs.table.tree.get_children()[0]
    runs.table.tree.selection_set(top)
    gui.pump()
    runs.table.tree.yview_moveto(0.6)
    gui.pump()
    before = runs.table.tree.yview()[0]
    run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": "t-supplier-north"})
    gui.pump(2.5)
    assert runs.table.selected() == top
    assert runs.table.tree.yview()[0] == pytest.approx(before, abs=0.03)


def test_f15_review_refreshes_after_unrelated_library_change(gui, paths):
    from lucid_warrant.library import set_template_enabled

    w = make_warrant(paths)
    req = submit_request(paths, load_public_key(paths), warrant_id=w["warrant_id"], skeleton="{a} AND {b}",
                         bindings={"a": "t-project-cedar", "b": "t-supplier-north"}, rationale="")
    gui.app.refresh_all()
    gui.app.show_approval("req:" + req["request_id"])
    set_template_enabled(paths, key_for(paths), "any-of-two", False)
    gui.pump(2.5)
    gui.click(find(gui.app.approvals.inspector.actions, text="Approve for one run"))
    assert list_onetime(paths, load_public_key(paths)), "approval should succeed after the review refreshed"


def test_f16_undoing_hidden_rejection_waits_for_signing(gui, paths):
    c = gui.app.compose
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    gui.key(c.tree, "r")
    sign_from_compose(gui)
    assert "SUPPLIER_NORTH" in load_rejected(paths)
    gui.click(find(c, ttk.Checkbutton, "Show hidden"))
    row = next(i for i in c.tree.get_children() if i.startswith("r"))
    c.tree.selection_set(row)
    gui.click(find(c, text="Return to pending"))
    assert "SUPPLIER_NORTH" in load_rejected(paths), "not committed until the next signature"
    c.tree.selection_set(f"q{next(i for i, t in enumerate(c.terms) if t['literal'] == 'SUPPLIER_NORTH')}")
    gui.key(c.tree, "a")
    sign_from_compose(gui)
    assert "SUPPLIER_NORTH" not in load_rejected(paths)


def test_f17_onetime_expiry_noticed_without_file_changes(gui, paths):
    from datetime import timedelta as td

    from lucid_warrant.onetime import approve_request
    from lucid_warrant.signing import write_signed
    from lucid_warrant.util import to_iso

    w = make_warrant(paths)
    submit_request(paths, load_public_key(paths), warrant_id=w["warrant_id"], skeleton="{a}",
                   bindings={"a": "t-project-cedar"}, rationale="")
    (request,) = list_requests(paths)
    approval = approve_request(paths, key_for(paths), request)
    approval["expires_at"] = to_iso(utc_now() + td(seconds=3))
    write_signed(paths.onetime / f"{approval['onetime_id']}.json", approval, key_for(paths))
    gui.app.refresh_all()
    assert gui.app.approvals.items["ot:" + approval["onetime_id"]]["stage"] == "Ready"
    gui.pump(6)
    assert gui.app.approvals.items["ot:" + approval["onetime_id"]]["status"] == "Expired"


def test_f9_f19_malformed_requests_have_working_reject(gui, paths):
    paths.syntax_requests.mkdir(parents=True, exist_ok=True)
    (paths.syntax_requests / "req-malformed.json").write_text("{")
    (paths.syntax_requests / "req-20261017-000001-abcdef.json").write_text('{"request_id": "req-20261017-000001-abcdef"}')
    gui.pump(2.5)
    ap = gui.app.approvals
    for iid in ("req:req-malformed", "req:req-20261017-000001-abcdef"):
        gui.app.show_approval(iid)
        assert "Can't be reviewed" in texts(ap.inspector)
        gui.answers["askyesno"].append(True)
        gui.click(find(ap.inspector.actions, text="Reject"))
    assert list_requests(paths) == []


# --- second round -----------------------------------------------------------------------

def test_malformed_files_never_stall_refresh(gui, paths):
    paths.syntax_requests.mkdir(parents=True, exist_ok=True)
    (paths.syntax_requests / "rejected").mkdir(exist_ok=True)
    for name, content in (("rejected/req-20261017-000003-abcdef.json", "[]"), ("rejected/req-junk.json", "null")):
        (paths.syntax_requests / name).write_text(content)
    paths.onetime.mkdir(parents=True, exist_ok=True)
    (paths.onetime / "ot-20261017-000000-abcdef.json").write_text('{"expires_at": null}')
    gui.pump(2.5)
    assert not [k for k in gui.app.problems if k not in ("theme",)], gui.app.problems
    gui.app.approvals.filter.set("Rejected")
    gui.app.approvals.render()
    assert len(gui.app.approvals.table.tree.get_children()) == 2
    proposal = json.loads(paths.proposal.read_text())
    proposal["terms"].append({"literal": "STILL_LIVE", "reason": "x"})
    paths.proposal.write_text(json.dumps(proposal))
    gui.pump(2.5)
    assert any(t["literal"] == "STILL_LIVE" for t in gui.app.compose.terms), "polling keeps working"


def test_hidden_selection_follows_the_term(gui, paths):
    from lucid_warrant.review import reject as remember_rejection

    c = gui.app.compose
    remember_rejection(paths, {"literal": "BBB", "term_id": "t-b"})
    gui.app.refresh_all()
    gui.click(find(c, ttk.Checkbutton, "Show hidden"))
    target = next(i for i in c.tree.get_children() if c.tree.item(i)["values"][1] == "BBB")
    c.tree.selection_set(target)
    remember_rejection(paths, {"literal": "AAA", "term_id": "t-a"})  # sorts before BBB
    gui.app.refresh_all()
    gui.pump()
    (selected,) = c.tree.selection()
    assert c.tree.item(selected)["values"][1] == "BBB"


def test_calendar_stays_inside_allowed_years(gui):
    from lucid_warrant.ui.widgets import MonthCalendar

    cal = MonthCalendar(gui.app, on_select=lambda d: None)
    from datetime import date as d
    cal.show(d(2999, 12, 31))
    cal._move(1)
    cal.shift(12)
    assert cal.cursor == d(2999, 12, 31) and cal.year == 2999


def test_keyboard_focus_reveals_offscreen_control_but_clicks_dont_move(gui):
    c = gui.app.compose
    gui.app.geometry("1100x560")
    gui.pump()
    c.page.canvas.yview_moveto(0)
    gui.pump()
    add_range = find(c, text="Add range", mapped=False)
    assert add_range.winfo_rooty() > c.page.canvas.winfo_rooty() + c.page.canvas.winfo_height(), "starts below the fold"
    time.sleep(0.6)  # clearly not part of a click
    add_range.focus_force()
    gui.pump()
    top, bottom = c.page.canvas.winfo_rooty(), c.page.canvas.winfo_rooty() + c.page.canvas.winfo_height()
    assert top <= add_range.winfo_rooty() and add_range.winfo_rooty() + add_range.winfo_height() <= bottom
    gui.answers["range"].append({"start": "2026-02-01", "end": "2026-02-02"})
    gui.click(add_range)  # the click helper fails if the button moves between press and release
    assert c.blocked == [{"start": "2026-02-01", "end": "2026-02-02"}]


def test_warrants_findable_by_id_and_search(gui, paths):
    old = make_warrant(paths)
    other = make_warrant(paths, terms=("NORTH_SUPPLY_CO",))
    revoke_warrant(paths, old["warrant_id"])
    gui.app.refresh_all()
    gui.app.event_generate("<Control-Key-2>")
    gui.app.notebook.select(gui.app.warrants_tab)
    gui.pump()
    wt = gui.app.warrants_tab
    first_column = wt.table.tree["columns"][0]
    assert first_column == "id" and wt.table.tree.item(old["warrant_id"])["values"][0] == old["warrant_id"]
    wt.search.set(old["warrant_id"][-6:])
    gui.pump()
    assert list(wt.table.tree.get_children()) == [old["warrant_id"]]
    wt.search.set("north_supply")
    assert list(wt.table.tree.get_children()) == [other["warrant_id"]]
    wt.search.set("nothing-matches")
    assert "No warrants match" in wt.table.empty_label.cget("text")
    gui.app.show_warrant(old["warrant_id"])
    assert wt.search.get() == "" and wt.table.selected() == old["warrant_id"]


@pytest.mark.parametrize("size", ["1024x680", "1600x1000"])
def test_sizing_no_clipped_ids_times_or_buttons(gui, paths, size):
    """IDs, times, statuses and buttons must never be cut off; free text may be capped (full text in the inspector)."""
    from tkinter import font as tkfont

    from lucid_warrant.ui.widgets import Table

    w = make_warrant(paths, terms=("PROJECT_CEDAR", "SUPPLIER_NORTH", "NORTH_SUPPLY_CO"))
    run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": "t-project-cedar"})
    submit_request(paths, load_public_key(paths), warrant_id=w["warrant_id"], skeleton="({a} OR {b}) AND NOT {c}",
                   bindings={"a": "t-project-cedar", "b": "t-supplier-north", "c": "t-north-supply-co"}, rationale="r")
    gui.app.geometry(size)
    gui.app.refresh_all()
    body = tkfont.Font(font=ttk.Style().lookup("Treeview", "font") or "TkDefaultFont")
    problems = []
    for tab in (gui.app.compose, gui.app.warrants_tab, gui.app.templates, gui.app.approvals, gui.app.runs):
        gui.app.notebook.select(tab)
        gui.pump()
        table = getattr(tab, "table", None)
        if isinstance(table, Table) and table.tree.get_children():
            table.tree.selection_set(table.tree.get_children()[0])
            gui.pump()
            for cid, cap in table.fit_content.items():
                if cap:
                    continue  # capped free text
                width = table.tree.column(cid, "width")
                need = max(body.measure(str(table.tree.set(r, cid))) for r in table.tree.get_children()) + 12
                if need > width:
                    problems.append(f"{type(tab).__name__}.{cid}: {need}px needed, {width}px")
        stack = [tab]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if isinstance(widget, ttk.Button) and widget.winfo_ismapped() and widget.winfo_reqwidth() > widget.winfo_width() + 2:
                problems.append(f"button {widget.cget('text')!r} clipped")
    assert not problems, problems


def test_rate_limit_field_signed_and_shown(gui, paths):
    c = gui.app.compose
    assert c.rate.get() == "60", "default from the organization configuration"
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    c.rate.set("0")
    c.p1_display.set(c.p1_box.cget("values")[1]); c.parameter_1_changed(); c.chips.vars["ATTR_1000"].set(True); c._p2_changed()
    certify(gui)
    assert "per hour must be 1" in c.footer_text.cget("text")
    c.rate.set("5")
    certify(gui)
    gui.click(c.sign_button)
    (w,) = list_warrants(paths, load_public_key(paths))
    assert w["doc"]["max_queries_per_hour"] == 5
    assert "Maximum searches per hour" in gui.prompts[-1]
    run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": w["doc"]["terms"][0]["term_id"]})
    gui.app.refresh_all()
    gui.app.show_warrant(w["warrant_id"])
    assert "1 of 5 searches" in texts(gui.app.warrants_tab.inspector)


# --- certifications, country scope, documents, About/Appearance ----------------------------

def test_certifications_untick_when_what_they_certify_changes(gui):
    c = gui.app.compose
    certify(gui)
    c.purpose.insert("end", " more")
    gui.pump()
    assert not c.cert_purpose.get() and c.cert_limits.get()
    certify(gui)
    c.rows.set("100")
    gui.pump()
    assert c.cert_purpose.get() and not c.cert_limits.get()
    certify(gui)
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    assert not c.cert_purpose.get()
    certify(gui)
    c.exclude_table.tree.selection_set("0")
    gui.click(find(c.exclude_table.master, text="Remove"))
    assert not c.cert_limits.get(), "changing country scope uncertifies the limits"


def test_country_scope_and_blocked_tables(gui, paths):
    c = gui.app.compose
    assert [r["code"] for r in c.exclude_rows] == ["DE", "FR", "IT"]
    assert c.exclude_table.tree.cget("height") == 3
    with mock.patch("lucid_warrant.ui.widgets.CountryDialog.ask", return_value={"code": "ES", "reason": "GDPR"}):
        gui.click(find(c.exclude_table.master, text="Add exclusion"))
    assert c.exclude_table.tree.cget("height") == 4, "the table grows instead of scrolling"
    gui.click(find(c, ttk.Radiobutton, "Only these countries"))
    with mock.patch("lucid_warrant.ui.widgets.CountryDialog.ask", return_value={"code": "US"}):
        gui.click(find(c.include_table.master, text="Add country"))
    assert c.include_rows == [{"code": "US", "reason": ""}]
    assert c.blocked_table.tree.cget("height") == 1
    for start in ("2026-01-01", "2026-02-01"):
        gui.answers["range"].append({"start": start, "end": start})
        gui.click(find(c, text="Add range"))
    assert c.blocked_table.tree.cget("height") == 2
    assert [c.blocked_table.tree.heading(col, "text") for col in c.blocked_table.tree["columns"]] == ["From (UTC)", "To (UTC, inclusive)"]
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    sign_from_compose(gui)
    (w,) = list_warrants(paths, load_public_key(paths))
    assert w["doc"]["country_scope"] == {"include": [{"code": "US", "reason": ""}], "exclude": [
        {"code": "DE", "reason": "GDPR"}, {"code": "ES", "reason": "GDPR"}, {"code": "FR", "reason": "GDPR"}, {"code": "IT", "reason": "GDPR"}]}
    assert "V. Geographic Constraint" in gui.prompts[-1] and "ES — Spain" in gui.prompts[-1]


def test_footer_only_says_whats_missing(gui):
    c = gui.app.compose
    text = c.footer_text.cget("text")
    assert text.startswith("Still needed:") and "certify" in text and "Sign:" not in text
    assert "Selected:" not in c.p2_hint.cget("text")


def test_print_export_matches_and_escapes(gui, paths):
    import webbrowser

    doc = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose='<script>alert(1)</script> & "quotes"',
                        ttl_minutes=60, max_rows=10, max_queries_per_hour=7, rbac=get_rbac(paths), paths=paths,
                        terms=[{"term_id": "t-x", "literal": "<b>BOLD</b>", "source": "agent", "provenance": "<img src=x>"}],
                        country_exclude=[{"code": "DE", "reason": "<i>GDPR</i>"}],
                        certifications=[{"id": "purpose", "text": "I certify A"}, {"id": "limits", "text": "I certify B"}])
    save_warrant(paths, doc, key_for(paths))
    gui.app.refresh_all()
    gui.app.show_warrant(doc["warrant_id"])
    with mock.patch.object(webbrowser, "open") as opened:
        gui.click(find(gui.app.warrants_tab.inspector.actions, text="Print / Save as PDF"))
    html_text = (paths.data / "exports" / f"{doc['warrant_id']}.html").read_text("utf-8")
    assert opened.called
    assert "<script>" not in html_text and "&lt;script&gt;" in html_text and "<img" not in html_text
    for expected in ("SEARCH WARRANT".title(), "I. Definitions", "II. Authorization", "VIII. Certifications", "I certify A", "7 in any rolling hour",
                     "Agent proposal", "DE — Germany"):
        assert expected in html_text, expected


def test_about_and_appearance(gui, paths):
    from lucid_warrant.ui.dialogs import AboutDialog, AppearanceDialog

    about = AboutDialog(gui.app)
    gui.pump()
    rows = {title: value for title, value, _ in about.details["rows"]}
    assert rows["Configuration hash"] == oc_hash(gui.app.settings) and rows["Status"] == "None"
    gui.click(find(about, text="Copy details"))
    assert "Signing key fingerprint" in gui.app.clipboard_get()
    about.destroy()
    look = AppearanceDialog(gui.app)
    look.scheme.set("teal")
    look.mode.set("dark")
    look.save()
    prefs = json.loads((paths.data.parent / "prefs.json").read_text())
    assert prefs["ui"]["scheme"] == "teal" and set(prefs) <= {"schema_version", "ui"}
    assert not look.winfo_exists(), "Save closes the dialog"
    assert gui.app.settings["ui"]["scheme"] == "teal"
    look = AppearanceDialog(gui.app)
    labels = [w.cget("text") for w in look.winfo_children()[0].winfo_children()[-1].winfo_children()]
    assert sorted(labels) == ["Cancel", "Save"]
    look.cancel()


def oc_hash(settings):
    from lucid_warrant.org_config import authority_hash

    return authority_hash(settings)


def test_n_f01_live_agent_purpose_change_uncertifies(gui, paths):
    c = gui.app.compose
    certify(gui)
    paths.proposal.write_text(json.dumps({"purpose": "Agent replaced the certified purpose"}))
    gui.pump(2.5)
    assert c.purpose.get("1.0", "end").strip() == "Agent replaced the certified purpose"
    assert not c.cert_purpose.get()


def test_n_f03_edit_exclusion_reason(gui):
    c = gui.app.compose
    c.exclude_table.tree.selection_set("0")
    gui.answers["askstring"].append("Works council")
    gui.click(find(c.exclude_table.master, text="Edit reason"))
    assert c.exclude_rows[0] == {"code": "DE", "reason": "Works council"}


def test_n_f17_about_shows_selected_org_file(paths, monkeypatch):
    """Constructed directly (its own process via the gui marker) with an explicit organization file."""
    if not CHILD:
        return
    from lucid_warrant.ui import app as appmod
    from lucid_warrant.ui.dialogs import AboutDialog

    override = sign_org_file(paths, {"schema_version": 1, "organization": {"name": "Override Org"}},
                             paths.data / "override.json", passphrase=PASSPHRASE)
    with mock.patch.object(appmod.App, "_unlock_or_setup", return_value=True):
        app = appmod.App(paths, str(override))
        try:
            rows = {t: v for t, v, _ in AboutDialog(app).details["rows"]}
            assert rows["Configuration file"].startswith(str(override)) and "Override Org" in rows["Organization"]
        finally:
            app.destroy()


def test_n_f18_terminology_in_document_and_inspector(paths, monkeypatch):
    if not CHILD:
        return
    from lucid_warrant.ui import app as appmod

    sign_org_file(paths, {
        "schema_version": 1, "terms": {"warrant": {"singular": "Permit", "plural": "Permits"},
                                       "hit_term": {"singular": "Keyword", "plural": "Keywords"}},
        "parameters": {"parameter_1": {"label": "Case authority"}}}, passphrase=PASSPHRASE)
    h = Harness(paths)
    try:
        assert h.app.notebook.tab(h.app.warrants_tab, "text").strip() == "Permits"
        c = h.app.compose
        c.tree.selection_set("q0")
        h.key(c.tree, "a")
        sign_from_compose(h)
        assert "Search Permit" in h.prompts[-1] and "Included Keywords" in h.prompts[-1] and "Case authority" in h.prompts[-1]
        assert "Permit No." in h.prompts[-1] and "What This Permit Authorizes" in h.prompts[-1]
        assert "Warrant" not in h.prompts[-1].split("<body>")[1].replace("noprint", "")
        (w,) = list_warrants(paths, load_public_key(paths))
        h.app.show_warrant(w["warrant_id"])
        assert "Case authority" in texts(h.app.warrants_tab.inspector) and "Keywords" in texts(h.app.warrants_tab.inspector)
        compose = h.app.compose
        assert "Keywords" in texts(compose) and compose.tree.heading("literal", "text") == "Keyword"
        assert not h.errors
    finally:
        h.close()


def test_color_scheme_recolors_sun_valley_and_previews_live(gui):
    from lucid_warrant.ui import theme
    from lucid_warrant.ui.dialogs import AppearanceDialog

    app = gui.app

    def accent_pixel():
        img = app.tk.splitlist(app.tk.call("set", f"ttk::theme::sv_{theme.MODE}::I(button-accent-rest)"))[0]
        w, h = int(app.tk.call("image", "width", img)), int(app.tk.call("image", "height", img))
        return "#%02x%02x%02x" % tuple(int(v) for v in app.tk.splitlist(app.tk.call(img, "get", w // 2, h // 2)))

    original, original_bg = accent_pixel(), str(app.compose.page.canvas.cget("background"))
    dialog = AppearanceDialog(app)
    dialog.scheme.set("green")
    gui.pump(0.2)
    assert accent_pixel() != original and theme.PALETTE["accent"] != theme.PALETTES[theme.MODE]["accent"]
    assert not theme.ACTIVE.startswith("clam")
    tinted = str(app.compose.page.canvas.cget("background"))
    assert not gui.errors, gui.errors
    assert tinted != original_bg and tinted == theme.PALETTE["bg"], "the background follows the scheme live"
    assert str(ttk.Style().lookup(".", "background")) == theme.PALETTE["bg"]
    with mock.patch("tkinter.colorchooser.askcolor", return_value=((1, 2, 3), "#336699")):
        dialog.pick_custom()
    gui.pump(0.2)
    assert app.settings["ui"]["scheme"] == "custom" and app.settings["ui"]["theme_color"] == "#336699"
    dialog.cancel()
    gui.pump(0.2)
    assert accent_pixel() == original and app.settings["ui"]["scheme"] == "sun-valley"
    assert str(app.compose.page.canvas.cget("background")) == original_bg


# --- round 4 (Codex): theme behavior on a running app ----------------------------------------

def _rendered(widget, option):
    return "#%02x%02x%02x" % tuple(v // 257 for v in widget.winfo_rgb(widget.cget(option)))


def test_r4_3_10_text_and_surface_survive_theme_changes(gui):
    from lucid_warrant import org_config
    from lucid_warrant.ui import theme

    app = gui.app
    custom = {"label": "Inverse", "light": dict(org_config.BASE_PALETTES["dark"])}
    theme.apply(app, {**app.settings["ui"], "theme_mode": "light", "scheme": "inv"}, {"inv": custom})
    gui.pump(0.3)
    label = ttk.Label(app, text="x")
    assert str(ttk.Style().lookup("TLabel", "foreground")) == theme.PALETTE["text"] == "#f2f2f2"
    label.destroy()
    theme.apply(app, {**app.settings["ui"], "theme_mode": "dark", "scheme": "green"}, org_config.BUILTIN_SCHEMES)
    text = tk.Text(app, **theme.text_widget_colors())
    gui.pump(0.3)
    assert str(text.cget("background")) == theme.PALETTE["surface"] != theme.PALETTE["bg"]
    theme.apply(app, {**app.settings["ui"], "theme_mode": "dark", "scheme": "red"}, org_config.BUILTIN_SCHEMES)
    gui.pump(0.3)
    assert str(text.cget("background")) == theme.PALETTE["surface"], "survives Sun Valley's idle palette reset"


def test_r4_4_treeview_tags_follow_light_to_dark(gui):
    from lucid_warrant import org_config
    from lucid_warrant.ui import theme

    app = gui.app
    tree = app.compose.tree
    theme.apply(app, {**app.settings["ui"], "theme_mode": "light"}, org_config.schemes(app.settings))
    gui.pump(0.2)
    tags = {t: str(tree.tag_configure(t, "foreground")) for t in tree.tk.splitlist(tree.tk.call(tree, "tag", "names"))}
    theme.apply(app, {**app.settings["ui"], "theme_mode": "dark"}, org_config.schemes(app.settings))
    gui.pump(0.2)
    for tag, before in tags.items():
        if before.lower() in {v.lower() for v in theme.PALETTES["light"].values()} and before:
            after = str(tree.tag_configure(tag, "foreground"))
            assert after != before and org_config.contrast(after, theme.PALETTE["bg"]) >= 4.5, tag


def test_r4_5_cancel_restores_system_font_size(gui):
    from tkinter import font as tkfont

    from lucid_warrant.ui.dialogs import AppearanceDialog

    before = tkfont.nametofont("TkDefaultFont").cget("size")
    dialog = gui.app.appearance()
    dialog.font.set("20")
    gui.pump(0.2)
    assert tkfont.nametofont("TkDefaultFont").cget("size") == 20
    dialog.cancel()
    gui.pump(0.2)
    assert tkfont.nametofont("TkDefaultFont").cget("size") == before
    assert isinstance(dialog, AppearanceDialog)


def test_r4_9_only_one_appearance_dialog(gui):
    first = gui.app.appearance()
    assert gui.app.appearance() is first
    first.scheme.set("green")
    first.save()
    second = gui.app.appearance()
    assert second is not first
    second.cancel()
    assert gui.app.settings["ui"]["scheme"] == "green"


def test_r4_6_longest_reason_fits_at_minimum_size(gui):
    from tkinter import font as tkfont

    app = gui.app
    app.geometry("1024x680")
    c = app.compose
    c.exclude_rows[:] = [{"code": "DE", "reason": "W" * 60}]
    c._render_scope()
    c.exclude_table.tree.see("0")
    gui.pump(0.4)
    need = tkfont.nametofont("TkDefaultFont").measure("W" * 60)
    assert int(c.exclude_table.tree.column("reason", "width")) >= need


# --- round 5 (Codex) --------------------------------------------------------------------------

def test_r5_1_text_tags_follow_light_to_dark(gui):
    from lucid_warrant import org_config
    from lucid_warrant.ui import theme

    app = gui.app
    theme.apply(app, {**app.settings["ui"], "theme_mode": "light"}, org_config.schemes(app.settings))
    text = tk.Text(app, **theme.text_widget_colors())
    text.tag_configure("warn", foreground=theme.PALETTE["warn"])
    text.tag_configure("muted", foreground=theme.PALETTE["muted"])
    theme.apply(app, {**app.settings["ui"], "theme_mode": "dark"}, org_config.schemes(app.settings))
    gui.pump(0.2)
    assert text.tag_cget("warn", "foreground") == theme.PALETTE["warn"] == "#fce100"
    assert text.tag_cget("muted", "foreground") == theme.PALETTE["muted"]


def test_r5_2_root_background_not_remapped_twice(gui):
    from lucid_warrant import org_config
    from lucid_warrant.ui import theme

    app = gui.app
    theme.apply(app, {**app.settings["ui"], "theme_mode": "light", "scheme": "sun-valley"}, org_config.BUILTIN_SCHEMES)
    gui.pump(0.2)
    inverse = {"inv": {"label": "Inverse", "light": dict(org_config.BASE_PALETTES["dark"])}}
    theme.apply(app, {**app.settings["ui"], "theme_mode": "light", "scheme": "inv"}, inverse)
    gui.pump(0.3)
    assert str(app.cget("background")) == "#1c1c1c"


def test_r5_3_restore_skips_a_widget_replaced_at_the_same_path(gui):
    from lucid_warrant import org_config
    from lucid_warrant.ui import theme

    app = gui.app
    tk.Text(app, name="victim", background="#123456")

    def swap(event):
        if event.widget is app:
            app.nametowidget("victim").destroy()
            tk.Text(app, name="victim", background="#abcdef")
    app.bind("<<ThemeChanged>>", swap, "+")
    theme.apply(app, {**app.settings["ui"], "scheme": "green"}, org_config.BUILTIN_SCHEMES)
    gui.pump(0.3)
    assert str(app.nametowidget("victim").cget("background")) != "#123456"


def test_r5_4_tooltip_and_callbacks_follow_the_table(gui):
    from lucid_warrant.ui.widgets import GrowTable

    app = gui.app
    holder = ttk.Frame(app)
    holder.place(x=0, y=0)
    table = GrowTable(holder, [("code", "Code", 40), ("reason", "Reason", 60)])
    table.pack()
    table.set_rows([["DE", "W" * 60]])
    gui.pump(0.2)
    table.tree.column("reason", width=60)
    bbox = table.tree.bbox("0", "reason")

    class Event:
        x, y = bbox[0] + 5, bbox[1] + 3
        x_root, y_root = 10, 10
    table._hover(Event)
    assert table.tip is not None and table.tip.winfo_exists()
    table.set_rows([["FR", "short"]])
    assert table.tip is None, "replacing rows hides a tooltip for a row that's gone"
    table.destroy()
    holder.destroy()
    app.geometry("1100x700")
    gui.pump(0.3)
    assert not gui.errors, gui.errors


def test_r5_5_clamp_moves_toward_contrast_with_black_text():
    from lucid_warrant import org_config as oc
    from lucid_warrant.ui import theme

    palette = {**oc.BASE_PALETTES["light"], "accent": "#757575", "accent_text": "#000000"}
    width, height, rows, regions = theme._recolored_sheet("light", palette, None)
    for name, x, y, w, h in regions:
        if name.startswith("button-accent") and not name.endswith("-dis"):
            r, g, b = rows[y + h // 2][(x + w // 2) * 4:(x + w // 2) * 4 + 3]
            assert oc.contrast("#%02x%02x%02x" % (r, g, b), "#000000") >= 4.5, name


# --- client ID and geographic constraint -----------------------------------------------------

def test_client_id_column_signed_and_printed(gui, paths):
    c = gui.app.compose
    columns = list(c.tree.cget("columns"))
    assert columns.index("client_id") == columns.index("literal") + 1 == columns.index("source") - 1
    assert c.tree.heading("client_id", "text") == "Client ID"
    rows = {c.tree.set(i, "literal"): c.tree.set(i, "client_id") for i in c.tree.get_children()}
    assert rows["PROJECT_CEDAR"] == "CL-100482" and rows["SUPPLIER_NORTH"] == "—"
    cedar = next(i for i in c.tree.get_children() if c.tree.set(i, "literal") == "PROJECT_CEDAR")
    c.tree.selection_set(cedar)
    gui.key(c.tree, "a")
    sign_from_compose(gui)
    assert "CL-100482" in gui.prompts[-1] and "not added to searches" in gui.prompts[-1]
    assert "Geographic Constraint" in gui.prompts[-1] and "Geographic Scope" not in gui.prompts[-1]
    (w,) = list_warrants(paths, load_public_key(paths))
    assert w["doc"]["terms"][0]["client_id"] == "CL-100482"
    gui.app.show_warrant(w["warrant_id"])
    assert "Client ID CL-100482" in texts(gui.app.warrants_tab.inspector)
    assert "Geographic constraint" in texts(c) and "Geographic scope" not in texts(c)


def test_definitions_section_replaces_origin_notes(gui, paths):
    c = gui.app.compose
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    sign_from_compose(gui)
    doc = gui.prompts[-1]
    assert "I. Definitions" in doc and "&ldquo;Script nomination&rdquo;</strong> means a hit term proposed" in doc
    assert "&ldquo;Client ID&rdquo;</strong> means" in doc and "Origin: " not in doc
    assert doc.index("I. Definitions") < doc.index("II. Authorization") < doc.index("IV. Included Hit Terms")


def test_click_a_cell_copies_it(gui):
    c = gui.app.compose
    gui.pump(0.2)
    cedar = next(i for i in c.tree.get_children() if c.tree.set(i, "literal") == "PROJECT_CEDAR")
    c.tree.see(cedar)
    gui.pump(0.1)
    for column, expected in (("literal", "PROJECT_CEDAR"), ("client_id", "CL-100482")):
        x, y, w, h = c.tree.bbox(cedar, column)
        c.tree.event_generate("<ButtonPress-1>", x=x + 5, y=y + h // 2)
        c.tree.event_generate("<ButtonRelease-1>", x=x + 5, y=y + h // 2)
        gui.pump(0.05)
        assert gui.app.clipboard_get() == expected
        assert "Copied" in gui.app.notice.cget("text")
    assert c.tree.selection() == (cedar,), "the click still selects the row"
    north = next(i for i in c.tree.get_children() if c.tree.set(i, "literal") == "SUPPLIER_NORTH")
    c.tree.selection_set((cedar, north))
    c.copy_selected()
    assert gui.app.clipboard_get() == "PROJECT_CEDAR\tCL-100482\nSUPPLIER_NORTH\t"
    assert not gui.errors


def test_copy_modifiers_per_platform():
    from lucid_warrant.ui.widgets import select_modifiers

    numlock_windows, alt_windows, cmd_mac, shift, control = 0x8, 0x20000, 0x8, 0x1, 0x4
    assert not select_modifiers("win32") & numlock_windows, "NumLock on Windows must not stop a copy"
    assert select_modifiers("win32") & alt_windows and select_modifiers("darwin") & cmd_mac
    for platform in ("win32", "darwin", "linux"):
        assert select_modifiers(platform) & shift and select_modifiers(platform) & control


def test_flash_cleans_up_when_its_owner_goes_first(gui):
    from lucid_warrant.ui.widgets import flash

    owner = ttk.Frame(gui.app)
    tip = flash(owner, "Copied!", 10, 10, ms=300)
    owner.destroy()
    gui.pump(0.6)
    assert not tip.winfo_exists() and not gui.errors, gui.errors


def test_included_country_reason_and_runs_addendum_in_print(gui, paths):
    from lucid_warrant.executor import run_template

    c = gui.app.compose
    c.country_mode.set("only")
    c._country_mode_changed()
    with mock.patch("lucid_warrant.ui.widgets.CountryDialog.ask",
                    return_value={"code": "US", "reason": "Home country of client CL-100482"}) as ask:
        gui.click(c.include_add)
    assert ask.call_args.kwargs["reason_required"] is False
    assert c.include_rows == [{"code": "US", "reason": "Home country of client CL-100482"}]
    assert c.include_table.tree.set("0", "reason") == "Home country of client CL-100482"
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    sign_from_compose(gui)
    doc_html = gui.prompts[-1]
    assert "Included country" in doc_html and "Home country of client CL-100482" in doc_html
    assert "Countries searched" not in doc_html and "sent with every search in the payload" in doc_html
    (w,) = list_warrants(paths, load_public_key(paths))
    assert w["doc"]["country_scope"]["include"] == [{"code": "US", "reason": "Home country of client CL-100482"}]
    term = w["doc"]["terms"][0]["term_id"]
    run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": term})
    gui.app.refresh_all()
    printed = gui.app.warrants_tab.export_html(gui.app.warrants_tab.warrants[w["warrant_id"]])
    assert "Addendum A. Searches Run Under This Warrant" in printed and "Run 1 of 1" in printed
    assert printed.index("IX. Signature") < printed.index("class='footer'") < printed.index("Addendum A") < printed.index("Run 1 of 1")
    assert "&quot;purpose&quot;" in printed, "the payload, purpose included, is printed"


def test_every_warrant_status_has_a_consistent_message():
    from lucid_warrant.ui.warrants import RECORD_ONLY, status_message

    d = {"expires_at": "2026-10-17T00:00:00Z"}
    active = status_message({"status": "active"}, {"expires_at": "2099-01-01T00:00:00Z"})
    assert active[0] == "Active" and "can run searches" in active[1]
    for status, heading in (("expired", "Expired · record only"), ("revoked", "Revoked · record only"),
                            ("superseded", "Replaced · record only")):
        title, text, _tone = status_message({"status": status, "superseded_by": "w-new"}, d)
        assert title == heading and RECORD_ONLY in text, status


def test_runs_table_shows_the_warrant(gui, paths):
    from lucid_warrant.executor import run_template

    c = gui.app.compose
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    sign_from_compose(gui)
    (w,) = list_warrants(paths, load_public_key(paths))
    run_template(paths, warrant_id=w["warrant_id"], template_id="single-term",
                 bindings={"term": w["doc"]["terms"][0]["term_id"]})
    gui.app.refresh_all()
    tree = gui.app.runs.table.tree
    assert tree.heading("warrant", "text") == "Warrant"
    assert [tree.set(i, "warrant") for i in tree.get_children()] == [w["warrant_id"]]
    assert int(tree.column("warrant", "width")) >= tkfont_measure(w["warrant_id"]), "the full ID is visible"


def tkfont_measure(text):
    from tkinter import font as tkfont

    return tkfont.nametofont("TkDefaultFont").measure(text)


def test_replacing_a_warrant_warns_and_strands_its_requests(gui, paths):
    from lucid_warrant.onetime import submit_request

    c = gui.app.compose
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    sign_from_compose(gui)
    (w,) = list_warrants(paths, load_public_key(paths))
    submit_request(paths, load_public_key(paths), warrant_id=w["warrant_id"], skeleton="{a}",
                   bindings={"a": w["doc"]["terms"][0]["term_id"]}, rationale="probe")
    gui.app.refresh_all()
    gui.app.warrants_tab.select(w["warrant_id"])
    gui.app.warrants_tab.edit()
    gui.pump(0.2)
    certify(gui)
    gui.answers["askyesno"].append(False)  # decline the warning: nothing is signed
    gui.click(c.sign_button)
    assert len(list_warrants(paths, load_public_key(paths))) == 1
    gui.answers["askyesno"].append(True)
    gui.click(c.sign_button)
    gui.app.refresh_all()
    stages = {iid: item["stage"] for iid, item in gui.app.approvals.items.items()}
    assert list(stages.values()) == ["Unavailable"], stages
    item = next(iter(gui.app.approvals.items.values()))
    assert "re-request under" in item["status"]


def test_text_prompts_are_themed_and_readable(gui):
    """The built-in simpledialog drew Cancel as white text on a white macOS button under dark schemes."""
    from lucid_warrant.ui import theme
    from lucid_warrant.ui.widgets import TextDialog

    dialog = TextDialog(gui.app, "Edit reason", "Reason for excluding PL (Poland):", "GDPR")
    gui.pump(0.1)
    buttons = [w for w in dialog.frame.winfo_children()[-1].winfo_children()]
    assert all(isinstance(b, ttk.Button) for b in buttons) and {b.cget("text") for b in buttons} == {"OK", "Cancel"}
    dialog.value.set("GDPR (EU member state)")
    dialog._ok()
    assert dialog.result == "GDPR (EU member state)"
    import inspect
    from lucid_warrant.ui import compose
    assert "simpledialog" not in inspect.getsource(compose)


# --- rejection reasons and renomination waits ----------------------------------------------------

def test_reject_asks_for_reason_and_wait_then_records_them(gui, paths):
    from lucid_warrant.review import load_rejected

    c = gui.app.compose
    test_row = next(i for i in c.tree.get_children() if c.tree.set(i, "literal") == "TEST_DATA")
    c.tree.selection_set(test_row)
    gui.answers["reject"].append(None)  # cancelled: nothing changes
    gui.key(c.tree, "r")
    assert c.terms[int(test_row[1:])]["decision"] == "pending"
    gui.answers["reject"].append({"reason": "Test records are out of scope for DEMO-42.", "minutes": 120})
    c.tree.selection_set(test_row)
    gui.key(c.tree, "r")
    term = c.terms[int(test_row[1:])]
    assert term["decision"] == "rejected" and term["reassess_minutes"] == 120
    assert "renominate: 2 hours" in c.tree.set(test_row, "status")
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    sign_from_compose(gui)
    entry = load_rejected(paths)["TEST_DATA"]
    assert entry["reason"] == "Test records are out of scope for DEMO-42." and entry["renominate_after"]


def test_warrant_actions_include_print_and_runs_and_wrap(gui, paths):
    c = gui.app.compose
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    sign_from_compose(gui)
    (w,) = list_warrants(paths, load_public_key(paths))
    gui.app.show_warrant(w["warrant_id"])
    gui.pump(0.3)
    view = gui.app.warrants_tab.inspector
    labels = [b.cget("text") for b in view.actions.winfo_children() if isinstance(b, ttk.Button)]
    assert labels == ["Edit…", "Revoke…", "Print / Save as PDF…", "Runs under this warrant  →"]
    assert "Print / Save as PDF…" not in [t for t in texts(view.body).splitlines()]
    gui.app.geometry("1024x680")
    gui.pump(0.4)
    for b in view.actions.winfo_children():
        right = b.winfo_x() + b.winfo_width()
        assert right <= view.actions.winfo_width() + 1, f"{b.cget('text')} is clipped"


def test_warrants_and_runs_open_split_50_50(gui):
    for tab in (gui.app.warrants_tab, gui.app.runs):
        gui.app.notebook.select(tab)
        gui.pump(0.4)
        panes = next(w for w in tab.winfo_children() if isinstance(w, ttk.PanedWindow))
        assert abs(panes.sashpos(0) - panes.winfo_width() / 2) <= 6, (tab, panes.sashpos(0), panes.winfo_width())


# --- tutorial and first launch ----------------------------------------------------------------

def test_tutorial_pages_navigation_and_sections(gui):
    from lucid_warrant.ui.tutorial import FIRST_TOUR_PAGE, PAGES

    app = gui.app
    tour = app.tutorial()
    gui.pump(0.2)
    assert app.tutorial() is tour, "one tutorial at a time"
    assert tour.heading.cget("text") == "What Lucid Warrant does" and str(tour.prev_button.cget("state")) == "disabled"
    assert tour.counter.cget("text") == f"1 of {len(PAGES)}"
    tour.next()
    assert str(app.paths.data.parent) in tour.text.get("1.0", "end-1c"), "names the warrant directory"
    tour.next()
    assert "Your signing key is in" in " ".join(str(w.cget("text")) for w in tour.extra.winfo_children())
    tour.next()
    gui.pump(0.2)
    assert tour.index == FIRST_TOUR_PAGE and app.notebook.select() == str(app.compose)
    seen = set()
    while tour.index < len(PAGES) - 1:
        tour.next()
        gui.pump(0.05)
        seen.add(app.notebook.select())
    assert {str(app.warrants_tab), str(app.templates), str(app.approvals), str(app.runs)} <= seen
    assert tour.next_button.cget("text") == "Finish"
    tour.previous()
    assert tour.next_button.cget("text") == "Next"
    tour.next(); tour.next()
    assert not tour.winfo_exists() and tour.finished
    assert not gui.errors


def test_tutorial_readable_at_largest_size_on_small_screen(gui, monkeypatch):
    from tkinter import font as tkfont
    from lucid_warrant.ui import theme
    from lucid_warrant.ui.tutorial import Tutorial

    app = gui.app
    app.geometry("1024x680+0+0")
    # Exercise the real widgets and layout against a small display's bounds,
    # even when the developer running this has a larger monitor.
    monkeypatch.setattr("lucid_warrant.ui.tutorial.work_area", lambda _owner: (0, 30, 1024, 640))
    monkeypatch.setenv("LUCID_WARRANT_THEME", "dark")
    app.tk.call("tk", "scaling", 4 / 3)  # Windows at 96 DPI
    theme.apply(app, {**app.settings["ui"], "theme_mode": "dark", "base_font_pt": 9})
    tour = Tutorial(app)
    try:
        gui.pump()
        font = tkfont.nametofont(tour.text.cget("font"))
        assert font.cget("size") >= tkfont.nametofont("TkDefaultFont").cget("size") + 2
        assert str(tour.text.cget("state")) == "disabled"
        assert int(tour.text.cget("spacing2")) > 0 and int(tour.text.tag_cget("paragraph", "spacing3")) > 0
        assert tour.text.get("1.0", "end-1c").count("\n") >= 2
        # 20 is the largest Appearance choice; configuration also allows 24.
        for size, mode in ((20, "light"), (24, "dark")):
            tour.phase, tour.index = "tour", 3
            tour.show()
            monkeypatch.setenv("LUCID_WARRANT_THEME", mode)
            theme.apply(app, {**app.settings["ui"], "theme_mode": mode, "base_font_pt": size})
            gui.pump()
            assert font.cget("size") >= size + 2, "an open tutorial follows text size changes"
            assert tour.box.bars and all(bar.cget("bg") == theme.HIGHLIGHT[mode] for bar in tour.box.bars)
            for phase in ("tour", "intro"):
                tour.phase = phase
                for index in range(tour.last_index() + 1):
                    tour.index = index
                    tour.show()
                    gui.pump(0.3)  # the tutorial places itself again once the page has settled
                    assert tour.winfo_viewable()
                    assert tour.cget("bg") == tour.text.cget("bg") == theme.PALETTE["surface"]
                    assert tour.cget("bg") != app.cget("bg")
                    assert tour.cget("highlightbackground") == theme.HIGHLIGHT[mode]
                    assert int(tour.cget("highlightthickness")) == 1
                    assert tour.counter.cget("bg") == theme.PALETTE["accent_soft"]
                    assert str(ttk.Style().lookup(tour.frame.cget("style"), "background")) == theme.PALETTE["surface"]
                    assert 600 <= tour.text.winfo_width() <= 640
                    assert tour.winfo_width() <= 1024 and tour.winfo_height() <= 600, (size, phase, index, tour.geometry())
                    assert 0 <= tour.winfo_rootx() and tour.winfo_rootx() + tour.winfo_width() <= 1024
                    assert 0 <= tour.winfo_rooty() and tour.winfo_rooty() + tour.winfo_height() <= 680
                    for button in (tour.close_button, tour.prev_button, tour.next_button):
                        assert button.winfo_ismapped()
                        assert button.winfo_rooty() + button.winfo_height() <= tour.winfo_rooty() + tour.winfo_height()
                    assert tour.text.dlineinfo("1.0") is not None, "each page opens at its start"
                    if tour.text.yview()[1] < 1:
                        assert tour.scrollbar.winfo_ismapped()
                        tour.scrollbar.tk.call(tour.scrollbar.cget("command"), "moveto", 1)
                        gui.pump()
                        assert tour.text.dlineinfo("end-1c") is not None, "the final paragraph is reachable"
                        assert tour.text.yview()[0] > 0, "scrolling actually moved the page"
    finally:
        tour.destroy()


def test_tutorial_outlines_each_section_and_points_at_it(gui):
    from lucid_warrant.ui import theme
    from lucid_warrant.ui.tutorial import PAGES

    app = gui.app
    app.geometry("1100x700+0+0")
    gui.pump(0.3)
    tour = app.tutorial()
    gui.pump(0.2)
    assert not tour.box.bars, "no outline on a page about nothing on screen"

    def rect(w):
        return w.winfo_rootx(), w.winfo_rooty(), w.winfo_rootx() + w.winfo_width(), w.winfo_rooty() + w.winfo_height()

    for index, (_title, _text, target) in enumerate(PAGES):
        if not isinstance(target, tuple):
            continue
        tour.index = index
        tour.show()
        gui.pump(0.2)
        bars = tour.box.bars
        assert len(bars) == 4 and all(b.winfo_ismapped() for b in bars), target
        assert {b.cget("bg") for b in bars} == {theme.HIGHLIGHT[theme.MODE]}
        left, top, right, bottom = rect(bars[0])[0], rect(bars[0])[1], rect(bars[1])[2], rect(bars[1])[3]
        heading = app.compose.sections[target[1]]
        hl, ht, hr, hb = rect(heading)
        assert left <= hl and hr <= right and top <= ht and hb <= bottom, f"{target}: the outline surrounds its heading"
        if tour.placed:  # beside the outline: the tutorial doesn't cover it, and the arrow faces it
            tl, tt, tr, tb = rect(tour)
            seen = tour.box.screen_rect()
            sl, st, sr, sb = seen[0], seen[1], seen[0] + seen[2], seen[1] + seen[3]
            assert tr <= sl or tl >= sr or tb <= st or tt >= sb, f"{target}: tutorial clear of the outline"
            assert tour.arrow.winfo_ismapped() and tour.arrow.cget("fg") == theme.HIGHLIGHT[theme.MODE]
    tour.index = PAGES.index(next(page for page in PAGES if page[2] == "runs"))
    tour.show()
    gui.pump(0.2)
    assert tour.box.container is app.runs and all(b.winfo_ismapped() for b in tour.box.bars), "a whole tab is outlined"
    tour.close()
    gui.pump(0.1)
    assert not any(isinstance(w, tk.Frame) and w.cget("bg") == theme.HIGHLIGHT[theme.MODE]
                   for w in app.runs.winfo_children()), "closing removes the outline"
    assert "LWSectionBox" not in app.runs.bindtags()
    assert not gui.errors

def test_first_launch_chooses_directory_creates_key_and_tours(tmp_path, monkeypatch):
    if not CHILD:
        return
    from lucid_warrant import workspace
    from lucid_warrant.signing import keys_exist
    from lucid_warrant.ui import app as appmod, tutorial as tut, widgets as w
    from lucid_warrant.config import Paths

    monkeypatch.setattr(workspace, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setenv("LUCID_WARRANT_PREFS", str(tmp_path / "prefs.json"))
    for var in ("LUCID_WARRANT_DATA", "LUCID_WARRANT_KEYS", "LUCID_WARRANT_WORKSPACE", "LUCID_WARRANT_ORG_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    target = tmp_path / "Chosen"
    passphrases = []

    def run_intro(self):
        assert self.phase == "intro" and str(self.next_button.cget("state")) == "normal"
        light_bg = self.cget("bg")
        self.mode_choice.set("dark")
        self.scheme_choice.set("Green")
        self._apply_appearance()  # what the radio button and the list call
        self.update()
        assert self.cget("bg") != light_bg and self.app.cget("bg") != self.cget("bg"), "the tutorial keeps its own surface"
        saved = json.loads((tmp_path / "prefs.json").read_text("utf-8"))["ui"]
        assert {"theme_mode": "dark", "scheme": "green"}.items() <= saved.items(), "kept for next time"
        self.next(); self.next()  # to the key page
        assert "How it looks" not in [str(w.cget("text")) for w in self.extra.winfo_children() if isinstance(w, ttk.Label)]
        assert str(self.next_button.cget("state")) == "disabled", "can't move on without a key"
        self._create_key()
        assert str(self.next_button.cget("state")) == "normal"
        self.next()
        return self.finished

    with mock.patch.object(w.WorkspaceDialog, "ask", return_value=target), \
            mock.patch.object(w.PassphraseDialog, "ask", side_effect=lambda *a, **k: passphrases.append(a[1]) or "practice-passphrase"), \
            mock.patch.object(messagebox, "askyesno", return_value=True), \
            mock.patch.object(tut.Tutorial, "run", run_intro):
        app = appmod.App(Paths.default(), None, first_run=workspace.needs_setup())
        try:
            assert app.ready and keys_exist(app.paths) and app.paths.data == target / "data"
            assert workspace.configured() == target.resolve() or workspace.configured() == target
            assert app.paths.library.exists(), "the starter library is signed with the new key"
            assert passphrases == ["Choose a signing passphrase"], "no unlock prompt straight after creating the key"
            end = time.monotonic() + 1.0
            while time.monotonic() < end and not getattr(app, "tutorial_window", None):
                app.update()
                time.sleep(0.02)
            assert app.tutorial_window.index == tut.FIRST_TOUR_PAGE, "the tour continues over the main window"
        finally:
            app.destroy()


def test_agent_message_has_its_own_box_and_can_be_dismissed(gui, paths):
    from lucid_warrant.review import read_agent_note

    doc = json.loads(paths.proposal.read_text())
    paths.proposal.write_text(json.dumps({**doc, "note": "A 90-day window would cover the invoices.",
                                          "note_at": "2026-10-17T12:00:00Z"}))
    c = gui.app.compose
    gui.pump(2.5)  # the inbox is polled every two seconds
    assert c.agent_box.winfo_ismapped() and c.agent_text.cget("text") == "A 90-day window would cover the invoices."
    assert c.agent_heading.cget("text").startswith("Message from the agent · received")
    assert c.agent_box.winfo_rooty() < c.sections["terms"].winfo_rooty() + 40, "at the top, not under Purpose"
    gui.click(find(c.agent_box, text="Dismiss"))
    assert not c.agent_box.winfo_ismapped() and read_agent_note(paths) is None
    assert json.loads(paths.proposal.read_text())["purpose"] == doc["purpose"], "only the note is cleared"


def test_compose_keeps_a_readable_width_on_wide_screens(gui):
    from lucid_warrant.ui.compose import PAGE_WIDTH

    app = gui.app
    app.geometry(f"{min(app.winfo_screenwidth(), 2400)}x900")
    gui.pump(0.4)
    canvas, body = app.compose.page.canvas, app.compose.page.body
    if canvas.winfo_width() <= PAGE_WIDTH:
        return  # this screen isn't wide enough to test
    assert body.winfo_width() == PAGE_WIDTH
    left = body.winfo_rootx() - canvas.winfo_rootx()
    right = canvas.winfo_width() - (left + body.winfo_width())
    assert abs(left - right) <= 2, "centered"


def test_run_shows_returns_terms_and_summary_before_the_payload(gui, paths):
    from lucid_warrant.executor import run_template
    from lucid_warrant.summaries import write_summary

    c = gui.app.compose
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    sign_from_compose(gui, attrs=("ATTR_1000", "ATTR_1200"))
    (w,) = list_warrants(paths, load_public_key(paths))
    run = run_template(paths, warrant_id=w["warrant_id"], template_id="single-term",
                       bindings={"term": w["doc"]["terms"][0]["term_id"]})
    assert run["count"] >= 1
    write_summary(paths, run["run_id"], "rec-001 shows the kickoff; nothing on invoices.")
    gui.app.refresh_all()
    gui.app.show_run(run["run_id"])
    gui.pump(0.3)
    text = texts(gui.app.runs.inspector)
    assert "What came back" in text and "Records containing each hit term, by country" in text
    assert "Records" in text and "Full results" in text, "a profile and the export command, not a list"
    boxes = [w for w in _all_widgets(gui.app.runs.inspector.body) if isinstance(w, tk.Text)]
    assert any("All records" in b.get("1.0", "end") for b in boxes), "the hit term × country pivot"
    assert "rec-001 shows the kickoff" in text and "its words, not verified" in text
    body = gui.app.runs.inspector.body
    came_back, payload = (find(body, cls=ttk.Label, text=t) for t in ("What came back", "Payload sent"))
    assert came_back.winfo_y() < payload.winfo_y(), "results and summary come before the payload"
    printed = gui.app.warrants_tab.export_html(gui.app.warrants_tab.warrants[w["warrant_id"]])
    page = printed[printed.index("Run 1 of 1"):]
    assert page.index("What came back") < page.index("All records") < page.index("Agent&#x27;s summary") < page.index("Payload sent")
    assert "rec-001 shows the kickoff" in page


def test_wheel_scrolls_over_the_margins_of_a_centered_page(gui):
    page = gui.app.compose.page
    gui.app.geometry(f"{min(gui.app.winfo_screenwidth(), 2400)}x700")
    gui.pump(0.4)
    before = page.canvas.yview()[0]
    page._on_wheel(type("E", (), {"widget": page.canvas, "delta": -5 if os.name != "nt" else -600, "num": None})())
    gui.pump(0.1)
    assert page.canvas.yview()[0] > before, "the background beside the content scrolls the page too"



def _all_widgets(widget):
    for child in widget.winfo_children():
        yield child
        yield from _all_widgets(child)


def test_scrolling_stays_cheap(gui):
    """The tab pane's border is drawn, not a window-sized image (which made each scroll step ~60 ms on macOS),
    and a burst of wheel events scrolls once."""
    assert ttk.Style().layout("TNotebook")[0][0] == "Plain.Notebook.border"
    assert ttk.Style().layout("Treeview")[0][0] == "Plain.Treeview.field"
    page = gui.app.compose.page
    gui.pump(0.2)
    calls = []
    real = page.canvas.yview_scroll
    page.canvas.yview_scroll = lambda *a: calls.append(a) or real(*a)
    try:
        for _ in range(10):
            page._on_wheel(type("E", (), {"widget": page.body, "delta": -1 if os.name != "nt" else -120, "num": None})())
        gui.pump(0.1)
    finally:
        del page.canvas.yview_scroll
    assert len(calls) == 1, "ten events, one redraw"


def test_round7_destination_chip_follows_a_signed_config_change(gui):
    """Codex round 7 #14: the status bar mustn't keep saying "mock search" after searches were pointed elsewhere."""

    app = gui.app
    chips = lambda: " ".join(w.cget("text") for w in app.destination.winfo_children())  # noqa: E731
    assert "mock search" in chips()
    sign_org_file(app.paths, {"schema_version": 1, "search_backend": {"type": "http", "http": {
        "url": "https://records.example/search", "body": {"q": "{query}"}, "results_path": "hits",
        "fields": {"record_id": "id"}}}}, passphrase=PASSPHRASE)
    gui.pump((app.POLL_MS + 600) / 1000)
    assert "Searches go to records.example" in chips() and "mock search" not in chips()
    (app.paths.data / "organization.json").write_text('{"schema_version": 1}')  # edited after signing
    gui.pump((app.POLL_MS + 600) / 1000)
    assert "SEARCHES REFUSED" in chips()


def test_message_dialogs_theme_position_keyboard_and_modal_return(monkeypatch):
    if not CHILD:
        return
    from lucid_warrant.ui import theme
    from lucid_warrant.ui.widgets import MessageDialog

    monkeypatch.setenv("LUCID_WARRANT_THEME", "dark")
    root = tk.Tk()
    errors, seen = [], []
    try:
        theme.apply(root, {"theme_mode": "dark"})
        root.geometry("760x500+100+100")
        parent = ttk.Frame(root)
        parent.pack(fill="both", expand=True)
        root.update()
        root.grab_set()

        def answer(action):
            dialog = next(w for w in parent.winfo_children() if isinstance(w, MessageDialog))
            try:
                root.update()
                assert dialog.winfo_viewable() and dialog.grab_current() is dialog
                assert dialog.cget("bg") == theme.PALETTE["bg"]
                style = ttk.Style(root)
                assert str(style.lookup("TFrame", "background")) == theme.PALETTE["bg"]
                assert str(style.lookup("TLabel", "foreground")) == theme.PALETTE["text"]
                assert int(dialog.message.cget("wraplength")) == 480
                size, x, y = dialog.geometry().split("+")
                width, height = map(int, size.split("x"))
                assert abs(int(x) + width / 2 - (root.winfo_rootx() + root.winfo_width() / 2)) <= 2
                assert abs(int(y) + height / 2 - (root.winfo_rooty() + root.winfo_height() / 2)) <= 2
                assert all(isinstance(button, ttk.Button) for button in dialog.buttons.values())
                seen.append(dialog.message.cget("text"))
                if action == "close":
                    dialog.tk.call(dialog.protocol("WM_DELETE_WINDOW"))
                elif action in dialog.buttons:
                    dialog.buttons[action].invoke()
                else:
                    dialog.focus_force()
                    root.update()
                    dialog.event_generate(f"<KeyPress-{action}>")
                    root.update()
                assert not dialog.winfo_exists(), "the chosen action closes the message"
            except Exception as exc:
                errors.append(exc)
            finally:
                if dialog.winfo_exists():
                    dialog.destroy()

        for helper, action, default, expected in (
                (messagebox.askyesno, "Return", "no", False),
                (messagebox.askyesno, "yes", "no", True),
                (messagebox.askyesno, "Escape", "yes", False),
                (messagebox.askokcancel, "Return", "ok", True),
                (messagebox.askokcancel, "close", "ok", False),
                (messagebox.showinfo, "Return", "ok", "ok"),
                (messagebox.showwarning, "Escape", "ok", "ok"),
                (messagebox.showerror, "ok", "ok", "ok")):
            root.after(100, lambda action=action: answer(action))
            result = helper("Review", "Check this message.", parent=parent, default=default)
            assert not errors, errors
            assert result == expected and type(result) is type(expected)
            assert root.grab_current() is root, "the parent dialog is still modal"
        assert len(seen) == 8
    finally:
        root.destroy()


def test_dialogs_open_where_they_belong(gui):
    """Not at Windows' default spot in the top-left corner: the calendar opens at the button that opened it,
    other dialogs centered over the window."""
    from lucid_warrant.ui import widgets

    app = gui.app
    app.geometry("1100x700+300+150")
    gui.pump(0.3)
    c = app.compose
    c.page.scroll_to(c.add_range_button)
    gui.pump(0.2)
    gui.answers["range"].append(None)
    c.add_blocked_range()
    assert widgets.DateRangeDialog.ask.call_args.kwargs["anchor"] is c.add_range_button, "Compose anchors it"
    seen = {}
    for dialog in (widgets.DateRangeDialog(c), widgets.TextDialog(app, "Title", "Prompt")):
        dialog.anchor = c.add_range_button if isinstance(dialog, widgets.DateRangeDialog) else None
        dialog.place_on_screen()
        gui.pump(0.2)
        size, x, y = dialog.geometry().split("+")  # the window's outer position (rootx/rooty is inside its frame)
        seen[type(dialog).__name__] = (int(x), int(y), *map(int, size.split("x")))
        dialog.destroy()
    button = c.add_range_button
    x, y, _w, _h = seen["DateRangeDialog"]
    bx, by = button.winfo_rootx(), button.winfo_rooty()
    _x, _y, _w, dialog_h = seen["DateRangeDialog"]
    below = abs(y - (by + button.winfo_height() + 4)) <= 2
    above = y + dialog_h <= by  # no room below (e.g. above macOS's Dock): just above, title bar included
    assert abs(x - bx) <= 2 and (below or above), (seen, bx, by)
    x, y, w, h = seen["TextDialog"]
    middle = app.winfo_rootx() + app.winfo_width() // 2
    assert abs(x + w // 2 - middle) <= 20 and y > app.winfo_rooty(), (seen, middle)
    assert not gui.errors


def test_places_need_a_chosen_country_inside_it_before_approval(gui):
    """An agent-proposed place arrives pending; it can't be approved until the analyst chooses the country it's
    in (and the point is inside it); the signed warrant carries it with its own radius."""
    import json as _json

    from lucid_warrant.ui import places_panel

    app, c = gui.app, gui.app.compose
    app.paths.inbox.mkdir(parents=True, exist_ok=True)
    app.paths.proposal.write_text(_json.dumps({"places": [{"label": "Istanbul office", "lat": 41.0082, "lon": 28.9784,
                                                           "radius_km": 0.1, "reason": "Client's office",
                                                           "country_suggested": "TR"}]}))
    c.merge_inbox()
    panel = c.places_panel
    assert [r["decision"] for r in panel.rows] == ["pending"] and panel.rows[0]["country"] == ""
    assert "agent suggests TR" in panel.table.tree.item("0")["values"][4]
    panel.table.tree.selection_set("0")
    with mock.patch.object(places_panel.PlaceDialog, "ask", return_value=None):
        panel.approve()  # no country chosen: the dialog is cancelled, so it stays pending
    assert panel.rows[0]["decision"] == "pending"
    panel.table.tree.selection_set("0")
    with mock.patch.object(places_panel.PlaceDialog, "ask", return_value={**panel.rows[0], "country": "GR", "country_confirmed": True}):
        panel.approve()  # Istanbul isn't in Greece
    assert panel.rows[0]["decision"] == "pending" and any("isn't inside Greece" in str(s) for s in gui.shown)
    panel.table.tree.selection_set("0")
    with mock.patch.object(places_panel.PlaceDialog, "ask", return_value={**panel.rows[0], "country": "TR", "country_confirmed": True}):
        panel.approve()
    assert panel.rows[0]["decision"] == "approved"
    assert panel.for_warrant()[0]["radius_km"] == 0.1 and panel.for_warrant()[0]["country"] == "TR"
    c.country_mode.set("only")
    c.include_rows = [{"code": "AF", "reason": ""}]
    c._country_mode_changed()
    assert panel.rows[0]["decision"] == "pending", "Only these countries: Afghanistan, so the Turkey place can't stay approved"
    assert "isn't permitted" in panel.table.tree.item("0")["values"][0] and panel.for_warrant() == []
    c.include_rows.append({"code": "TR", "reason": ""})
    c._render_scope()
    assert panel.rows[0]["decision"] == "pending", "not re-approved behind the analyst's back"
    panel.table.tree.selection_set("0")
    panel.decide("approved")
    assert panel.rows[0]["decision"] == "approved" and "isn't permitted" not in panel.table.tree.item("0")["values"][0]


def test_converter_shows_lat_lon_and_a_warning(gui):
    panel = gui.app.compose.places_panel
    panel.coordinate.set("35TPF6637041552")
    panel.convert()
    assert "41.008200, 28.978400" in panel.result.cget("text") and "MGRS" in panel.result.cget("text")
    assert "Check this conversion yourself" in panel.warning.cget("text")
    assert str(panel.add_converted.cget("state")) == "normal"
    panel.coordinate.set("151.2, -33.8")
    panel.convert()
    assert "looks like lon, lat" in panel.result.cget("text") and str(panel.add_converted.cget("state")) == "disabled"


def test_places_use_the_same_a_r_p_keys_and_buttons_as_hit_terms(gui):
    import json as _json

    from lucid_warrant.ui import places_panel

    app, c = gui.app, gui.app.compose
    app.paths.inbox.mkdir(parents=True, exist_ok=True)
    app.paths.proposal.write_text(_json.dumps({"places": [
        {"label": "Istanbul office", "lat": 41.0082, "lon": 28.9784, "radius_km": 0.1, "reason": "office"},
        {"label": "Ankara branch", "lat": 39.9334, "lon": 32.8597, "radius_km": 0.1, "reason": "branch"}]}))
    c.merge_inbox()
    panel = c.places_panel
    tree = panel.table.tree
    texts = [w.cget("text") for w in panel.winfo_children()[-1].winfo_children()]
    assert any("(A)" in t for t in texts) and any("(R)" in t for t in texts) and any("(P)" in t for t in texts)
    tree.focus_force()
    tree.selection_set("0")
    tree.focus("0")
    gui.pump(0.1)
    with mock.patch.object(places_panel.PlaceDialog, "ask", return_value={**panel.rows[0], "country": "TR", "country_confirmed": True}):
        tree.event_generate("<KeyPress-A>")  # Shift+A / Caps Lock
        gui.pump(0.1)
    assert panel.rows[0]["decision"] == "approved" and tree.selection() == ("1",), "moves to the next place"
    tree.event_generate("<KeyPress-r>")
    gui.pump(0.1)
    assert panel.rows[1]["decision"] == "rejected"
    tree.selection_set("1")
    tree.event_generate("<KeyPress-P>")
    gui.pump(0.1)
    assert panel.rows[1]["decision"] == "pending"
    assert tree.item("0", "tags") == ("approved",) and tree.item("1", "tags") == ("pending",)


def test_import_chooser_drop_box_and_malformed_files(gui):
    from pathlib import Path

    from lucid_warrant.ui import places_panel

    panel = gui.app.compose.places_panel
    here = Path(__file__).parent / "fixtures" / "places"
    chooser = places_panel.ImportChooser(panel, max_radius_km=25)
    assert chooser.can_drop, "drag and drop is available (tkinterdnd2)"
    assert places_panel.DROP_TEXT in chooser.drop.itemcget(chooser.drop_label, "text")
    chooser.choose(here / "malformed" / "zip_renamed.csv")
    assert "can't be imported" in chooser.error.cget("text") and "export the list as CSV" in chooser.error.cget("text")
    assert chooser.winfo_exists() and chooser.result is None, "a malformed file keeps the dialog open"
    chooser.choose(here / "sites.geojson")
    assert chooser.result == here / "sites.geojson"


def test_import_review_rows_can_be_completed(gui):
    from pathlib import Path

    from lucid_warrant.ui import places_panel

    panel = gui.app.compose.places_panel
    path = Path(__file__).parent / "fixtures" / "places" / "missing_fields.csv"
    review = places_panel.ImportReview(panel, path, room=50, **panel._context())
    statuses = [r["status"].cget("text") for r in review.rows]
    assert statuses[0] == "⚠ radius is blank" and statuses[1] == "⚠ label is blank"
    assert str(review.ok.cget("state")) == "disabled"
    review.fill("radius", "0.1")
    review.fill("reason", "From the client's site list")
    review.rows[1]["label"].set("Ankara branch")
    review.rows[0]["country"].set(places_panel.choice_for(review.choices, "TR"))
    review.rows[2]["country"].set(places_panel.choice_for(review.choices, "TR"))
    statuses = [r["status"].cget("text") for r in review.rows]
    assert statuses[0] == "Country agrees with the bundled map" and "country to choose" in statuses[1] and statuses[2] == "⚠ at sea: not inside any country"
    assert str(review.ok.cget("state")) == "normal" and review.ok.cget("text") == "Import 3 places"
    review.rows[2]["include"].set(False)
    review._ok()
    places = review.result
    assert [p["label"] for p in places] == ["Istanbul office", "Ankara branch"] and places[0]["radius_km"] == 0.1
    with mock.patch.object(places_panel.ImportReview, "ask", return_value=places):
        panel.import_list(path)
    assert [r["decision"] for r in panel.rows] == ["pending", "pending"]
    assert panel.table.tree.item("0")["values"][6] == "Imported: missing_fields.csv", "not shown as the analyst's own"
    panel.table.tree.selection_set("0")
    with mock.patch.object(places_panel.PlaceDialog, "ask", return_value=None) as asked:
        panel.decide("approved")  # the file's country must be confirmed; cancelling leaves it pending
    assert asked.call_args.kwargs["title"] == "Confirm where this place is" and panel.rows[0]["decision"] == "pending"
    panel.table.tree.selection_set("0")
    with mock.patch.object(places_panel.PlaceDialog, "ask", return_value={**panel.rows[0], "country_confirmed": True}):
        panel.decide("approved")
    assert panel.rows[0]["decision"] == "approved" and panel.rows[0]["country_confirmed"]
    small = places_panel.ImportReview(panel, path, room=1, **panel._context())
    assert [r["include"].get() for r in small.rows] == [True, False, False], "only as many ticked as fit"
    small.destroy()


def test_converter_says_which_country_on_one_line(gui):
    panel = gui.app.compose.places_panel
    panel.coordinate.set("14T LP 0476 4930")
    panel.convert()
    text = panel.result.cget("text")
    assert "\n" not in text and "43.771056, -101.425797" in text and "(US)" in text and "10 m square" in text


def test_times_are_shown_in_utc_by_default(gui):
    from lucid_warrant.ui import widgets

    assert gui.app.settings["ui"]["time_zone"] == "utc"
    assert widgets.local_time("2026-10-18T21:05:00Z") == "2026-10-18 21:05 UTC"
    widgets.set_time_zone("local")
    try:
        assert not widgets.local_time("2026-10-18T21:05:00Z").endswith("UTC") or widgets.shown_time(
            __import__("datetime").datetime.now().astimezone()).utcoffset().total_seconds() == 0
    finally:
        widgets.set_time_zone("utc")



def test_excluded_countries_are_listed_and_marked(gui):
    """Germany (DE) is excluded by default: it's still in the list, marked, and a place there can't be approved."""
    from lucid_warrant.ui import places_panel
    from lucid_warrant.scope import countries

    c = gui.app.compose
    choices = places_panel.country_choices(c.current_scope(), countries(gui.app.paths))
    de = places_panel.choice_for(choices, "DE")
    assert de.startswith("DE — ") and "(excluded: GDPR)" in de and choices[de] == "DE"
    assert "(excluded" not in places_panel.choice_for(choices, "TR")
    review = places_panel.ImportReview(c.places_panel, Path(__file__).parent.parent / "examples" / "places" /
                                       "places_latlon.csv", room=50, **c.places_panel._context())
    berlin = next(r for r in review.rows if r["label"].get() == "Berlin supplier")
    assert berlin["country"].get() == de and "isn't permitted" in berlin["status"].cget("text")
    review.destroy()



def test_a_warrant_with_places_signs_and_prints(gui, paths):
    """Codex round 8 A1: the signing document (and the printout) of a warrant with places must render."""
    from lucid_warrant.ui import places_panel

    c = gui.app.compose
    c.tree.selection_set("q0")
    gui.key(c.tree, "a")
    with mock.patch.object(places_panel.PlaceDialog, "ask", return_value={
            "place_id": "p-ny", "label": "NY office", "lat": 40.712776, "lon": -74.005974, "radius_km": 0.1,
            "country": "US", "reason": "Kickoff venue", "source": "human", "country_confirmed": True}):
        c.places_panel.add()
    sign_from_compose(gui)
    (w,) = list_warrants(paths, load_public_key(paths))
    assert w["doc"]["places"][0]["label"] == "NY office" and w["doc"]["places"][0]["radius_km"] == 0.1
    assert "NY office" in gui.prompts[-1] and "INTERSECTS(40.712776,-74.005974,0.1)" in gui.prompts[-1]
    from lucid_warrant.ui.document import render_html, warrant_blocks
    blocks = warrant_blocks(w["doc"], countries={"US": "United States"}, labels={}, library_line="the library")
    assert "NY office" in render_html(blocks, "x") and "US — United States" in render_html(blocks, "x")


def test_duplicate_places_cant_be_approved_or_imported(gui):
    """The same label (ignoring case and spacing), or the same point under another name, can't be approved
    alongside a place that's already approved; the table and the import review say why."""
    import json as _json

    from lucid_warrant.ui import places_panel

    app, c = gui.app, gui.app.compose
    app.paths.inbox.mkdir(parents=True, exist_ok=True)
    app.paths.proposal.write_text(_json.dumps({"places": [
        {"label": "Istanbul office", "lat": 41.0082, "lon": 28.9784, "radius_km": 0.1, "reason": "office"},
        {"label": "istanbul  OFFICE", "lat": 39.9334, "lon": 32.8597, "radius_km": 0.1, "reason": "same name"},
        {"label": "Old town desk", "lat": 41.008200115, "lon": 28.978399944, "radius_km": 0.1, "reason": "same point"}]}))
    c.merge_inbox()
    panel = c.places_panel
    assert len(panel.rows) == 3

    def approve(index, country="TR"):
        panel.table.tree.selection_set(str(index))
        with mock.patch.object(places_panel.PlaceDialog, "ask", return_value={**panel.rows[index], "country": country}):
            panel.approve()

    approve(0)
    assert panel.rows[0]["decision"] == "approved"
    approve(1)
    assert panel.rows[1]["decision"] == "pending"
    assert any("another place already has this label" in str(s) for s in gui.shown)
    assert "another place already has this label" in panel.table.tree.item("1")["values"][0], "the table says why"
    approve(2)
    assert panel.rows[2]["decision"] == "pending"
    assert any("same coordinates as 'Istanbul office'" in str(s) for s in gui.shown)
    assert [p["label"] for p in panel.for_warrant()] == ["Istanbul office"] and not panel.problems()

    panel.table.tree.selection_set("0")
    panel.reject()  # the original is rejected, so its twin is no longer a duplicate
    assert "same coordinates" not in panel.table.tree.item("2")["values"][0]
    approve(2)
    assert panel.rows[2]["decision"] == "approved"

    # Editing a place into a duplicate leaves it pending, with the reason.
    approve(0)  # back: now a duplicate of Old town desk's point
    assert panel.rows[0]["decision"] == "rejected"
    panel.table.tree.selection_set("1")
    with mock.patch.object(places_panel.PlaceDialog, "ask",
                           return_value={**panel.rows[1], "label": "OLD TOWN DESK", "country": "TR"}):
        panel.edit()
    assert panel.rows[1]["decision"] == "pending" and "another place already has this label" in panel.rows[1]["held"]

    # Import: a row that repeats a place already in Compose, or an earlier row in the file, can't be imported.
    csv_path = app.paths.data.parent / "dupes.csv"
    csv_path.write_text("name,lat,lon,radius_m,country,reason\n"
                        "Old town desk,40.0,30.0,100,TR,same label as a place in Compose\n"
                        "Harbour gate,41.0257,28.9744,100,TR,new\n"
                        "harbour GATE,41.037,28.985,100,TR,same label as the row above\n"
                        "Ferry pier,41.0257,28.9744,100,TR,same point as Harbour gate\n", encoding="utf-8")
    existing = [r for r in panel.rows if r["decision"] != "rejected"]
    review = places_panel.ImportReview(panel, csv_path, room=50, existing=existing, **panel._context())
    try:
        gui.pump(0.1)
        statuses = [r["status"].cget("text") for r in review.rows]
        assert "another place already has this label" in statuses[0]
        assert "⚠" not in statuses[1] and "✕" not in statuses[1]
        assert "another place already has this label" in statuses[2]
        assert "same coordinates as 'Harbour gate'" in statuses[3]
        assert str(review.ok.cget("state")) == "disabled" and "duplicate" in review.summary.cget("text")
        review.rows[1]["include"].set(False)  # untick Harbour gate: the two rows after it are no longer duplicates
        statuses = [r["status"].cget("text") for r in review.rows]
        assert "⚠" not in statuses[2] and "⚠" not in statuses[3], statuses
        review.rows[0]["include"].set(False)
        assert str(review.ok.cget("state")) == "normal"
    finally:
        review.destroy()
    assert not gui.errors


def test_round8_a_draft_keeps_its_configuration_until_a_new_draft(gui):
    """Codex round 8 A7: a newly signed configuration doesn't half-apply to the draft in progress: signing that
    draft is blocked, and Start new draft rebuilds Compose under the new configuration."""
    app = gui.app
    sign_org_file(app.paths, {"schema_version": 1, "organization": {"revision": 2},
                              "defaults": {"max_rows_per_run": 7}, "geography": {"max_radius_km": 1}},
                  passphrase=PASSPHRASE)
    gui.pump((app.POLL_MS + 600) / 1000)
    c = app.compose
    assert c.rows.get() != "7" and app.settings["organization"]["revision"] == 1, "the draft isn't rewritten"
    assert c.config_banner.winfo_ismapped() and "start a new draft" in c.footer_text.cget("text")
    c.sign()
    assert any("configuration changed" in str(s) for s in gui.shown)
    app.adopt_new_config()
    gui.pump(0.3)
    c = app.compose
    assert c.rows.get() == "7" and app.settings["geography"]["max_radius_km"] == 1
    assert not c.config_banner.winfo_ismapped() and app.notebook.index(c) == 0


def test_round8_changing_places_needs_the_limits_certified_again(gui):
    """Codex round 8 A13: approving or removing a place changes the certified limits."""
    from lucid_warrant.ui import places_panel

    c = gui.app.compose
    c.cert_limits.set(True)
    with mock.patch.object(places_panel.PlaceDialog, "ask", return_value={
            "place_id": "p-ny", "label": "NY office", "lat": 40.712776, "lon": -74.005974, "radius_km": 0.1,
            "country": "US", "reason": "r", "source": "human", "country_confirmed": True}):
        c.places_panel.add()
    assert not c.cert_limits.get(), "adding a place unticks the limits certification"
    c.cert_limits.set(True)
    c.places_panel.table.tree.selection_set("0")
    c.places_panel.remove()
    assert not c.cert_limits.get(), "removing the last place widens searches: certify again"


def test_round8_editing_a_lonlat_proposal_keeps_its_point(gui):
    """Codex round 8 A9: opening a place to choose its country must not re-read (and flip) its coordinate."""
    from lucid_warrant.ui import places_panel

    panel = gui.app.compose.places_panel
    row = {"place_id": "p-x", "label": "x", "lat": 60.0, "lon": 65.0, "radius_km": 0.1, "reason": "r",
           "given_as": "65, 60", "read_as": "decimal degrees (given as lon, lat)", "source": "agent", "country": ""}
    dialog = places_panel.PlaceDialog(panel, place=row, title="t", **panel._context())
    dialog.country.set(places_panel.choice_for(dialog.country_names, "RU"))
    dialog._ok()
    assert (dialog.result["lat"], dialog.result["lon"]) == (60.0, 65.0)


def test_round8_time_zone_change_rerenders_open_inspectors(gui):
    app = gui.app
    app.warrants_tab._shown = ("something",)
    app.invalidate_inspectors()
    assert app.warrants_tab._shown is None and app.runs._shown is None


def test_country_lists_can_be_searched(gui):
    """Type part of a code or name to narrow the list, on Windows and macOS alike."""
    from lucid_warrant.ui.widgets import SearchPicker

    var = tk.StringVar()
    choices = ["DE — Germany  (excluded: GDPR)", "TR — Turkey", "US — United States of America"]
    picker = SearchPicker(gui.app, choices, var)
    var.set("ger")
    picker._filter()
    assert list(picker.cget("values")) == ["", "DE — Germany  (excluded: GDPR)"]
    assert picker.resolve() == "DE — Germany  (excluded: GDPR)" and var.get().startswith("DE")
    var.set("tr")
    assert picker.resolve() == "TR — Turkey", "an exact code wins over other matches (e.g. 'Country')"
    var.set("zzz")
    assert picker.resolve() == "TR — Turkey", "no match: back to the last good value"
    var.set("")
    assert picker.resolve() == ""
    picker.destroy()


def test_round8_import_review_status_is_in_view(gui):
    """Codex round 8 B2: at a normal window size, each row's status (e.g. the Greece/Turkey mismatch) is visible."""
    from lucid_warrant.ui import places_panel

    app = gui.app
    app.geometry("1200x720+40+40")
    gui.pump(0.3)
    panel = app.compose.places_panel
    review = places_panel.ImportReview(panel, Path(__file__).parent.parent / "examples" / "places" / "places_latlon.csv",
                                       room=50, **panel._context())
    review.place_on_screen()
    gui.pump(0.4)
    right = review.winfo_rootx() + review.winfo_width()
    thessaloniki = next(r for r in review.rows if r["label"].get().startswith("Thessaloniki"))
    status = thessaloniki["status"]
    assert status.cget("text") == "⚠ in Greece, not Turkey (TR)"
    assert status.winfo_rootx() + 40 < right, "the status column starts inside the visible dialog"
    # Nothing cut off inside the label: on Windows (Segoe UI 8 pt) a width of 28 characters was 168 px while the
    # text wrapped at 210 px, so each line lost its end ("it's in Greec").
    for r in review.rows:
        status = r["status"]
        # The width the wrapped text needs: the same label with nothing limiting its width.
        probe = ttk.Label(review, text=status.cget("text"), style=status.cget("style"),
                          wraplength=status.cget("wraplength"), justify="left")
        needed = probe.winfo_reqwidth()
        probe.destroy()
        assert status.winfo_width() >= needed, (status.cget("text"), status.winfo_width(), needed)
        assert str(status.cget("width")) in ("", "0"), "sized in pixels by its column, not in characters of a font"
    review.destroy()


def test_round8_review_keys_are_deliberate(gui):
    """Codex round 8 B4 / round 9 #6: one decision per physical press, however long the key is held;
    several selected rows need one confirmation."""
    from lucid_warrant.ui import widgets

    c = gui.app.compose
    tree = c.tree
    tree.selection_set("q0")
    tree.focus("q0")
    tree.focus_force()
    gui.pump()
    tree.event_generate("<KeyPress-a>")
    gui.pump(0.5)
    tree.event_generate("<KeyPress-a>")  # still held after the initial repeat delay
    tree.event_generate("<KeyRelease-a>")
    tree.event_generate("<KeyPress-a>")  # X11 auto-repeat: release and press together
    gui.pump()
    assert [t["decision"] for t in c.terms[:2]] == ["approved", "pending"], "one press, one decision"
    tree.event_generate("<KeyRelease-a>")
    gui.pump(0.1)
    tree.event_generate("<KeyPress-A>")  # a new press (Shift / Caps Lock)
    tree.event_generate("<KeyRelease-A>")
    gui.pump(0.1)
    assert [t["decision"] for t in c.terms[:2]] == ["approved", "approved"]
    for t in c.terms[:2]:
        t["decision"] = "pending"
    c.render_terms()
    c = gui.app.compose
    c.tree.selection_set(("q0", "q1"))
    gui.answers["askyesno"].append(False)
    c.decide("approved")
    assert [t["decision"] for t in c.terms[:2]] == ["pending", "pending"], "declined: nothing included"
    gui.answers["askyesno"].append(True)
    c.decide("approved")
    assert [t["decision"] for t in c.terms[:2]] == ["approved", "approved"]

def test_round8_signing_document_starts_with_what_it_authorizes(gui, paths):
    """Codex round 8 B1: a summary first, and a removed site restriction said plainly."""
    from lucid_warrant.ui.document import glance_blocks

    doc = {"parameter_1": "0012", "parameter_2": ["ATTR_1000"], "purpose": "Find X", "terms": [{"literal": "A"}],
           "max_rows_per_run": 20000, "max_queries_per_hour": 60, "expires_at": "2026-10-19T12:00:00Z",
           "country_scope": {"include": [], "exclude": [{"code": "DE", "reason": "GDPR"}]}, "places": []}
    blocks = glance_blocks(doc, countries={}, labels={}, hits="Hit terms", replaces="w-20261018-000000-abcdef",
                           new_literals={"A"}, removed=[], old_places=[{"label": "NY office"}])
    text = str(blocks)
    assert "1,200,000 records an hour" in text and "All countries, except DE" in text
    assert "No site restriction" in text and "SITE RESTRICTION REMOVED" in text and "Places removed: NY office" in text


def test_round8_edit_keeps_status_undo_and_rejections_stick(gui):
    """Codex round 8 B9: editing isn't approving; decisions and removals can be undone; a rejected agent place
    doesn't come back after signing."""
    import json as _json

    from lucid_warrant.ui import places_panel

    app, c = gui.app, gui.app.compose
    panel = c.places_panel
    app.paths.inbox.mkdir(parents=True, exist_ok=True)
    app.paths.proposal.write_text(_json.dumps({"places": [
        {"label": "Istanbul office", "lat": 41.0082, "lon": 28.9784, "radius_km": 0.1, "reason": "office"},
        {"label": "Ankara branch", "lat": 39.9334, "lon": 32.8597, "radius_km": 0.1, "reason": "branch"}]}))
    c.merge_inbox()
    panel.table.tree.selection_set("0")
    with mock.patch.object(places_panel.PlaceDialog, "ask", return_value={
            **panel.rows[0], "country": "TR", "reason": "edited", "country_confirmed": True}):
        panel.edit()
    assert panel.rows[0]["decision"] == "pending" and panel.rows[0]["reason"] == "edited", "an edit isn't an approval"
    panel.table.tree.selection_set("1")
    panel.reject()
    panel.table.tree.selection_set("0")
    panel.remove()
    assert len(panel.rows) == 1
    panel.undo()
    assert len(panel.rows) == 2 and panel.rows[1]["decision"] == "rejected"
    panel.undo()
    assert panel.rows[1]["decision"] == "pending"
    panel.table.tree.selection_set("1")
    panel.reject()
    panel.record_rejections(app.paths)  # what signing does
    panel.rows = []
    c.merge_inbox()
    assert [r["label"] for r in panel.rows] == ["Istanbul office"], "the rejected place isn't proposed again"


def test_round8_warrant_inspector_lists_places(gui):
    """Codex round 8 B5: the enforced places are visible where the analyst looks before renewing or widening."""
    from lucid_warrant.ui.describe import scope_fields

    fields = []
    view = type("V", (), {"field": lambda self, label, value, **k: fields.append((label, value))})()
    doc = {"parameter_1": "0012", "parameter_2": ["ATTR_1000"], "expires_at": "2026-10-19T12:00:00Z",
           "max_rows_per_run": 10, "terms": [], "country_scope": {"include": [], "exclude": []},
           "places": [{"label": "NY office", "lat": 40.712776, "lon": -74.005974, "radius_km": 0.1, "country": "US"}]}
    scope_fields(view, doc)
    assert ("Places", "NY office (40.712776, -74.005974, 100 m, US)") in fields
    fields.clear()
    scope_fields(view, {**doc, "places": []})
    assert ("Places", "No site restriction") in fields


def test_round8_an_agent_purpose_keeps_its_origin_through_small_edits(gui, paths):
    """Codex round 8 B6: editing punctuation doesn't make an agent's draft the analyst's words."""
    c = gui.app.compose
    draft = "Find records linking PROJECT_CEDAR and SUPPLIER_NORTH for case DEMO-42."
    c.agent_purpose = draft
    c._set_purpose(draft, agent=True)
    assert c.purpose_origin() == "agent"
    c.purpose.insert("end", " Include invoices.")
    c._purpose_modified()
    assert c.purpose_origin() == "agent-edited" and "edited by you" in c.agent_note.cget("text")
    c.cert_purpose.set(True)
    assert "reviewed and certified by you" in c.agent_note.cget("text")
    c._set_purpose("Entirely my own wording about a different investigation into supplier pricing.")
    assert c.purpose_origin() == "analyst"
    from lucid_warrant.ui.document import warrant_blocks
    from lucid_warrant.warrant import build_warrant
    from lucid_warrant.rbac import get_rbac
    doc = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose=draft, ttl_minutes=60, max_rows=5,
                        terms=[{"term_id": "t-a", "literal": "A", "source": "agent"}], rbac=get_rbac(paths), paths=paths,
                        purpose_origin="agent-edited")
    assert "drafted by the agent, edited and certified" in str(warrant_blocks(doc, labels={}, countries={},
                                                                            library_line="x"))


def test_round8_defaults_show_their_combined_effect(gui):
    """Codex round 8 B7: the per-search cap isn't mistaken for a total; pending places don't limit anything."""
    import json as _json

    c = gui.app.compose
    c.rows.set("20000")
    c.rate.set("60")
    c.update_footer()
    assert c.limits_effect.cget("text") == "Together: up to 1,200,000 records an hour"
    gui.app.paths.inbox.mkdir(parents=True, exist_ok=True)
    gui.app.paths.proposal.write_text(_json.dumps({"places": [
        {"label": "Istanbul office", "lat": 41.0082, "lon": 28.9784, "radius_km": 0.1, "reason": "office"}]}))
    c.merge_inbox()
    note = c.places_panel.pending_note.cget("text")
    assert "won't limit searches until included" in note and "aren't limited to sites" in note
    assert c.places_panel.table.tree.item("0")["values"][3] == "100 m"


def test_round8_new_nominations_note_stays_until_reviewed(gui):
    """Codex round 8 B8: an arrival note on a timer was easy to miss; it stays until the analyst acts."""
    import json as _json

    c = gui.app.compose
    gui.app.paths.inbox.mkdir(parents=True, exist_ok=True)
    gui.app.paths.proposal.write_text(_json.dumps({"terms": [{"literal": "BRAND_NEW_TERM", "reason": "r"}]}))
    c.merge_inbox()
    assert "arrived" in c.inbox_note.cget("text")
    gui.pump(0.5)
    assert "arrived" in c.inbox_note.cget("text")
    c.tree.selection_set(c.tree.get_children()[0])
    c.decide("pending")
    assert c.inbox_note.cget("text") == ""


def test_about_reports_key_access_and_restricts_on_request(gui):
    if os.name == "nt":
        return
    from lucid_warrant.ui.dialogs import AboutDialog

    # A readable directory alone is fine; write access lets another account replace the keys.
    gui.app.paths.keys.chmod(0o777)
    about = AboutDialog(gui.app)
    assert "keys folder can be changed by" in about.key_access.cget("text") and about.restrict_button.winfo_manager()
    about._restrict()
    assert "Only you" in about.key_access.cget("text") and not about.restrict_button.winfo_manager()
    about.destroy()


def test_round9_import_precision_survives_confirmation_and_signing(gui, tmp_path):
    from lucid_warrant import geo
    from lucid_warrant.ui import places_panel

    panel = gui.app.compose.places_panel
    path = tmp_path / "grid.csv"
    path.write_text('name,coordinate,radius_m,country,reason\nOffice,35TPF6641,100,TR,Review\n')
    review = places_panel.ImportReview(panel, path, room=50, **panel._context())
    review.place_on_screen()
    gui.pump()
    row = review.rows[0]
    assert "uncertainty" in row["status"].cget("text")
    assert "35TPF6641" in row["evidence"].cget("text") and "1000 m square" in row["evidence"].cget("text")
    assert row["evidence"].winfo_ismapped()
    assert "Country agrees with the bundled map" in row["status"].cget("text")
    row["country"].set(places_panel.choice_for(review.choices, "GR"))
    assert "in Turkey, not Greece (GR)" in row["status"].cget("text") and "uncertainty" in row["status"].cget("text")
    row["country"].set(places_panel.choice_for(review.choices, "TR"))
    review._ok()
    with mock.patch.object(places_panel.ImportReview, "ask", return_value=review.result):
        panel.import_list(path)
    place = panel.rows[0]
    dialog = places_panel.PlaceDialog(panel, place=place, title="Review", **panel._context())
    gui.pump()
    assert "1000 m square" in dialog.preview.cget("text")
    assert "Country agrees with the bundled map" in dialog.preview.cget("text")
    gui.answers["askyesno"].append(False)
    dialog._ok()
    assert dialog.result is None and dialog.winfo_exists(), "declining leaves the point open for refinement"
    gui.answers["askyesno"].append(True)
    dialog._ok()
    assert dialog.result["given_as"] == "35TPF6641"
    assert not geo.precision_needs_review(dialog.result)
    with mock.patch.object(places_panel.PlaceDialog, "ask", return_value=dialog.result):
        panel.table.tree.selection_set("0")
        panel.approve()
    assert panel.rows[0]["decision"] == "approved"
    c = gui.app.compose
    gui.key(c.tree, "a")
    c.purpose.delete("1.0", "end")
    c.purpose.insert("1.0", "Review office records")
    gui.pump()
    c.tree.selection_set("q0")
    c.decide("approved")
    sign_from_compose(gui)
    signed = list_warrants(gui.app.paths, gui.app.public_key)[0]["doc"]["places"][0]
    assert signed["given_as"] == "35TPF6641" and signed["cell_size_m"] == 1000
    assert signed["precision_acknowledged"] == geo.precision_key(signed)


def test_round9_precision_rechecks_radius_and_refined_point(gui):
    from lucid_warrant import geo
    from lucid_warrant.ui import places_panel

    panel = gui.app.compose.places_panel
    c = geo.parse_coordinate("35TPF6641")
    place = {"place_id": "p-office", "label": "Office", "lat": round(c.lat, 6), "lon": round(c.lon, 6),
             "radius_km": .1, "country": "TR", "reason": "Review", **geo.input_evidence(c, "35TPF6641")}
    place["precision_acknowledged"] = geo.precision_key(place)
    dialog = places_panel.PlaceDialog(panel, place=place, title="Edit", **panel._context())
    dialog.radius.set(".05")
    gui.answers["askyesno"].append(False)
    dialog._ok()
    assert dialog.result is None
    dialog.coordinate.set("41.0082, 28.9784")
    gui.pump()
    assert not dialog.precision_note.cget("text")
    dialog._ok()
    assert dialog.result["cell_size_m"] is None
    assert dialog.result["given_as"] == "41.0082, 28.9784"
    panel.coordinate.set("35TPF6641")
    panel.convert()
    assert all(t in panel.result.cget("text") for t in ("35TPF6641", "MGRS", "1 km square", c.text()))
    panel.coordinate.set("39.9334, 32.8597")  # edited after Convert, without converting again
    with mock.patch.object(panel, "add") as add:
        panel.add_from_converter()
    assert add.call_args.args[0]["cell_size_m"] == 1000
    assert add.call_args.args[0]["given_as"] == "35TPF6641", "evidence belongs to the converted point"


def test_round9_organization_review_and_exact_snapshot(gui):
    from lucid_warrant import org_config as oc
    from lucid_warrant.ui.org_review import OrganizationReview

    candidate = {"schema_version": 1, "certification": {"purpose": "Review custom wording"},
                 "geography": {"place_clause": "CIRCLE({lat},{lon},{km})"}}
    path = gui.app.paths.data / "organization.json"
    path.write_text(json.dumps(candidate))
    dialog = OrganizationReview(gui.app, path, candidate)
    dialog.place_on_screen()
    gui.pump()
    assert "Changes from built-in defaults" in texts(dialog)
    assert "Review custom wording" in texts(dialog) and "Place clause" in texts(dialog)
    assert not any(isinstance(w, tk.Text) for w in dialog.view.body.winfo_children())
    dialog.full.set(True)
    dialog.render()
    gui.pump()
    stack, boxes = [dialog.view.body], []
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if isinstance(w, tk.Text):
            boxes.append(w)
    assert any('"place_clause": "CIRCLE' in w.get("1.0", "end") for w in boxes)
    dialog.destroy()

    def accept(review):
        path.write_text(json.dumps({"schema_version": 1, "organization": {"name": "Unreviewed"}}))
        return True

    with mock.patch.object(OrganizationReview, "run", accept):
        assert gui.app._review_unsigned_org(key_for(gui.app.paths))
    assert oc.load(gui.app.paths)["certification"]["purpose"] == "Review custom wording"
    assert oc.load(gui.app.paths)["organization"]["name"] != "Unreviewed"


def test_round9_vocabulary_details_and_run_literals(gui, paths):
    from lucid_warrant.ui.widgets import _Dialog
    from lucid_warrant.ui import compose

    c = gui.app.compose
    assert compose.STATUS == compose.STATUS_EDIT
    assert "term_id" not in c.tree.cget("displaycolumns")
    c.tree.selection_set("q0")
    observed = []

    def inspect(dialog):
        gui.pump()
        observed.append(texts(dialog))
        assert "Internal ID" in texts(dialog)
        assert c.terms[0]["term_id"] in texts(dialog)
        assert any(w.cget("text") == "Copy" for w in dialog.winfo_children()[0].winfo_children()[0].body.winfo_children()
                   if isinstance(w, ttk.Button))
        dialog.destroy()

    with mock.patch.object(_Dialog, "run", inspect):
        c.term_details()
    assert observed
    w = make_warrant(paths)
    run = run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": "t-project-cedar"})
    gui.app.runs.refresh()
    value = gui.app.runs.table.tree.set(run["run_id"], "terms")
    assert value == "PROJECT_CEDAR" and "t-project" not in value
    revoke_warrant(paths, w["warrant_id"])
    gui.app.runs.refresh()
    assert gui.app.runs.table.tree.set(run["run_id"], "terms") == "PROJECT_CEDAR"


def test_round9_configured_actions_and_tutorial(gui):
    from lucid_warrant.ui.compose import ComposeTab
    from lucid_warrant.ui.tutorial import Tutorial, PAGES

    gui.app.settings["terms"]["hit_term"] = {"singular": "Keyword", "plural": "Keywords"}
    # Rebuild the tab as a new configuration does, so these are actual visible actions.
    replacement = ComposeTab(gui.app.notebook, gui.app)
    gui.app.notebook.add(replacement, text="Compose test")
    gui.app.notebook.select(replacement)
    gui.pump()
    assert "Add keyword…" in texts(replacement) and "Reject keyword" in texts(replacement)
    assert "Add term" not in texts(replacement) and "Reject nomination" not in texts(replacement)
    assert "enforces every line" not in PAGES[0][1]
    assert "repeated searches" in PAGES[0][1] and "cannot check" in PAGES[0][1]
    tutorial = Tutorial(gui.app)
    gui.pump()
    assert "repeated searches" in tutorial.text.get("1.0", "end-1c")
    assert "included keywords" in tutorial.text.get("1.0", "end-1c")
    tutorial.index = 3
    tutorial.show()
    gui.pump()
    assert tutorial.heading.cget("text") == "Compose: keywords"
    tutorial.destroy()
    replacement.destroy()


def test_round9_changed_circles_are_named_in_the_signing_summary(gui):
    """Codex round 9 #5: same label, wider radius or moved centre, isn't 'none'."""
    from lucid_warrant.ui.document import glance_blocks

    old = {"label": "Office", "lat": 41.0082, "lon": 28.9784, "radius_km": 0.1, "country": "TR"}
    new = {**old, "radius_km": 20, "lat": 41.0182}
    doc = {"parameter_1": "0012", "parameter_2": ["ATTR_1000"], "purpose": "p", "terms": [], "max_rows_per_run": 1,
           "expires_at": "2026-10-19T12:00:00Z", "country_scope": {"include": [], "exclude": []}, "places": [new]}
    text = str(glance_blocks(doc, countries={}, labels={}, hits="Hit terms", replaces="w-20261018-000000-abcdef",
                             new_literals=set(), removed=[], old_places=[old]))
    assert "radius 100 m → 20000 m (WIDER)" in text and "centre moved 1,112 m" in text and "none to terms" not in text


def test_round9_import_review_handles_a_vanished_file(gui, tmp_path):
    from lucid_warrant.ui import places_panel

    panel = gui.app.compose.places_panel
    assert places_panel.ImportReview.ask(panel, tmp_path / "gone.csv", room=50, **panel._context()) is None
    assert any("can't be read any more" in str(s) for s in gui.shown)


def test_round9_place_rejection_has_a_reason_and_import_edits_are_protected(gui):
    """Codex round 9 #7."""
    import json as _json

    from lucid_warrant.places import load_rejected_places
    from lucid_warrant.ui import places_panel

    app, c = gui.app, gui.app.compose
    panel = c.places_panel
    app.paths.inbox.mkdir(parents=True, exist_ok=True)
    app.paths.proposal.write_text(_json.dumps({"places": [
        {"label": "Ankara branch", "lat": 39.9334, "lon": 32.8597, "radius_km": 0.1, "reason": "branch"}]}))
    c.merge_inbox()
    panel.table.tree.selection_set("0")
    gui.answers["askstring"].append(None)
    panel.reject()
    assert panel.rows[0]["decision"] == "pending", "cancelled: nothing changes"
    gui.answers["askstring"].append("Not part of this case")
    panel.reject()
    panel.record_rejections(app.paths)
    (entry,) = load_rejected_places(app.paths).values()
    assert entry["reason"] == "Not part of this case" and entry["label"] == "Ankara branch"

    path = Path(__file__).parent.parent / "examples" / "places" / "places_latlon.csv"
    review = places_panel.ImportReview(panel, path, room=50, **panel._context())
    review.rows[0]["reason"].set("My corrected reason")
    review.vars["label"].set("Notes")
    gui.answers["askyesno"].append(False)
    review.reread()
    assert review.rows[0]["reason"].get() == "My corrected reason" and review.vars["label"].get() == "Site Name"
    review.destroy()


def test_issue14_compose_uses_wide_screens_and_text_stays_readable(gui):
    """Issue #14 (with #10): the page grows with the window, and explanatory text wraps to the page (at 1024)
    or to a reading width (on a wide screen)."""
    app, c = gui.app, gui.app.compose
    wide = min(app.winfo_screenwidth(), 2400)
    app.geometry(f"{wide}x800+0+0")
    gui.pump(0.5)
    page_width = c.page.body.winfo_width()
    assert page_width > 1300 or wide < 1500, "wider than the old 1280 px column on a wide window"
    hint = next(w for w in _all_widgets(c.page.body) if isinstance(w, ttk.Label)
                and "two-letter country code" in str(w.cget("text")))
    assert int(str(hint.cget("wraplength"))) <= 960
    app.geometry("1024x680")
    gui.pump(0.5)
    assert int(str(hint.cget("wraplength"))) <= c.page.body.winfo_width() - 40, "never wider than the page"


def _assert_label_fits(label):
    """Measure the rendered text with the same font and wrap width, without a geometry constraint."""
    options = {key: label.cget(key) for key in ("text", "style", "font", "wraplength", "justify")}
    probe = ttk.Label(label.winfo_toplevel(), **options)
    needed = (probe.winfo_reqwidth(), probe.winfo_reqheight())
    probe.destroy()
    assert label.winfo_width() >= needed[0], (options["text"], label.winfo_width(), needed)
    assert label.winfo_height() >= needed[1], (options["text"], label.winfo_height(), needed)


def test_places_fit_small_window(gui):
    """Issue #10: measure actual controls/text after shrinking and expanding with the harness's fonts."""
    panel = gui.app.compose.places_panel
    panel.rows = [{"place_id": "p-office", "label": "A long office name " * 5, "lat": 41.0082, "lon": 28.9784,
                   "radius_km": 0.1, "country": "TR", "reason": "A long explanation " * 15,
                   "source": "human", "origin": "An imported source " * 5, "decision": "pending"}]
    panel.render()
    panel.coordinate.set("41.0082, 28.9784")
    panel.convert()
    for size in ("1024x680", "1200x720", "1024x680"):
        gui.app.geometry(size + "+0+40")
        gui.pump(0.15)
        gui.app.compose.page.scroll_to(panel)
        gui.pump(0.15)
        right = min(gui.app.winfo_rootx() + gui.app.winfo_width(),
                    gui.app.compose.page.canvas.winfo_rootx() + gui.app.compose.page.canvas.winfo_width())
        stack = [panel]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            assert widget.winfo_ismapped(), str(widget)
            assert widget.winfo_rootx() + widget.winfo_width() <= right, str(widget)
            if isinstance(widget, ttk.Label) and widget.cget("text"):
                _assert_label_fits(widget)
            if isinstance(widget, ttk.Button):
                assert widget.winfo_width() >= widget.winfo_reqwidth(), widget.cget("text")
        tree = panel.table.tree
        gui.app.compose.page.scroll_to(tree)
        gui.pump()
        source = tree.bbox("0", "source")
        assert source and source[0] + source[2] <= tree.winfo_width(), source
        assert tree.winfo_rootx() + source[0] + source[2] <= right
        entry = next(w for w in panel.add_converted.master.winfo_children() if isinstance(w, ttk.Entry)
                     and not isinstance(w, ttk.Combobox))
        converter = panel.add_converted.master
        controls = converter.winfo_children()
        needed = sum(w.winfo_width() for w in controls) + 6 * (len(controls) - 1)
        if needed > converter.winfo_width():
            assert panel.add_converted.winfo_y() > entry.winfo_y(), "converter buttons should wrap"
        else:
            assert panel.add_converted.winfo_y() == entry.winfo_y(), "controls fit on one line with these fonts"


def test_import_review_fits_small_window_with_complete_warnings(gui):
    """Issue #10: every column is reachable and the complete country warnings really fit their labels."""
    from lucid_warrant.ui.places_panel import ImportReview, choice_for

    panel = gui.app.compose.places_panel
    gui.app.geometry("1024x680+0+40")
    gui.pump()
    review = ImportReview(panel, Path(__file__).parent.parent / "examples" / "places" / "places_latlon.csv",
                          room=50, **panel._context())
    review.place_on_screen()
    gui.pump(0.15)
    assert review.winfo_width() <= 1024 and review.winfo_height() <= 680
    expected = {"Thessaloniki": "⚠ in Greece, not Turkey (TR)",
                "Offshore": "⚠ at sea: not inside any country",
                "Berlin": "⚠ DE isn't permitted (excluded: GDPR)"}
    for size in ("1024x680", "1200x720", "1024x680"):
        review.geometry(size + "+0+40")
        gui.pump(0.15)
        right = review.winfo_rootx() + review.winfo_width()
        stack = [review.frame]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            assert widget.winfo_ismapped(), str(widget)
            assert widget.winfo_rootx() + widget.winfo_width() <= right, str(widget)
            if isinstance(widget, ttk.Label) and widget.cget("text"):
                _assert_label_fits(widget)
        canvas_right = review.page.canvas.winfo_rootx() + review.page.canvas.winfo_width()
        for widget in review.grid_frame.winfo_children():
            assert widget.winfo_rootx() + widget.winfo_width() <= canvas_right, str(widget)
        for prefix, message in expected.items():
            row = next(r for r in review.rows if r["label"].get().startswith(prefix))
            assert row["status"].cget("text") == message
            assert row["coordinate"].get() not in message
            review.page.scroll_to(row["status"])
            gui.pump()
            status = row["status"]
            canvas = review.page.canvas
            assert canvas.winfo_rooty() <= status.winfo_rooty()
            assert status.winfo_rooty() + status.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height()
            _assert_label_fits(status)
            _assert_label_fits(row["evidence"])
        assert review.ok.winfo_rooty() + review.ok.winfo_height() <= review.winfo_rooty() + review.winfo_height()
        assert review.page.canvas.winfo_height() >= 100
    row = next(r for r in review.rows if r["label"].get().startswith("Thessaloniki"))
    assert "isn't inside Turkey" in row["evidence"].cget("text")
    assert "it's in Greece" in row["evidence"].cget("text")
    row["country"].set(choice_for(review.choices, "GR"))
    gui.pump()
    assert row["status"].cget("text") == "Country agrees with the bundled map"
    assert "isn't inside Turkey" not in row["evidence"].cget("text")
    # A long duplicate warning also stays intact, rather than losing the distinguishing end of a label.
    first = review.rows[0]
    long_label = "Client regional office beside the main city railway terminal"
    first["label"].set(long_label)
    row["coordinate"].set(first["coordinate"].get())
    gui.pump()
    assert long_label in row["status"].cget("text")
    _assert_label_fits(row["status"])
    review.destroy()


def test_tutorial_sits_beside_its_section_and_stays_on_screen(gui):
    """The tutorial is placed where the outline ends up after Compose scrolls, its buttons are always within the
    usable screen (above the Dock / taskbar), and it never shows an arrow unless it's beside the outline."""
    from lucid_warrant.ui.tutorial import PAGES, Tutorial

    app = gui.app
    app.geometry(f"1100x{min(760, app.winfo_screenheight() - 120)}+0+30")
    gui.pump(0.4)
    tutorial = Tutorial(app)
    top, bottom = tutorial._usable_area()
    for index in range(len(PAGES)):
        tutorial.index = index
        tutorial.show()
        gui.pump(0.4)
        y, h = tutorial.winfo_rooty(), tutorial.winfo_height()
        assert top - 30 <= y and y + h <= bottom + 2, (index, y, h, bottom)
        buttons_bottom = tutorial.next_button.winfo_rooty() + tutorial.next_button.winfo_height()
        assert buttons_bottom <= bottom + 2, (index, "Next button below the usable area")
        rect = tutorial.box.screen_rect()
        if tutorial.placed == "top" and rect:
            outline_bottom = rect[1] + rect[3]
            assert 0 <= y - outline_bottom <= 60, (index, "should sit just below the outline", y, outline_bottom)
        if tutorial.placed == "bottom" and rect:
            assert 0 <= rect[1] - (y + h) <= 60, (index, "should sit just above the outline")
        if tutorial.placed is None:
            assert not tutorial.arrow.winfo_ismapped(), "no arrow when it isn't beside the outline"
    tutorial.destroy()


def test_tutorial_moves_once_per_page_without_flicker(gui):
    """Cycling pages lays the tutorial out and moves it once per page (trying each side on the real window, and
    always moving it again a moment later, was drawn each time: a flicker)."""
    from lucid_warrant.ui.tutorial import Tutorial

    app = gui.app
    app.geometry("1100x760+0+30")
    gui.pump(0.4)
    tutorial = Tutorial(app)
    gui.pump(0.4)
    calls = {"arrow": 0, "geometry": 0}
    real_arrow, real_geometry = tutorial._set_arrow, tutorial.geometry

    def count_arrow(side):
        calls["arrow"] += 1
        return real_arrow(side)

    def count_geometry(*args):
        if args:
            calls["geometry"] += 1
        return real_geometry(*args)

    tutorial._set_arrow, tutorial.geometry = count_arrow, count_geometry
    for index in (4, 5, 6):
        calls.update(arrow=0, geometry=0)
        tutorial.index = index
        tutorial.show()
        gui.pump(0.4)
        assert calls["arrow"] <= 2 and calls["geometry"] <= 2, (index, calls)
    tutorial.destroy()


@pytest.mark.parametrize("buttons,negative", [("yesno", "no"), ("okcancel", "cancel")])
@pytest.mark.parametrize("key", ["Return", "KP_Enter"])
def test_message_keyboard_before_app_bindings(paths, tmp_path, monkeypatch, buttons, negative, key):
    """Drive startup's modal prompt before App installs its TButton bindings."""
    from lucid_warrant.ui.app import App
    from lucid_warrant.ui.widgets import MessageDialog

    for name, folder in (("KEYS", paths.keys), ("DATA", paths.data), ("PREFS", tmp_path / "prefs")):
        monkeypatch.setenv("LUCID_WARRANT_" + name, str(folder))
    answers, pressed = [], []

    def startup(app):
        assert not hasattr(app, "notebook")
        dialog = MessageDialog(app, "Startup confirmation", "Check access?", buttons=buttons)

        def answer():
            dialog.default_button.focus_force()
            app.update()
            dialog.default_button.event_generate("<Tab>")
            app.update()
            assert dialog.focus_get() is dialog.buttons[negative]
            pressed.append(key)
            dialog.focus_get().event_generate(f"<{key}>")

        app.after(100, answer)
        app.after(2000, lambda: dialog.destroy() if dialog.winfo_exists() else None)
        answers.append(dialog.run())
        return False

    with mock.patch.object(App, "_unlock_or_setup", startup):
        app = App(paths)
    app.update()
    app.destroy()
    assert pressed == [key]
    assert answers == [False]


def test_long_message_scrolls_inside_independent_work_rectangle(gui, monkeypatch):
    from lucid_warrant.ui import widgets, theme
    from lucid_warrant.ui.geometry import frame_insets

    theme.apply(gui.app, {**gui.app.settings["ui"], "base_font_pt": 24})
    gui.pump()
    # Independent side/top/bottom reservations; not derived from the implementation.
    area = (85, 70, 1000, 650)
    monkeypatch.setattr(widgets, "work_area", lambda _w: area, raising=False)
    message = "\n".join(f"signing_key.enc.json can be reached by DOMAIN\\AnalystGroup{i}." for i in range(80))
    dialog = widgets.MessageDialog(gui.app, "Permissions", message, buttons="yesno", detail="Last detail to read.")
    dialog.place_on_screen()
    gui.pump()
    dl, dt, dr, db = frame_insets(dialog)
    x, y = dialog.winfo_rootx(), dialog.winfo_rooty()
    assert y + dialog.winfo_height() + db <= area[3]
    assert x - dl >= area[0] and y - dt >= area[1]
    assert x + dialog.winfo_width() + dr <= area[2]
    for button in dialog.buttons.values():
        assert button.winfo_ismapped()
        assert button.winfo_rooty() + button.winfo_height() <= area[3]
    assert dialog.page.canvas.yview()[1] < 1
    dialog.page.canvas.yview_moveto(1)
    gui.pump()
    detail = dialog.page.body.winfo_children()[-1]
    assert detail.winfo_rooty() >= dialog.page.canvas.winfo_rooty()
    assert detail.winfo_rooty() + detail.winfo_height() <= dialog.page.canvas.winfo_rooty() + dialog.page.canvas.winfo_height()
    gui.click(dialog.buttons["no"])
    assert dialog.result is False


@pytest.mark.parametrize("size", [20, 24])
def test_import_review_large_text_has_rows_and_reserved_actions(gui, size):
    from lucid_warrant.ui import theme
    from lucid_warrant.ui.places_panel import ImportReview

    app = gui.app
    app.geometry("1024x680+0+40")
    theme.apply(app, {**app.settings["ui"], "base_font_pt": size})
    gui.pump()
    panel = app.compose.places_panel
    review = ImportReview(panel, Path(__file__).parent.parent / "examples/places/places_latlon.csv",
                          room=50, **panel._context())
    review.place_on_screen()
    gui.pump(.2)
    assert review.winfo_width() <= 1024 and review.winfo_height() <= 680
    canvas = review.page.canvas
    assert canvas.winfo_height() >= 300
    for row in (review.rows[0], review.rows[-1]):
        review.page.scroll_to(row["status"])
        gui.pump()
        status = row["status"]
        assert canvas.winfo_rooty() <= status.winfo_rooty()
        assert status.winfo_rooty() + status.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height()
        assert review.ok.winfo_ismapped()
        assert review.ok.winfo_rooty() >= canvas.winfo_rooty() + canvas.winfo_height()
        assert review.ok.winfo_rooty() + review.ok.winfo_height() <= review.winfo_rooty() + review.winfo_height()
    review.page.canvas.yview_moveto(0)
    gui.pump()
    assert review.header.winfo_rooty() >= canvas.winfo_rooty()
    review.destroy()


def test_places_prose_uses_shared_readable_wrap_while_tables_expand(gui):
    from lucid_warrant.ui.compose import READ_WIDTH
    from lucid_warrant.ui.places_panel import CONVERSION_WARNING
    from lucid_warrant import geo

    app = gui.app
    panel = app.compose.places_panel
    panel.coordinate.set("41.0082, 28.9784")
    panel.convert()
    labels = [w for w in _all_widgets(panel) if isinstance(w, ttk.Label) and
              (str(w.cget("text")) in (geo.MAP_LIMITATION, CONVERSION_WARNING) or
               str(w.cget("text")).startswith("Limit every search"))]
    assert len(labels) >= 3
    widths = []
    for width in (1024, 1728, 1024):
        app.geometry(f"{width}x680")
        gui.pump(.2)
        widths.append(panel.table.tree.winfo_width())
        for label in labels:
            assert 0 < int(label.cget("wraplength")) <= READ_WIDTH
            assert int(label.cget("wraplength")) <= label.master.winfo_width()
            _assert_label_fits(label)
    assert widths[1] > widths[0] + 100


def test_tutorial_measurement_does_not_render_intermediate_content(gui, monkeypatch):
    from lucid_warrant.ui.tutorial import Tutorial

    app = gui.app
    app.geometry("1024x680+0+40")
    gui.pump()
    tour = Tutorial(app, start=4)
    gui.pump(.2)
    # Observe actual drawn widgets at every explicit idle flush. In particular,
    # measurement must not replace a horizontal arrow by the vertical glyph.
    tour._set_arrow("left")
    gui.pump()
    before = (tour.arrow.cget("text"), tour.arrow.winfo_width(), tour.geometry())
    observed = []
    flush = tour.update_idletasks

    def observe():
        flush()
        observed.append((tour.arrow.cget("text"), tour.arrow.winfo_width(), tour.geometry()))

    monkeypatch.setattr(tour, "update_idletasks", observe)
    tour._measure()
    gui.pump()
    assert (tour.arrow.cget("text"), tour.arrow.winfo_width(), tour.geometry()) == before
    assert all(state == before for state in observed)
    # No flush may expose new text at the preceding page's geometry.
    heading = tour.heading.cget("text")
    exposed = []
    app_flush = app.update_idletasks

    def observe_page():
        app_flush()
        exposed.append(tour.heading.cget("text"))

    monkeypatch.setattr(app, "update_idletasks", observe_page)
    tour.index = 5
    tour.show()
    gui.pump(.2)
    assert exposed and all(text == heading for text in exposed)
    assert tour.heading.cget("text") != heading
    tour.destroy()


def test_tutorial_follows_resize_scroll_and_work_area_changes(gui, monkeypatch):
    from lucid_warrant.ui import tutorial as module

    app = gui.app
    app.geometry("1024x680+0+40")
    gui.pump()
    area = [0, 33, 1728, 1032]
    monkeypatch.setattr(module, "work_area", lambda _owner: tuple(area), raising=False)
    tour = module.Tutorial(app, start=5)
    gui.pump(.3)  # past the old single correction callback
    before = tour.geometry()
    app.geometry("1400x680+0+40")
    # Wait for the layout to settle rather than a fixed 0.3 s: on Windows the page re-lays out more slowly.
    gui.pump_until(lambda: tour.geometry() != before and tour._placed_for == tour.box.screen_rect())
    assert tour.geometry() != before
    assert tour._placed_for == tour.box.screen_rect()
    if tour.placed:
        x, y, w, h = tour.box.screen_rect()
        tx, ty = tour.winfo_rootx(), tour.winfo_rooty()
        assert tx >= x + w or tx + tour.winfo_width() <= x or ty >= y + h or ty + tour.winfo_height() <= y
    app.compose.page.canvas.yview_scroll(80, "units")
    gui.pump_until(lambda: tour._placed_for == tour.box.screen_rect())
    assert tour._placed_for == tour.box.screen_rect()
    # An independently supplied work rectangle reserves every edge.
    area[:] = [85, 70, 1000, 650]
    app.event_generate("<Configure>")
    gui.pump(.3)
    from lucid_warrant.ui.geometry import frame_insets
    dl, dt, dr, db = frame_insets(tour)
    assert tour.winfo_rootx() - dl >= area[0]
    assert tour.winfo_rooty() - dt >= area[1]
    assert tour.winfo_rootx() + tour.winfo_width() + dr <= area[2]
    assert tour.winfo_rooty() + tour.winfo_height() + db <= area[3]
    assert tour.next_button.winfo_rooty() + tour.next_button.winfo_height() <= area[3]
    geometry = tour.geometry()
    app.event_generate("<Configure>")
    gui.pump(.2)
    assert tour.geometry() == geometry
    tour._bounds_changed()
    pending = tour._pending_place
    tour.destroy()
    assert pending not in app.tk.splitlist(app.tk.call("after", "info"))


@pytest.mark.parametrize("area", [(85, 70, 1000, 650), (-200, 70, 1000, 650)])
def test_tutorial_inside_independent_monitor_work_rectangle(gui, monkeypatch, area):
    from lucid_warrant.ui import tutorial as module
    from lucid_warrant.ui.geometry import frame_insets

    monkeypatch.setattr(module, "work_area", lambda _owner: area, raising=False)
    # A user-set maximum is unrelated to the OS work rectangle.
    gui.app.maxsize(2400, 4000)
    tour = module.Tutorial(gui.app, start=6)
    gui.pump(.3)
    dl, dt, dr, db = frame_insets(tour)
    assert tour.winfo_rootx() - dl >= area[0]
    assert tour.winfo_rooty() - dt >= area[1]
    assert tour.winfo_rootx() + tour.winfo_width() + dr <= area[2]
    assert tour.winfo_rooty() + tour.winfo_height() + db <= area[3]
    for button in (tour.next_button, tour.prev_button, tour.close_button):
        assert button.winfo_ismapped()
        assert button.winfo_rooty() + button.winfo_height() <= area[3]
    tour.destroy()


def test_tutorial_configure_frames_have_final_arrow_and_geometry(gui, monkeypatch):
    from lucid_warrant.ui import tutorial as module

    app = gui.app
    app.geometry("1024x680+0+33")
    gui.pump()
    area = [0, 33, 1728, 1032]
    monkeypatch.setattr(module, "work_area", lambda _owner: tuple(area), raising=False)
    tour = module.Tutorial(app, start=4)
    gui.pump(.2)
    sides = []
    # Room for the tutorial beside the window, from its real width: with Windows fonts (Tk scaling 1.33) it's about
    # 690 px, so the 1728 px of a MacBook screen left ~15 px too little and it correctly went above instead.
    roomy = app.winfo_rootx() + app.winfo_width() + tour.winfo_reqwidth() + 200
    for right in (roomy, 1024):
        area[2] = right
        tour.index = 4
        tour.show()
        gui.pump(.2)
        frames = []
        binding = tour.bind("<Configure>", lambda e: frames.append(
            (tour.heading.cget("text"), tour.arrow.cget("text"), tour.geometry())) if e.widget is tour else None, "+")
        tour.index = 5
        tour.show()
        gui.pump(.2)
        final = (tour.heading.cget("text"), tour.arrow.cget("text"), tour.geometry())
        rendered = [frame for frame in frames if frame[0] == final[0]]
        assert rendered and all(frame == final for frame in rendered), (rendered, final)
        sides.append(tour.placed)
        tour.unbind("<Configure>", binding)
    assert sides[0] in ("left", "right") and sides[1] in ("top", "bottom"), sides
    tour.destroy()


def test_tutorial_first_map_has_complete_page(gui):
    from lucid_warrant.ui.tutorial import Tutorial

    mapped = []

    def observe(event):
        if isinstance(event.widget, Tutorial):
            tour = event.widget
            mapped.append((tour.heading.cget("text"), tour.arrow.cget("text"), tour.winfo_width(), tour.winfo_height()))

    gui.app.bind_all("<Map>", observe, "+")
    tour = Tutorial(gui.app, start=5)
    gui.pump(.2)
    final = (tour.heading.cget("text"), tour.arrow.cget("text"), tour.winfo_width(), tour.winfo_height())
    assert mapped and all(frame == final for frame in mapped), (mapped, final)
    tour.destroy()


def test_pattern_dialog_validation_defaults_and_cancel(gui):
    from lucid_warrant import org_config
    from lucid_warrant.syntax import SyntaxRejected, validate_pattern
    from lucid_warrant.ui import theme
    from lucid_warrant.ui.term_dialog import TermDialog

    theme.apply(gui.app, {**gui.app.settings["ui"], "theme_mode": "dark"}, org_config.schemes(gui.app.settings))
    dialog = TermDialog(gui.app, "Add hit term", "Hit term")
    gui.pump()
    exact = find(dialog, ttk.Radiobutton, "Exact text")
    pattern = find(dialog, ttk.Radiobutton, "Pattern")
    assert exact.instate(["selected"]) and not pattern.instate(["selected"])
    assert str(ttk.Style().lookup("TRadiobutton", "foreground")) != str(ttk.Style().lookup("TRadiobutton", "background"))
    dialog.entry.insert(0, "P*")
    gui.click(pattern)
    gui.click(find(dialog, text="Save"))
    with pytest.raises(SyntaxRejected) as exc:
        validate_pattern("P*")
    assert dialog.error.winfo_ismapped() and dialog.error.cget("text") == str(exc.value)
    gui.click(exact)  # the same wildcard-looking text is allowed as exact text
    gui.click(find(dialog, text="Save"))
    assert dialog.result == {"literal": "P*", "match": "exact"}

    dialog = TermDialog(gui.app, "Edit hit term", "Hit term", {"literal": "PROJECT*", "match": "pattern"})
    gui.pump()
    assert find(dialog, ttk.Radiobutton, "Pattern").instate(["selected"])
    gui.click(find(dialog, ttk.Radiobutton, "Exact text"))
    gui.click(find(dialog, text="Cancel"))
    assert dialog.result is None


def test_pattern_widgets_sign_run_print_and_replace(gui, paths):
    from lucid_warrant.ui.term_dialog import TermDialog
    from lucid_warrant.ui.widgets import PassphraseDialog

    app, c = gui.app, gui.app.compose
    paths.proposal.write_text(json.dumps({"purpose": "Review the project family", "terms": [
        {"literal": "PROJECT*", "match": "pattern", "reason": "all project codes"}]}))
    c.merge_inbox()
    gui.pump()
    row = next(i for i in c.tree.get_children() if c.tree.set(i, "literal") == "PROJECT*")
    for size in ("1024x680", "1600x900"):
        app.geometry(size)
        gui.pump()
        assert c.tree.set(row, "matching") == "Pattern: any word starting with PROJECT"
        assert "matching" in c.tree.cget("displaycolumns")
        x, y, width, height = c.tree.bbox(row, "matching")
        assert x >= 0 and x + width <= c.tree.winfo_width() and height > 0
    c.tree.selection_set(row)

    def edit_to(mode):
        def drive(dialog):
            gui.pump()
            assert find(dialog, ttk.Radiobutton, "Pattern" if mode == "exact" else "Exact text").instate(["selected"])
            gui.click(find(dialog, ttk.Radiobutton, "Pattern" if mode == "pattern" else "Exact text"))
            gui.click(find(dialog, text="Save"))
            return dialog.result
        with mock.patch.object(TermDialog, "run", drive):
            gui.click(find(c, text="Edit hit term"))

    edit_to("exact")
    assert c.tree.set(row, "matching") == "Exact text"
    c.merge_inbox()  # an agent refresh cannot undo the analyst's explicit choice
    assert c.tree.set(row, "matching") == "Exact text"
    edit_to("pattern")
    gui.key(c.tree, "a")

    def sign_document(master, title, summary="", confirm=False, document=None):
        dialog = PassphraseDialog(master, title, summary, confirm, document)
        gui.pump()
        rendered = find(dialog, tk.Text).get("1.0", "end")
        assert "1 (1 pattern: PROJECT*)" in rendered and "Pattern: any word starting with PROJECT" in rendered
        dialog.first.insert(0, PASSPHRASE)
        gui.click(find(dialog, text="Sign"))
        return dialog.result

    with mock.patch.object(PassphraseDialog, "ask", side_effect=sign_document):
        sign_from_compose(gui)
    (w,) = list_warrants(paths, load_public_key(paths))
    assert w["doc"]["terms"][0]["match"] == "pattern"
    term_id = w["doc"]["terms"][0]["term_id"]
    run = run_template(paths, warrant_id=w["warrant_id"], template_id="single-term", bindings={"term": term_id})
    app.refresh_all()
    app.show_run(run["run_id"])
    gui.pump()
    assert "PROJECT* (pattern)" in texts(app.runs.inspector)
    app.show_warrant(w["warrant_id"])
    gui.pump()
    assert "Pattern: any word starting with PROJECT" in texts(app.warrants_tab.inspector)
    with mock.patch("webbrowser.open") as opened:
        gui.click(find(app.warrants_tab.inspector.actions, text="Print / Save as PDF"))
    printed = paths.data / "exports" / f"{w['warrant_id']}.html"
    assert opened.call_args.args == (printed.resolve().as_uri(),)
    html = printed.read_text("utf-8")
    assert "1 (1 pattern: PROJECT*)" in html and "Pattern: any word starting with PROJECT" in html
    assert "PROJECT* (pattern)" in html  # run addendum

    gui.click(find(app.warrants_tab.inspector.actions, text="Edit"))
    c.tree.selection_set("q0")
    edit_to("exact")
    certify(gui)
    gui.click(c.sign_button)
    assert "PROJECT* (pattern) → PROJECT* (exact)" in gui.prompts[-1]
    replacement = next(r for r in list_warrants(paths, load_public_key(paths)) if r["status"] == "active")
    assert "match" not in replacement["doc"]["terms"][0]
    html = app.warrants_tab.export_html(replacement)
    assert "PROJECT* (pattern) → PROJECT* (exact)" in html


def test_add_pattern_and_restore_rejected_pattern(gui, paths):
    from lucid_warrant.review import reject
    from lucid_warrant.ui.term_dialog import TermDialog

    c = gui.app.compose
    def drive(dialog):
        gui.pump()
        assert find(dialog, ttk.Radiobutton, "Exact text").instate(["selected"])
        dialog.entry.insert(0, "SUPPLIER_N?RTH")
        gui.click(find(dialog, ttk.Radiobutton, "Pattern"))
        gui.click(find(dialog, text="Save"))
        return dialog.result
    with mock.patch.object(TermDialog, "run", drive):
        gui.click(find(c, text="Add hit term"))
    row = c.tree.get_children()[-1]
    assert c.tree.set(row, "literal") == "SUPPLIER_N?RTH" and c.tree.set(row, "matching").startswith("Pattern:")
    reject(paths, {"term_id": "t-pattern", "literal": "PROJECT*", "match": "pattern", "source": "agent"})
    gui.click(find(c, ttk.Checkbutton, "Show hidden"))
    row = next(i for i in c.tree.get_children() if i.startswith("r") and c.tree.set(i, "literal") == "PROJECT*")
    assert c.tree.set(row, "matching").startswith("Pattern:")
    c.tree.selection_set(row)
    gui.key(c.tree, "a")
    gui.pump()
    row = next(i for i in c.tree.get_children() if i.startswith("q") and c.tree.set(i, "literal") == "PROJECT*")
    assert c.tree.set(row, "matching").startswith("Pattern:")


def test_pattern_proposal_is_reviewable_despite_existing_exact_approval(gui, paths):
    doc = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="exact approval", ttl_minutes=60,
                        max_rows=10, terms=[{"term_id": "t-project", "literal": "PROJECT*", "source": "human"}],
                        rbac=get_rbac(paths), paths=paths)
    save_warrant(paths, doc, key_for(paths))
    paths.proposal.write_text(json.dumps({"terms": [{"literal": "PROJECT*", "match": "pattern", "reason": "family"}]}))
    gui.app.compose.merge_inbox()
    gui.pump()
    tree = gui.app.compose.tree
    row = next(i for i in tree.get_children() if tree.set(i, "literal") == "PROJECT*")
    assert tree.set(row, "matching").startswith("Pattern:") and tree.set(row, "status") == "● Pending"
