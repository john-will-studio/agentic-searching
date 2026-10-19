"""Theme: Sun Valley (sv-ttk), following the OS light/dark setting at startup.

Custom-drawn pieces (chips, calendar, badges) take their colors from PALETTE so
light and dark stay consistent. Override the mode with LUCID_WARRANT_THEME=light|dark.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from ..org_config import BASE_PALETTES as PALETTES  # noqa: E402 - one definition, shared with the contrast check

PALETTE: dict[str, str] = dict(PALETTES["light"])
FONTS: dict[str, tkfont.Font] = {}
MODE = "light"
ACTIVE = "not applied"


def detect_mode(setting: str = "system") -> str:
    forced = os.environ.get("LUCID_WARRANT_THEME", "").lower() or setting
    if forced in PALETTES:
        return forced
    try:
        import darkdetect

        return "dark" if darkdetect.isDark() else "light"
    except Exception:
        return "light"


DENSITY_ROWS = {"compact": 1.5, "comfortable": 1.9, "spacious": 2.2}


# Sun Valley draws buttons, checkboxes, switches, tabs, fields and cards from image sprites in its own
# blue and greys. A color scheme recolors those sprites: accent pixels take the scheme's accent (kept at
# 4.5:1 or better against the text drawn on them, hover and pressed included), grey pixels take a faint
# tint of its hue (as agent-gui tints its panels). The sprite sheet PNG is decoded in Python and copied
# back into Sun Valley's images, which works on Tk 8.6 as well as Tk 9.
SV_ACCENT = {"light": "#005fb8", "dark": "#57c8ff"}
# The tutorial's outline around the section it describes: gold, at least 3:1 against every built-in scheme's
# background. Not the danger red, which means a problem elsewhere in the app.
HIGHLIGHT = {"light": "#a86d00", "dark": "#f2c94c"}
SPRITE_TINT = {"light": 0.22, "dark": 0.10}
MIN_CONTRAST = 4.6
_SHEETS: dict = {}


def _hls(hex_color: str):
    import colorsys

    return colorsys.rgb_to_hls(*(int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)))


def _sheet(mode: str):
    """(width, height, original RGBA rows, sprite regions) for Sun Valley's sprite sheet, decoded once."""
    if mode not in _SHEETS:
        import re
        from pathlib import Path

        import sv_ttk

        from .pngdata import read_rgba

        folder = Path(sv_ttk.__file__).parent / "theme"
        width, height, rows = read_rgba((folder / f"spritesheet_{mode}.png").read_bytes())
        info = (folder / f"sprites_{mode}.tcl").read_text("utf-8")
        regions = [(m[0], int(m[1]), int(m[2]), int(m[3]), int(m[4]))
                   for m in re.findall(r"^\s*([\w-]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", info, re.M)]
        _SHEETS[mode] = (width, height, rows, regions)
    return _SHEETS[mode]


def _recolored_sheet(mode: str, palette: dict, theme_color: str | None) -> tuple[int, int, list[bytearray], list]:
    import colorsys

    from ..org_config import contrast, theme_hue_chroma

    width, height, original, regions = _sheet(mode)
    base_h, base_l, _ = _hls(SV_ACCENT[mode])
    target = _hls(palette["accent"])
    on_accent = palette["accent_text"]
    step = -0.02 if _hls(on_accent)[1] > 0.5 else 0.02  # light text: darken the accent; dark text: lighten it
    tint = theme_hue_chroma(theme_color) if theme_color else None
    disabled = bytearray(width * height)  # disabled sprites don't need text contrast
    for name, x, y, w, h in regions:
        if name.endswith("-dis") or "-dis-" in name:
            for yy in range(y, min(y + h, height)):
                disabled[yy * width + x:yy * width + min(x + w, width)] = b"\x01" * (min(x + w, width) - x)
    cache: dict = {}
    rows = []
    for yy, row in enumerate(original):
        out = bytearray(row)
        for xx in range(width):
            i = xx * 4
            if row[i + 3] == 0:
                continue
            key = (row[i], row[i + 1], row[i + 2], disabled[yy * width + xx])
            if key not in cache:
                h, l, s = colorsys.rgb_to_hls(row[i] / 255, row[i + 1] / 255, row[i + 2] / 255)
                new = None
                if s > 0.35 and abs(h - base_h) < 0.06:  # the accent, not the red error color
                    sat = min(target[2] * s / 0.9, 1.0)
                    l = min(max(l * (target[1] / base_l), 0.0), 1.0)
                    if not key[3]:
                        for _ in range(60):  # hover/pressed shades too: keep the text on them readable
                            r, g, b = colorsys.hls_to_rgb(target[0], l, sat)
                            if contrast("#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255)), on_accent) >= MIN_CONTRAST:
                                break
                            l = min(max(l + step, 0.0), 1.0)
                    new = (target[0], l, sat)
                elif tint is not None and s < 0.15:  # greys: fields, buttons, cards, borders
                    new = (tint[0], l, min(SPRITE_TINT[mode] * tint[1], 1.0))
                cache[key] = None if new is None else tuple(round(c * 255) for c in colorsys.hls_to_rgb(*new))
            if cache[key] is not None:
                out[i:i + 3] = bytes(cache[key])
        rows.append(out)
    return width, height, rows, regions


def recolor_sprites(root: tk.Misc, mode: str, palette: dict, theme_color: str | None = None) -> int:
    """Rebuild Sun Valley's sprites for this palette from the original sheet (so switching schemes never
    compounds). Returns the number of sprites updated."""
    import base64

    from .pngdata import write_rgba

    array = f"ttk::theme::sv_{mode}::I"
    width, height, rows, regions = _recolored_sheet(mode, palette, theme_color)
    sheet = root.tk.call("image", "create", "photo", "-format", "png",
                         "-data", base64.b64encode(write_rgba(width, height, rows)).decode("ascii"))
    try:
        count = 0
        for name, x, y, w, h in regions:
            try:
                image = root.tk.splitlist(root.tk.call("set", f"{array}({name})"))[0]
            except tk.TclError:
                continue
            root.tk.call(image, "copy", sheet, "-from", x, y, x + w, y + h, "-to", 0, 0, "-compositingrule", "set")
            count += 1
        return count
    finally:
        root.tk.call("image", "delete", sheet)


_SV_COLOR_DEFAULTS: dict = {}


def _set_sv_colors(root: tk.Misc, mode: str, palette: dict, colored: bool) -> None:
    """Sun Valley re-colors the base style and every classic tk widget (tk_setPalette) from its own color table
    whenever the theme changes. Point that table at our palette, so its reset applies the scheme rather than
    undoing it. The handler runs on the next idle, after this."""
    array = f"ttk::theme::sv_{mode}::colors"
    key = (str(root), mode)
    names = ("-bg", "-fg", "-selbg", "-selfg", "-accent")
    if key not in _SV_COLOR_DEFAULTS:
        _SV_COLOR_DEFAULTS[key] = {name: str(root.tk.call("set", f"{array}({name})")) for name in names}
    values = dict(_SV_COLOR_DEFAULTS[key])
    values.update({"-bg": palette["bg"], "-fg": palette["text"]})
    if colored:
        values.update({"-selbg": palette["accent"], "-selfg": palette["accent_text"], "-accent": palette["accent"]})
    for name, value in values.items():
        root.tk.call("set", f"{array}({name})", value)


RETINT_OPTIONS = ("background", "foreground", "highlightbackground", "highlightcolor", "selectbackground",
                  "selectforeground", "insertbackground", "activebackground", "activeforeground", "troughcolor",
                  "disabledforeground", "readonlybackground")


def _walk(root: tk.Misc):
    stack = [root]
    while stack:
        widget = stack.pop()
        try:
            stack.extend(widget.winfo_children())
        except tk.TclError:
            continue
        yield widget


def retint_widgets(root: tk.Misc, old: dict, new: dict) -> int:
    """Swap old palette colors for new ones on every classic tk widget, Text tag, Canvas item and Treeview
    tag already on screen, so a scheme or light/dark change shows without restarting."""
    mapping = {}
    for token, value in old.items():
        if new.get(token) and new[token].lower() != value.lower():
            mapping.setdefault(value.lower(), new[token])
    if not mapping:
        return 0
    changed = 0
    for widget in _walk(root):
        for option in RETINT_OPTIONS:
            try:
                value = str(widget.cget(option)).lower()
            except (tk.TclError, AttributeError, ValueError):
                continue
            if value in mapping:
                widget.configure(**{option: mapping[value]})
                changed += 1
        if isinstance(widget, (tk.Text, ttk.Treeview)):
            for tag in widget.tk.splitlist(widget.tk.call(widget, "tag", "names")):
                for option in ("background", "foreground"):
                    try:
                        if isinstance(widget, tk.Text):  # Text's `tag configure` returns a descriptor; cget the value
                            value = str(widget.tag_cget(tag, option)).lower()
                        else:
                            value = str(widget.tk.call(widget, "tag", "configure", tag, "-" + option)).lower()
                    except tk.TclError:
                        continue
                    if value in mapping:
                        widget.tk.call(widget, "tag", "configure", tag, "-" + option, mapping[value])
        if isinstance(widget, tk.Canvas):
            for item in widget.find_all():
                for option in ("fill", "outline"):
                    try:
                        value = str(widget.itemcget(item, option)).lower()
                    except tk.TclError:
                        continue
                    if value in mapping:
                        widget.itemconfigure(item, **{option: mapping[value]})
    return changed


def _keep_widget_colors(root: tk.Misc) -> None:
    """Sun Valley's <<ThemeChanged>> handler (on the Tk class) runs tk_setPalette, which overwrites every classic
    widget's colors with the general background, so a Text's lighter surface is lost. Record the colors just
    before that handler (a binding on the root widget runs first) and put them back after it (an "all" binding
    runs last)."""
    if getattr(root, "_lw_keep_colors", False):
        return
    root._lw_keep_colors = True
    saved: list = []

    def before(event):
        if event.widget is not root:
            return
        saved.clear()
        for widget in _walk(root):
            if isinstance(widget, ttk.Widget):
                continue
            for option in RETINT_OPTIONS:
                try:
                    saved.append((widget, option, widget.cget(option)))
                except (tk.TclError, ValueError):
                    pass

    def after(event):
        if event.widget is not root:
            return
        for widget, option, value in saved:
            try:
                if not widget.winfo_exists() or root.nametowidget(str(widget)) is not widget:
                    continue  # destroyed, or replaced by a new widget with the same path
                widget.configure(**{option: value})
            except (tk.TclError, KeyError):
                pass
        saved.clear()

    root.bind("<<ThemeChanged>>", before, "+")
    root.bind_all("<<ThemeChanged>>", after, "+")


_FONT_DEFAULTS: dict = {}
SCALED_FONTS = ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont")


def _plain_notebook_border(root: tk.Misc, style: ttk.Style, p: dict) -> None:
    """Draw the tab pane's and tables' borders as plain lines instead of Sun Valley's stretched images.

    On macOS, scrolling a page redraws the pane around it; a 9-slice image the size of the window made every
    scroll step take ~60 ms (laggy with a trackpad's stream of events). A drawn border takes ~20 ms."""
    try:
        root.tk.call("ttk::style", "element", "create", "Plain.Notebook.border", "from", "clam", "border")
    except tk.TclError:
        pass  # this theme already has it
    style.layout("TNotebook", [("Plain.Notebook.border", {"sticky": "nswe", "children": [
        ("TNotebook.Tab", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("TNotebook", bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                    borderwidth=1, relief="solid")
    # Tables: the same stretched image frames every Treeview (each costs ~3 ms per scroll step).
    try:
        root.tk.call("ttk::style", "element", "create", "Plain.Treeview.field", "from", "clam", "field")
    except tk.TclError:
        pass
    style.layout("Treeview", [("Plain.Treeview.field", {"sticky": "nswe", "border": "1", "children": [
        ("Treeview.padding", {"sticky": "nswe", "children": [("Treeview.treearea", {"sticky": "nswe"})]})]})])
    style.configure("Treeview", bordercolor=p["border"], lightcolor=p["border"])


def apply(root: tk.Tk, ui: dict | None = None, schemes: dict | None = None) -> str:
    """Apply the theme, color scheme, named fonts and density from the configuration's "ui" section.
    Safe to call again on a running app: everything already on screen follows. Returns the light/dark mode."""
    global MODE, ACTIVE
    ui = ui or {}
    from ..org_config import derive_palette
    from .widgets import set_time_zone

    set_time_zone(ui.get("time_zone", "utc"))

    old_palette = dict(PALETTE) if getattr(root, "_lw_themed", False) else None
    root._lw_themed = True
    MODE = detect_mode(ui.get("theme_mode", "system"))
    scheme = (schemes or {}).get(ui.get("scheme", "sun-valley"), {})
    colored = bool(scheme.get("color") or scheme.get(MODE))
    PALETTE.clear()
    PALETTE.update(derive_palette(MODE, scheme))
    try:
        import sv_ttk

        sv_ttk.set_theme(MODE)
        ACTIVE = "sun-valley"
        _set_sv_colors(root, MODE, PALETTE, colored)
        _keep_widget_colors(root)
    except Exception as exc:  # noqa: BLE001 - the app reports this in its status bar
        ttk.Style().theme_use("clam")
        ACTIVE = f"clam fallback: {exc}"
    else:
        try:
            recolor_sprites(root, MODE, PALETTE, scheme.get("color"))
        except Exception as exc:  # noqa: BLE001 - keep Sun Valley's own blue rather than lose the theme
            ACTIVE = f"sun-valley (scheme colors not applied: {exc})"

    key = str(root)
    if key not in _FONT_DEFAULTS:  # the system sizes, so "System" can be restored after a preview
        _FONT_DEFAULTS[key] = {}
        for name in SCALED_FONTS:
            try:
                _FONT_DEFAULTS[key][name] = tkfont.nametofont(name, root=root).cget("size")
            except (tk.TclError, TypeError):
                try:
                    _FONT_DEFAULTS[key][name] = tkfont.nametofont(name).cget("size")
                except tk.TclError:
                    pass
    for name, system_size in _FONT_DEFAULTS[key].items():
        try:
            tkfont.nametofont(name).configure(size=ui.get("base_font_pt") or system_size)
        except tk.TclError:
            pass
    base = tkfont.nametofont("TkDefaultFont")
    size = base.cget("size") or 10
    family = base.cget("family")
    # Some platforms express the system font in pixels (a negative Tk size).
    tutorial_pt = size if size > 0 else round(-size / root.winfo_fpixels("1p"))
    fonts = {
        "LW.Body": dict(family=family, size=size),
        "LW.Tutorial": dict(family=family, size=tutorial_pt + 3),
        "LW.Small": dict(family=family, size=max(size - 1, 8) if size > 0 else size + 1),
        "LW.Strong": dict(family=family, size=size, weight="bold"),
        "LW.Heading": dict(family=family, size=size + 3 if size > 0 else size - 3, weight="bold"),
        "LW.Title": dict(family=family, size=size + 6 if size > 0 else size - 6, weight="bold"),
        "LW.Mono": dict(family=tkfont.nametofont("TkFixedFont").cget("family"), size=size),
    }
    for name, spec in fonts.items():
        # Keep a reference: Tk deletes a named font when its Python object is collected.
        font = FONTS.get(name)
        if font is not None and getattr(font, "_root", None) is root:
            font.configure(**spec)
        else:  # first use, or a new Tk interpreter: fonts belong to the interpreter that made them
            font = tkfont.Font(root=root, name=name, exists=False, **spec)
            font._root = root
            FONTS[name] = font

    style = ttk.Style()
    p = PALETTE
    style.configure("Muted.TLabel", foreground=p["muted"])
    style.configure("Small.TLabel", foreground=p["muted"], font="LW.Small")
    style.configure("Heading.TLabel", font="LW.Heading")
    style.configure("Title.TLabel", font="LW.Title")
    style.configure("Strong.TLabel", font="LW.Strong")
    style.configure("Danger.TLabel", foreground=p["danger"])
    style.configure("Warn.TLabel", foreground=p["warn"])
    style.configure("Ok.TLabel", foreground=p["ok"])
    style.configure("Link.TLabel", foreground=p["accent"])
    for name in ("TFrame", "TLabel", "Title.TLabel", "Small.TLabel", "Strong.TLabel", "Ok.TLabel",
                 "TRadiobutton"):
        style.configure("Tutorial." + name, background=p["surface"])
    if not ACTIVE.startswith("clam"):
        # Sun Valley's flat backgrounds and text take the (possibly tinted) palette.
        style.configure(".", background=p["bg"], foreground=p["text"])
        style.configure("Treeview", background=p["bg"], fieldbackground=p["bg"], foreground=p["text"])
        style.map("Treeview", background=[("selected", p["accent_soft"])], foreground=[("selected", p["text"])])
        _plain_notebook_border(root, style, p)
        style.configure("Accent.TButton", foreground=p["accent_text"])
        style.map("Accent.TButton", foreground=[("disabled", p["accent_text"]), ("pressed", p["accent_text"]),
                                                 ("active", p["accent_text"])])
    if old_palette is not None:
        retint_widgets(root, old_palette, p)
    try:
        root.configure(bg=p["bg"])  # after the retint: a new color can equal another token's old color
    except tk.TclError:
        pass
    style.configure("Treeview", rowheight=int(tkfont.nametofont("LW.Body").metrics("linespace")
                                              * DENSITY_ROWS.get(ui.get("density", "comfortable"), 1.9)))
    return MODE


def text_widget_colors() -> dict:
    """Colors for plain tk.Text / tk.Listbox so they match the theme."""
    p = PALETTE
    return dict(background=p["surface"], foreground=p["text"], insertbackground=p["text"],
                selectbackground=p["accent"], selectforeground=p["accent_text"],
                highlightthickness=1, highlightbackground=p["border"], highlightcolor=p["accent"],
                relief="flat", borderwidth=0)
