"""Organization configuration: one validated JSON file, plus per-user appearance preferences.

Precedence (later wins): built-in DEFAULTS < organization file < user preferences.
- Organization file: --org-config / LUCID_WARRANT_ORG_CONFIG, else <data>/organization.json.
- User preferences: %APPDATA%\\LucidWarrant\\preferences.json (Windows),
  ~/Library/Application Support/LucidWarrant/preferences.json (macOS), ~/.config/lucid-warrant/ (other).
  Preferences may only contain "ui"; anything else is refused.

Everything outside "ui" is authority-relevant: it shapes what a signer sees and approves.
Signed warrants record a snapshot of those values and their hash (config_context), so changing
the configuration never changes the meaning of an existing warrant.

Schema: docs/CONFIG-REFERENCE.md. Agents change it with `lucid-warrant config ...` (docs/AGENT-CLI.md).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from .config import Paths

SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_BYTES = 1024 * 1024

CERT_PURPOSE = ("I certify that I have reviewed and validated the approved hit terms for compliance. "
                "The narrative above meets my organization's business needs and intent.")
CERT_LIMITS = ("I certify that these limits and constraints are appropriately defined, narrowly tailored to the "
               "organization's legitimate business needs, and consistent with applicable company policies, legal "
               "requirements, and precedent.")

PALETTE_TOKENS = ("bg", "surface", "text", "muted", "border", "accent", "accent_text", "accent_soft", "ok", "warn",
                  "danger", "info", "warn_bg", "danger_bg", "info_bg", "agent_bg", "agent_border", "code_bg")

DEFAULTS: dict = {
    "schema_version": SCHEMA_VERSION,
    "organization": {"id": "practice", "name": "Practice organization", "revision": 1},
    "parameters": {
        "parameter_1": {"label": "Search authorization",
                        "description": "Authorization identifier checked by the entitlement service."},
        "parameter_2": {"label": "Data visibility",
                        "description": "Visibility attributes checked by the entitlement service."},
    },
    "terms": {
        "warrant": {"singular": "Warrant", "plural": "Warrants"},
        "hit_term": {"singular": "Hit term", "plural": "Hit terms"},
        "client_id": {"singular": "Client ID", "plural": "Client IDs"},
        "template": {"singular": "Template", "plural": "Templates"},
        "syntax_request": {"singular": "Syntax request", "plural": "Syntax requests"},
        "run": {"singular": "Run", "plural": "Runs"},
    },
    "source_labels": {"script": "Script nomination", "agent": "Agent proposal", "human": "Analyst",
                      "human-edited": "Analyst-edited nomination"},
    "query_fields": {"content": "content", "date": "timestamp", "country": "country"},
    "defaults": {"expiry_minutes": 120, "max_rows_per_run": 20000, "max_queries_per_hour": 60, "lookback_days": 30,
                 "blocked_ranges": []},
    "country_scope": {
        "mode": "all",
        "include": [],
        "exclude": [{"code": "DE", "reason": "GDPR"}, {"code": "FR", "reason": "GDPR"}, {"code": "IT", "reason": "GDPR"}],
        "reason_label": "Reason",
        "default_reason": "GDPR",
    },
    # Places (circles) in the geographic constraint: how the search service writes "within this circle", and the
    # largest radius a place may have. Check the template against your service: argument order and units vary.
    "geography": {"place_clause": "INTERSECTS({lat},{lon},{km})", "max_radius_km": 25},
    "certification": {"purpose": CERT_PURPOSE, "limits": CERT_LIMITS},
    # Where searches go. "mock": the practice records. "http": a REST search API described here (no code).
    # "python": an adapter class you write. Secrets are never stored here: secret_headers name environment
    # variables. See docs/SEARCH-ADAPTER.md.
    "search_backend": {
        "type": "mock",
        "http": {
            "url": "", "method": "POST", "headers": {}, "secret_headers": {}, "params": {}, "body": {},
            "timeout_seconds": 30, "verify_tls": True, "results_path": "",
            "fields": {"record_id": "id", "title": "title", "timestamp": "timestamp", "country": "country",
                       "snippet": "content"},
            "service_query_id": {"header": "", "path": ""},
        },
        "python": {"adapter": "", "options": {}},
    },
    "ui": {
        "theme_mode": "system",
        "scheme": "sun-valley",
        "theme_color": "",
        "schemes": {},
        "window": {"width": 1280, "height": 860},
        "base_font_pt": 0,
        "density": "comfortable",
        "time_zone": "utc",  # times are shown in UTC; "local" shows this computer's time zone
    },
}

# Built-in color schemes, ported from agent-gui: each is one theme color whose hue tints the whole app
# (background, panels, borders, muted text and the accent), scaled by its saturation. Status colors
# (ok/warn/danger) and body text never rotate with the theme color, so contrast holds.
BUILTIN_SCHEMES = {
    "sun-valley": {"label": "Sun Valley (neutral)"},
    "green": {"label": "Green", "color": "#75b991"},
    "blue": {"label": "Blue", "color": "#6e9fe8"},
    "red": {"label": "Red", "color": "#dc7777"},
    "brown": {"label": "Brown", "color": "#aa8261"},
    "pink": {"label": "Pink", "color": "#df83b5"},
    "purple": {"label": "Purple", "color": "#ae8ade"},
    "orange": {"label": "Orange", "color": "#de9c61"},
    "teal": {"label": "Teal", "color": "#62bbb4"},
    "gold": {"label": "Gold", "color": "#cbb262"},
    "gray": {"label": "Gray", "color": "#a4a4a4"},
    # Earlier names, kept so saved preferences still load.
    "violet": {"label": "Purple", "color": "#ae8ade", "hidden": True},
    "forest": {"label": "Green", "color": "#75b991", "hidden": True},
    "graphite": {"label": "Gray", "color": "#a4a4a4", "hidden": True},
}

# Base light/dark palettes; the GUI's theme module uses these too.
BASE_PALETTES = {
    "light": {
        "bg": "#fafafa", "surface": "#ffffff", "text": "#1c1c1c", "muted": "#5f6368", "border": "#d0d0d0",
        "accent": "#005fb8", "accent_text": "#ffffff", "accent_soft": "#dbe9f7",
        "ok": "#0f7b0f", "warn": "#9d5d00", "danger": "#c42b1c", "info": "#005fb8",
        "warn_bg": "#fff4ce", "danger_bg": "#fde7e9", "info_bg": "#e5f1fb", "agent_bg": "#f3eefb", "agent_border": "#8764b8",
        "code_bg": "#f3f3f3",
    },
    "dark": {
        "bg": "#1c1c1c", "surface": "#2b2b2b", "text": "#f2f2f2", "muted": "#a8a8a8", "border": "#454545",
        "accent": "#57a6f0", "accent_text": "#000000", "accent_soft": "#1f3a55",
        "ok": "#6ccb5f", "warn": "#fce100", "danger": "#ff99a4", "info": "#99ccff",
        "warn_bg": "#433519", "danger_bg": "#442726", "info_bg": "#1a2d40", "agent_bg": "#2e2640", "agent_border": "#b39ddb",
        "code_bg": "#232323",
    },
}

# Neutral tokens a theme color tints: (lightness, saturation per unit of theme chroma). Agent-gui's
# dark panels are hsl(hue, 9.68% x chroma, 12.16%); light mode needs a little more to be visible.
NEUTRAL_TINT = {
    "light": {"bg": (0.955, 0.45), "code_bg": (0.935, 0.30), "border": (0.80, 0.20), "muted": (0.38, 0.12),
              "agent_bg": (0.95, 0.30)},
    "dark": {"bg": (0.115, 0.14), "surface": (0.165, 0.12), "code_bg": (0.135, 0.12), "border": (0.28, 0.10),
             "muted": (0.66, 0.08)},
}


def theme_hue_chroma(color: str) -> tuple[float, float]:
    """(hue 0–1, chroma 0–1.4) of a theme color, as agent-gui computes them."""
    import colorsys

    h, _l, s = colorsys.rgb_to_hls(*(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)))
    return h, min(1.4, s / 0.35)


def derive_palette(mode: str, scheme: dict) -> dict:
    """The full palette for a scheme: base palette, tinted by the scheme's color, then explicit token overrides.
    The accent is darkened (light mode) or lightened (dark mode) until its text reaches 4.5:1."""
    import colorsys

    palette = dict(BASE_PALETTES[mode])
    color = scheme.get("color")
    if color:
        h, chroma = theme_hue_chroma(color)

        def hls(l, s):
            r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
            return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))

        for token, (l, k) in NEUTRAL_TINT[mode].items():
            palette[token] = hls(l, k * chroma)
        if mode == "light":
            palette.update(accent_soft=hls(0.91, 0.45 * chroma), info_bg=hls(0.95, 0.5 * chroma))
            l, step, accent_text = 0.36, -0.02, "#ffffff"
        else:
            palette.update(accent_soft=hls(0.22, 0.35 * chroma), info_bg=hls(0.17, 0.35 * chroma))
            l, step, accent_text = 0.70, 0.02, "#101416"
        accent = hls(l, 0.55 * chroma if mode == "light" else 0.42 * chroma)
        while (contrast(accent, accent_text) < 4.6 or contrast(accent, palette["info_bg"]) < 4.6
               or contrast(accent, palette["bg"]) < 4.6) and 0.05 < l < 0.95:
            l += step
            accent = hls(l, 0.55 * chroma if mode == "light" else 0.42 * chroma)
        palette.update(accent=accent, info=accent, accent_text=accent_text)
    palette.update(scheme.get(mode) or {})
    return palette


USER_SECTIONS = {"schema_version", "ui"}


class ConfigError(ValueError):
    def __init__(self, path: str, message: str, code: str = "INVALID_VALUE"):
        super().__init__(f"{path}: {message}" if path else message)
        self.path, self.message, self.code = path, message, code


# --- locations --------------------------------------------------------------------------

def org_path(paths: Paths, override: str | None = None) -> tuple[Path, str]:
    """(path, how it was chosen)."""
    if override:
        return Path(override), "--org-config"
    if os.environ.get("LUCID_WARRANT_ORG_CONFIG"):
        return Path(os.environ["LUCID_WARRANT_ORG_CONFIG"]), "LUCID_WARRANT_ORG_CONFIG"
    return paths.data / "organization.json", "default (data folder)"


def user_path() -> Path:
    if os.environ.get("LUCID_WARRANT_PREFS"):
        return Path(os.environ["LUCID_WARRANT_PREFS"])
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "LucidWarrant" / "preferences.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LucidWarrant" / "preferences.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "lucid-warrant" / "preferences.json"


# --- reading ----------------------------------------------------------------------------

def _reject_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ConfigError(key, "duplicate key")
        seen[key] = value
    return seen


def read_file(path: Path) -> dict:
    """A config file's raw contents ({} if it doesn't exist). Never rewrites a broken file."""
    if not path.exists():
        return {}
    return parse_bytes(path, path.read_bytes())


def parse_bytes(path: Path, raw: bytes) -> dict:
    if len(raw) > MAX_BYTES:
        raise ConfigError(str(path), "file is larger than 1 MiB", "INVALID_CONFIG")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates,
                          parse_constant=lambda c: (_ for _ in ()).throw(ConfigError(c, "not a valid number")))
    except ConfigError as exc:
        raise ConfigError(str(path), exc.message, "INVALID_CONFIG") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigError(str(path), f"not valid JSON ({exc})", "INVALID_CONFIG") from exc
    if not isinstance(data, dict):
        raise ConfigError(str(path), "must be a JSON object", "INVALID_CONFIG")
    return data


# Numeric settings that may have decimals (the rest are whole numbers): e.g. a 0.1 km (100 m) radius cap.
DECIMAL_SETTINGS = {"geography.max_radius_km"}

# Maps whose keys are chosen by the user (scheme IDs, HTTP headers, request bodies), not fixed settings.
FREE_FORM = {"ui.schemes", "search_backend.http.headers", "search_backend.http.secret_headers",
             "search_backend.http.params", "search_backend.http.body", "search_backend.python.options"}


def merge(base: dict, override: dict, where: str = "") -> dict:
    """Objects merge key by key; anything else (lists included) replaces. Unknown keys are refused,
    except inside the free-form maps above."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        here = f"{where}.{key}" if where else key
        if where in FREE_FORM:
            # The user's own keys and values (a request body, headers): kept exactly, "_source" included.
            out[key] = copy.deepcopy(value)
            continue
        if key.startswith("_"):
            continue  # "_comment" and friends
        if value is None:
            raise ConfigError(here, "null isn't allowed; remove the key to use the default")
        if key not in base:
            raise ConfigError(here, "unknown setting", "UNKNOWN_KEY")
        elif isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ConfigError(here, "must be an object")
            out[key] = merge(base[key], value, here)
        else:
            out[key] = copy.deepcopy(value)
    return out


def read_verified(paths: Paths, path: Path) -> dict:
    """The organization file's contents, which must carry the analyst's signature whenever their public key is
    present (a machine that only runs searches may hold just the public key). It sets where searches go and
    what new warrants start with, so an unsigned edit could redirect or weaken them. Before any key exists
    (first setup) there is nothing to sign with, and nothing can be searched either: no warrant can be signed.

    The file is read once and the signature checked over exactly those bytes, so it can't be swapped between
    the check and its use."""
    from .signing import SignatureError, load_public_key, verify_bytes

    if not path.exists():
        return {}
    raw = path.read_bytes()
    if not paths.public_key.exists():
        return parse_bytes(path, raw)
    try:
        verify_bytes(path, raw, load_public_key(paths))
    except SignatureError as exc:
        raise ConfigError(str(path), f"isn't signed with your key, or was changed after it was signed ({exc}). "
                                     "Review it, then sign it with `lucid-warrant config sign` (it asks for your "
                                     "passphrase), or remove it to use the built-in defaults.", "UNSIGNED_CONFIG") from exc
    return parse_bytes(path, raw)


def verify_signature(paths: Paths, path: Path) -> None:
    """Raise UNSIGNED_CONFIG unless the organization file is signed (see read_verified)."""
    read_verified(paths, path)


def write_signed_config(path: Path, data: dict, private_key) -> None:
    """Write the organization file with its signature (organization.json.sig)."""
    from .signing import write_signed

    write_signed(path, data, private_key)


def unsigned_org_file(paths: Paths, org_override: str | None = None) -> Path | None:
    """An organization file with no signature yet (e.g. put in place before the key existed), or None."""
    from .signing import sig_path

    path, _how = org_path(paths, org_override)
    return path if path.exists() and not sig_path(path).exists() else None


def review_lines(data: dict) -> list[str]:
    """What an organization file sets that matters when signing it, in plain words."""
    c = merge(DEFAULTS, data)
    backend = c["search_backend"]
    if backend["type"] == "http":
        http = backend["http"]
        where = f"{http.get('method', 'POST')} {http.get('url', '(no URL)')}"
    elif backend["type"] == "python":
        where = f"the Python adapter {backend['python'].get('adapter', '(none)')}"
    else:
        where = "practice records (no real search service)"
    d = c["defaults"]
    excluded = [row["code"] if isinstance(row, dict) else str(row) for row in c["country_scope"].get("exclude", [])]
    lines = [
        "Review and sign organization configuration",
        f"Organization: {c['organization']['name']} (revision {c['organization']['revision']})",
        f"Searches go to: {where}",
        "Countries excluded by default: " + (", ".join(excluded) if excluded else "none"),
        f"Default limits: {d['max_rows_per_run']:,} records per search, {d['max_queries_per_hour']} searches an hour, "
        f"the last {d['lookback_days']} days, warrants last {d['expiry_minutes']} minutes",
    ]
    def shown(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    lines += [
        "Authorization-affecting settings",
        f"Destination: {where}",
        f"Place clause: {c['geography']['place_clause']}",
        "Field mappings (query): " + shown(c["query_fields"]),
        "Field mappings (response): " + shown(backend["http"]["fields"]),
        "Certification wording (purpose): " + c["certification"]["purpose"],
        "Certification wording (limits): " + c["certification"]["limits"],
        "Country mode: " + c["country_scope"]["mode"] + "; included: " + shown(c["country_scope"]["include"]),
        "Adapter options (Python): " + shown(backend["python"]["options"]),
        "Adapter options (HTTP): " + shown({k: v for k, v in backend["http"].items()
                                            if k not in ("fields", "url", "method")}),
        "Changes from built-in defaults",
    ]
    changes = []

    def compare(before, after, path=""):
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(before.keys() | after.keys()):
                compare(before.get(key), after.get(key), f"{path}.{key}" if path else key)
        elif before != after:
            changes.append(f"{path}: {shown(before)} → {shown(after)}")

    compare(DEFAULTS, c)
    return lines + (changes or ["None."])


REVISIONS_FILE = "config_revision.json"


class RevisionStateError(ValueError):
    """The record of configuration revisions used for searches can't be read."""


def used_revisions(paths: Paths) -> dict:
    """{"newest": n, "digests": {"n": authority hash}}: the configuration revisions searches have run under here.
    Missing means none yet; unreadable raises RevisionStateError (searches fail closed)."""
    mark = paths.state / REVISIONS_FILE
    if not mark.exists():
        return {"newest": 0, "digests": {}}
    try:
        data = json.loads(mark.read_text("utf-8"))
        newest = data["newest"]
        digests = data.get("digests", {})
        if isinstance(newest, bool) or not isinstance(newest, int) or not isinstance(digests, dict):
            raise ValueError("wrong shape")
        return {"newest": newest, "digests": {str(k): str(v) for k, v in digests.items()}}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RevisionStateError(f"{mark} can't be read ({exc}); searches are refused until it's repaired or "
                                 "removed") from exc


def record_revision(paths: Paths, revision: int, digest: str) -> None:
    from .signing import atomic_write

    state = used_revisions(paths)
    state["digests"][str(revision)] = digest
    state["newest"] = max(state["newest"], revision)
    (paths.state).mkdir(parents=True, exist_ok=True)
    atomic_write(paths.state / REVISIONS_FILE, (json.dumps(state, sort_keys=True) + "\n").encode("utf-8"))


def next_revision(paths: Paths, current: int) -> int:
    """A revision number no configuration has used here: above both the file's and the newest searches used, so
    two different signed configurations never share a number."""
    try:
        newest = used_revisions(paths)["newest"]
    except RevisionStateError:
        newest = 0
    return max(current, newest) + 1


def sign_reviewed(paths: Paths, path: Path, data: dict, private_key) -> None:
    """Sign an organization file the analyst has just reviewed (review_lines), as a new revision."""
    data = json.loads(json.dumps(data))
    data.setdefault("organization", {})
    data["organization"]["revision"] = next_revision(paths, merge(DEFAULTS, data)["organization"]["revision"])
    write_signed_config(path, data, private_key)


def load(paths: Paths, org_override: str | None = None, *, user: bool = True) -> dict:
    """Effective configuration, validated. Raises ConfigError naming the file and key."""
    path, _how = org_path(paths, org_override)
    if org_override and not path.exists():
        raise ConfigError(str(path), "selected organization file doesn't exist", "FILE_ERROR")
    org = read_verified(paths, path)
    effective = merge(DEFAULTS, org)
    if user:
        prefs = read_file(user_path())
        bad = set(prefs) - USER_SECTIONS - {k for k in prefs if k.startswith("_")}
        if bad:
            raise ConfigError(str(user_path()), f"personal preferences may only contain 'ui', not {sorted(bad)}", "SCOPE_REFUSED")
        effective = merge(effective, prefs)
    validate(effective, paths)
    return effective


# --- validation ---------------------------------------------------------------------------

def _text(value, where, low=1, high=80, multiline=False):
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise ConfigError(where, f"must be text, {low}–{high} characters")
    if not multiline and re.search(r"[\x00-\x1f\x7f]", value):
        raise ConfigError(where, "must not contain line breaks or control characters")


def _int(value, where, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(where, f"must be a whole number from {low} to {high}")


def _check_types(value, default, where: str) -> None:
    """Every value must have the type of its built-in default, at every level (bool is not a number)."""
    if where == "ui.schemes":
        if not isinstance(value, dict):
            raise ConfigError(where, "must be an object")
        for sid, scheme in value.items():
            if not isinstance(scheme, dict):
                raise ConfigError(f"{where}.{sid}", 'must be {"label", "light", "dark"}')
            for mode in ("light", "dark"):
                if mode in scheme and not isinstance(scheme[mode], dict):
                    raise ConfigError(f"{where}.{sid}.{mode}", "must be an object of color tokens")
        return
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise ConfigError(where, "must be an object")
        for key, sub_default in default.items():
            if key in value:
                _check_types(value[key], sub_default, f"{where}.{key}" if where else key)
    elif isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(where, "must be true or false")
    elif where in DECIMAL_SETTINGS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(where, "must be a number")
    elif isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(where, "must be a whole number")
    elif isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(where, "must be text")
    elif isinstance(default, list):
        if not isinstance(value, list):
            raise ConfigError(where, "must be a list")


TEMPLATE_PLACEHOLDERS = {"query", "limit", "warrant_id", "purpose", "parameter_1", "parameter_2", "parameter_2_csv",
                         "date_filter", "country_filter", "place_filter"}
_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_ADAPTER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")
_DOTTED = re.compile(r"^([A-Za-z0-9_@$-]+(\.[A-Za-z0-9_@$-]+)*)?$")


def _template_names(value) -> set[str]:
    if isinstance(value, str):
        return set(_PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        return set().union(*(_template_names(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_template_names(v) for v in value)) if value else set()
    return set()


def _validate_backend(b: dict) -> None:
    where = "search_backend"
    if b["type"] not in ("mock", "http", "python"):
        raise ConfigError(f"{where}.type", "must be mock, http or python")
    h = b["http"]
    if b["type"] == "http":
        if not re.match(r"^https?://[^\s/]+", h["url"]):
            raise ConfigError(f"{where}.http.url", "must start with https:// (or http://)")
        if "#" in h["url"]:
            # A fragment never reaches the server: anything after # (a {query} placed there, or parameters
            # appended after it) would silently not be sent.
            raise ConfigError(f"{where}.http.url", "must not contain a # fragment (it's never sent to the service)")
        if h["method"] not in ("GET", "POST"):
            raise ConfigError(f"{where}.http.method", "must be GET or POST")
        if "fields" not in h or not h["fields"].get("record_id"):
            raise ConfigError(f"{where}.http.fields.record_id", "say which response field holds each record's ID")
        if h["method"] == "GET" and h["body"]:
            raise ConfigError(f"{where}.http.body", "a GET request has no body, so this would never be sent; put the "
                                                     "query in params (or use POST)")
        # The query Lucid Warrant checked against the warrant has to reach the service; otherwise the service
        # would run something else (or nothing) while the record shows the checked query.
        carriers = _template_names(h["url"]) | _template_names(h["params"])
        if h["method"] == "POST":
            carriers |= _template_names(h["body"])
        if "query" not in carriers:
            raise ConfigError(f"{where}.http", "the request must send the checked query: put \"{query}\" in params "
                                               "or url" + (" or body" if h["method"] == "POST" else ""))
    for name in ("headers", "params"):
        for k, v in h[name].items():
            if not isinstance(v, str):
                raise ConfigError(f"{where}.http.{name}.{k}", "must be text")
    for header, env in h["secret_headers"].items():
        if not isinstance(env, str) or not _ENV_NAME.match(env):
            raise ConfigError(f"{where}.http.secret_headers.{header}",
                              "must name an environment variable (like SEARCH_API_TOKEN), never the secret itself")
    unknown = _template_names(h["body"]) | _template_names(h["params"]) | _template_names(h["url"]) | _template_names(h["headers"])
    unknown -= TEMPLATE_PLACEHOLDERS
    if unknown:
        raise ConfigError(f"{where}.http", f"unknown placeholder(s) {sorted(unknown)}; use {sorted(TEMPLATE_PLACEHOLDERS)}")
    _int(h["timeout_seconds"], f"{where}.http.timeout_seconds", 1, 300)
    for name in ("results_path",):
        if not _DOTTED.match(h[name]):
            raise ConfigError(f"{where}.http.{name}", "a dotted path such as hits.hits (or empty)")
    for field, path in h["fields"].items():
        if field not in ("record_id", "title", "timestamp", "country", "snippet", "lat", "lon"):
            raise ConfigError(f"{where}.http.fields.{field}",
                              "fields are record_id, title, timestamp, country, snippet, lat, lon")
        if not isinstance(path, str) or not _DOTTED.match(path):
            raise ConfigError(f"{where}.http.fields.{field}", "a dotted path such as _source.title")
    sqid = h["service_query_id"]
    if set(sqid) - {"header", "path"} or not all(isinstance(v, str) for v in sqid.values()):
        raise ConfigError(f"{where}.http.service_query_id", 'use {"header": "X-Request-Id"} or {"path": "request_id"}')
    p = b["python"]
    if b["type"] == "python" and not _ADAPTER.match(p["adapter"]):
        raise ConfigError(f"{where}.python.adapter", "module path and class, like mycompany.search:Adapter")


def validate(c: dict, paths: Paths) -> None:
    from .dates import DateLimitError, normalize_ranges
    from .warrant import MAX_QUERIES_PER_HOUR
    from .scope import ScopeError, normalize_country_scope

    _check_types(c, DEFAULTS, "")
    _int(c["schema_version"], "schema_version", SCHEMA_VERSION, SCHEMA_VERSION)
    if not isinstance(c["organization"]["id"], str) or not ID_RE.match(c["organization"]["id"]):
        raise ConfigError("organization.id", "lowercase letters, digits and dashes, starting with a letter")
    _text(c["organization"]["name"], "organization.name", 1, 100)
    _int(c["organization"]["revision"], "organization.revision", 1, 10**9)
    for key in ("parameter_1", "parameter_2"):
        _text(c["parameters"][key]["label"], f"parameters.{key}.label")
        _text(c["parameters"][key]["description"], f"parameters.{key}.description", 1, 1000, multiline=True)
    if c["parameters"]["parameter_1"]["label"].casefold() == c["parameters"]["parameter_2"]["label"].casefold():
        raise ConfigError("parameters", "the two parameter labels must differ")
    for key, value in c["terms"].items():
        _text(value["singular"], f"terms.{key}.singular")
        _text(value["plural"], f"terms.{key}.plural")
    labels = [v.casefold() for v in c["source_labels"].values()]
    for key, value in c["source_labels"].items():
        _text(value, f"source_labels.{key}")
    if len(set(labels)) != len(labels):
        raise ConfigError("source_labels", "labels must be distinct")
    fields = c["query_fields"]
    for key, value in fields.items():
        if not isinstance(value, str) or not FIELD_RE.match(value):
            raise ConfigError(f"query_fields.{key}", "must be a field name (letters, digits, _ and .)")
    if len({fields["content"], fields["date"], fields["country"]}) != 3:
        raise ConfigError("query_fields", "content, date and country fields must all differ")
    d = c["defaults"]
    _int(d["expiry_minutes"], "defaults.expiry_minutes", 1, 43200)
    _int(d["max_rows_per_run"], "defaults.max_rows_per_run", 1, 500000)
    _int(d["max_queries_per_hour"], "defaults.max_queries_per_hour", 1, MAX_QUERIES_PER_HOUR)
    _int(d["lookback_days"], "defaults.lookback_days", 0, 36500)
    try:
        normalize_ranges(d["blocked_ranges"])
    except (DateLimitError, KeyError, TypeError) as exc:
        raise ConfigError("defaults.blocked_ranges", str(exc)) from exc
    cs = c["country_scope"]
    if cs["mode"] not in ("all", "include"):
        raise ConfigError("country_scope.mode", 'must be "all" or "include"')
    if cs["mode"] == "include" and not cs["include"]:
        raise ConfigError("country_scope.include", 'can\'t be empty when mode is "include"')
    if cs["mode"] == "all" and cs["include"]:
        raise ConfigError("country_scope.include", 'must be empty when mode is "all"')
    try:
        normalize_country_scope(paths, cs["include"], cs["exclude"])
    except (ScopeError, AttributeError, TypeError) as exc:
        raise ConfigError("country_scope", str(exc)) from exc
    _text(cs["reason_label"], "country_scope.reason_label")
    _text(cs["default_reason"], "country_scope.default_reason", 1, 60)
    for key in ("purpose", "limits"):
        _text(c["certification"][key], f"certification.{key}", 1, 4000, multiline=True)
    from .places import CLAUSE_TEMPLATE_RE, MIN_RADIUS_KM

    g = c["geography"]
    if not isinstance(g["place_clause"], str) or not CLAUSE_TEMPLATE_RE.match(g["place_clause"]) or not all(
            p in g["place_clause"] for p in ("{lat}", "{lon}", "{km}")):
        raise ConfigError("geography.place_clause", "a clause using {lat}, {lon} and {km} (letters, digits, "
                                                    "( ) , . : _ and spaces), like INTERSECTS({lat},{lon},{km})")
    if isinstance(g["max_radius_km"], bool) or not isinstance(g["max_radius_km"], (int, float)) or not (
            MIN_RADIUS_KM <= g["max_radius_km"] <= 1000):
        raise ConfigError("geography.max_radius_km", f"must be {MIN_RADIUS_KM} to 1000")
    _validate_backend(c["search_backend"])
    ui = c["ui"]
    if ui["theme_mode"] not in ("system", "light", "dark"):
        raise ConfigError("ui.theme_mode", "must be system, light or dark")
    if ui["time_zone"] not in ("utc", "local"):
        raise ConfigError("ui.time_zone", 'must be "utc" or "local"')
    if ui["density"] not in ("compact", "comfortable", "spacious"):
        raise ConfigError("ui.density", "must be compact, comfortable or spacious")
    if ui["base_font_pt"] != 0:
        _int(ui["base_font_pt"], "ui.base_font_pt", 8, 24)
    _int(ui["window"]["width"], "ui.window.width", 640, 8192)
    _int(ui["window"]["height"], "ui.window.height", 480, 8192)
    if len(ui["schemes"]) > 32:
        raise ConfigError("ui.schemes", "at most 32 schemes")
    for sid, scheme in ui["schemes"].items():
        if not ID_RE.match(sid):
            raise ConfigError(f"ui.schemes.{sid}", "scheme IDs use lowercase letters, digits and dashes")
        if not isinstance(scheme, dict) or set(scheme) - {"label", "color", "light", "dark"}:
            raise ConfigError(f"ui.schemes.{sid}", 'must be {"label", "color", "light", "dark"}')
        _text(scheme.get("label", ""), f"ui.schemes.{sid}.label")
        if "color" in scheme and (not isinstance(scheme["color"], str) or not HEX_RE.match(scheme["color"])):
            raise ConfigError(f"ui.schemes.{sid}.color", "colors are #rrggbb")
        for mode in ("light", "dark"):
            for token, color in (scheme.get(mode) or {}).items():
                if token not in PALETTE_TOKENS:
                    raise ConfigError(f"ui.schemes.{sid}.{mode}.{token}", f"unknown color token; use {', '.join(PALETTE_TOKENS)}")
                if not isinstance(color, str) or not HEX_RE.match(color):
                    raise ConfigError(f"ui.schemes.{sid}.{mode}.{token}", "colors are #rrggbb")
    if ui["theme_color"] and not HEX_RE.match(ui["theme_color"]):
        raise ConfigError("ui.theme_color", "colors are #rrggbb")
    if ui["scheme"] == "custom" and not ui["theme_color"]:
        raise ConfigError("ui.scheme", "the custom scheme needs ui.theme_color")
    if ui["scheme"] not in schemes(c):
        raise ConfigError("ui.scheme", f"unknown scheme; available: {', '.join(schemes(c))}")


def schemes(c: dict) -> dict:
    custom = {"custom": {"label": "Custom", "color": c["ui"]["theme_color"]}} if c["ui"].get("theme_color") else {}
    return {**BUILTIN_SCHEMES, **c["ui"]["schemes"], **custom}


def field_mismatches(values: dict | None, library: dict) -> list[str]:
    """Where the configuration's query fields disagree with the signed template library's (the executor uses
    the library's). An empty list means they agree."""
    if not values or "query_fields" not in values:
        return []
    from .library import country_field_of, date_field_of

    pairs = (("content", library.get("default_field")), ("date", date_field_of(library)), ("country", country_field_of(library)))
    return [f"query_fields.{key} is {values['query_fields'][key]!r} but the library uses {actual!r}"
            for key, actual in pairs if values["query_fields"].get(key) != actual]


# --- authority snapshot -------------------------------------------------------------------

def authority_values(c: dict) -> dict:
    """Everything that shapes what a signer sees and approves (all but "ui")."""
    return {k: v for k, v in c.items() if k != "ui"}


def authority_hash(c: dict) -> str:
    canonical = json.dumps(authority_values(c), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def config_context(c: dict) -> dict:
    return {"schema": "org-authority-v1", "sha256": authority_hash(c), "values": authority_values(c)}


# --- contrast check -------------------------------------------------------------------------

def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


CONTRAST_PAIRS = [("text", "bg"), ("text", "surface"), ("muted", "surface"), ("accent_text", "accent"),
                  ("warn", "warn_bg"), ("danger", "danger_bg"), ("info", "info_bg"), ("text", "agent_bg")]


def contrast_problems(palette: dict, minimum: float = 4.5) -> list[str]:
    return [f"{fg} on {bg}: {contrast(palette[fg], palette[bg]):.2f}:1 (needs {minimum}:1)"
            for fg, bg in CONTRAST_PAIRS if contrast(palette[fg], palette[bg]) < minimum]


# --- writing (shared by the GUI's Appearance dialog and the CLI) ----------------------------

def write_json(path: Path, data: dict) -> None:
    """Atomic write: unique temp file in the same folder, fsync, replace. Never leaves a half-written file."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        from .util import replace_file

        replace_file(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class FileLock:
    """A lock file next to the target so writers can't interleave (Windows and Unix). Read inside it."""

    def __init__(self, target: Path, timeout: float = 5.0):
        self.path, self.timeout = target.with_name(target.name + ".lock"), timeout

    def __enter__(self):
        import time

        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 30:  # a crashed writer's stale lock
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise ConfigError(str(self.path), "another change is in progress", "LOCKED")
                time.sleep(0.1)

    def __exit__(self, *exc):
        os.close(self.fd)
        self.path.unlink(missing_ok=True)


def save_user_ui(paths: Paths, ui_changes: dict, org_override: str | None = None) -> dict:
    """Merge appearance changes into the user's preferences, reading and writing under the lock.
    Validated against the organization file actually in use (schemes can be defined there)."""
    with FileLock(user_path()):
        current = read_file(user_path())
        candidate = {"schema_version": SCHEMA_VERSION, **current, "ui": {**current.get("ui", {}), **ui_changes}}
        validate(merge(merge(DEFAULTS, read_file(org_path(paths, org_override)[0])), candidate), paths)
        write_json(user_path(), candidate)
    return candidate
