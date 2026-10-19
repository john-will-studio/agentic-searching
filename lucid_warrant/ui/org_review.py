"""Review the captured organization configuration before signing during setup."""

import json
import tkinter as tk
from tkinter import ttk

from .. import org_config
from .widgets import Inspector, _Dialog


class OrganizationReview(_Dialog):
    def __init__(self, master, path, data):
        super().__init__(master, "Review and sign organization configuration")
        self.data = data
        ttk.Label(self.frame, text="Review and sign organization configuration", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(self.frame, text=f"{path}\nThis sets the destination, query rules and wording used for future warrants.",
                  wraplength=700, justify="left").pack(anchor="w", pady=8)
        self.full = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.frame, text="Show full configuration", variable=self.full,
                        command=self.render).pack(anchor="w")
        self.view = Inspector(self.frame, "")
        self.view.pack(fill="both", expand=True, pady=8)
        buttons = ttk.Frame(self.frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Review and sign organization configuration", style="Accent.TButton",
                   command=self.sign).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.geometry("780x620")
        self.render()

    def render(self):
        self.view.begin()
        for line in org_config.review_lines(self.data):
            if line in ("Authorization-affecting settings", "Changes from built-in defaults"):
                self.view.section(line)
            else:
                self.view.para(line)
        if self.full.get():
            self.view.section("Full configuration")
            self.view.code(json.dumps(org_config.merge(org_config.DEFAULTS, self.data), indent=2, ensure_ascii=False))

    def sign(self):
        self.result = True
        self.destroy()
