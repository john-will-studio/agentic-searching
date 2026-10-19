"""Application shell: tabs, status bar, navigation, signing and live refresh."""

from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import ttk

from . import msg as messagebox

from ..config import Paths
from ..library import sign_seed_library
from ..rbac import MockRbacProvider, get_rbac
from ..signing import (
    MIN_PASSPHRASE_LENGTH,
    BadPassphrase,
    generate_keypair,
    key_id,
    keys_exist,
    load_private_key,
    load_public_key,
)
from ..util import from_iso, utc_now
from .. import org_config
from . import theme
from .. import workspace
from .dialogs import AboutDialog, AppearanceDialog
from .tutorial import FIRST_TOUR_PAGE, Tutorial
from .approvals import ApprovalsTab
from .compose import ComposeTab
from .runs import RunsTab
from .templates import TemplatesTab
from .warrants import WarrantsTab
from .widgets import PassphraseDialog, WorkspaceDialog, make_chip, plural, shown_time, zone_label

PROBLEM_TYPES = (Exception,)


class App(tk.Tk):
    POLL_MS = 2000

    def __init__(self, paths: Paths, org_config_path: str | None = None, first_run: bool = False):
        super().__init__()
        self.paths = paths
        self.first_run = first_run
        self._key_just_created = False
        self.org_config_path = org_config_path
        self.field_mismatch: list[str] = []
        self.ready = False
        self.problems: dict[str, str] = {}
        self.withdraw()
        try:
            self.settings = org_config.load(paths, org_config_path)
        except org_config.ConfigError as exc:
            # Authority-relevant settings can't be guessed: say exactly what's wrong and stop.
            messagebox.showerror("Configuration problem", f"Lucid Warrant can't start with this configuration.\n\n{exc}\n\n"
                                 "Check it with:  lucid-warrant config validate")
            return
        self.mode = theme.apply(self, self.settings["ui"], org_config.schemes(self.settings))
        self.configure(bg=theme.PALETTE["bg"])
        self._set_title()
        window = self.settings["ui"]["window"]
        self.geometry(f"{min(window['width'], self.winfo_screenwidth())}x{min(window['height'], self.winfo_screenheight() - 60)}")
        self.minsize(1024, 680)
        if first_run:
            # First launch: choose the warrant directory, then the tutorial's opening pages, where the analyst
            # creates the signing key. The tour of the tabs continues once the window is up.
            chosen = WorkspaceDialog.ask(self, initial=workspace.default_location())
            if chosen is None:
                return
            self.paths = paths = Paths.for_workspace(workspace.create(chosen))
            workspace.remember(chosen)
            self.settings = org_config.load(paths, org_config_path)
            self._set_title()
            if not keys_exist(paths):
                Tutorial(self, phase="intro").run()
        if not self._unlock_or_setup():
            return
        self.public_key = load_public_key(paths)
        self.rbac = get_rbac(paths)

        self._build_status_bar()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self.compose = ComposeTab(self.notebook, self)
        self.approvals = ApprovalsTab(self.notebook, self)
        self.warrants_tab = WarrantsTab(self.notebook, self)
        self.templates = TemplatesTab(self.notebook, self)
        self.runs = RunsTab(self.notebook, self)
        terms = self.settings["terms"]
        self.tab_names = {self.compose: "Compose", self.warrants_tab: terms["warrant"]["plural"],
                          self.templates: terms["template"]["plural"], self.approvals: terms["syntax_request"]["plural"],
                          self.runs: terms["run"]["plural"]}
        order = [(tab, self.tab_names[tab]) for tab in (self.compose, self.warrants_tab, self.templates, self.approvals, self.runs)]
        for tab, name in order:
            self.notebook.add(tab, text=f"  {name}  ")
        for i, (tab, _name) in enumerate(order, start=1):
            for modifier in ("Control", "Command"):
                self.bind_all(f"<{modifier}-Key-{i}>", lambda _e, t=tab: self.notebook.select(t))

        self._navigating = False
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)
        self.bind_all("<F5>", lambda _e: self.refresh_all())
        for modifier in ("Control", "Command"):
            self.bind_all(f"<{modifier}-comma>", lambda _e: self.appearance())
        for sequence in ("<Return>", "<KP_Enter>"):
            self.bind_class("TButton", sequence, lambda e: e.widget.invoke() if str(e.widget.cget("state")) != "disabled" else None)
        if theme.ACTIVE != "sun-valley":
            self.report_problem("theme", f"Sun Valley theme unavailable ({theme.ACTIVE}); colors may be inconsistent")

        self.deiconify()
        self.ready = True
        if first_run:
            self.after(400, lambda: self.tutorial(start=FIRST_TOUR_PAGE))
        self._last_state, self._last_inbox = self._snapshot()
        self._last_org = self._org_files_stat()
        self.refresh_all()
        self.after(self.POLL_MS, self._poll)

    # --- status bar -------------------------------------------------------------------

    def _build_status_bar(self):
        bar = ttk.Frame(self, padding=(12, 6))
        bar.pack(side="bottom", fill="x")
        ttk.Separator(self).pack(side="bottom", fill="x")
        self.destination = ttk.Frame(bar)
        self.destination.pack(side="left")
        self._show_destination()
        subject = getattr(self.rbac, "subject", lambda: "")()
        if subject:
            ttk.Label(bar, text=f"Signed in as {subject}", style="Muted.TLabel").pack(side="left", padx=12)
        ttk.Button(bar, text="About…", command=self.about).pack(side="right")
        ttk.Button(bar, text="Tutorial", command=self.tutorial).pack(side="right", padx=(8, 0))
        ttk.Button(bar, text="Appearance…", command=self.appearance).pack(side="right", padx=8)
        self.refresh_label = ttk.Label(bar, style="Muted.TLabel")
        self.refresh_label.pack(side="right", padx=8)
        self.problem_label = ttk.Label(bar, style="Danger.TLabel")
        self.problem_label.pack(side="right", padx=8)
        self.notice = ttk.Label(bar, style="Ok.TLabel")
        self.notice.pack(side="left", padx=12)

    def _show_destination(self, problem: str | None = None):
        """The chips saying where searches go (or that they're practice, or refused). Rebuilt when the signed
        organization configuration changes while the app is open."""
        for child in self.destination.winfo_children():
            child.destroy()
        if problem:
            make_chip(self.destination, "SEARCHES REFUSED · configuration problem", "danger").pack(side="left")
            return
        backend = getattr(self, "verified_settings", self.settings)["search_backend"]
        practice = [what for what, mock in (("mock entitlements", isinstance(self.rbac, MockRbacProvider)),
                                            ("mock search", backend["type"] == "mock")) if mock]
        if practice:
            make_chip(self.destination, "PRACTICE MODE · " + " · ".join(practice), "warn").pack(side="left")
        if backend["type"] != "mock":
            from urllib.parse import urlparse

            where = urlparse(backend["http"]["url"]).netloc if backend["type"] == "http" else backend["python"]["adapter"]
            make_chip(self.destination, f"Searches go to {where}", "info").pack(side="left", padx=(8, 0))

    def _config_changed(self):
        """The organization file or its signature changed: verify it again, and show where searches now go.
        Only the destination is taken over live; defaults for new warrants apply to the next composition, so a
        warrant being composed isn't rewritten underneath the analyst."""
        try:
            fresh = org_config.load(self.paths, self.org_config_path)
        except org_config.ConfigError as exc:
            self.report_problem("config", f"Organization configuration: {exc.message} ({exc.code}); searches are refused")
            self._show_destination(problem=str(exc))
            return
        self.report_problem("config", None)
        self.verified_settings = fresh  # what's in force now; the draft keeps the configuration it started with
        self._show_destination()
        outdated = org_config.authority_hash(fresh) != org_config.authority_hash(self.settings)
        self.report_problem("config_draft", (
            f"The organization configuration changed (revision {fresh['organization']['revision']}): start a new "
            "draft in Compose to use it") if outdated else None)
        self.compose.config_changed(outdated)

    def invalidate_inspectors(self):
        """Make every inspector render again (e.g. after the time zone changed: their text has times in it)."""
        for tab in (getattr(self, name, None) for name in ("approvals", "runs", "templates", "warrants_tab")):
            if tab is not None:
                tab._shown = None

    def adopt_new_config(self):
        """Start composing under the configuration now in force: a fresh Compose tab built from it (defaults,
        labels, certification wording, place limits), so no draft mixes two configurations."""
        fresh = getattr(self, "verified_settings", self.settings)
        self.settings = {**fresh, "ui": self.settings["ui"]}
        old = self.compose
        index = self.notebook.index(old)
        self.compose = ComposeTab(self.notebook, self)
        self.notebook.insert(index, self.compose, text=f"  {self.tab_names.pop(old, 'Compose')}  ")
        self.tab_names[self.compose] = "Compose"
        self.notebook.forget(old)
        old.destroy()
        for modifier in ("Control", "Command"):
            self.bind_all(f"<{modifier}-Key-1>", lambda _e: self.notebook.select(self.compose))
        self.notebook.select(self.compose)
        self.report_problem("config_draft", None)
        self.refresh_all()
        self.notify(f"Composing under configuration revision {self.settings['organization']['revision']}")

    def _org_files_stat(self):
        path = org_config.org_path(self.paths, self.org_config_path)[0]

        def stat(p):
            try:
                s = p.stat()
                return s.st_mtime_ns, s.st_size
            except FileNotFoundError:
                return None
        return stat(path), stat(path.with_name(path.name + ".sig"))

    def notify(self, text: str):
        self.notice.configure(text=text)
        self.after(10000, lambda: self.notice.configure(text="") if self.notice.cget("text") == text else None)

    def report_problem(self, key: str, text: str | None):
        if text:
            self.problems[key] = text
        else:
            self.problems.pop(key, None)
        self.problem_label.configure(text="  ·  ".join(self.problems.values()))

    def about(self):
        AboutDialog(self)

    def appearance(self):
        """One Appearance dialog at a time, so cancelling one can't undo another's saved changes."""
        existing = getattr(self, "appearance_dialog", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_set()
            return existing
        self.appearance_dialog = AppearanceDialog(self)
        return self.appearance_dialog

    # --- unlock / signing ------------------------------------------------------------

    def _set_title(self):
        """Organization · Lucid Warrant · the project, so separate projects are easy to tell apart."""
        folder = self.paths.data.parent
        project = folder.parent.name if folder.name == workspace.PROJECT_DIR else folder.name
        self.title(f"{self.settings['organization']['name']} · Lucid Warrant · {project}")

    def create_signing_key(self, parent=None) -> bool:
        """Create the signing key (and sign the starter template library with it). Returns True if created."""
        while True:
            passphrase = PassphraseDialog.ask(parent or self, "Choose a signing passphrase", confirm=True)
            if passphrase is None:
                return False
            try:
                generate_keypair(self.paths, passphrase)
                break
            except ValueError as exc:
                messagebox.showerror("Passphrase", str(exc), parent=parent or self)
        key = load_private_key(self.paths, passphrase)
        if not self.paths.library.exists():
            sign_seed_library(self.paths, key)
        self._review_unsigned_org(key, parent)
        del passphrase, key
        self._offer_key_restriction(parent)
        self._key_just_created = True  # no need to unlock straight after choosing the passphrase
        return True

    def _offer_key_restriction(self, parent=None) -> None:
        """Right after the key is made: say if other accounts can reach it, and offer to restrict access."""
        from ..key_access import problems, restrict

        found = problems(self.paths)
        if found and messagebox.askyesno(
                "Key access", " ".join(found) + "\n\nOther accounts could try passphrases offline, or replace the "
                "public key. Restrict the keys folder to you (and, on Windows, SYSTEM and Administrators)?",
                parent=parent or self):
            try:
                restrict(self.paths)
            except Exception as exc:  # noqa: BLE001 - shown to the analyst
                messagebox.showerror("Key access", f"Couldn't change the permissions: {exc}", parent=parent or self)

    def _review_unsigned_org(self, private_key, parent=None) -> bool:
        """An organization file put in place before the key existed is signed only after the analyst has seen what
        it sets (where searches go, default exclusions and limits), never as a side effect of creating the key."""
        path = org_config.unsigned_org_file(self.paths, self.org_config_path)
        if path is None:
            return False
        data = org_config.read_file(path)
        from .org_review import OrganizationReview

        if not OrganizationReview(parent or self, path, data).run():
            return False
        org_config.sign_reviewed(self.paths, path, data, private_key)
        return True

    def tutorial(self, start: int = 0):
        existing = getattr(self, "tutorial_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return existing
        self.tutorial_window = Tutorial(self, start=start)
        return self.tutorial_window

    def _unlock_or_setup(self) -> bool:
        paths = self.paths
        if self._key_just_created and keys_exist(paths):
            return True
        created = not keys_exist(paths)
        if created:
            if not messagebox.askokcancel(
                "Create signing key",
                f"No signing key found in {paths.keys}.\n\nCreate one now? You'll choose a passphrase (at least "
                f"{MIN_PASSPHRASE_LENGTH} characters). It is never stored; if you lose it, create a new key and re-sign the library.",
            ):
                return False
            while True:
                passphrase = PassphraseDialog.ask(self, "Choose a signing passphrase", confirm=True)
                if passphrase is None:
                    return False
                try:
                    generate_keypair(paths, passphrase)
                    break
                except ValueError as exc:
                    messagebox.showerror("Passphrase", str(exc))
        else:
            while True:
                passphrase = PassphraseDialog.ask(self, "Unlock Lucid Warrant")
                if passphrase is None:
                    return False
                try:
                    load_private_key(paths, passphrase)
                    break
                except BadPassphrase:
                    messagebox.showerror("Unlock", "Wrong passphrase.")
        if not paths.library.exists() and messagebox.askyesno(
            "Template library", "No signed template library yet. Sign the seed library (5 basic templates) now?"
        ):
            sign_seed_library(paths, load_private_key(paths, passphrase))
        if created:
            self._review_unsigned_org(load_private_key(paths, passphrase))
        del passphrase
        return True

    def sign(self, title: str, summary: str, action, document=None):
        """Show what's being signed (a summary or a document), ask for the passphrase, then run
        action(private_key). Returns its result, or None."""
        while True:
            passphrase = PassphraseDialog.ask(self, title, summary, document=document)
            if passphrase is None:
                return None
            try:
                key = load_private_key(self.paths, passphrase)
            except BadPassphrase:
                messagebox.showerror(title, "Wrong passphrase.", parent=self)
                continue
            finally:
                del passphrase
            try:
                return action(key)
            except Exception as exc:  # noqa: BLE001 - every failure is shown, nothing is signed
                messagebox.showerror(title, str(exc), parent=self)
                return None
            finally:
                del key

    # --- navigation ----------------------------------------------------------------------

    def _tab_changed(self, _event=None):
        if self.notebook.select() == str(self.runs):
            self.runs.entered_from_tab()

    def _go(self, tab):
        """Switch tabs on behalf of a link (so link-applied filters are kept)."""
        self._navigating = True
        self.notebook.select(tab)
        self.update_idletasks()
        self._navigating = False

    def show_compose(self):
        self.notebook.select(self.compose)

    def new_warrant(self):
        if self.compose.editing and not messagebox.askyesno(
            "New warrant", f"You're editing {self.compose.editing}. Discard that edit and start a new warrant?", parent=self
        ):
            self.notebook.select(self.compose)
            return
        self.compose.cancel_edit()
        self.notebook.select(self.compose)

    def edit_warrant(self, warrant_id, doc, expired):
        self.compose.start_edit(warrant_id, doc, expired)
        self.notebook.select(self.compose)

    def show_warrant(self, warrant_id):
        self._go(self.warrants_tab)
        self.warrants_tab.select(warrant_id)

    def show_template(self, template_id):
        self._go(self.templates)
        self.templates.select(template_id)

    def show_approval(self, iid):
        self._go(self.approvals)
        self.approvals.select(iid)

    def show_run(self, run_id):
        self.runs.arriving_by_link = self.notebook.select() != str(self.runs)
        self._go(self.runs)
        self.runs.select(run_id)

    def show_runs(self, warrant=None, template=None):
        self.runs.arriving_by_link = self.notebook.select() != str(self.runs)
        self._go(self.runs)
        self.runs.filter_by(warrant=warrant, template=template)

    # --- refresh ---------------------------------------------------------------------------

    def refresh_all(self):
        from ..executor import audit_problems
        from ..library import load_library

        try:
            self.field_mismatch = org_config.field_mismatches(org_config.authority_values(self.settings),
                                                              load_library(self.paths, self.public_key))
        except Exception:  # noqa: BLE001 - library problems are reported by the Templates tab
            self.field_mismatch = []
        self.report_problem("fields", "Configuration and template library disagree on field names: "
                            + "; ".join(self.field_mismatch) if self.field_mismatch else None)

        try:
            self.compose.refresh_rbac()
            self.report_problem("rbac", None)
        except Exception as exc:  # noqa: BLE001
            self.report_problem("rbac", f"Entitlements couldn't refresh: {exc}")
        from ..executor import ledger_problems

        bad_lines = audit_problems(self.paths)
        self.report_problem("audit", f"{plural(len(bad_lines), 'unreadable audit line')} skipped" if bad_lines else None)
        bad_ledger = ledger_problems(self.paths)
        self.report_problem("ledger", "Single-use ledger unreadable: one-time approvals are blocked until it's repaired"
                            if bad_ledger else None)
        for name, tab in (("approvals", self.approvals), ("warrants", self.warrants_tab),
                          ("templates", self.templates), ("runs", self.runs)):
            try:
                tab.refresh()
                if name != "templates":
                    self.report_problem(name, None)
            except Exception as exc:  # noqa: BLE001 - keep the app usable and say what failed
                self.report_problem(name, f"{name.capitalize()} couldn't refresh: {exc}")
        self.compose.refresh_warrants()
        self.compose.update_footer()
        pending = getattr(self.approvals, "needs_review", 0)
        name = self.tab_names[self.approvals]
        self.notebook.tab(self.approvals, text=f"  {name} ({pending})  " if pending else f"  {name}  ")
        failing = [k for k in self.problems if k not in ("theme", "audit", "ledger", "fields")]
        now = shown_time(utc_now())
        if failing:
            self._last_state = None  # retry on the next poll even if no file changes
            self.refresh_label.configure(text=f"Refresh incomplete at {now:%H:%M:%S} {zone_label(now)}")
        else:
            self.refresh_label.configure(text=f"Live · updated {now:%H:%M:%S} {zone_label(now)}")

    def _snapshot(self):
        p = self.paths

        def stat(path):
            try:
                s = path.stat()
                return s.st_mtime_ns, s.st_size
            except FileNotFoundError:
                return None

        def listing(folder, pattern):
            if not folder.exists():
                return ()
            return tuple((x.name, stat(x)) for x in sorted(folder.glob(pattern)))

        def expired_now():
            out = []
            folders = [f for f in (p.warrants, p.onetime) if f.exists()]
            for path in sorted(x for f in folders for x in list(f.glob("w-*.json")) + list(f.glob("ot-*.json"))):
                try:
                    out.append(from_iso(json.loads(path.read_text("utf-8"))["expires_at"]) <= utc_now())
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    out.append(None)  # malformed: the tabs report it; polling carries on
            return tuple(out)

        state = (
            listing(p.syntax_requests, "req-*.json"),
            listing(p.syntax_requests / "rejected", "req-*.json"),
            listing(p.onetime, "ot-*.json*"),
            listing(p.warrants, "w-*.json*"),
            listing(p.warrants / "revoked", "w-*"),
            expired_now(),
            stat(p.audit_log),
            listing(p.state / "summaries", "run-*.json"),  # the agent's run summaries
            stat(p.used_onetime),
            stat(p.library),
            stat(p.library.with_name(p.library.name + ".sig")),
            stat(p.rbac),
        )
        return state, (stat(p.nominations), stat(p.proposal))

    def _poll(self):
        try:
            state, inbox = self._snapshot()
            if state != self._last_state:
                self._last_state = state
                self.refresh_all()
                self.compose.merge_inbox()
            if inbox != self._last_inbox:
                self._last_inbox = inbox
                self.compose.inbox_changed()
            org = self._org_files_stat()
            if org != self._last_org:
                self._last_org = org
                self._config_changed()
            self.report_problem("poll", None)
        except Exception as exc:  # noqa: BLE001
            self.report_problem("poll", f"Live refresh failed: {exc}")
        finally:
            self.after(self.POLL_MS, self._poll)


def main() -> None:
    app = App(Paths.default(), os.environ.get("LUCID_WARRANT_ORG_CONFIG"), first_run=workspace.needs_setup())
    if app.ready:
        app.mainloop()
    else:
        app.destroy()
