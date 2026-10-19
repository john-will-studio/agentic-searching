"""An analyst explicitly chooses exact text or a pattern before including a term."""

import tkinter as tk
from tkinter import ttk

from ..syntax import SyntaxRejected, validate_literal, validate_pattern
from .widgets import _Dialog


def matching(term: dict) -> str:
    if term.get("match") != "pattern":
        return "Exact text"
    literal = term["literal"]
    if literal.endswith("*") and literal.count("*") == 1 and "?" not in literal:
        return f"Pattern: any word starting with {literal[:-1]}"
    return "Pattern: * any characters, ? one character (within one word)"


def term_label(term: dict) -> str:
    return term["literal"] + (" (pattern)" if term.get("match") == "pattern" else "")


class TermDialog(_Dialog):
    def __init__(self, master, title: str, label: str, term=None):
        super().__init__(master, title)
        term = term or {}
        ttk.Label(self.frame, text=label).pack(anchor="w")
        self.literal = tk.StringVar(value=term.get("literal", ""))
        self.mode = tk.StringVar(value=term.get("match", "exact"))
        self.entry = ttk.Entry(self.frame, textvariable=self.literal, width=48)
        self.entry.pack(fill="x", pady=(6, 12))
        ttk.Radiobutton(self.frame, text="Exact text", variable=self.mode, value="exact").pack(anchor="w")
        ttk.Radiobutton(self.frame, text="Pattern (* any characters, ? one character)", variable=self.mode,
                        value="pattern").pack(anchor="w")
        ttk.Label(self.frame, text="Patterns match within one word and must begin with at least three ordinary "
                  "characters before the first wildcard. Example: PROJECT*.", wraplength=460,
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        self.error = ttk.Label(self.frame, text="", wraplength=460, style="Danger.TLabel")
        self.error.pack(fill="x", pady=8)
        buttons = ttk.Frame(self.frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.bind("<Return>", lambda _e: self._save())
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def _save(self):
        literal, mode = self.literal.get(), self.mode.get()
        try:
            (validate_pattern if mode == "pattern" else validate_literal)(literal)
        except SyntaxRejected as exc:
            self.error.configure(text=str(exc))
            return
        self.result = {"literal": literal, "match": mode}
        self.destroy()

    @classmethod
    def ask(cls, master, title: str, label: str, term=None):
        return cls(master, title, label, term).run()
