"""About and Appearance dialogs."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import tkinter as tk
from tkinter import ttk

from . import msg as messagebox

from .. import __version__, org_config
from ..rbac import MockRbacProvider
from ..signing import key_id
from . import theme


class AboutDialog(tk.Toplevel):
    """What this installation is using, each item with a plain explanation of why it matters."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("About Lucid Warrant")
        self.configure(bg=theme.PALETTE["bg"])
        self.transient(app)
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Lucid Warrant", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text=f"Version {__version__} · signed approval of agent searches",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.details = self._details()
        row = 2
        for title, value, why in self.details["rows"]:
            ttk.Label(frame, text=title, style="Strong.TLabel").grid(row=row, column=0, sticky="nw", pady=(8, 0), padx=(0, 16))
            entry = ttk.Entry(frame, width=64)
            entry.insert(0, value)
            entry.configure(state="readonly")
            entry.grid(row=row, column=1, sticky="ew", pady=(8, 0))
            ttk.Label(frame, text=why, style="Small.TLabel", wraplength=560, justify="left").grid(
                row=row + 1, column=1, sticky="w")
            row += 2
        # Who else on this computer can reach the signing key (check and warn; restrict only when asked).
        ttk.Label(frame, text="Key access", style="Strong.TLabel").grid(row=row, column=0, sticky="nw", pady=(8, 0),
                                                                        padx=(0, 16))
        self.key_access = ttk.Label(frame, wraplength=560, justify="left")
        self.key_access.grid(row=row, column=1, sticky="w", pady=(8, 0))
        self.restrict_button = ttk.Button(frame, text="Restrict access", command=self._restrict)
        self.restrict_button.grid(row=row + 1, column=1, sticky="w", pady=(4, 0))
        self._show_key_access()
        row += 2
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Copy details", command=self._copy).pack(side="right", padx=8)
        from .. import workspace

        change = ttk.Button(buttons, text="Open or create project…", command=self._change_workspace)
        change.pack(side="right")
        if workspace.overridden_by_environment():
            change.configure(state="disabled")
        self.bind("<Escape>", lambda _e: self.destroy())

    def _show_key_access(self):
        from ..key_access import problems

        found = problems(self.app.paths)
        if found:
            self.key_access.configure(style="Warn.TLabel", text="⚠ " + " ".join(found) + " Other accounts could try "
                                      "passphrases offline, or replace the public key.")
            self.restrict_button.grid()
        else:
            self.key_access.configure(style="TLabel", text="Only you (and the system) can reach the signing key.")
            self.restrict_button.grid_remove()

    def _restrict(self):
        from ..key_access import restrict

        try:
            restrict(self.app.paths)
        except Exception as exc:  # noqa: BLE001 - shown to the analyst
            messagebox.showerror("Restrict access", f"Couldn't change the permissions: {exc}", parent=self)
        self._show_key_access()

    def _details(self) -> dict:
        app, s = self.app, self.app.settings
        org_file, how = org_config.org_path(app.paths, getattr(app, "org_config_path", None))
        try:
            fingerprint = key_id(app.public_key)
        except Exception:  # noqa: BLE001
            fingerprint = "unavailable"
        rbac = "Mock file (practice mode)" if isinstance(app.rbac, MockRbacProvider) else type(app.rbac).__name__
        problems = "; ".join(app.problems.values()) or "None"
        from .. import workspace

        if workspace.overridden_by_environment():
            where = ("Set by LUCID_WARRANT_DATA / LUCID_WARRANT_KEYS",
                     "Folders chosen by environment variables (tests, scenarios or a scripted setup).")
        else:
            where = (str(app.paths.data.parent),
                     "Everything for this deployment: the signing key, warrants, the agent's inbox and the record of "
                     "every search. Remembered in " + str(workspace.workspace_file()) + "; the agent's commands use it too.")
        rows = [
            ("Warrant directory", *where),
            ("Organization", f"{s['organization']['name']} · revision {s['organization']['revision']}",
             "The organization configuration in use: labels, defaults and certification wording."),
            ("Configuration file", f"{org_file}{'' if org_file.exists() else ' (not present: using built-in defaults)'}",
             f"Chosen by {how}. Agents change it with “lucid-warrant config …”; see docs/AGENT-CLI.md."),
            ("Configuration hash", org_config.authority_hash(s),
             "Fingerprint of the configuration. Every warrant you sign records it, so later changes can't alter its meaning."),
            ("Signing key fingerprint", fingerprint,
             "Identifies your signing key. Printed warrants show it; compare to confirm who signed."),
            ("Signing key folder", str(app.paths.keys), "Your encrypted private key. Keep it backed up; never share it."),
            ("Data folder", str(app.paths.data),
             "Warrants, approvals, the template library, the agent's inbox and the audit log of every search."),
            ("Entitlements", f"{rbac} · {app.paths.rbac}",
             "Where the search authorization and visibility options come from. The executor re-checks them on every search."),
            ("Appearance", f"{s['ui']['scheme']} scheme · {theme.MODE} mode · {theme.ACTIVE}",
             "Personal display preferences. Change them under Appearance… (Ctrl+,)."),
            ("Status", problems, "Anything currently preventing normal operation. “None” is what you want to see."),
            ("Software", f"Python {platform.python_version()} · Tk {app.tk.call('info', 'patchlevel')} · {platform.system()} {platform.release()}",
             "Include this when reporting a problem."),
        ]
        return {"rows": rows}

    def _change_workspace(self):
        """Create or open another warrant directory (a separate project), then restart in it."""
        from .. import workspace
        from .widgets import WorkspaceDialog

        chosen = WorkspaceDialog.ask(self, initial=workspace.default_location(), intro=False)
        if chosen is None:
            return
        if chosen.resolve() == self.app.paths.data.parent.resolve():
            return
        if not messagebox.askyesno("Warrant directory", f"Lucid Warrant will restart in {chosen}. Continue?", parent=self):
            return
        workspace.create(chosen)
        workspace.remember(chosen)
        relaunch_in(chosen)
        self.app.destroy()

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(json.dumps({title: value for title, value, _ in self.details["rows"]}, indent=2))
        self.app.notify("Copied installation details")


def relaunch_in(chosen) -> subprocess.Popen:
    """Start a new app in `chosen` (the caller then closes this one)."""
    command, options = restart_in(chosen)
    return subprocess.Popen(command, **options)


def restart_in(chosen) -> tuple[list[str], dict]:
    """(command, Popen options) for a new app in `chosen`. The choice is passed explicitly: otherwise the old
    project could win again (found from the current folder, or an inherited LUCID_WARRANT_WORKSPACE)."""
    import os

    from .. import workspace

    env = {k: v for k, v in os.environ.items() if k not in ("LUCID_WARRANT_DATA", "LUCID_WARRANT_KEYS")}
    env[workspace.WORKSPACE_ENV] = str(chosen)
    return [sys.executable, "-m", "lucid_warrant.gui"], {"env": env, "cwd": str(chosen)}


class AppearanceDialog(tk.Toplevel):
    """Personal display preferences: theme mode, color scheme, font size, density."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Appearance")
        self.configure(bg=theme.PALETTE["bg"])
        self.transient(app)
        ui = app.settings["ui"]
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Appearance", style="Heading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="Personal preferences; they never change what a warrant authorizes.",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="Theme").grid(row=2, column=0, sticky="nw", pady=4)
        self.mode = tk.StringVar(value=ui["theme_mode"])
        modes = ttk.Frame(frame)
        modes.grid(row=2, column=1, sticky="w")
        for value, label in (("system", "Follow system"), ("light", "Light"), ("dark", "Dark")):
            ttk.Radiobutton(modes, text=label, value=value, variable=self.mode).pack(side="left", padx=(0, 12))

        ttk.Label(frame, text="Color scheme").grid(row=3, column=0, sticky="nw", pady=(10, 4))
        self.scheme = tk.StringVar(value=ui["scheme"])
        self.theme_color = ui.get("theme_color", "")
        self.schemes_frame = ttk.Frame(frame)
        self.schemes_frame.grid(row=3, column=1, sticky="w", pady=(10, 0))
        self._draw_schemes()
        ttk.Label(frame, text="The scheme's color tints the background, panels and borders, and colors buttons, "
                              "checkboxes, tabs and links.", style="Muted.TLabel", wraplength=480, justify="left"
                  ).grid(row=4, column=1, sticky="w", pady=(4, 0))

        ttk.Label(frame, text="Text size").grid(row=5, column=0, sticky="w", pady=(10, 4))
        size = ttk.Frame(frame)
        size.grid(row=5, column=1, sticky="w", pady=(10, 0))
        self.font = tk.StringVar(value=str(ui["base_font_pt"] or "System"))
        ttk.Combobox(size, textvariable=self.font, state="readonly", width=10,
                     values=["System"] + [str(n) for n in range(9, 21)]).pack(side="left")
        ttk.Label(size, text="points", style="Muted.TLabel").pack(side="left", padx=6)

        ttk.Label(frame, text="Density").grid(row=6, column=0, sticky="w", pady=(10, 4))
        self.density = tk.StringVar(value=ui["density"])
        dens = ttk.Frame(frame)
        dens.grid(row=6, column=1, sticky="w", pady=(10, 0))
        for value in ("compact", "comfortable", "spacious"):
            ttk.Radiobutton(dens, text=value.capitalize(), value=value, variable=self.density).pack(side="left", padx=(0, 12))

        ttk.Label(frame, text="Times").grid(row=7, column=0, sticky="w", pady=(10, 4))
        self.time_zone = tk.StringVar(value=ui.get("time_zone", "utc"))
        zones = ttk.Frame(frame)
        zones.grid(row=7, column=1, sticky="w", pady=(10, 0))
        from datetime import datetime

        here = datetime.now().astimezone().strftime("%Z")
        for value, label in (("utc", "UTC"), ("local", f"This computer's time zone ({here})")):
            ttk.Radiobutton(zones, text=label, value=value, variable=self.time_zone).pack(side="left", padx=(0, 12))

        self.note = ttk.Label(frame, text="Changes show as you choose them. Save keeps them; Cancel puts back the previous look.",
                              style="Muted.TLabel", wraplength=480, justify="left")
        self.note.grid(row=8, column=0, columnspan=2, sticky="w", pady=(14, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=9, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right", padx=8)
        self.bind("<Return>", lambda _e: self.save())
        self.bind("<Escape>", lambda _e: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.saved_ui = dict(ui)
        for var in (self.mode, self.scheme, self.font, self.density, self.time_zone):
            var.trace_add("write", lambda *_: self.preview())

    def _draw_schemes(self) -> None:
        for child in self.schemes_frame.winfo_children():
            child.destroy()
        settings = {**self.app.settings, "ui": {**self.app.settings["ui"], "theme_color": self.theme_color}}
        shown = [(sid, sc) for sid, sc in org_config.schemes(settings).items()
                 if not sc.get("hidden") or sid == self.scheme.get()]
        for i, (sid, scheme) in enumerate(shown):
            row = ttk.Frame(self.schemes_frame)
            row.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 18), pady=2)
            swatch = scheme.get("color") or org_config.derive_palette(theme.MODE, scheme)["accent"]
            tk.Label(row, width=2, bg=swatch, relief="flat").pack(side="left", padx=(0, 6))
            ttk.Radiobutton(row, text=scheme.get("label", sid), value=sid, variable=self.scheme).pack(side="left")
        ttk.Button(self.schemes_frame, text="Custom color…", command=self.pick_custom).grid(
            row=(len(shown) + 2) // 3, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def pick_custom(self) -> None:
        from tkinter import colorchooser

        _rgb, color = colorchooser.askcolor(color=self.theme_color or "#75b991", parent=self, title="Theme color")
        if not color:
            return
        self.theme_color = color.lower()
        self._draw_schemes()
        if self.scheme.get() == "custom":
            self.preview()
        else:
            self.scheme.set("custom")

    def changes(self) -> dict:
        font = self.font.get()
        return {"theme_mode": self.mode.get(), "scheme": self.scheme.get(), "theme_color": self.theme_color,
                "base_font_pt": 0 if font == "System" else int(font), "density": self.density.get(),
                "time_zone": self.time_zone.get()}

    def preview(self) -> None:
        """Apply the dialog's current choices to the running app straight away."""
        self.app.settings["ui"] = {**self.app.settings["ui"], **self.changes()}
        theme.apply(self.app, self.app.settings["ui"], org_config.schemes(self.app.settings))
        self.configure(bg=theme.PALETTE["bg"])
        self.app.invalidate_inspectors()  # times on screen follow the time zone choice, open inspectors included
        self.app.refresh_all()

    def cancel(self) -> None:
        if self.app.settings["ui"] != self.saved_ui:
            self.app.settings["ui"] = dict(self.saved_ui)
            theme.apply(self.app, self.saved_ui, org_config.schemes(self.app.settings))
            self.app.invalidate_inspectors()
            self.app.refresh_all()
        self.destroy()

    def save(self):
        """Keep the choices (already showing) and close."""
        try:
            org_config.save_user_ui(self.app.paths, self.changes(), getattr(self.app, "org_config_path", None))
        except org_config.ConfigError as exc:
            messagebox.showerror("Appearance", str(exc), parent=self)
            return
        mode = theme.detect_mode(self.mode.get())
        scheme = org_config.schemes(self.app.settings).get(self.scheme.get(), {})
        problems = org_config.contrast_problems(org_config.derive_palette(mode, scheme))
        if problems:
            messagebox.showwarning("Appearance", "Saved, but some text may be hard to read with this scheme:\n\n"
                                   + "\n".join(problems), parent=self)
        self.saved_ui = dict(self.app.settings["ui"])
        self.app.notify("Appearance saved")
        self.destroy()
