"""The executor: the only code path that builds and sends a search payload.

It accepts IDs and slot bindings, never parameters or query strings. Parameter
1 and parameter 2 always come from the signed warrant.

Checks on every run:
1. warrant signature, expiry, and current (mock) entitlements;
2. library signature (template runs) or one-time signature and single use;
3. every bound term is approved in the warrant; syntax is valid;
4. row cap.

Every run gets a run ID. When a payload is sent, the exact payload and the
search service's own tracking ID are saved under data/state/payloads/, and the
audit log records both, so each request can be traced afterwards.

In practice mode everything runs as your OS user, so this catches agent
mistakes and drift; it is not a boundary against an agent that can edit this
code or read the search credentials. See README.
"""

from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from datetime import datetime

from .backend import Backend, BackendError, BackendNotReady, Plan, SearchResponse
from .config import Paths
from .dates import apply_window, window
from .library import LibraryError, country_field_of, date_field_of, get_template, load_library
from .org_config import field_mismatches
from .places import apply_place_filter, place_check, place_filter
from .scope import allowed_countries, apply_country_filter, countries, country_filter
from .onetime import load_onetime
from .rbac import get_rbac
from .signing import SignatureError, atomic_write, canonical_bytes, load_public_key
from .lint import lint, lint_query
from .syntax import SyntaxRejected, compile_query
from .util import check_id, from_iso, new_id, to_iso, utc_now
from .warrant import (
    warrant_patterns,
    WarrantError,
    check_entitlements,
    is_expired,
    load_warrant,
    resolve_bindings,
)

_DENIABLE = (SignatureError, WarrantError, LibraryError, SyntaxRejected, ValueError)


class ExecutionDenied(Exception):
    """The run was refused before anything was sent to the search service. run_id is the refusal's log entry."""

    def __init__(self, message: str, run_id: str | None = None):
        super().__init__(message)
        self.run_id = run_id


def run_template(
    paths: Paths,
    *,
    warrant_id: str,
    template_id: str,
    bindings: dict[str, str],
    limit: int | None = None,
    backend: Backend | None = None,
    now: datetime | None = None,
) -> dict:
    record = {
        "run_id": new_id("run"),
        "kind": "template",
        "warrant_id": warrant_id,
        "template_id": template_id,
        "bindings": bindings,
        "term_ids": _term_ids(bindings),
    }
    try:
        public_key = load_public_key(paths)
        warrant = _usable_warrant(paths, public_key, warrant_id, now)
        library = load_library(paths, public_key)
        template = get_template(library, template_id)
        literals = resolve_bindings(warrant, bindings)
        compiled = compile_query(
            template["skeleton"],
            literals,
            default_field=library["default_field"],
            allowed_fields=library["allowed_fields"],
            patterns=warrant_patterns(warrant),
        )
        record["library_version"] = library["version"]
    except _DENIABLE as exc:
        _audit(paths, {**record, "outcome": "denied", "reason": str(exc)}, now)
        raise ExecutionDenied(str(exc), record["run_id"]) from exc
    plan = Plan(compiled.node, literals, library["default_field"], warrant_patterns(warrant))
    return _dispatch(paths, warrant, compiled.query, compiled.gloss, plan, limit, backend, record, now,
                     library, template["skeleton"])


def run_onetime(
    paths: Paths,
    *,
    onetime_id: str,
    limit: int | None = None,
    backend: Backend | None = None,
    now: datetime | None = None,
) -> dict:
    record = {"run_id": new_id("run"), "kind": "onetime", "onetime_id": onetime_id}
    try:
        public_key = load_public_key(paths)
        approval = load_onetime(paths, public_key, onetime_id)
        record["warrant_id"] = approval["warrant_id"]
        record["term_ids"] = _term_ids(approval["bindings"])
        warrant = _usable_warrant(paths, public_key, approval["warrant_id"], now)
        if (now or utc_now()) >= from_iso(approval["expires_at"]):
            raise WarrantError(f"one-time approval {onetime_id} has expired")
        library = load_library(paths, public_key)
        compiled = compile_query(
            approval["skeleton"],
            approval["literals"],
            default_field=library["default_field"],
            allowed_fields=library["allowed_fields"],
            patterns=warrant_patterns(warrant),
        )
        # Consistency check only: the frozen query is what runs.
        if compiled.query != approval["query"]:
            raise WarrantError("one-time query no longer renders identically (library fields or renderer changed)")
        # The frozen terms must still be what the warrant approves under those IDs.
        if resolve_bindings(warrant, approval["bindings"]) != approval["literals"]:
            raise WarrantError(f"warrant {warrant['warrant_id']} no longer approves the terms this one-time query was approved with")
    except _DENIABLE as exc:
        _audit(paths, {**record, "outcome": "denied", "reason": str(exc)}, now)
        raise ExecutionDenied(str(exc), record["run_id"]) from exc
    plan = Plan(compiled.node, approval["literals"], library["default_field"], warrant_patterns(warrant))
    return _dispatch(paths, warrant, approval["query"], approval["structure"], plan, limit, backend, record, now,
                     library, approval["skeleton"])


def _term_ids(bindings) -> list[str]:
    if not isinstance(bindings, dict):
        return []
    return sorted({str(v) for v in bindings.values()})


def _usable_warrant(paths: Paths, public_key, warrant_id: str, now) -> dict:
    warrant = load_warrant(paths, public_key, warrant_id)
    if is_expired(warrant, now):
        raise WarrantError(f"warrant {warrant_id} expired at {warrant['expires_at']}")
    # Re-check against current entitlements, not just what was true at signing.
    check_entitlements(warrant["parameter_1"], warrant["parameter_2"], get_rbac(paths))
    return warrant


def _dispatch(paths, warrant, content_query, structure, plan, limit, backend, record, now, library, skeleton) -> dict:
    if limit is not None:
        record["requested_limit"] = limit  # recorded as asked, so a clamp to the warrant's cap is visible
    cap = warrant["max_rows_per_run"]
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        _audit(paths, {**record, "outcome": "denied", "reason": "limit must be a positive integer"}, now)
        raise ExecutionDenied("limit must be a positive integer", record["run_id"])
    # Date limits wrap the agent's content query from outside; no template can reach them.
    date_field = date_field_of(library)
    mismatch = field_mismatches((warrant.get("config_context") or {}).get("values"), library)
    if mismatch:
        reason = "the warrant was signed for different field names than the template library uses: " + "; ".join(mismatch)
        _audit(paths, {**record, "outcome": "denied", "reason": reason}, now)
        raise ExecutionDenied(reason, record["run_id"])
    date_filter = window(warrant, now or utc_now(), date_field)
    country = country_filter(warrant, country_field_of(library), countries(paths))
    places = place_filter(warrant)
    query = apply_window(apply_place_filter(apply_country_filter(content_query, country), places), date_filter)

    # Lint the structure and, independently, the exact string about to be sent.
    report = lint(skeleton, plan.literals, default_field=library["default_field"], patterns=plan.patterns,
                  allowed_fields=library["allowed_fields"], stage="fire")
    final = lint_query(query, set(plan.literals.values()), allowed_fields=library["allowed_fields"], date_field=date_field,
                       patterns=plan.patterns,
                       country_field=country["field"] if country else None,
                       country_codes=set(country["include"] + country["exclude"]) if country else set(),
                       place_clauses=places["clauses"] if places else None)
    report.findings += [f for f in final.findings if f not in report.findings]
    report.query_sha256 = final.query_sha256
    lint_record = report.to_dict()
    record["lint"] = {"passed": report.passed, "summary": report.summary(), "codes": [f.code for f in report.findings],
                      "warnings": len(report.warnings), "errors": len(report.errors)}
    if not report.passed:
        reason = "lint failed: " + "; ".join(f"{f.code} {f.message}" for f in report.errors)
        _audit(paths, {**record, "outcome": "denied", "query": query, "reason": reason}, now)
        raise ExecutionDenied(reason, record["run_id"])
    payload = {
        # From the signed warrant, never from the agent: the search service and its audit see why it's searching.
        "warrant_id": warrant["warrant_id"],
        "purpose": warrant["purpose"],
        "parameter_1": warrant["parameter_1"],
        "parameter_2": list(warrant["parameter_2"]),
        "query": query,
        "limit": min(limit or cap, cap),
    }
    if date_filter:
        payload["date_filter"] = date_filter
    if country:
        payload["country_filter"] = country
    if places:
        payload["place_filter"] = places
    # Where it goes, and whether it can go at all, before any quota is reserved or a one-time approval is used.
    try:
        if backend is None:
            # The signed organization configuration says where searches go (an unsigned or edited one is refused).
            from . import org_config
            from .backend import backend_from_settings

            settings = org_config.load(paths, user=False)
            _check_config_is_current(paths, settings, warrant)
            backend = backend_from_settings(settings, paths.corpus)
        ready = getattr(backend, "check_ready", None)
        if ready:
            ready()
        prepare = getattr(backend, "prepare", None)
        if prepare:
            prepare(payload)  # builds the exact request (credentials included) so a problem refuses here
    except Exception as exc:  # noqa: BLE001 - nothing was sent or reserved; say why
        _audit(paths, {**record, "outcome": "denied", "query": query, "reason": f"search service unavailable: {exc}"}, now)
        raise ExecutionDenied(f"search service unavailable: {exc}", record["run_id"]) from exc
    try:
        _reserve_rate_slot(paths, warrant, record["run_id"], now)
    except WarrantError as exc:
        _audit(paths, {**record, "outcome": "denied", "query": query, "reason": str(exc)}, now)
        raise ExecutionDenied(str(exc), record["run_id"]) from exc
    if record["kind"] == "onetime":
        # Consumed only now, after every check has passed, immediately before sending.
        try:
            _consume_onetime(paths, record["onetime_id"], now)
        except WarrantError as exc:
            _cancel_rate_slot(paths, warrant, record["run_id"])  # a refused search uses no quota
            _audit(paths, {**record, "outcome": "denied", "query": query, "reason": str(exc)}, now)
            raise ExecutionDenied(str(exc), record["run_id"]) from exc
    sent_at = to_iso(now or utc_now())
    scrub = getattr(backend, "scrub", None) or (lambda text: text)
    try:  # the record of this search must be writable before anything is sent
        paths.state.mkdir(parents=True, exist_ok=True)
        with open(paths.audit_log, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        _cancel_rate_slot(paths, warrant, record["run_id"])
        if record["kind"] == "onetime":
            _release_onetime(paths, record["onetime_id"], record["run_id"], now)
        raise ExecutionDenied(f"the audit log can't be written ({exc}); nothing was sent", record["run_id"]) from None
    try:
        response = backend.search(payload, plan)
    except BackendNotReady as exc:  # nothing reached the service: a refusal that uses no quota and no approval
        reason = scrub(str(exc))
        _cancel_rate_slot(paths, warrant, record["run_id"])
        if record["kind"] == "onetime":
            _release_onetime(paths, record["onetime_id"], record["run_id"], now)
        _audit(paths, {**record, "outcome": "denied", "query": query, "reason": f"search service unavailable: {reason}"}, now)
        raise ExecutionDenied(f"search service unavailable: {reason}", record["run_id"]) from None
    except Exception as exc:  # noqa: BLE001 - recorded, then reported without the original (it may quote a secret)
        reason = scrub(f"{type(exc).__name__}: {exc}" if not isinstance(exc, BackendError) else str(exc))
        try:
            _save_payload(paths, record["run_id"], sent_at, payload, None, None, reason, lint_record,
                          request=getattr(backend, "last_request", None))
        except Exception as save_exc:  # noqa: BLE001 - the audit entry below must still be written
            reason += f" (and the payload record couldn't be saved: {save_exc})"
        _audit(paths, {**record, "outcome": "error", "query": query, "reason": reason}, now)
        raise BackendError(reason) from None
    # Sent. From here on, any failure is still audited, with the service's ID, before it's reported.
    service_id = getattr(response, "service_query_id", None)
    try:
        if not isinstance(response, SearchResponse):  # adapters that return a bare result list
            response = SearchResponse(list(response))
        results, service_id = list(response.results), response.service_query_id
        # Defense in depth: never hand back a record from outside the geographic constraint, whatever the service did.
        allowed = allowed_countries(country)
        withheld = {"countries": 0, "places": 0, "no_location": 0}
        if allowed is not None:
            kept = [r for r in results if not (isinstance(r, dict) and "country" in r and r["country"] not in allowed)]
            withheld["countries"], results = len(results) - len(kept), kept
        if places:  # and none that can't be shown to be inside an approved place
            kept = []
            for r in results:
                where = place_check(places, r)
                if where == "inside":
                    kept.append(r)
                else:
                    withheld["places" if where == "outside" else "no_location"] += 1
            results = kept
        results = results[: payload["limit"]]  # the warrant's record cap holds even if the service sends more
    except Exception as exc:  # noqa: BLE001
        reason = scrub(f"sent, but the response couldn't be processed: {type(exc).__name__}: {exc}")
        _audit(paths, {**record, "outcome": "error", "query": query, "reason": reason, "service_query_id": service_id}, now)
        raise BackendError(reason) from None
    total_withheld = sum(withheld.values())
    # The search has been sent: whatever happens to the auxiliary records below, the run is always audited.
    from .results import profile, save_results, without_full_text

    problems = []
    try:
        prof = profile(results, record.get("term_ids") or [], warrant)
    except Exception as exc:  # noqa: BLE001
        prof, _ = None, problems.append(f"profile: {exc}")
    results = without_full_text(results)
    try:
        save_results(paths, record["run_id"], results)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"saving the results: {exc}")
    try:
        _save_payload(paths, record["run_id"], sent_at, payload, service_id, len(results), None, lint_record,
                      profile=prof, request=getattr(backend, "last_request", None))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"saving the payload record: {exc}")
    _audit(paths, {**record, "outcome": "ok", "query": query, "count": len(results), "service_query_id": service_id,
                   **({"withheld": total_withheld, "withheld_detail": {k: v for k, v in withheld.items() if v}}
                      if total_withheld else {}),
                   **({"storage_problem": "; ".join(problems)} if problems else {})}, now)
    return {
        "run_id": record["run_id"],
        "service_query_id": service_id,
        "warrant_id": warrant["warrant_id"],
        "purpose": warrant["purpose"],
        "query": query,
        "structure": structure,
        "lint": record["lint"],
        "limit": payload["limit"],
        "date_filter": date_filter,
        "country_filter": country,
        "place_filter": places,
        "count": len(results),
        "withheld_outside_geographic_constraint": total_withheld,
        "withheld_detail": withheld,
        "results": results,
    }


@contextmanager
def _exclusive(fh):
    """Exclusive lock on an open file, on Windows and on Unix."""
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


RATE_WINDOW_SECONDS = 3600


def rate_ledger_path(paths: Paths):
    return paths.state / "rate_ledger.jsonl"


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate field in record")
    return dict(pairs)


def warrant_lineage(paths: Paths, warrant_id: str, first_supersedes: str | None = None) -> set[str]:
    """This warrant and every warrant it replaced. A replacement inherits its predecessors' searches in the
    last hour, so editing a warrant can't reset the hourly limit; raising the limit is a signed decision."""
    ids, current = {warrant_id}, first_supersedes
    if current is None:
        current = _supersedes_of(paths, warrant_id)
    for _ in range(100):
        if not current or current in ids:
            break
        ids.add(current)
        current = _supersedes_of(paths, current)
    return ids


def _supersedes_of(paths: Paths, warrant_id: str) -> str | None:
    for folder in (paths.warrants, paths.warrants / "revoked"):
        try:
            value = json.loads((folder / f"{check_id(warrant_id, 'w')}.json").read_text("utf-8")).get("supersedes")
            return value if isinstance(value, str) else None
        except (OSError, ValueError, AttributeError):
            continue
    return None


def _read_rate_ledger(lines, warrant_id, cutoff: float) -> tuple[list, list[str]]:
    """(reservations in the window for this warrant or its lineage, problems). Cancelled reservations don't count."""
    warrant_ids = {warrant_id} if isinstance(warrant_id, str) else set(warrant_id)
    active, cancelled, problems, seen_runs = {}, set(), [], set()
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line, object_pairs_hook=_no_duplicate_keys)
            if not isinstance(entry, dict):
                raise ValueError("not an object")
            if set(entry) == {"cancel"}:
                cancelled.add(check_id(entry["cancel"], "run"))
                continue
            if set(entry) != {"warrant_id", "run_id", "at"}:
                raise ValueError(f"unexpected fields {sorted(entry)}")
            check_id(entry["warrant_id"], "w")
            run_id = check_id(entry["run_id"], "run")
            if run_id in active or run_id in seen_runs:
                raise ValueError(f"run {run_id} reserved twice")
            seen_runs.add(run_id)
            at = from_iso(entry["at"])
            if entry["warrant_id"] in warrant_ids and at.timestamp() > cutoff:
                active[run_id] = at
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            problems.append(f"rate ledger line {number}: {exc}")
    return [at for run_id, at in active.items() if run_id not in cancelled], problems


def queries_in_last_hour(paths: Paths, warrant_id: str, now=None) -> int:
    """Searches sent (or being sent) under a warrant in the last hour, from the rate ledger."""
    path = rate_ledger_path(paths)
    if not path.exists():
        return 0
    cutoff = (now or utc_now()).timestamp() - RATE_WINDOW_SECONDS  # display only; enforcement fails closed
    return len(_read_rate_ledger(path.read_text("utf-8", errors="replace").splitlines(),
                                 warrant_lineage(paths, warrant_id), cutoff)[0])


def _reserve_rate_slot(paths: Paths, warrant: dict, run_id: str, now) -> None:
    """Enforce the warrant's searches-per-hour limit. The check and the reservation happen under one
    lock, so parallel calls can't both take the last slot. An unreadable ledger fails closed: a damaged
    line might be a used slot."""
    limit = warrant.get("max_queries_per_hour")
    if not limit:
        return  # warrants signed before this limit existed
    now = now or utc_now()
    paths.state.mkdir(parents=True, exist_ok=True)
    with open(rate_ledger_path(paths), "a+", encoding="utf-8") as fh, _exclusive(fh):
        fh.seek(0)
        lineage = warrant_lineage(paths, warrant["warrant_id"], warrant.get("supersedes"))
        used, problems = _read_rate_ledger(fh.read().splitlines(), lineage, now.timestamp() - RATE_WINDOW_SECONDS)
        if problems:
            raise WarrantError("the searches-per-hour ledger is unreadable; searches are blocked until it's repaired ("
                               + problems[0] + ")")
        if len(used) >= limit:
            wait = max(1, math.ceil((min(used).timestamp() + RATE_WINDOW_SECONDS - now.timestamp()) / 60))
            raise WarrantError(f"searches-per-hour limit reached ({len(used)} of {limit} in the last hour); "
                               f"the next search is allowed in about {wait} min")
        fh.seek(0, os.SEEK_END)
        fh.write(json.dumps({"warrant_id": warrant["warrant_id"], "run_id": run_id, "at": to_iso(now)}) + "\n")
        fh.flush()


def _cancel_rate_slot(paths: Paths, warrant: dict, run_id: str) -> None:
    if not warrant.get("max_queries_per_hour"):
        return
    with open(rate_ledger_path(paths), "a+", encoding="utf-8") as fh, _exclusive(fh):
        fh.seek(0, os.SEEK_END)
        fh.write(json.dumps({"cancel": run_id}) + "\n")
        fh.flush()


def _consume_onetime(paths: Paths, onetime_id: str, now) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    with open(paths.used_onetime, "a+", encoding="utf-8") as fh, _exclusive(fh):
        fh.seek(0)
        used = set()
        for line in fh:
            if not line.strip():
                continue
            try:
                entry_id, released = _ledger_entry(line)
            except ValueError as exc:
                # Fail closed: an unreadable consumption record could hide a previous use.
                raise WarrantError("the single-use ledger is unreadable; one-time approvals are blocked until it's repaired") from exc
            (used.discard if released else used.add)(entry_id)
        if onetime_id in used:
            raise WarrantError(f"one-time approval {onetime_id} has already been used")
        fh.seek(0, os.SEEK_END)
        fh.write(json.dumps({"onetime_id": onetime_id, "used_at": to_iso(now or utc_now())}) + "\n")
        fh.flush()


def _release_onetime(paths: Paths, onetime_id: str, run_id: str, now) -> None:
    """The search was refused before anything was sent (BackendNotReady), so the approval wasn't really used."""
    with open(paths.used_onetime, "a", encoding="utf-8") as fh, _exclusive(fh):
        fh.write(json.dumps({"onetime_id": onetime_id, "released": True, "run_id": run_id,
                             "released_at": to_iso(now or utc_now())}) + "\n")
        fh.flush()


def payload_path(paths: Paths, run_id: str):
    return paths.state / "payloads" / f"{check_id(run_id, 'run')}.json"


def _check_config_is_current(paths: Paths, settings: dict, warrant: dict) -> None:
    """Refuse an organization configuration that isn't the current one. A signature proves the analyst signed a file
    once, not that it's the one in force: restoring an older signed copy could restore an older search destination.
    Checks: the revision and content the warrant was signed under (part of the signed warrant, so it can't be
    altered), and the newest revision searches have used here, with each revision's content fingerprint."""
    from . import org_config

    revision = settings["organization"]["revision"]
    digest = org_config.authority_hash(settings)
    context = warrant.get("config_context") or {}
    signed_under = ((context.get("values") or {}).get("organization") or {}).get("revision")
    if isinstance(signed_under, int):
        if revision < signed_under:
            raise RuntimeError(f"the organization configuration is revision {revision}, older than revision "
                               f"{signed_under} that this warrant was signed under (was an older copy restored?)")
        if revision == signed_under and context.get("sha256") and context["sha256"] != digest:
            raise RuntimeError(f"the organization configuration is revision {revision}, but not the revision {revision} "
                               "this warrant was signed under (different content under the same number)")
    # One transaction across processes: read the record, check against it, and update it under a lock, so a
    # concurrent check can't write back an older state over a newer one.
    with org_config.FileLock(paths.state / org_config.REVISIONS_FILE):
        try:
            state = org_config.used_revisions(paths)
        except org_config.RevisionStateError as exc:
            raise RuntimeError(str(exc)) from exc
        if revision < state["newest"]:
            raise RuntimeError(f"the organization configuration is revision {revision}, but searches here have already "
                               f"used revision {state['newest']} (was an older copy restored?). Re-sign the configuration "
                               "you want with `lucid-warrant config sign`.")
        known = state["digests"].get(str(revision))
        if known and known != digest:
            raise RuntimeError(f"the organization configuration is a different revision {revision} from the one searches "
                               "here already used (different content under the same number). Re-sign the configuration "
                               "you want with `lucid-warrant config sign`.")
        if not known:
            org_config.record_revision(paths, revision, digest)


def _save_payload(paths, run_id, sent_at, payload, service_id, count, error, lint_record, *, profile=None,
                  request=None) -> None:
    record = {"run_id": run_id, "sent_at": sent_at, "service_query_id": service_id,
              "result_count": count, "error": error, "lint": lint_record, "payload": payload}
    if profile is not None:
        record["profile"] = profile
    if request is not None:
        record["request_sent"] = request  # the actual request, credentials redacted
    atomic_write(payload_path(paths, run_id), canonical_bytes(record))


def load_payload(paths: Paths, run_id: str) -> dict | None:
    """The exact payload sent for a run, or None if nothing was sent (refused runs)."""
    try:
        return json.loads(payload_path(paths, run_id).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def record_refused_request(paths: Paths, *, warrant_id, skeleton, bindings, reason: str, now=None) -> str:
    """Log a syntax request that was refused (bad warrant, failed lint), so the analyst sees what the agent tried.
    Returns the log entry's run ID."""
    run_id = new_id("run")
    _audit(paths, {"run_id": run_id, "kind": "syntax_request", "warrant_id": str(warrant_id or ""),
                   "skeleton": str(skeleton or ""), "bindings": bindings if isinstance(bindings, dict) else {},
                   "term_ids": sorted(set(v for v in (bindings or {}).values() if isinstance(v, str))) if isinstance(bindings, dict) else [],
                   "outcome": "denied", "reason": reason}, now)
    return run_id


def payload_record(paths: Paths, run_id) -> tuple[dict | None, str | None]:
    """(saved payload record, problem). problem is None when it was read, "missing" when there's no file, and
    "unreadable: …" when the file exists but can't be used. A missing file is not proof that nothing was sent."""
    try:
        path = payload_path(paths, run_id)
    except (ValueError, TypeError) as exc:
        return None, f"unreadable: bad run ID ({exc})"
    if not path.exists():
        return None, "missing"
    try:
        record = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"unreadable: {exc}"
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        return None, "unreadable: not a payload record"
    return record, None


def run_term_ids(paths: Paths, entry: dict) -> list[str]:
    """The term IDs a run used: recorded ones, or recovered from its bindings for runs logged before term IDs were."""
    term_ids = entry.get("term_ids")
    if term_ids is None:
        bindings = entry.get("bindings")
        if not isinstance(bindings, dict):
            onetime = entry.get("onetime_id")
            bindings = _onetime_bindings(paths, onetime) if isinstance(onetime, str) else {}
        try:
            term_ids = _term_ids(bindings or {})
        except (TypeError, AttributeError, ValueError):
            term_ids = []
    if not isinstance(term_ids, list):
        return []
    return [t for t in term_ids if isinstance(t, str)]


def template_usage(paths: Paths) -> dict[str, dict]:
    """Per template ID: successful queries, records returned, last used."""
    usage: dict[str, dict] = {}
    for entry in read_audit(paths, limit=None):
        tid = entry.get("template_id")
        if entry.get("kind") != "template" or not tid or entry.get("outcome") != "ok":
            continue
        u = usage.setdefault(tid, {"queries": 0, "records": 0, "last_used": None})
        u["queries"] += 1
        u["records"] += entry.get("count", 0)
        u["last_used"] = entry["ts"]
    return usage


def used_onetime_ids(paths: Paths) -> set[str]:
    """IDs recorded as used. Unreadable lines are skipped here; see ledger_problems() (execution fails closed)."""
    return _read_ledger(paths)[0]


def ledger_problems(paths: Paths) -> list[str]:
    return _read_ledger(paths)[1]


def _read_ledger(paths: Paths) -> tuple[set[str], list[str]]:
    if not paths.used_onetime.exists():
        return set(), []
    used, problems = set(), []
    for number, line in enumerate(paths.used_onetime.read_text("utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry_id, released = _ledger_entry(line)
            (used.discard if released else used.add)(entry_id)
        except ValueError as exc:
            problems.append(f"single-use ledger line {number}: {exc}")
    return used, problems


def _ledger_id(line: str) -> str:
    return _ledger_entry(line)[0]


def _ledger_entry(line: str) -> tuple[str, bool]:
    """(approval ID, whether this line releases it) for one ledger line; ValueError unless well formed."""
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError("not a JSON object")
    onetime_id = record.get("onetime_id")
    if not isinstance(onetime_id, str):
        raise ValueError("missing onetime_id")
    check_id(onetime_id, "ot")
    released = record.get("released", False)
    if not isinstance(released, bool):
        raise ValueError("released must be true or false")
    return onetime_id, released


def _audit(paths: Paths, entry: dict, now) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": to_iso(now or utc_now()), **entry}, ensure_ascii=False)
    with open(paths.audit_log, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def term_usage(paths: Paths, warrant_id: str) -> dict[str, dict]:
    """Per term ID under one warrant: queries run, records returned, refused attempts, last use.

    A query counts once per term, even if the term fills two slots. Refused
    attempts include term IDs the warrant never approved.
    """
    usage: dict[str, dict] = {}
    for entry in read_audit(paths, limit=None):
        if entry.get("warrant_id") != warrant_id:
            continue
        term_ids = entry.get("term_ids")
        if term_ids is None:  # entries written before term_ids was recorded
            term_ids = _term_ids(entry.get("bindings") or _onetime_bindings(paths, entry.get("onetime_id")))
        for term_id in term_ids:
            u = usage.setdefault(term_id, {"queries": 0, "records": 0, "refused": 0, "last_used": None})
            if entry.get("outcome") == "ok":
                u["queries"] += 1
                u["records"] += entry.get("count", 0)
                u["last_used"] = entry["ts"]
            elif entry.get("outcome") == "denied":
                u["refused"] += 1
    return usage


def _onetime_bindings(paths: Paths, onetime_id) -> dict:
    """Bindings of a one-time approval, for display only (signature not checked)."""
    try:
        bindings = json.loads((paths.onetime / f"{check_id(onetime_id, 'ot')}.json").read_text("utf-8")).get("bindings", {})
        return bindings if isinstance(bindings, dict) else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def read_audit(paths: Paths, limit: int | None = 200) -> list[dict]:
    """Audit entries, oldest first. Unreadable lines are skipped; see audit_problems()."""
    return _read_audit(paths, limit)[0]


def audit_problems(paths: Paths) -> list[str]:
    """Lines of the audit log that couldn't be parsed (a torn last line, or corruption)."""
    return _read_audit(paths, None)[1]


def _read_audit(paths: Paths, limit: int | None) -> tuple[list[dict], list[str]]:
    if not paths.audit_log.exists():
        return [], []
    lines = paths.audit_log.read_text("utf-8", errors="replace").splitlines()
    if limit is not None:
        lines = lines[-limit:]
    entries, problems = [], []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or "ts" not in entry or "outcome" not in entry:
                raise ValueError("missing fields")
            from_iso(entry["ts"])  # a bad timestamp would break every view that shows it
            entries.append(entry)
        except (ValueError, TypeError) as exc:
            where = "last line (possibly an interrupted write)" if number == len(lines) else f"line {number}"
            problems.append(f"audit log {where}: {exc}")
    return entries, problems
