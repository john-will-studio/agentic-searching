"""`lucid-warrant config ...`: read and change the organization configuration from the command line.

Built for agents: every command prints one JSON object, and exit codes are stable.
See docs/AGENT-CLI.md.

Exit codes: 0 ok · 1 internal error · 2 usage · 3 invalid configuration · 4 refused (scope or missing
acknowledgement) · 5 file/permission problem · 6 concurrent change (hash mismatch or lock held).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from . import org_config as oc
from .config import Paths
from .util import new_id, stdin_is_terminal, to_iso, utc_now

EXIT = {"ok": 0, "internal": 1, "usage": 2, "invalid": 3, "refused": 4, "file": 5, "conflict": 6}


class CommandError(Exception):
    def __init__(self, code: str, message: str, path: str = "", exit_code: int = EXIT["usage"]):
        super().__init__(message)
        self.code, self.message, self.path, self.exit_code = code, message, path, exit_code


def add_parser(sub) -> None:
    config = sub.add_parser("config", help="read or change the organization configuration (JSON output)")
    config.add_argument("--org-config", metavar="PATH", help="organization file to use instead of the default")
    cs = config.add_subparsers(dest="config_cmd", required=True)
    show = cs.add_parser("show", help="the effective configuration, its hash and where each value comes from")
    show.add_argument("--layer", choices=["effective", "builtin", "org", "user"], default="effective")
    show.add_argument("--sources", action="store_true", help="include where each value comes from")
    get = cs.add_parser("get", help="one value, e.g. parameters.parameter_1.label")
    get.add_argument("key")
    for name, helptext in (("set", "change one value"), ("reset", "remove an override (back to the lower layer)")):
        p = cs.add_parser(name, help=helptext)
        if name == "set":
            p.add_argument("key")
            value = p.add_mutually_exclusive_group(required=True)
            value.add_argument("--value", help="a text value")
            value.add_argument("--value-json", help="any JSON value, e.g. 30 or [\"US\",\"CA\"]")
            value.add_argument("--value-file", help="read a JSON value from a file (- for stdin)")
        else:
            target = p.add_mutually_exclusive_group(required=True)
            target.add_argument("key", nargs="?")
            target.add_argument("--all", action="store_true")
        p.add_argument("--scope", choices=["user", "org"], default="user",
                       help="user: personal appearance only (default); org: the organization file")
        p.add_argument("--dry-run", action="store_true", help="validate and show the change without writing")
        p.add_argument("--reason", help="required for organization changes; kept in the change history")
        p.add_argument("--passphrase-stdin", action="store_true",
                       help="read the analyst's passphrase from stdin instead of prompting (organization changes are "
                            "signed with it)")
        p.add_argument("--if-hash", help="required for organization changes: the file hash from `config path` "
                                         "or a dry run (or 'missing' to create the file)")
    sign = cs.add_parser("sign", help="Review and sign organization configuration (asks for the passphrase)")
    sign.add_argument("--reason", required=True, help="why it changed; kept in the change history")
    sign.add_argument("--if-hash", required=True, help="the file hash you reviewed, from `config path`")
    sign.add_argument("--passphrase-stdin", action="store_true", help="read the passphrase from stdin")
    validate = cs.add_parser("validate", help="check a configuration file without changing anything")
    validate.add_argument("--file", help="file to check (default: the selected organization file and preferences)")
    validate.add_argument("--scope", choices=["org", "user"], default="org")
    cs.add_parser("path", help="where the organization and preference files are, and their hashes")
    cs.add_parser("schema", help="every setting: type, default, allowed values, and whether it affects authority")
    theme = cs.add_parser("theme", help="color schemes")
    ts = theme.add_subparsers(dest="theme_cmd", required=True)
    ts.add_parser("list", help="available color schemes")
    tset = ts.add_parser("set", help="choose a color scheme (personal preference)")
    tset.add_argument("scheme", help='a scheme ID, or "custom" with --color')
    tset.add_argument("--color", help="theme color #rrggbb for the custom scheme")
    tset.add_argument("--mode", choices=["system", "light", "dark"])
    tset.add_argument("--dry-run", action="store_true")


def run(args, paths: Paths) -> int:
    try:
        data, warnings = _dispatch(args, paths)
        _emit({"ok": True, "command": _name(args), "data": data, "warnings": warnings})
        return EXIT["ok"]
    except CommandError as exc:
        _emit({"ok": False, "command": _name(args), "error": {"code": exc.code, "path": exc.path, "message": exc.message}})
        return exc.exit_code
    except oc.ConfigError as exc:
        exit_code = {"SCOPE_REFUSED": EXIT["refused"], "FILE_ERROR": EXIT["file"], "LOCKED": EXIT["conflict"]}.get(exc.code, EXIT["invalid"])
        _emit({"ok": False, "command": _name(args), "error": {"code": exc.code, "path": exc.path, "message": exc.message}})
        return exit_code
    except OSError as exc:
        _emit({"ok": False, "command": _name(args), "error": {"code": "FILE_ERROR", "path": str(getattr(exc, "filename", "")),
                                                               "message": str(exc)}})
        return EXIT["file"]
    except Exception as exc:  # noqa: BLE001 - the contract is one JSON object, even for bugs
        _emit({"ok": False, "command": _name(args), "error": {"code": "INTERNAL", "path": "", "message": repr(exc)}})
        return EXIT["internal"]


def _name(args) -> str:
    return "config." + ".".join(x for x in (args.config_cmd, getattr(args, "theme_cmd", None)) if x)


def _emit(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def _read_reviewed(target: Path, if_hash: str) -> bytes | None:
    """The file's bytes, read once and checked against the hash the analyst reviewed. Everything that follows uses
    these bytes, so a file replaced between the check and the signature can't be signed in its place."""
    raw = target.read_bytes() if target.exists() else None
    current = hashlib.sha256(raw).hexdigest() if raw is not None else "missing"
    if if_hash != current:
        raise CommandError("CONFLICT", f"the file isn't the version you reviewed (hash {current}); review again",
                           str(target), EXIT["conflict"])
    return raw


def _get(tree: dict, key: str):
    node = tree
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise CommandError("UNKNOWN_KEY", f"no setting {key!r}; see `lucid-warrant config schema`", key, EXIT["invalid"])
        node = node[part]
    return node


def _set(tree: dict, key: str, value) -> dict:
    out = copy.deepcopy(tree)
    node = out
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise CommandError("INVALID_KEY", f"{key!r} goes inside a value that isn't an object", key)
    node[parts[-1]] = value
    return out


def _unset(tree: dict, key: str) -> dict:
    out = copy.deepcopy(tree)
    node, parts = out, key.split(".")
    for part in parts[:-1]:
        node = node.get(part)
        if not isinstance(node, dict):
            return out
    node.pop(parts[-1], None)
    return out


def _sources(paths: Paths, org_override) -> dict:
    """Which layer each leaf value comes from."""
    org = oc.read_file(oc.org_path(paths, org_override)[0])
    user = oc.read_file(oc.user_path())
    out = {}

    def walk(node, prefix):
        for key, value in node.items():
            here = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict) and value and here != "ui.schemes":
                walk(value, here)
                continue
            for layer, data in (("user", user), ("org", org)):
                try:
                    _get(data, here)
                    out[here] = layer
                    break
                except CommandError:
                    continue
            else:
                out[here] = "builtin"
    walk(oc.load(paths, org_override), "")
    return out


def _dispatch(args, paths: Paths):
    org_override = args.org_config
    warnings: list[str] = []
    cmd = args.config_cmd
    if cmd == "path":
        org, how = oc.org_path(paths, org_override)
        raw = org.read_bytes() if org.exists() else None
        candidate = oc.parse_bytes(org, raw) if raw is not None else {}
        return {"org": {"path": str(org), "selected_by": how, "exists": raw is not None,
                        "hash": hashlib.sha256(raw).hexdigest() if raw is not None else "missing",
                        "review": oc.review_lines(candidate), "full_config": oc.merge(oc.DEFAULTS, candidate)},
                "user": {"path": str(oc.user_path()), "exists": oc.user_path().exists(), "hash": _file_hash(oc.user_path())},
                "history": str(_history_path(paths))}, warnings
    if cmd == "schema":
        return {"settings": _schema(), "authority_rule": "everything outside 'ui' affects authority; 'ui' is personal"}, warnings
    if cmd == "theme":
        effective = oc.load(paths, org_override)
        if args.theme_cmd == "list":
            return {"current": effective["ui"]["scheme"], "mode": effective["ui"]["theme_mode"],
                    "schemes": {sid: {"label": s.get("label", sid), "color": s.get("color", "")}
                                for sid, s in oc.schemes(effective).items() if not s.get("hidden")}}, warnings
        changes = {"scheme": args.scheme, **({"theme_mode": args.mode} if args.mode else {}),
                   **({"theme_color": args.color.lower()} if args.color else {})}
        if args.color and not oc.HEX_RE.match(args.color):
            raise CommandError("INVALID_VALUE", "colors are #rrggbb", "ui.theme_color", EXIT["invalid"])
        if args.scheme == "custom" and not (args.color or effective["ui"]["theme_color"]):
            raise CommandError("INVALID_VALUE", "the custom scheme needs --color #rrggbb", "ui.theme_color", EXIT["invalid"])
        if args.scheme not in oc.schemes(effective) and args.scheme != "custom":
            raise CommandError("INVALID_VALUE", f"unknown scheme; available: {', '.join(oc.schemes(effective))}", "ui.scheme", EXIT["invalid"])
        if not args.dry_run:
            oc.save_user_ui(paths, changes, org_override)
        return {"changed": changes, "written": not args.dry_run, "applies": "the next time the app starts; a running app keeps its current look until then (Appearance… changes apply immediately)"}, warnings
    if cmd == "validate":
        if args.file:
            if not Path(args.file).exists():
                raise CommandError("FILE_ERROR", "the file to validate doesn't exist", args.file, EXIT["file"])
            data = oc.read_file(Path(args.file))
            if args.scope == "user":
                bad = set(data) - oc.USER_SECTIONS - {k for k in data if k.startswith("_")}
                if bad:
                    raise oc.ConfigError(args.file, f"personal preferences may only contain 'ui', not {sorted(bad)}", "SCOPE_REFUSED")
            effective = oc.merge(oc.DEFAULTS, data)
            oc.validate(effective, paths)
        else:
            effective = oc.load(paths, org_override)
        _check_library(effective, paths)
        problems = oc.contrast_problems({**_palette(effective, "light")}) + oc.contrast_problems({**_palette(effective, "dark")})
        warnings += [f"low contrast: {p}" for p in problems]
        return {"valid": True, "authority_hash": oc.authority_hash(effective)}, warnings
    if cmd == "show":
        layer = args.layer
        if layer == "builtin":
            data = oc.DEFAULTS
        elif layer == "org":
            data = oc.read_file(oc.org_path(paths, org_override)[0])
        elif layer == "user":
            data = oc.read_file(oc.user_path())
        else:
            data = oc.load(paths, org_override)
        out = {"layer": layer, "config": data}
        if layer == "effective":
            out["authority_hash"] = oc.authority_hash(data)
        if args.sources:
            out["sources"] = _sources(paths, org_override)
        return out, warnings
    if cmd == "get":
        effective = oc.load(paths, org_override)
        sources = _sources(paths, org_override)
        return {"key": args.key, "value": _get(effective, args.key),
                "source": sources.get(args.key, "mixed")}, warnings
    if cmd == "sign":
        if not (args.reason or "").strip():
            raise CommandError("ACK_REQUIRED", "give a --reason", "", EXIT["refused"])
        return _sign_existing(args, paths, org_override), warnings
    if cmd in ("set", "reset"):
        return _mutate(args, paths, org_override, warnings)
    raise CommandError("USAGE", f"unknown command {cmd!r}")


def _palette(effective, mode):
    return oc.derive_palette(mode, oc.schemes(effective).get(effective["ui"]["scheme"], {}))


def _parse_value(args):
    if args.value is not None:
        return args.value
    text = sys.stdin.read() if args.value_file == "-" else Path(args.value_file).read_text("utf-8") if args.value_file else args.value_json
    try:
        return json.loads(text, object_pairs_hook=oc._reject_duplicates)
    except oc.ConfigError as exc:
        raise CommandError("INVALID_JSON", f"duplicate key {exc.path!r} in the value", exc.path, EXIT["invalid"]) from exc
    except ValueError as exc:
        raise CommandError("INVALID_JSON", f"not valid JSON: {exc}", "", EXIT["invalid"]) from exc


MANAGED_KEYS = {"schema_version", "organization", "organization.revision"}


def _signing_key(paths: Paths, args):
    """The analyst's private key, for signing an organization change. Only someone who knows the passphrase can
    change the organization file: an agent running commands can't."""
    import getpass

    from .signing import BadPassphrase, keys_exist, load_private_key

    if not keys_exist(paths):
        raise CommandError("NO_KEY", "create the signing key first (open the GUI, or run lucid-warrant init): "
                                     "organization changes are signed with it", str(paths.keys), EXIT["refused"])
    if args.passphrase_stdin:
        passphrase = sys.stdin.readline().rstrip("\r\n")
    elif stdin_is_terminal():
        passphrase = getpass.getpass("Passphrase to sign the organization change: ", stream=sys.stderr)
    else:
        raise CommandError("PASSPHRASE_REQUIRED", "organization changes are signed with the analyst's passphrase: run "
                                                  "this in a terminal, or pass it on stdin with --passphrase-stdin",
                           "", EXIT["refused"])
    try:
        return load_private_key(paths, passphrase)
    except BadPassphrase as exc:
        raise CommandError("BAD_PASSPHRASE", "wrong passphrase", "", EXIT["refused"]) from exc
    finally:
        del passphrase


def _sign_existing(args, paths, org_override):
    """Sign the organization file as it is, after the analyst has reviewed a hand edit."""
    target = oc.org_path(paths, org_override)[0]
    if not target.exists():
        raise CommandError("FILE_ERROR", "there's no organization file to sign", str(target), EXIT["file"])
    with _lock(target):
        raw = _read_reviewed(target, args.if_hash)
        current = args.if_hash
        data = oc.parse_bytes(target, raw)
        effective = oc.merge(oc.DEFAULTS, data)
        oc.validate(effective, paths)
        _check_library(effective, paths)
        # Keep stdout machine-readable; the human review precedes the passphrase on stderr.
        print("\n".join(oc.review_lines(data)), file=sys.stderr)
        print("Full configuration: lucid-warrant config show --layer org", file=sys.stderr)
        private_key = _signing_key(paths, args)
        change_id = new_id("chg")
        _record_history(paths, {"change_id": change_id, "status": "pending", "at": to_iso(utc_now()), "file": str(target),
                                "key": "(signed as edited)", "reason": args.reason.strip(), "old_file_hash": current,
                                "new_authority_hash": oc.authority_hash(effective)})
        oc.sign_reviewed(paths, target, data, private_key)  # as a new revision, so an older copy can't stand in for it
        _record_history(paths, {"change_id": change_id, "status": "committed", "at": to_iso(utc_now()),
                                "new_file_hash": _file_hash(target)})
    signed = oc.read_file(target)
    return {"signed": str(target), "review": oc.review_lines(signed),
            "authority_hash": oc.authority_hash(oc.merge(oc.DEFAULTS, signed)), "new_file_hash": _file_hash(target)}


def _mutate(args, paths, org_override, warnings):
    scope = args.scope
    key = getattr(args, "key", None)
    if key in MANAGED_KEYS:
        raise CommandError("SCOPE_REFUSED", f"{key} is managed by the CLI", key, EXIT["refused"])
    if key and not key.startswith("ui.schemes."):
        _get(oc.DEFAULTS, key)  # unknown keys: exit 3
    if scope == "user" and key and not key.startswith("ui.") and key != "ui":
        raise CommandError("SCOPE_REFUSED", f"{key!r} affects authority, so it can only be changed in the organization file "
                                            "(--scope org, with --reason and --if-hash; it's signed with your passphrase)",
                           key, EXIT["refused"])
    if scope == "user" and getattr(args, "all", False) is False and key is None:
        raise CommandError("USAGE", "give a key or --all")
    target = oc.org_path(paths, org_override)[0] if scope == "org" else oc.user_path()
    value = _parse_value(args) if args.config_cmd == "set" else None
    if args.dry_run:
        return _compute(args, paths, org_override, scope, key, target, value)[0], warnings
    private_key = None
    if scope == "org":
        missing = [flag for flag, ok in (("--reason", bool((args.reason or "").strip())),
                                          ("--if-hash", bool(args.if_hash))) if not ok]
        if missing:
            raise CommandError("ACK_REQUIRED", "organization changes need " + ", ".join(missing)
                               + "; run with --dry-run first to see the change and the current file hash", key or "", EXIT["refused"])
        private_key = _signing_key(paths, args)
    with _lock(target):
        # Read inside the lock, so two writers can't both start from the same old file. For the organization file
        # the change is built from the exact bytes whose hash the analyst reviewed (read once).
        raw = _read_reviewed(target, args.if_hash) if scope == "org" else _UNREAD
        result, after_file = _compute(args, paths, org_override, scope, key, target, value, raw=raw)
        if not result["changed"]:
            return result, warnings
        if scope == "org":
            current = args.if_hash
            # History first, as a transaction: if the intent can't be recorded, nothing changes; the outcome
            # (committed or aborted) is recorded after the write, so the log never claims a change that didn't happen.
            change_id = new_id("chg")
            _record_history(paths, {"change_id": change_id, "status": "pending", "at": to_iso(utc_now()),
                                    "file": str(target), "key": key or "(all)", "reason": args.reason.strip(),
                                    "old_file_hash": current, "old_authority_hash": result["old_authority_hash"],
                                    "new_authority_hash": result["new_authority_hash"], "diff": result["diff"]})
            try:
                oc.write_signed_config(target, after_file, private_key)
            except BaseException as exc:
                _record_history(paths, {"change_id": change_id, "status": "aborted", "at": to_iso(utc_now()), "error": repr(exc)})
                raise
            _record_history(paths, {"change_id": change_id, "status": "committed", "at": to_iso(utc_now()),
                                    "new_file_hash": _file_hash(target)})
        else:
            oc.write_json(target, after_file)
    result["written"] = True
    result["new_file_hash"] = _file_hash(target)
    if result["authority_changed"]:
        warnings.append("New warrants will be signed under this configuration. Existing warrants keep the configuration "
                        "they were signed under; replace or revoke them if they should change.")
    return result, warnings


_UNREAD = object()


def _compute(args, paths, org_override, scope, key, target, value, raw=_UNREAD):
    before_file = oc.read_file(target) if raw is _UNREAD else (oc.parse_bytes(target, raw) if raw else {})
    before_effective = oc.load(paths, org_override)
    if args.config_cmd == "set":
        after_file = _set(before_file, key, value)
    elif args.all:
        after_file = {"schema_version": oc.SCHEMA_VERSION}
        if scope == "org" and "organization" in before_file:
            after_file["organization"] = {"revision": before_effective["organization"]["revision"]}
    else:
        after_file = _unset(before_file, key)
    after_file.setdefault("schema_version", oc.SCHEMA_VERSION)
    if scope == "org" and after_file != before_file:
        # Every organization change gets a new revision.
        after_file.setdefault("organization", {})["revision"] = oc.next_revision(
            paths, before_effective["organization"]["revision"])
    org_layer = after_file if scope == "org" else oc.read_file(oc.org_path(paths, org_override)[0])
    user_layer = after_file if scope == "user" else oc.read_file(oc.user_path())
    after_effective = oc.merge(oc.merge(oc.DEFAULTS, org_layer), user_layer)
    oc.validate(after_effective, paths)
    _check_library(after_effective, paths)
    result = {
        "scope": scope, "file": str(target), "changed": after_file != before_file,
        "authority_changed": oc.authority_hash(after_effective) != oc.authority_hash(before_effective),
        "old_file_hash": _file_hash(target),
        "old_authority_hash": oc.authority_hash(before_effective), "new_authority_hash": oc.authority_hash(after_effective),
        "diff": _diff(before_file, after_file), "written": False,
    }
    result["requires_review"] = result["authority_changed"]
    return result, after_file


def _check_library(effective: dict, paths: Paths) -> None:
    """The configuration's field names must match the signed template library (the executor uses the library's)."""
    try:
        from .library import load_library
        from .signing import load_public_key

        library = load_library(paths, load_public_key(paths))
    except Exception:  # noqa: BLE001 - no library or key yet: nothing to compare against
        return
    mismatch = oc.field_mismatches(oc.authority_values(effective), library)
    if mismatch:
        raise oc.ConfigError("query_fields", "; ".join(mismatch) + ". Change the configuration or re-sign the library.",
                             "FIELD_MISMATCH")


def _diff(before: dict, after: dict, prefix: str = "") -> list[dict]:
    changes = []
    for key in sorted(set(before) | set(after)):
        here = f"{prefix}.{key}" if prefix else key
        a, b = before.get(key, "<unset>"), after.get(key, "<unset>")
        if isinstance(a, dict) and isinstance(b, dict):
            changes += _diff(a, b, here)
        elif a != b:
            changes.append({"key": here, "old": a, "new": b})
    return changes


def _history_path(paths: Paths) -> Path:
    return paths.data / "config-history.jsonl"


def _record_history(paths: Paths, entry: dict) -> None:
    path = _history_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


class _lock(oc.FileLock):
    """The shared configuration lock; a held lock is reported as a concurrent-change refusal."""

    def __enter__(self):
        try:
            return super().__enter__()
        except oc.ConfigError as exc:
            raise CommandError("LOCKED", f"another change is in progress ({self.path})", str(self.path), EXIT["conflict"]) from exc


def _schema() -> list[dict]:
    rules = {
        "schema_version": "managed by the CLI",
        "organization.id": "lowercase letters, digits, dashes; starts with a letter",
        "organization.revision": "managed by the CLI; increases on every organization change",
        "parameters.*.label": "1–80 characters; the two labels must differ",
        "parameters.*.description": "1–1000 characters",
        "terms.*.*": "1–80 characters",
        "source_labels.*": "1–80 characters, all distinct",
        "query_fields.*": "field names; content, date and country must differ; must match the signed template library",
        "defaults.expiry_minutes": "1–43200",
        "defaults.max_rows_per_run": "1–500000",
        "defaults.max_queries_per_hour": "1–100; searches sent per warrant in any rolling hour",
        "defaults.lookback_days": "0–36500 (0 = no date restriction)",
        "defaults.blocked_ranges": 'list of {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, years 1900–2999, at most 50',
        "country_scope.mode": '"all" or "include"',
        "country_scope.include": "ISO 3166-1 two-letter codes; empty when mode is all, non-empty when include",
        "country_scope.exclude": 'list of {"code": "DE", "reason": "GDPR"}; no overlap with include',
        "certification.*": "1–4000 characters; shown verbatim and signed into each warrant",
        "ui.theme_mode": '"system", "light" or "dark"',
        "ui.scheme": "a scheme ID from `config theme list`",
        "ui.theme_color": '#rrggbb used by the "custom" scheme, or ""',
        "search_backend.type": "where searches go: mock (practice records), http (a REST API described here) or "
                               "python (an adapter class); see docs/SEARCH-ADAPTER.md",
        "search_backend.http.url": "https:// URL that runs a search; may use placeholders like {warrant_id}",
        "search_backend.http.method": "GET or POST",
        "search_backend.http.headers": "fixed headers (text; placeholders allowed)",
        "search_backend.http.secret_headers": "header name -> ENVIRONMENT_VARIABLE holding its value; never the secret",
        "search_backend.http.params": "URL query parameters; placeholders such as {query} and {limit}",
        "search_backend.http.body": "JSON request body for POST; placeholders such as {query} and {limit}",
        "search_backend.http.timeout_seconds": "1-300",
        "search_backend.http.verify_tls": "check the service's TLS certificate (keep true)",
        "search_backend.http.results_path": "dotted path to the list of results, e.g. hits.hits",
        "search_backend.http.fields": "record_id (required), title, timestamp, country, snippet: dotted paths in each result",
        "search_backend.http.service_query_id": '{"header": "X-Request-Id"} or {"path": "request_id"}',
        "search_backend.python.adapter": "module.path:ClassName with a search(payload, plan) method",
        "search_backend.python.options": "keyword arguments for the adapter class",
        "ui.schemes": 'map of id -> {"label", "color": "#rrggbb", "light": {token: "#rrggbb"}, "dark": {...}}; tokens: ' + ", ".join(oc.PALETTE_TOKENS),
        "ui.window.width": "640–8192",
        "ui.window.height": "480–8192",
        "ui.base_font_pt": "0 (system default) or 8–24",
        "ui.density": '"compact", "comfortable" or "spacious"',
    }
    out = []

    def walk(node, prefix):
        for key, value in node.items():
            here = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict) and value:
                walk(value, here)
                continue
            rule = rules.get(here) or next((r for pattern, r in rules.items() if _matches(pattern, here)), "")
            out.append({"key": here, "type": type(value).__name__, "default": value, "rule": rule,
                        "affects_authority": not here.startswith("ui")})
    walk(oc.DEFAULTS, "")
    return out


def _matches(pattern: str, key: str) -> bool:
    p, k = pattern.split("."), key.split(".")
    return len(p) == len(k) and all(a in ("*", b) for a, b in zip(p, k))
