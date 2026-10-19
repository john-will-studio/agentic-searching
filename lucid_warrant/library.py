"""The signed template library: syntax the agent may use without asking.

The executor loads only a library whose signature verifies, so an agent that
can write files still can't grant itself new syntax.
"""

from __future__ import annotations

import json
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .config import Paths
from .dates import DEFAULT_DATE_FIELD
from .scope import DEFAULT_COUNTRY_FIELD
from .signing import read_signed, write_signed
from .syntax import SyntaxRejected, explain, gloss, parse_skeleton, render, slots, uses_proximity
from .util import to_iso, utc_now

SCHEMA = 1
TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


class LibraryError(Exception):
    """The library or a template in it is invalid."""


def validate_library(doc: dict) -> dict:
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise LibraryError("library has an unsupported schema")
    allowed = doc.get("allowed_fields")
    default = doc.get("default_field")
    if not isinstance(allowed, list) or not allowed or not all(isinstance(f, str) for f in allowed):
        raise LibraryError("library needs a non-empty allowed_fields list")
    if default not in allowed:
        raise LibraryError("library default_field must be one of allowed_fields")
    date_field = doc.get("date_field", DEFAULT_DATE_FIELD)
    if not isinstance(date_field, str) or not FIELD_RE.match(date_field):
        raise LibraryError(f"invalid date_field {date_field!r}")
    if date_field in allowed:
        raise LibraryError("date_field must not be a template-searchable field; date limits are set only by warrants")
    templates = doc.get("templates")
    if not isinstance(templates, list) or not templates:
        raise LibraryError("library has no templates")
    seen = set()
    for t in templates:
        tid = t.get("template_id")
        if not isinstance(tid, str) or not TEMPLATE_ID_RE.match(tid):
            raise LibraryError(f"invalid template id: {tid!r}")
        if tid in seen:
            raise LibraryError(f"duplicate template id: {tid}")
        seen.add(tid)
        for key in ("name", "description", "skeleton"):
            if not isinstance(t.get(key), str) or not t[key].strip():
                raise LibraryError(f"template {tid} is missing {key}")
        if not isinstance(t.get("enabled", True), bool):
            raise LibraryError(f"template {tid}: enabled must be true or false")
        try:
            parse_skeleton(t["skeleton"], allowed)
        except SyntaxRejected as exc:
            raise LibraryError(f"template {tid}: {exc}") from exc
    return doc


def load_library(paths: Paths, public_key: Ed25519PublicKey) -> dict:
    return validate_library(read_signed(paths.library, public_key))


def country_field_of(library: dict) -> str:
    """Field holding a record's two-letter country code, used by warrants' country scope."""
    return library.get("country_field", DEFAULT_COUNTRY_FIELD)


def date_field_of(library: dict) -> str:
    """Timestamp field the executor's date limits apply to."""
    return library.get("date_field", DEFAULT_DATE_FIELD)


def get_template(library: dict, template_id: str, *, enabled_only: bool = True) -> dict:
    for t in library["templates"]:
        if t["template_id"] == template_id:
            if enabled_only and not t.get("enabled", True):
                raise LibraryError(f"template {template_id!r} is disabled in the library")
            return t
    raise LibraryError(f"no template {template_id!r} in the signed library")


def describe_template(library: dict, template: dict) -> dict:
    node = parse_skeleton(template["skeleton"], library["allowed_fields"])
    return {
        "template_id": template["template_id"],
        "enabled": template.get("enabled", True),
        "name": template["name"],
        "description": template["description"],
        "plain_english": explain(node, None, library["default_field"]),
        "lucene_shape": render(node, {s: "{" + s + "}" for s in slots(node)}, library["default_field"]),
        "skeleton": template["skeleton"],
        "slots": list(slots(node)),
        "structure": gloss(node, None, library["default_field"]),
        "expands_matching": uses_proximity(node),
    }


def sign_seed_library(paths: Paths, private_key: Ed25519PrivateKey) -> dict:
    if paths.library.exists():
        raise FileExistsError(f"{paths.library} already exists")
    seed = json.loads((paths.seed / "seed_library.json").read_text("utf-8"))
    now = to_iso(utc_now())
    doc = {
        **seed,
        "schema": SCHEMA,
        "version": 1,
        "updated_at": now,
        "templates": [{**t, "origin": "seed", "added_at": now} for t in seed["templates"]],
    }
    validate_library(doc)
    write_signed(paths.library, doc, private_key)
    return doc


def suggest_template_id(library: dict, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:56] or "template"
    taken = {t["template_id"] for t in library["templates"]}
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _expect_version(library: dict, expected_version: int | None) -> None:
    if expected_version is not None and library["version"] != expected_version:
        raise LibraryError(
            f"the template library changed (v{expected_version} -> v{library['version']}) since you reviewed it; review again"
        )


def set_template_enabled(paths: Paths, private_key: Ed25519PrivateKey, template_id: str, enabled: bool,
                         *, expected_version: int | None = None) -> dict:
    library = load_library(paths, private_key.public_key())
    _expect_version(library, expected_version)
    get_template(library, template_id, enabled_only=False)
    now = to_iso(utc_now())
    templates = [
        {**t, "enabled": enabled, "status_changed_at": now} if t["template_id"] == template_id else t
        for t in library["templates"]
    ]
    doc = {**library, "version": library["version"] + 1, "updated_at": now, "templates": templates}
    validate_library(doc)
    write_signed(paths.library, doc, private_key)
    return doc


def promote_template(
    paths: Paths,
    private_key: Ed25519PrivateKey,
    *,
    skeleton: str,
    template_id: str,
    name: str,
    description: str,
    origin: str,
    expected_version: int | None = None,
) -> dict:
    library = load_library(paths, private_key.public_key())
    _expect_version(library, expected_version)
    if any(t["template_id"] == template_id for t in library["templates"]):
        raise LibraryError(f"template id {template_id!r} already exists")
    now = to_iso(utc_now())
    template = {
        "template_id": template_id,
        "name": name.strip(),
        "description": description.strip(),
        "skeleton": skeleton,
        "origin": origin,
        "added_at": now,
    }
    doc = {
        **library,
        "version": library["version"] + 1,
        "updated_at": now,
        "templates": [*library["templates"], template],
    }
    validate_library(doc)
    write_signed(paths.library, doc, private_key)
    return doc
