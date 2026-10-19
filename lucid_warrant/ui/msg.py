"""Themed replacements for tkinter.messagebox's common prompts."""

import tkinter as tk


def _show(title, message, kind, buttons, **options):
    # Import here: widgets also uses these helpers for validation messages.
    from .widgets import MessageDialog

    parent = options.pop("parent", None) or tk._default_root
    if parent is None:
        # Startup failures before a Tk window exists still need a way to be shown.
        from tkinter import messagebox

        return messagebox._show(title, message, kind, buttons, **options)
    return MessageDialog(parent, title or "", message or "", kind=options.pop("icon", kind),
                         buttons=options.pop("type", buttons), **options).run()


def askyesno(title=None, message=None, **options):
    return _show(title, message, "question", "yesno", **options) in (True, "yes")


def askokcancel(title=None, message=None, **options):
    return _show(title, message, "question", "okcancel", **options) in (True, "ok")


def showinfo(title=None, message=None, **options):
    return _show(title, message, "info", "ok", **options)


def showwarning(title=None, message=None, **options):
    return _show(title, message, "warning", "ok", **options)


def showerror(title=None, message=None, **options):
    return _show(title, message, "error", "ok", **options)
