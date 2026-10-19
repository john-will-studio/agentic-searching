"""Command line for the agent (and for setup).

Agent commands print JSON. None of them accept parameter 1, parameter 2, or a
raw query string: those come only from signed files.
"""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
from pathlib import Path

from . import config_cli
from .config import Paths
from .executor import ExecutionDenied, queries_in_last_hour, record_refused_request, run_onetime, run_template, used_onetime_ids
from .library import LibraryError, describe_template, load_library, sign_seed_library
from .onetime import list_onetime, list_requests, submit_request
from .review import is_held, load_rejected
from .signing import (
    BadPassphrase,
    SignatureError,
    generate_keypair,
    keys_exist,
    load_private_key,
    load_public_key,
)
from .summaries import SummaryError
from .syntax import SyntaxRejected
from .util import check_id, stdin_is_terminal, to_iso, utc_now
from .warrant import WarrantError, list_warrants


def _utf8_output() -> None:
    """Agents read the JSON through a pipe, and on Windows a pipe defaults to the ANSI code page (cp1252): a path
    with "ü" came out as invalid UTF-8, and "→", "İstanbul" or Greek text crashed the command. Always write UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # replaced by something that isn't a text stream (tests, embedding)
            pass


def _print(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


class UsageError(ValueError):
    """Bad command-line arguments from the agent; reported as JSON like other refusals."""


def _bindings(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs or []:
        slot, sep, term_id = pair.partition("=")
        if not sep or not slot or not term_id:
            raise UsageError(f"--bind expects slot=term_id, got {pair!r}")
        if slot in out:
            raise UsageError(f"slot {slot!r} bound twice")
        out[slot] = term_id
    return out


def cmd_init(paths: Paths, args) -> int:
    """Set up a project folder for an agent (warrant directory, skill, AGENTS.md and CLAUDE.md sections),
    then create the signing key unless --no-key."""
    from .project import init_project

    result = init_project(Path(args.project or Path.cwd()))
    print(f"Project:            {result['project']}")
    print(f"Warrant directory:  {result['warrant_directory']}")
    print(f"Agent skill:        {result['skill']}")
    for name, what in result["instructions"].items():
        print(f"{name + ':':<20}{what} the Lucid Warrant section")
    paths = Paths.for_workspace(Path(result["warrant_directory"]))
    if args.no_key:
        if keys_exist(paths):
            print("Signing key already in place. Setup complete. Start your agent in the project folder.")
        else:
            print("Open the project in the GUI (About… → Open or create project…) to create your signing key.")
        return 0
    if keys_exist(paths) and paths.library.exists():
        print("Signing key and template library already in place.")
        print("Setup complete. Start your agent in the project folder.")
        return 0
    if not stdin_is_terminal():
        print("No terminal to ask for a passphrase: open the project in the GUI to create your signing key.")
        return 0
    created = not keys_exist(paths)
    if created:
        print(f"Creating a signing key in {paths.keys}.")
        while True:
            first = getpass.getpass("New passphrase (8+ characters): ")
            if first != getpass.getpass("Repeat passphrase: "):
                print("Passphrases differ; try again.")
                continue
            try:
                key_id = generate_keypair(paths, first)
                break
            except ValueError as exc:
                print(exc)
        print(f"Key created (id {key_id}).")
    else:
        first = getpass.getpass("Passphrase: ")
    if not paths.library.exists():
        sign_seed_library(paths, load_private_key(paths, first))
        print(f"Signed the seed template library at {paths.library}.")
    from .config_cli import _file_hash
    from .org_config import read_file, review_lines, unsigned_org_file

    unsigned = unsigned_org_file(paths)
    if unsigned:  # never signed as a side effect of creating a key: the analyst reviews it first
        print(f"\nThere's an organization file that isn't signed: {unsigned}")
        for line in review_lines(read_file(unsigned)):
            print(f"  {line}")
        print("Searches are refused until it's signed. If this is right, sign it with:\n"
              f"  lucid-warrant config sign --reason \"Reviewed at setup\" --if-hash {_file_hash(unsigned)}\n"
              "If it isn't, remove the file to use the built-in defaults.")
    print("Setup complete. Start your agent in the project folder.")
    return 0


def cmd_demo_inbox(paths: Paths, args) -> int:
    paths.inbox.mkdir(parents=True, exist_ok=True)
    for src, dst in (("example_nominations.json", paths.nominations), ("example_proposal.json", paths.proposal)):
        if dst.exists() and not args.force:
            print(f"{dst} exists; use --force to overwrite", file=sys.stderr)
            continue
        shutil.copyfile(paths.seed / src, dst)
        print(f"wrote {dst}")
    return 0


def cmd_where(paths: Paths, _args) -> int:
    from . import workspace

    env = workspace.overridden_by_environment()
    chosen, how = workspace.resolve()
    if env:
        chosen, how = None, "LUCID_WARRANT_DATA / LUCID_WARRANT_KEYS"
    elif chosen is None:
        how = "not set up: run `lucid-warrant init` in the project folder, or open the GUI once"
    _print({"warrant_directory": str(chosen) if chosen else None, "chosen_by": how,
            "keys": str(paths.keys), "data": str(paths.data), "inbox": str(paths.inbox)})
    return 0


def _countries_summary(d: dict) -> dict:
    from .scope import include_rows

    scope = d.get("country_scope") or {"include": [], "exclude": []}
    included = [r["code"] for r in include_rows(scope.get("include"))]
    return {"searched": included or "all countries except those excluded",
            "excluded": [{"code": r["code"], "reason": r["reason"]} for r in scope.get("exclude", [])],
            "note": "records whose country is missing or not a recognized two-letter code are excluded too"}


def _for_agent(result: dict) -> dict:
    """A run's result as the agent sees it: the long permitted-country list summarized (the exact payload is
    kept in the run record), and a plain word on what an empty result means."""
    from .scope import summarize_country_filter

    out = dict(result)
    if out.get("country_filter"):
        out["query"], out["country_filter"] = summarize_country_filter(out.get("query", ""), out["country_filter"])
    if out.get("count") == 0:
        out["note"] = ("Nothing within this warrant's scope matched. Lucid Warrant deliberately doesn't say whether "
                       "anything exists outside it (other countries, dates or visibility).")
    return out


def _warrant_summary(paths: Paths, w: dict) -> dict:
    if not w["doc"]:
        return {"warrant_id": w["warrant_id"], "status": w["status"], "error": w.get("error")}
    d = w["doc"]
    return {
        "warrant_id": w["warrant_id"], "status": w["status"], "expires_at": d["expires_at"], "purpose": d["purpose"],
        **({"replaces": d["supersedes"]} if d.get("supersedes") else {}),
        "max_rows_per_run": d["max_rows_per_run"], "max_queries_per_hour": d.get("max_queries_per_hour"),
        "queries_in_last_hour": queries_in_last_hour(paths, w["warrant_id"]),
        "search_window": (f"last {d['max_lookback_days']} days, up to the time of each search"
                          if d.get("max_lookback_days") else "no date restriction"),
        "max_lookback_days": d.get("max_lookback_days", 0), "blocked_date_ranges": d.get("blocked_date_ranges", []),
        "countries": _countries_summary(d),
        **({"places": [{k: p[k] for k in ("label", "lat", "lon", "radius_km", "country")} for p in d["places"]],
            "places_note": "every search is limited to these circles (the radius is the signed maximum)"}
           if d.get("places") else {}),
        "terms": [{"term_id": t["term_id"], "literal": t["literal"], **({"client_id": t["client_id"]} if t.get("client_id") else {}),
                   **({"match": "pattern"} if t.get("match") == "pattern" else {})}
                  for t in d["terms"]],
    }


MAX_PURPOSE = 2000
MAX_PROPOSAL_REASON = 500
WAIT_INTERVAL = 2.0


def cmd_propose(paths: Paths, args) -> int:
    """Propose a purpose and hit terms for the analyst (data/inbox/proposal.json), with every input checked."""
    from .review import held_rejections
    from .signing import atomic_write
    from .syntax import validate_literal
    from .warrant import normalize_client_id

    if not (args.purpose or args.term or args.withdraw or args.note is not None or args.place or args.withdraw_place):
        raise UsageError("give --purpose, --term (with --reason), --place (with --label and --reason), --note, "
                         "--withdraw or --withdraw-place")
    if args.term and args.place:
        raise UsageError("propose a term and a place in separate calls (each has its own --reason)")
    try:
        doc = json.loads(paths.proposal.read_text("utf-8")) if paths.proposal.exists() else {}
    except (OSError, ValueError):
        doc = {}  # an unreadable proposal is replaced; the analyst only ever sees valid ones
    if not isinstance(doc, dict):
        doc = {}
    terms = [t for t in doc.get("terms", []) if isinstance(t, dict) and isinstance(t.get("literal"), str)]
    notes = []
    if args.purpose is not None:
        purpose = " ".join(args.purpose.split())
        if not purpose or len(purpose) > MAX_PURPOSE:
            raise UsageError(f"the purpose must be 1 to {MAX_PURPOSE} characters")
        doc["purpose"] = purpose
    if args.note is not None:
        from .review import MAX_AGENT_NOTE

        note = " ".join(args.note.split())
        if len(note) > MAX_AGENT_NOTE:
            raise UsageError(f"a message is at most {MAX_AGENT_NOTE} characters: use it to ask for something (a wider "
                             "window, another country, a higher limit) or to point out new proposals. Write up what a "
                             "search found with `summarize RUN_ID --text ...` instead")
        if note:
            doc["note"], doc["note_at"] = note, to_iso(utc_now())
        else:
            doc.pop("note", None)  # an empty --note clears it
            doc.pop("note_at", None)
    if args.term is not None:
        literal = args.term
        if args.pattern:
            from .syntax import validate_pattern

            validate_pattern(literal)
        else:
            validate_literal(literal)
        reason = " ".join((args.reason or "").split())
        if not reason or len(reason) > MAX_PROPOSAL_REASON:
            raise UsageError(f"--reason is required (1 to {MAX_PROPOSAL_REASON} characters): say why the term matters")
        client_id = normalize_client_id(args.client_id)
        held = held_rejections(paths).get(literal)
        if held:
            when = (f"it may be proposed again after {held['renominate_after']}" if held.get("renominate_after")
                    else "it may not be proposed again")
            raise WarrantError(f"{literal!r} was rejected on {held.get('rejected_at', '?')}"
                               + (f" ({held['reason']})" if held.get("reason") else "") + f"; {when}")
        for w in (list_warrants(paths, load_public_key(paths)) if keys_exist(paths) else []):
            if w["status"] == "active" and w["doc"] and any(
                    t["literal"] == literal and (t.get("match") == "pattern") == args.pattern for t in w["doc"]["terms"]):
                term_id = next(t["term_id"] for t in w["doc"]["terms"] if t["literal"] == literal)
                notes.append(f"{literal!r} is already approved in {w['warrant_id']} as {term_id}")
        terms = [t for t in terms if t["literal"] != literal] + [
            {"literal": literal, "reason": reason, **({"client_id": client_id} if client_id else {}),
             **({"match": "pattern"} if args.pattern else {})}]
    for literal in args.withdraw or []:
        before = len(terms)
        terms = [t for t in terms if t["literal"] != literal]
        if len(terms) == before:
            notes.append(f"{literal!r} wasn't proposed")
    doc["terms"] = terms
    places = [p for p in doc.get("places", []) if isinstance(p, dict) and isinstance(p.get("label"), str)]
    if args.place is not None:
        places = [p for p in places if p["label"] != (args.label or "").strip()] + [_proposed_place(paths, args)]
    for label in args.withdraw_place or []:
        before = len(places)
        places = [p for p in places if p["label"] != label]
        if len(places) == before:
            notes.append(f"no place labelled {label!r} was proposed")
    if places:
        doc["places"] = places
    else:
        doc.pop("places", None)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.proposal, (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    _print({"proposal": doc, **({"warnings": notes} if notes else {}),
            "next": "the analyst reviews it in the GUI; run `wait --for warrant`"})
    return 0


def _proposed_place(paths: Paths, args) -> dict:
    """A place for the analyst to review: read like the GUI's converter reads it, stored as lat, lon."""
    from . import org_config
    from .geo import CoordinateError, parse_coordinate
    from .places import DEFAULT_RADIUS_KM, MAX_LABEL, MAX_REASON, MIN_RADIUS_KM

    try:
        point = parse_coordinate(args.place, args.place_form)
    except CoordinateError as exc:
        raise UsageError(f"--place: {exc}") from exc
    label = " ".join((args.label or "").split())
    if not 1 <= len(label) <= MAX_LABEL:
        raise UsageError(f"--label is required with --place (1 to {MAX_LABEL} characters), e.g. \"Istanbul office\"")
    reason = " ".join((args.reason or "").split())
    if not 1 <= len(reason) <= MAX_REASON:
        raise UsageError(f"--reason is required with --place (1 to {MAX_REASON} characters): say why this place matters")
    try:
        cap = org_config.load(paths, user=False)["geography"]["max_radius_km"]
    except org_config.ConfigError:
        cap = 25
    radius = DEFAULT_RADIUS_KM if args.radius_km is None else args.radius_km
    if not MIN_RADIUS_KM <= radius <= cap:
        raise UsageError(f"--radius-km must be {MIN_RADIUS_KM} to {cap:g} (the default is {DEFAULT_RADIUS_KM}, 100 m)")
    country = (args.country or "").strip().upper()
    if country and len(country) != 2:
        raise UsageError("--country is a two-letter code, e.g. TR")
    return {"label": label, "lat": round(point.lat, 6), "lon": round(point.lon, 6), "radius_km": radius,
            "reason": reason, "given_as": args.place.strip(), "read_as": point.form,
            **({"country_suggested": country} if country else {})}


def cmd_wait(paths: Paths, args) -> int:
    """Block until the analyst acts: a newly signed (or replacement) warrant, or a decision on a syntax request.
    Exit 0 with what happened; exit 3 on timeout."""
    import time

    from .onetime import list_onetime

    if not 1 <= args.timeout <= 86400:
        raise UsageError("--timeout must be 1 to 86400 seconds")
    if args.kind == "request":
        if not args.request:
            raise UsageError("--for request needs --request req-…")
        check_id(args.request, "req")
    public_key = load_public_key(paths)
    deadline = time.monotonic() + args.timeout
    started = time.monotonic()

    def active_ids():
        return {w["warrant_id"]: w for w in list_warrants(paths, public_key) if w["status"] == "active"}

    baseline = set(active_ids())
    while True:
        if args.kind == "warrant":
            current = active_ids()
            new = [w for wid, w in sorted(current.items()) if wid not in baseline]
            if new:
                _print({"event": "warrant_signed", "warrants": [_warrant_summary(paths, w) for w in new],
                        "waited_seconds": round(time.monotonic() - started)})
                return 0
        else:
            approval = next((o for o in list_onetime(paths, public_key)
                             if "invalid" not in o and o.get("request_id") == args.request), None)
            if approval:
                _print({"event": "request_approved", "request_id": args.request, "onetime_id": approval["onetime_id"],
                        "query": approval.get("query"), "next": f"run-onetime {approval['onetime_id']} (runs once)",
                        "waited_seconds": round(time.monotonic() - started)})
                return 0
            if (paths.syntax_requests / "rejected" / f"{args.request}.json").exists():
                _print({"event": "request_rejected", "request_id": args.request,
                        "waited_seconds": round(time.monotonic() - started)})
                return 0
            if not (paths.syntax_requests / f"{args.request}.json").exists():
                _print({"event": "request_not_found", "request_id": args.request})
                return 2
        if time.monotonic() >= deadline:
            _print({"event": "timeout", "waited_seconds": round(time.monotonic() - started),
                    "next": "the analyst hasn't acted yet; check `status`, tell the user, or wait again"})
            return 3
        time.sleep(min(WAIT_INTERVAL, max(deadline - time.monotonic(), 0.05)))


def _proposal_state(paths: Paths, public_key) -> dict | None:
    """Your current proposal, with each term marked approved (and its term ID), rejected, or pending review."""
    try:
        doc = json.loads(paths.proposal.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    approved = {}
    for w in list_warrants(paths, public_key):
        if w["status"] == "active" and w["doc"]:
            for t in w["doc"]["terms"]:
                approved.setdefault(t["literal"], (t["term_id"], w["warrant_id"]))
    rejected = load_rejected(paths)
    terms = []
    for t in doc.get("terms", []):
        if not isinstance(t, dict) or not isinstance(t.get("literal"), str):
            continue
        literal = t["literal"]
        if literal in approved:
            state = {"state": "approved", "term_id": approved[literal][0], "warrant_id": approved[literal][1]}
        elif literal in rejected:
            state = {"state": "rejected", "reason": rejected[literal].get("reason", "")}
        else:
            state = {"state": "pending review"}
        terms.append({"literal": literal, **state})
    from .places import load_rejected_places, place_key

    rejected_places = load_rejected_places(paths)
    signed_places = {}
    for w in list_warrants(paths, public_key):
        if w["status"] == "active" and w["doc"]:
            for p in w["doc"].get("places") or []:
                signed_places.setdefault(place_key(p), w["warrant_id"])
    places = []
    for p in doc.get("places", []):
        if not isinstance(p, dict) or not isinstance(p.get("label"), str):
            continue
        try:
            key = place_key(p)
        except (KeyError, TypeError, ValueError):
            continue
        if key in signed_places:
            state = {"state": "approved", "warrant_id": signed_places[key]}
        elif key in rejected_places:
            state = {"state": "rejected", "reason": rejected_places[key].get("reason", "")}
        else:
            state = {"state": "pending review"}
        places.append({"label": p["label"], **state})
    return {"purpose": doc.get("purpose"), "note": doc.get("note"), "terms": terms,
            **({"places": places} if places else {})}


EXAMPLE_PAYLOAD = {
    "warrant_id": "w-EXAMPLE", "purpose": "Example purpose.", "parameter_1": "0012", "parameter_2": ["ATTR_1000"],
    "query": '(content:"EXAMPLE_TERM") AND timestamp:["2026-09-01T00:00:00Z" TO "2026-10-01T00:00:00Z"]', "limit": 50,
    "date_filter": {"field": "timestamp", "from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z", "exclude": []},
    "country_filter": {"field": "country", "include": ["TR", "US"], "exclude": ["DE"]},
}


def cmd_results(paths: Paths, args) -> int:
    """A run's saved results: their profile, or an export for investigation (parquet, csv, jsonl)."""
    from .executor import load_payload
    from .results import ExportError, export, results_path

    path = results_path(paths, args.run_id)
    if not path.exists():
        raise UsageError(f"no saved results for {args.run_id} (only searches that returned results have them)")
    if not args.format:
        saved = load_payload(paths, args.run_id) or {}
        _print({"run_id": args.run_id, "saved": str(path), "profile": saved.get("profile")})
        return 0
    out = Path(args.out or f"{args.run_id}.{args.format}")
    try:
        n = export(paths, args.run_id, args.format, out, overwrite=args.force)
    except (RuntimeError, ExportError) as exc:
        raise UsageError(str(exc)) from exc
    _print({"run_id": args.run_id, "exported": str(out.resolve()), "format": args.format, "records": n})
    return 0


def cmd_summarize(paths: Paths, args) -> int:
    """Attach your summary of a search's results to its run (shown in the Runs tab and the printed warrant)."""
    from .summaries import write_summary

    doc = write_summary(paths, args.run_id, args.text)
    _print({"summarized": doc["run_id"], "at": doc["at"], "characters": len(doc["text"])})
    return 0


def cmd_backend_check(paths: Paths, args) -> int:
    """Check the configured search service without sending a search: the configuration, the adapter, the secrets it
    needs, the exact request an example search would produce, and (with --sample-response) how a response maps."""
    from . import org_config
    from .backend import BackendError, HttpBackend, backend_from_settings, map_results

    settings = org_config.load(paths, user=False)
    spec = settings["search_backend"]
    report = {"type": spec["type"], "problems": []}
    try:
        backend = backend_from_settings(settings, paths.corpus)
    except BackendError as exc:
        report["problems"].append(str(exc))
        _print(report)
        return 2
    if isinstance(backend, HttpBackend):
        missing = backend.missing_secrets()
        report["secrets"] = {header: {"environment_variable": env, "set": env not in missing}
                             for header, env in spec["http"]["secret_headers"].items()}
        report["problems"] += [f"environment variable {env} isn't set" for env in missing]
        try:
            import os

            for env in missing:  # placeholders only for the preview; never sent
                os.environ[env] = "<unset>"
            url, method, headers, body = backend.request(EXAMPLE_PAYLOAD)
        finally:
            for env in missing:
                os.environ.pop(env, None)
        redacted = {k: ("<secret from " + spec["http"]["secret_headers"][k] + ">" if k in spec["http"]["secret_headers"] else v)
                    for k, v in headers.items()}
        report["example_request"] = {"method": method, "url": url, "headers": redacted,
                                     "body": json.loads(body) if body else None, "for_payload": EXAMPLE_PAYLOAD}
        if args.sample_response:
            try:
                sample = json.loads(Path(args.sample_response).read_text("utf-8"))
                results = map_results(sample, spec["http"])
                report["sample_results"] = results[:5]
                missing_country = sum(1 for r in results if "country" not in r)
                if missing_country:
                    report["problems"].append(f"{missing_country} sample result(s) have no country: the executor can't "
                                              "double-check the geographic constraint for them")
                rule = spec["http"]["service_query_id"]
                if rule.get("path"):
                    from .backend import _get_path
                    report["sample_service_query_id"] = _get_path(sample, rule["path"])
                elif not rule.get("header"):
                    report["problems"].append("no service_query_id rule: runs can't be matched to the service's logs")
            except (OSError, ValueError, BackendError) as exc:
                report["problems"].append(f"sample response: {exc}")
    elif spec["type"] == "python":
        report["adapter"] = spec["python"]["adapter"]
    else:
        report["practice_records"] = str(paths.corpus)
        report["note"] = "practice mode: searches go to the mock records, not a real service"
    report["ok"] = not report["problems"]
    _print(report)
    return 0 if report["ok"] else 2


def cmd_status(paths: Paths, args) -> int:
    public_key = load_public_key(paths)
    library = load_library(paths, public_key)
    used = used_onetime_ids(paths)
    _print({
        "warrants": [_warrant_summary(paths, w) for w in list_warrants(paths, public_key) if w["status"] != "expired"],
        "library_version": library["version"],
        "proposal": _proposal_state(paths, public_key),
        "templates": [({"template_id": t["template_id"], "slots": describe_template(library, t)["slots"],
                        "plain_english": describe_template(library, t)["plain_english"]} if args.brief
                       else describe_template(library, t))
                      for t in library["templates"] if t.get("enabled", True)],
        "disabled_templates": [t["template_id"] for t in library["templates"] if not t.get("enabled", True)],
        "pending_syntax_requests": [r["request_id"] for r in list_requests(paths)],
        # Why the analyst rejected terms, and when each may be nominated again (null: never).
        "rejected_terms": [{"literal": e["literal"], "reason": e.get("reason", ""), "rejected_at": e.get("rejected_at"),
                            "renominate_after": e.get("renominate_after"), "may_renominate_now": not is_held(e)}
                           for e in sorted(load_rejected(paths).values(), key=lambda e: e["literal"])],
        "onetime_approvals": [
            {"onetime_id": o["onetime_id"], "used": o["onetime_id"] in used, "query": o.get("query")}
            for o in list_onetime(paths, public_key)
            if "invalid" not in o
        ],
    })
    return 0


def cmd_run(paths: Paths, args) -> int:
    result = run_template(
        paths, warrant_id=args.warrant, template_id=args.template, bindings=_bindings(args.bind), limit=args.limit
    )
    _print(_for_agent(result))
    return 0


def cmd_run_onetime(paths: Paths, args) -> int:
    _print(_for_agent(run_onetime(paths, onetime_id=args.onetime_id, limit=args.limit)))
    return 0


def cmd_request_syntax(paths: Paths, args) -> int:
    bindings = _bindings(args.bind)
    try:
        request = submit_request(
            paths,
            load_public_key(paths),
            warrant_id=args.warrant,
            skeleton=args.skeleton,
            bindings=bindings,
            rationale=args.rationale,
        )
    except (SyntaxRejected, WarrantError, ValueError) as exc:
        run_id = record_refused_request(paths, warrant_id=args.warrant, skeleton=args.skeleton, bindings=bindings,
                                        reason=str(exc))
        _print({"denied": str(exc), "run_id": run_id})
        return 2
    _print({**request, "status": "pending human approval in the GUI"})
    return 0


class _JsonArgumentParser(argparse.ArgumentParser):
    """Argument errors become exceptions, so they can be reported as JSON like every other refusal."""

    def error(self, message):
        raise UsageError(f"{self.prog}: {message}")


def main(argv=None) -> int:
    _utf8_output()
    parser = _JsonArgumentParser(prog="lucid-warrant", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="set up a project folder for an agent; creates the signing key only if there "
                                       "isn't one (asks for a passphrase), and leaves an existing key alone")
    init.add_argument("project", nargs="?", help="the project folder (default: the current folder)")
    init.add_argument("--no-key", action="store_true", help="skip creating the signing key (create it in the GUI)")
    propose = sub.add_parser("propose", help="propose a purpose and hit terms for the analyst to review")
    propose.add_argument("--purpose", help="what the research is for")
    propose.add_argument("--term", help="a hit term: the exact text to search for")
    propose.add_argument("--reason", help="why the term matters (required with --term)")
    propose.add_argument("--pattern", action="store_true",
                         help="--term is a pattern: * any characters, ? one character, after at least 3 ordinary "
                              "characters (e.g. PROJECT*); the analyst approves it as a pattern")
    propose.add_argument("--client-id", help="the client the term is associated with")
    propose.add_argument("--withdraw", action="append", metavar="TERM", help="remove a proposed term (repeatable)")
    propose.add_argument("--place", metavar="COORDINATE",
                         help='a place to limit searches to: lat, lon (e.g. "41.0082, 28.9784"), MGRS or UTM; needs '
                              "--label and --reason")
    propose.add_argument("--place-form", choices=["auto", "latlon", "lonlat", "mgrs", "utm"], default="auto",
                         help="how to read --place (default: detect)")
    propose.add_argument("--radius-km", type=float, help="the place's search radius in km (default 0.1, i.e. 100 m)")
    propose.add_argument("--label", help="a short name for the place, e.g. \"Istanbul office\"")
    propose.add_argument("--country", help="the two-letter country the place is in (a suggestion; the analyst confirms)")
    propose.add_argument("--withdraw-place", action="append", metavar="LABEL", help="remove a proposed place")
    propose.add_argument("--note", help="a short message to the analyst (at most 280 characters): ask for a wider "
                                        "window, country or limit, or point out new proposals; replaces any earlier "
                                        "message (\"\" clears it). Write up results with `summarize` instead")
    results = sub.add_parser("results", help="a run's saved results: their profile, or export them for investigation")
    results.add_argument("run_id")
    results.add_argument("--format", choices=["parquet", "csv", "jsonl"], help="export in this format (parquet needs pyarrow)")
    results.add_argument("--out", help="where to write the export (default RUN_ID.FORMAT in the current folder)")
    results.add_argument("--force", action="store_true", help="replace the --out file if it exists")
    summarize = sub.add_parser("summarize", help="attach your summary of a search's results to its run")
    summarize.add_argument("run_id")
    summarize.add_argument("--text", required=True, help="what the records show, citing record IDs (at most 2000 characters)")
    wait = sub.add_parser("wait", help="wait for the analyst: a newly signed warrant, or a decision on a syntax request")
    wait.add_argument("--for", dest="kind", choices=["warrant", "request"], default="warrant")
    wait.add_argument("--request", help="the req-… ID (with --for request)")
    wait.add_argument("--timeout", type=int, default=1800, help="seconds to wait (1 to 86400; default 1800)")
    demo = sub.add_parser("demo-inbox", help="practice only: copy example nominations and a proposal into the inbox "
                                             "(never in a real case)")
    demo.add_argument("--force", action="store_true")
    status = sub.add_parser("status", help="active warrants, your proposal's state, templates, requests, rejections")
    status.add_argument("--brief", action="store_true", help="templates as id and slots only")
    sub.add_parser("where", help="show the warrant directory and folders in use")
    backend = sub.add_parser("backend", help="the search service Lucid Warrant sends searches to (administrators)")
    bs = backend.add_subparsers(dest="backend_cmd", required=True)
    check = bs.add_parser("check", help="check the configured search service without sending a search")
    check.add_argument("--sample-response", help="a saved JSON response from your search API, to test the mapping")

    run = sub.add_parser("run", help="run a library template under a warrant")
    run.add_argument("--warrant", required=True)
    run.add_argument("--template", required=True)
    run.add_argument("--bind", action="append", metavar="SLOT=TERM_ID", help="repeat for each slot")
    run.add_argument("--limit", type=int)

    one = sub.add_parser("run-onetime", help="run an approved one-time query (single use)")
    one.add_argument("onetime_id")
    one.add_argument("--limit", type=int)

    req = sub.add_parser("request-syntax", help="ask the human to approve new syntax for one run")
    req.add_argument("--warrant", required=True)
    req.add_argument("--skeleton", required=True, help="e.g. '{a} AND ({b} OR {c})'")
    req.add_argument("--bind", action="append", metavar="SLOT=TERM_ID")
    req.add_argument("--rationale", required=True)

    config_cli.add_parser(sub)
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(raw)
    except UsageError as exc:
        if raw[:1] == ["config"]:
            _print({"ok": False, "command": "config", "error": {"code": "USAGE", "path": "", "message": str(exc)}})
        else:
            _print({"error": str(exc)})
        return 2
    paths = Paths.default()
    if args.cmd == "config":
        return config_cli.run(args, paths)
    handlers = {
        "init": cmd_init,
        "demo-inbox": cmd_demo_inbox,
        "where": cmd_where,
        "summarize": cmd_summarize,
        "results": cmd_results,
        "backend": cmd_backend_check,
        "propose": cmd_propose,
        "wait": cmd_wait,
        "status": cmd_status,
        "run": cmd_run,
        "run-onetime": cmd_run_onetime,
        "request-syntax": cmd_request_syntax,
    }
    try:
        return handlers[args.cmd](paths, args)
    except ExecutionDenied as exc:
        _print({"denied": str(exc), **({"run_id": exc.run_id} if exc.run_id else {})})
        return 2
    except (SignatureError, WarrantError, LibraryError, SyntaxRejected, BadPassphrase, UsageError, SummaryError) as exc:
        _print({"error": str(exc)})
        return 2
    except (OSError, ValueError) as exc:  # e.g. an unreadable data file: report it, don't dump a traceback
        _print({"error": f"{type(exc).__name__}: {exc}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
