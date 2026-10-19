"""New syntax: agent requests, one-time approvals, and promotion to the library.

The agent submits a skeleton plus slot bindings to approved term IDs. Requests
are unsigned and carry no authority. Approving one writes a signed, single-use
record with the literals and rendered query frozen. After it runs, the user can
promote the skeleton into the library.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .config import Paths
from .library import load_library
from .signing import SignatureError, atomic_write, canonical_bytes, read_signed, write_signed
from .lint import lint
from .syntax import Compiled, SyntaxRejected, compile_query, suggest_name
from .util import check_id, from_iso, new_id, to_iso, utc_now
from .warrant import warrant_patterns, WarrantError, is_expired, load_warrant, resolve_bindings

SCHEMA = 1


def warrant_digest(warrant: dict) -> str:
    return hashlib.sha256(canonical_bytes(warrant)).hexdigest()


def preview(paths: Paths, public_key: Ed25519PublicKey, warrant_id: str, skeleton: str, bindings: dict) -> tuple[dict, Compiled]:
    """Validate a skeleton + bindings against a warrant and the library. Raises on any problem."""
    warrant = load_warrant(paths, public_key, warrant_id)
    if is_expired(warrant):
        raise WarrantError(f"warrant {warrant_id} has expired")
    library = load_library(paths, public_key)
    literals = resolve_bindings(warrant, bindings)
    compiled = compile_query(
        skeleton, literals, default_field=library["default_field"], allowed_fields=library["allowed_fields"],
        patterns=warrant_patterns(warrant),
    )
    return warrant, compiled


def lint_request(paths: Paths, public_key: Ed25519PublicKey, warrant_id: str, skeleton: str, bindings: dict, stage: str):
    """Deterministic lint of a requested query against its warrant's approved terms and the library."""
    warrant = load_warrant(paths, public_key, warrant_id)
    library = load_library(paths, public_key)
    literals = resolve_bindings(warrant, bindings)
    return lint(skeleton, literals, default_field=library["default_field"], allowed_fields=library["allowed_fields"],
                library=library, stage=stage, patterns=warrant_patterns(warrant))


def submit_request(paths: Paths, public_key: Ed25519PublicKey, *, warrant_id: str, skeleton: str, bindings: dict, rationale: str) -> dict:
    report = lint_request(paths, public_key, warrant_id, skeleton, bindings, "nomination")
    if not report.passed:
        raise SyntaxRejected("lint failed: " + "; ".join(f"{f.code} {f.message}" for f in report.errors))
    _, compiled = preview(paths, public_key, warrant_id, skeleton, bindings)
    request = {
        "schema": SCHEMA,
        "request_id": new_id("req"),
        "submitted_at": to_iso(utc_now()),
        "warrant_id": warrant_id,
        "skeleton": skeleton,
        "bindings": dict(bindings),
        "rationale": rationale.strip(),
        "suggested_name": suggest_name(compiled.node),
        "lint": report.to_dict(),
    }
    atomic_write(paths.syntax_requests / f"{request['request_id']}.json", canonical_bytes(request))
    return {**request, "preview_query": compiled.query, "preview_structure": compiled.gloss}


def list_requests(paths: Paths) -> list[dict]:
    rows = []
    if not paths.syntax_requests.exists():
        return rows
    for path in sorted(paths.syntax_requests.glob("req-*.json")):
        try:
            doc = json.loads(path.read_text("utf-8"))
            problem = _request_problem(doc, path.stem)
        except (OSError, ValueError) as exc:
            doc, problem = {}, f"unreadable: {exc}"
        if problem:
            rows.append({"request_id": path.stem, "malformed": True, "problem": problem, "file": path.name})
        else:
            rows.append({**doc, "file": path.name})
    return rows


def _request_problem(doc, stem: str) -> str | None:
    """Why a request file can't be trusted as a request, or None."""
    if not isinstance(doc, dict):
        return "not a JSON object"
    try:
        check_id(doc.get("request_id"), "req")
    except ValueError as exc:
        return str(exc)
    if doc["request_id"] != stem:
        return f"the ID inside the file ({doc['request_id']}) doesn't match its file name ({stem})"
    try:
        check_id(doc.get("warrant_id"), "w")
    except ValueError as exc:
        return str(exc)
    if not isinstance(doc.get("skeleton"), str):
        return "missing skeleton"
    bindings = doc.get("bindings")
    if not isinstance(bindings, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in bindings.items()):
        return "bindings must map slot names to term IDs"
    if not isinstance(doc.get("rationale", ""), str):
        return "rationale must be text"
    if "lint" in doc and not isinstance(doc["lint"], dict):
        return "recorded lint must be an object"
    if "submitted_at" in doc:
        try:
            from_iso(doc["submitted_at"])
        except (TypeError, ValueError):
            return "submitted_at is not a timestamp"
    return None


def _archive_request(paths: Paths, request_id: str, outcome: str, file: str | None = None) -> None:
    """Move a request file out of the inbox. With `file`, move exactly that file (it may be malformed)."""
    if file is not None:
        name = Path(file).name
        if name != file or not name.endswith(".json") or not name.startswith("req-"):
            raise ValueError(f"not a request file name: {file!r}")
        path = paths.syntax_requests / name
    else:
        path = paths.syntax_requests / f"{check_id(request_id, 'req')}.json"
    target = paths.syntax_requests / outcome
    target.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.replace(target / path.name)


def reject_request(paths: Paths, request_id: str, file: str | None = None) -> None:
    _archive_request(paths, request_id, "rejected", file)


def approve_request(
    paths: Paths,
    private_key: Ed25519PrivateKey,
    request: dict,
    *,
    reviewed_query: str | None = None,
    reviewed_library_version: int | None = None,
    reviewed_warrant_digest: str | None = None,
) -> dict:
    """Sign a one-time approval. If the reviewed query or library version is given and
    anything differs from what's current, refuse: the human must review again."""
    warrant, compiled = preview(
        paths, private_key.public_key(), request["warrant_id"], request["skeleton"], request["bindings"]
    )
    if reviewed_warrant_digest is not None and warrant_digest(warrant) != reviewed_warrant_digest:
        raise WarrantError(f"warrant {warrant['warrant_id']} changed since you reviewed this request; review it again")
    if reviewed_query is not None and compiled.query != reviewed_query:
        raise WarrantError("the query changed since you reviewed it; review it again before approving")
    if reviewed_library_version is not None:
        current = load_library(paths, private_key.public_key())["version"]
        if current != reviewed_library_version:
            raise WarrantError(
                f"the template library changed (v{reviewed_library_version} -> v{current}) since you reviewed; review again"
            )
    literals = resolve_bindings(warrant, request["bindings"])
    report = lint_request(paths, private_key.public_key(), request["warrant_id"], request["skeleton"],
                          request["bindings"], "approval")
    if not report.passed:
        raise SyntaxRejected("lint failed: " + "; ".join(f"{f.code} {f.message}" for f in report.errors))
    doc = {
        "schema": SCHEMA,
        "onetime_id": new_id("ot"),
        "request_id": request["request_id"],
        "approved_at": to_iso(utc_now()),
        "expires_at": warrant["expires_at"],
        "warrant_id": warrant["warrant_id"],
        "skeleton": request["skeleton"],
        "bindings": dict(request["bindings"]),
        "literals": literals,
        "query": compiled.query,
        "structure": compiled.gloss,
        "expands_matching": compiled.expands_matching,
        "max_runs": 1,
        "suggested_name": suggest_name(compiled.node),
        "lint": report.to_dict(),
    }
    write_signed(paths.onetime / f"{doc['onetime_id']}.json", doc, private_key)
    _archive_request(paths, request["request_id"], "approved")
    return doc


def load_onetime(paths: Paths, public_key: Ed25519PublicKey, onetime_id: str) -> dict:
    try:
        path = paths.onetime / f"{check_id(onetime_id, 'ot')}.json"
    except ValueError as exc:
        raise WarrantError(str(exc)) from exc
    if not path.exists():
        raise WarrantError(f"no one-time approval {onetime_id}")
    doc = read_signed(path, public_key)
    if doc.get("onetime_id") != onetime_id:
        raise WarrantError(f"one-time file {path.name} does not match its id")
    return doc


def list_onetime(paths: Paths, public_key: Ed25519PublicKey) -> list[dict]:
    rows = []
    if not paths.onetime.exists():
        return rows
    for path in sorted(paths.onetime.glob("ot-*.json"), reverse=True):
        try:
            rows.append(load_onetime(paths, public_key, path.stem))
        except (SignatureError, WarrantError) as exc:
            rows.append({"onetime_id": path.stem, "invalid": str(exc)})
    return rows
