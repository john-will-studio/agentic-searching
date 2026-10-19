"""Warrants: the signed per-task approval.

A warrant fixes parameter 1, parameter 2, the purpose, the approved hit terms
(with their literals frozen), an expiry and a row cap. Under it the agent may
combine those terms with any template in the signed library.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .config import Paths
from .dates import DateLimitError, normalize_ranges, validate_lookback
from .rbac import EntitlementError, RbacProvider
from .scope import ScopeError, normalize_certifications, normalize_country_scope
from .signing import SignatureError, read_signed, sig_path, write_signed
from .syntax import SyntaxRejected, validate_literal, validate_pattern
from .util import check_id, from_iso, new_id, to_iso, utc_now

SCHEMA = 3
# Older warrants still verify and run: schema 1 has no date limits, schema 2 no country scope or certifications.
READABLE_SCHEMAS = {1, 2, 3}
TERM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# The client (customer) a hit term is associated with, from the separate client database. Recorded for
# audit; never added to the search, because client IDs don't necessarily appear in the searched records.
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}$")


def normalize_client_id(value) -> str:
    """'' for no client; otherwise a validated client ID. Raises ValueError."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not CLIENT_ID_RE.match(value.strip()):
        raise ValueError(f"invalid client ID {value!r}: use up to 64 letters, digits and _ . : / -")
    return value.strip()
TERM_SOURCES = {"script", "agent", "human", "human-edited"}
MAX_TTL_DAYS = 30
MAX_TTL_MINUTES = MAX_TTL_DAYS * 24 * 60
DEFAULT_MAX_ROWS = 20_000
MAX_ROWS = 500_000
MAX_QUERIES_PER_HOUR = 100


class WarrantError(Exception):
    """A warrant is invalid, expired, or not usable by the current entitlements."""


def check_entitlements(parameter_1, parameter_2, rbac: RbacProvider) -> None:
    try:
        rbac.check(parameter_1, parameter_2)
    except EntitlementError as exc:
        raise WarrantError(str(exc)) from exc


def build_warrant(
    *,
    parameter_1: str,
    parameter_2: list[str],
    purpose: str,
    terms: list[dict],
    ttl_minutes: int,
    max_rows: int,
    rbac: RbacProvider,
    lookback_days: int = 0,
    blocked_ranges: list[dict] | None = None,
    supersedes: str | None = None,
    country_include: list[dict | str] | None = None,
    country_exclude: list[dict] | None = None,
    certifications: list[dict] | None = None,
    config_context: dict | None = None,
    max_queries_per_hour: int | None = None,
    places: list[dict] | None = None,
    max_radius_km: float | None = None,
    purpose_origin: str | None = None,
    paths: Paths | None = None,
    now: datetime | None = None,
) -> dict:
    check_entitlements(parameter_1, parameter_2, rbac)
    if not isinstance(purpose, str) or not purpose.strip():
        raise WarrantError("purpose is required")
    if not terms:
        raise WarrantError("at least one approved term is required")
    seen_ids, seen_literals, clean = set(), set(), []
    for term in terms:
        tid, literal, source = term.get("term_id"), term.get("literal"), term.get("source")
        if not isinstance(tid, str) or not TERM_ID_RE.match(tid):
            raise WarrantError(f"invalid term id {tid!r}")
        if tid in seen_ids:
            raise WarrantError(f"duplicate term id {tid}")
        match = term.get("match", "exact")
        if match not in ("exact", "pattern"):
            raise WarrantError(f"term {tid}: match must be exact or pattern")
        try:
            (validate_pattern if match == "pattern" else validate_literal)(literal)
        except SyntaxRejected as exc:
            raise WarrantError(str(exc)) from exc
        if literal in seen_literals:
            raise WarrantError(f"duplicate term {literal!r}")
        if source not in TERM_SOURCES:
            raise WarrantError(f"term {tid} has unknown source {source!r}")
        try:
            client_id = normalize_client_id(term.get("client_id"))
        except ValueError as exc:
            raise WarrantError(f"term {tid}: {exc}") from exc
        seen_ids.add(tid)
        seen_literals.add(literal)
        clean.append({
            "term_id": tid,
            "literal": literal,
            "source": source,
            "provenance": str(term.get("provenance") or ""),
            **({"client_id": client_id} if client_id else {}),
            **({"match": "pattern"} if match == "pattern" else {}),
        })
    if not isinstance(ttl_minutes, int) or not 1 <= ttl_minutes <= MAX_TTL_MINUTES:
        raise WarrantError(f"expiry must be between 1 minute and {MAX_TTL_DAYS} days")
    if not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_ROWS:
        raise WarrantError(f"max rows per run must be 1 to {MAX_ROWS:,}")
    try:
        lookback_days = validate_lookback(lookback_days)
        blocked = normalize_ranges(blocked_ranges)
    except (DateLimitError, KeyError, TypeError) as exc:
        raise WarrantError(str(exc)) from exc
    if supersedes is not None:
        check_id(supersedes, "w")
    if max_queries_per_hour is not None and (isinstance(max_queries_per_hour, bool) or not isinstance(max_queries_per_hour, int)
                                             or not 1 <= max_queries_per_hour <= MAX_QUERIES_PER_HOUR):
        raise WarrantError(f"max searches per hour must be 1 to {MAX_QUERIES_PER_HOUR:,}")
    try:
        scope = normalize_country_scope(paths or Paths.default(), country_include, country_exclude)
        certs = normalize_certifications(certifications)
    except ScopeError as exc:
        raise WarrantError(str(exc)) from exc
    from .places import DEFAULT_MAX_RADIUS_KM, PlaceError, normalize_places

    if max_radius_km is None:
        max_radius_km = (((config_context or {}).get("values") or {}).get("geography") or {}).get(
            "max_radius_km", DEFAULT_MAX_RADIUS_KM)
    try:
        signed_places = normalize_places(places, scope, max_radius_km)
    except PlaceError as exc:
        raise WarrantError(str(exc)) from exc
    now = now or utc_now()
    doc = {
        "schema": SCHEMA,
        "warrant_id": new_id("w"),
        "created_at": to_iso(now),
        "expires_at": to_iso(now + timedelta(minutes=ttl_minutes)),
        "parameter_1": parameter_1,
        "parameter_2": sorted(parameter_2),
        "purpose": purpose.strip(),
        "terms": clean,
        "max_rows_per_run": max_rows,
        "max_lookback_days": lookback_days,
        "blocked_date_ranges": blocked,
        "country_scope": scope,
        **({"places": signed_places} if signed_places else {}),
        "certifications": [{**c, "accepted": True, "accepted_at": to_iso(now)} for c in certs],
        "template_policy": "any template in the signed library; other syntax needs one-time approval",
    }
    if supersedes:
        doc["supersedes"] = supersedes
    if config_context:
        doc["config_context"] = config_context
    if max_queries_per_hour is not None:
        doc["max_queries_per_hour"] = max_queries_per_hour
    if purpose_origin is not None:
        if purpose_origin not in PURPOSE_ORIGINS:
            raise WarrantError(f"unknown purpose origin {purpose_origin!r}")
        doc["purpose_origin"] = purpose_origin
    return doc


# Who wrote the purpose: kept with the signed warrant so an agent's draft never reads as the analyst's own words.
PURPOSE_ORIGINS = {"agent": "drafted by the agent; certified unchanged by the signing analyst",
                   "agent-edited": "drafted by the agent, edited and certified by the signing analyst",
                   "analyst": "written by the signing analyst"}


def warrant_patterns(warrant: dict) -> frozenset:
    """The hit terms this warrant approved as patterns (PROJECT*), by their text."""
    return frozenset(t["literal"] for t in warrant.get("terms", []) if t.get("match") == "pattern")


def format_duration(minutes: int) -> str:
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} d" if days else "", f"{hours} h" if hours else "", f"{mins} min" if mins else ""]
    return " ".join(p for p in parts if p) or "0 min"


def warrant_path(paths: Paths, warrant_id: str):
    return paths.warrants / f"{check_id(warrant_id, 'w')}.json"


def save_warrant(paths: Paths, doc: dict, private_key: Ed25519PrivateKey):
    path = warrant_path(paths, doc["warrant_id"])
    write_signed(path, doc, private_key)
    return path


def load_warrant(paths: Paths, public_key: Ed25519PublicKey, warrant_id: str) -> dict:
    try:
        path = warrant_path(paths, warrant_id)
    except ValueError as exc:
        raise WarrantError(str(exc)) from exc
    if not path.exists():
        marker = paths.warrants / "revoked" / f"{warrant_id}.superseded_by"
        if marker.exists():
            raise WarrantError(f"warrant {warrant_id} was superseded by {marker.read_text('utf-8').strip()}; use that id")
        raise WarrantError(f"no active warrant {warrant_id} (never signed, or revoked)")
    doc = read_signed(path, public_key)
    if doc.get("schema") not in READABLE_SCHEMAS or doc.get("warrant_id") != warrant_id:
        raise WarrantError(f"warrant file {path.name} does not match its id")
    return doc


def is_expired(doc: dict, now: datetime | None = None) -> bool:
    return (now or utc_now()) >= from_iso(doc["expires_at"])


def resolve_bindings(warrant: dict, bindings: dict[str, str]) -> dict[str, str]:
    """Map slot -> term_id to slot -> literal, using only the warrant's frozen terms."""
    literals = {t["term_id"]: t["literal"] for t in warrant["terms"]}
    resolved = {}
    for slot, term_id in bindings.items():
        if term_id not in literals:
            raise WarrantError(f"term {term_id!r} is not approved in warrant {warrant['warrant_id']}")
        resolved[slot] = literals[term_id]
    return resolved


def list_warrants(paths: Paths, public_key: Ed25519PublicKey, *, include_archived: bool = False) -> list[dict]:
    """Every warrant with its verification status, newest first.

    With include_archived, revoked and superseded warrants are listed too
    (status "revoked" or "superseded", plus "superseded_by"). They are
    read-only records: the executor never loads them.
    """
    rows = []
    if not paths.warrants.exists():
        return rows
    for path in sorted(paths.warrants.glob("w-*.json"), reverse=True):
        warrant_id = path.stem
        try:
            doc = load_warrant(paths, public_key, warrant_id)
            status = "expired" if is_expired(doc) else "active"
            rows.append({"warrant_id": warrant_id, "status": status, "doc": doc})
        except (SignatureError, WarrantError, ValueError) as exc:
            rows.append({"warrant_id": warrant_id, "status": "INVALID", "error": str(exc), "doc": None})
    archive = paths.warrants / "revoked"
    if include_archived and archive.exists():
        for path in sorted(archive.glob("w-*.json"), reverse=True):
            warrant_id = path.stem
            marker = archive / f"{warrant_id}.superseded_by"
            row = {"warrant_id": warrant_id, "status": "superseded" if marker.exists() else "revoked"}
            if marker.exists():
                row["superseded_by"] = marker.read_text("utf-8").strip()
            try:
                row["doc"] = read_signed(path, public_key)
            except SignatureError as exc:
                row.update(status="INVALID", error=str(exc), doc=None)
            rows.append(row)
        rows.sort(key=lambda r: r["warrant_id"], reverse=True)
    return rows


def replace_warrant(paths: Paths, old_id: str, new_doc: dict, private_key: Ed25519PrivateKey) -> None:
    """Retire old_id and sign its replacement.

    The old warrant is moved out first, with a single atomic rename. If it was already revoked or
    replaced (for example while the passphrase dialog was open), nothing is signed. If signing the
    replacement then fails, the old warrant stays retired: an error can only reduce authority."""
    path = warrant_path(paths, old_id)
    archive = paths.warrants / "revoked"
    archive.mkdir(parents=True, exist_ok=True)
    try:
        path.replace(archive / path.name)
    except FileNotFoundError as exc:
        raise WarrantError(f"{old_id} was revoked or replaced while you were signing; review again") from exc
    if sig_path(path).exists():
        sig_path(path).replace(archive / sig_path(path).name)
    save_warrant(paths, new_doc, private_key)
    (archive / f"{old_id}.superseded_by").write_text(new_doc["warrant_id"] + "\n", "utf-8")


def supersede_warrant(paths: Paths, old_id: str, new_id: str) -> None:
    """Retire old_id after a replacement (with its terms carried over) has been signed."""
    check_id(new_id, "w")
    if not warrant_path(paths, old_id).exists():
        raise WarrantError(f"{old_id} is no longer active (revoked or replaced)")
    revoke_warrant(paths, old_id)
    (paths.warrants / "revoked" / f"{old_id}.superseded_by").write_text(new_id + "\n", "utf-8")


def revoke_warrant(paths: Paths, warrant_id: str) -> None:
    path = warrant_path(paths, warrant_id)
    target = paths.warrants / "revoked"
    target.mkdir(parents=True, exist_ok=True)
    for p in (path, sig_path(path)):
        if p.exists():
            p.replace(target / p.name)
