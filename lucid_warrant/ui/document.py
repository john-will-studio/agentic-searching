"""The warrant as a document: one model, rendered on screen (for signing) and as HTML (for printing).

Both renderings come from the same blocks, so what is printed is what was shown at signing.
"""

from __future__ import annotations

import html
import json
import tkinter as tk
from tkinter import font as tkfont

from ..scope import include_rows
from . import theme
from .term_dialog import matching, term_label
from .widgets import local_time

SOURCE_LABELS = {
    "script": "Script nomination",
    "agent": "Agent proposal",
    "human": "Analyst",
    "human-edited": "Analyst-edited nomination",
}
# What each origin means, completing "… means a hit term {meaning}." in the Definitions section.
SOURCE_MEANING = {"script": "proposed by the term-nomination script", "agent": "proposed by the AI agent",
                  "human": "entered by the signing analyst",
                  "human-edited": "nominated by the script or the agent and then changed by the signing analyst"}


def source_label(source: str, labels: dict | None = None) -> str:
    return (labels or SOURCE_LABELS).get(source, source)


# --- model -------------------------------------------------------------------------------

WITHHELD_REASONS = {"countries": "outside the permitted countries", "places": "outside every included place",
                    "no_location": "without a usable location (under a place limit)"}


def withheld_text(total: int, detail: dict | None) -> str:
    """How many returned records were held back from the agent, and why."""
    parts = [f"{n} {WITHHELD_REASONS.get(k, k)}" for k, n in (detail or {}).items() if isinstance(n, int) and n]
    if not parts:  # recorded before the reasons were kept separately
        return f"{total} returned record(s) from outside the geographic constraint"
    return f"{total} returned record(s): " + "; ".join(parts)


from ..warrant import PURPOSE_ORIGINS  # noqa: E402


def glance_blocks(doc: dict, *, countries: dict, labels: dict, hits: str, replaces: str | None, new_literals,
                  removed, old_places: list | None, old_terms: list | None = None) -> list[tuple]:
    """What this authorizes, in one place, before the formal sections: what a signer most needs to see. When it
    replaces a warrant, what changed, with a widened scope said plainly."""
    places = doc.get("places") or []
    scope = doc.get("country_scope") or {"include": [], "exclude": []}
    included = [r["code"] for r in include_rows(scope["include"])]
    excluded = [r["code"] for r in scope["exclude"]]
    countries_text = (f"Only {', '.join(included)}" if included else "All countries") + (
        f", except {', '.join(excluded)}" if excluded else "")
    rows, per_hour = doc["max_rows_per_run"], doc.get("max_queries_per_hour")
    limits = (f"Up to {rows:,} records per search" + (f", {per_hour:,} searches an hour (so up to {rows * per_hour:,} "
              "records an hour)" if per_hour else "") + f". Expires {local_time(doc['expires_at'])}.")
    literals = [t["literal"] for t in doc["terms"]]
    shown = ", ".join(literals[:8]) + (f" and {len(literals) - 8} more" if len(literals) > 8 else "")
    patterns = [t["literal"] for t in doc["terms"] if t.get("match") == "pattern"]
    pattern_note = (f" ({len(patterns)} {'pattern' if len(patterns) == 1 else 'patterns'}: "
                    + ", ".join(patterns[:8]) + (f" and {len(patterns) - 8} more" if len(patterns) > 8 else "")
                    + ")") if patterns else ""
    glance = [
        ("Authorization", f"{named(doc['parameter_1'], labels.get('p1', {}))}; visibility "
                          + ", ".join(named(v, labels.get("p2", {})) for v in doc["parameter_2"])),
        ("Purpose", doc["purpose"] if len(doc["purpose"]) <= 220 else doc["purpose"][:220] + "…"),
        (hits, f"{len(literals)}{pattern_note}: {shown}"),
        ("Places", "; ".join(f"{p['label']} ({p['radius_km'] * 1000:g} m)" for p in places) if places
         else "No site restriction: searches may cover anywhere in the permitted countries"),
        ("Countries", countries_text),
        ("Limits", limits),
        ("Search window", (f"Last {doc['max_lookback_days']} days" if doc.get("max_lookback_days") else "No date restriction")),
    ]
    blocks = [("heading", "At a glance"), ("meta", glance)]
    if replaces:
        old_by_label = {p["label"]: p for p in old_places or []}
        new_by_label = {p["label"]: p for p in places}
        old_labels, new_labels = set(old_by_label), set(new_by_label)
        changes = []
        old_by_id = {t.get("term_id"): t for t in old_terms or [] if t.get("term_id")}
        for term in doc["terms"]:
            before = old_by_id.get(term.get("term_id"))
            if before and (before["literal"], before.get("match", "exact")) != (term["literal"], term.get("match", "exact")):
                changes.append(f"{hits} changed: {before['literal']} ({before.get('match', 'exact')}) → "
                               f"{term['literal']} ({term.get('match', 'exact')})")
        changed = []
        for label in sorted(old_labels & new_labels):
            before, after = old_by_label[label], new_by_label[label]
            parts = []
            if after["radius_km"] != before["radius_km"]:
                wider = after["radius_km"] > before["radius_km"]
                parts.append(f"radius {before['radius_km'] * 1000:g} m → {after['radius_km'] * 1000:g} m"
                             + (" (WIDER)" if wider else ""))
            if (after["lat"], after["lon"]) != (before["lat"], before["lon"]):
                from ..geo import distance_km

                moved = distance_km(before["lat"], before["lon"], after["lat"], after["lon"])
                parts.append(f"centre moved {moved * 1000:,.0f} m")
            if after.get("country") != before.get("country"):
                parts.append(f"country {before.get('country')} → {after.get('country')}")
            if parts:
                changed.append(f"{label} ({'; '.join(parts)})")
        if new_literals:
            changes.append(f"{hits} added: " + ", ".join(sorted(new_literals)))
        if removed:
            changes.append(f"{hits} removed: " + ", ".join(removed))
        if new_labels - old_labels:
            changes.append("Places added: " + ", ".join(sorted(new_labels - old_labels)))
        if old_labels - new_labels:
            changes.append("Places removed: " + ", ".join(sorted(old_labels - new_labels)))
        if changed:
            changes.append("Places changed: " + "; ".join(changed))
        if old_labels and not places:
            changes.append(f"SITE RESTRICTION REMOVED: {replaces} was limited to {len(old_labels)} place(s); this one "
                           "searches anywhere in the permitted countries")
        blocks.append(("note", f"Changes from {replaces}: " + ("; ".join(changes) if changes else
                                                               "none to terms or places (limits may differ)")))
    blocks.append(("rule",))
    return blocks


def named(code, table):
    """A code with its name from `table` ("TR — Turkey"), or the code alone."""
    name = table.get(code)
    return f"{code} — {name}" if name and name != code else code


def _places_blocks(doc: dict, countries) -> list:
    """Places: each signed with its own maximum search radius."""
    places = doc.get("places") or []
    if not places:
        return [("note", "No places: searches aren't limited to particular sites.")]
    from ..places import clause, clause_template

    template = clause_template(doc)
    return [
        ("table", ["Place", "Lat, lon (WGS84)", "Maximum radius", "Country", "Reason"],
         [[p["label"], f"{p['lat']:.6f}, {p['lon']:.6f}", f"{p['radius_km']:g} km ({p['radius_km'] * 1000:g} m)",
           named(p["country"], countries), p["reason"]] for p in places]),
        ("note", "Every search is limited to records within these circles, written for the search service as "
                 f"{clause(template, places[0])}" + (" (one per place, any of them)" if len(places) > 1 else "")
                 + ". Any returned record whose coordinates fall outside every circle, or that has no usable "
                   "coordinates, is withheld from the agent."),
    ]


def warrant_blocks(doc: dict, *, labels: dict, countries: dict, library_line: str, status: str = "UNSIGNED DRAFT",
                   replaces: str | None = None, signature: dict | None = None,
                   new_literals=frozenset(), removed=(), sources: dict | None = None,
                   param_names: tuple[str, str] = ("Search authorization", "Data visibility"),
                   terms: dict | None = None, old_places: list | None = None, old_terms: list | None = None) -> list[tuple]:
    """Blocks: ("title", text) ("meta", [(label, value)]) ("heading", text) ("para", text)
    ("note", text) ("table", headers, rows) ("checks", [text]) ("definitions", [(term, meaning)]) ("rule",)"""
    p1, p2 = doc["parameter_1"], doc["parameter_2"]
    word = ((terms or {}).get("warrant") or {}).get("singular", "Warrant")
    hit, hits = (((terms or {}).get("hit_term") or {}).get(k, d) for k, d in (("singular", "Hit term"), ("plural", "Hit terms")))
    client = ((terms or {}).get("client_id") or {}).get("singular", "Client ID")
    blocks: list[tuple] = [
        ("title", f"Search {word}"),
        ("subtitle", f"Authorization for agent-run searches · {status}"),
        ("meta", [(f"{word} No.", doc["warrant_id"]), ("Issued", local_time(doc["created_at"])),
                  ("Expires", f"{local_time(doc['expires_at'])}")]
         + ([("Replaces", replaces)] if replaces else [])),
        ("rule",),
        *glance_blocks(doc, countries=countries, labels=labels, hits=hits, replaces=replaces,
                       new_literals=new_literals, removed=removed, old_places=old_places, old_terms=old_terms),
        ("heading", "I. Definitions"),
        ("para", f"In this {word.lower()}:"),
        ("definitions", [
            (hit, "means text included in Section IV that the agent may search for, either as exact text or as a marked pattern."),
            ("Pattern", "means a word in which * matches any run of characters (including none) and ? matches exactly "
             "one character, within one word. At least three ordinary characters come before the first wildcard. "
             "For example, PROJECT* finds any word starting with PROJECT. Exact text does not authorize wildcards."),
            (client, f"means the identifier, from the client database, of the client a {hit.lower()} is associated with. "
                     "It is recorded for the audit trail and is not added to searches."),
            ("Term ID", f"means the identifier of a {hit.lower()} in this {word.lower()}; records of each search use it."),
            ("Origin", f"means who proposed a {hit.lower()}, as defined below."),
        ] + [(label, f"means a {hit.lower()} {SOURCE_MEANING[code]}.")
             for code, label in (sources or SOURCE_LABELS).items() if code in SOURCE_MEANING] + [
            ("Provenance / reason", f"means where a {hit.lower()} came from, or why it was included."),
            ("Agent", f"means the AI agent that runs searches under this {word.lower()}."),
        ]),
        ("heading", "II. Authorization"),
        ("meta", [(f"{param_names[0]} (parameter 1)", named(p1, labels.get("p1", {}))),
                  (f"{param_names[1]} (parameter 2)", "; ".join(named(v, labels.get("p2", {})) for v in p2))]),
        ("heading", "III. Purpose"),
        ("para", doc["purpose"]),
        *([("note", "Authorship: " + PURPOSE_ORIGINS[doc["purpose_origin"]] + ".")]
          if doc.get("purpose_origin") in PURPOSE_ORIGINS else []),
        ("note", f"The agent conducts its searches for this purpose, and it is sent with every search in the payload. "
                 f"The signing analyst has reviewed it for compliance and intent. It is not checked automatically: what the "
                 f"agent can search is limited by Sections II and IV–VI, which are enforced on every search."),
        ("heading", f"IV. Included {hits.title()}"),
        ("table", ["No.", hit, "Matching", client, "Origin", "Term ID", "Provenance / reason"],
         [[str(i), t["literal"] + ("  [new]" if t["literal"] in new_literals else ""), matching(t), t.get("client_id") or "—",
           source_label(t["source"], sources), t["term_id"], t.get("provenance", "")]
          for i, t in enumerate(doc["terms"], start=1)]),
        ("note", "Column terms are defined in Section I."),
    ]
    if removed:
        blocks.append(("note", f"Removed from the replaced {word.lower()}: " + ", ".join(f"“{x}”" for x in removed)))
    scope = doc.get("country_scope") or {"include": [], "exclude": []}
    blocks += [
        ("heading", "V. Geographic Constraint"),
        ("table", ["Included country", "Reason"], [[named(r["code"], countries), r["reason"] or "—"]
                                                   for r in include_rows(scope["include"])])
        if scope["include"] else ("note", "Included countries: all, except those excluded below."),
        ("table", ["Excluded country", "Reason"], [[named(r["code"], countries), r["reason"]] for r in scope["exclude"]])
        if scope["exclude"] else ("note", "No countries are excluded."),
        *([("note", "Every search is limited to an explicit list of permitted country codes, so records whose country "
                    "is missing or not a recognized two-letter code are excluded too. Any returned record from outside "
                    "the permitted countries is withheld from the agent.")] if scope["include"] or scope["exclude"] else []),
        *_places_blocks(doc, countries),
        ("heading", "VI. Limits"),
        ("meta", [("Expires", local_time(doc["expires_at"])),
                  ("Maximum records per search", f"{doc['max_rows_per_run']:,}"),
                  ("Maximum searches per hour", f"{doc['max_queries_per_hour']:,} in any rolling hour"
                   if doc.get("max_queries_per_hour") else "Not limited (signed before this limit existed)"),
                  ("Search window", f"Last {doc['max_lookback_days']} days, up to the time of each search (records dated later are excluded)"
                   if doc.get("max_lookback_days") else "No date restriction")]),
    ]
    ranges = doc.get("blocked_date_ranges") or []
    blocks.append(("table", ["Blocked from (UTC)", "Blocked to (UTC, inclusive)"], [[r["start"], r["end"]] for r in ranges])
                  if ranges else ("note", "No date ranges are blocked."))
    blocks += [
        ("heading", f"VII. What This {word.title()} Authorizes"),
        ("para", f"Until it expires, the agent may combine the {hits.lower()} in Section IV with any enabled template in "
                 f"{library_line}, under the authorization, geographic constraint and limits above. Any other query syntax "
                 f"requires a separate one-time approval. Nothing outside this {word.lower()} is authorized."),
    ]
    certs = doc.get("certifications") or []
    blocks.append(("heading", "VIII. Certifications"))
    if certs:
        blocks.append(("checks", [c["text"] for c in certs]))
        if any(c.get("accepted_at") for c in certs):
            blocks.append(("note", "Accepted " + local_time(next(c["accepted_at"] for c in certs if c.get("accepted_at")))))
    else:
        blocks.append(("note", "No certifications were recorded (signed before certifications existed)."))
    context = doc.get("config_context")
    if context:
        blocks.append(("note", f"Signed under organization configuration {context['values']['organization']['name']} "
                               f"revision {context['values']['organization']['revision']} (hash {context['sha256'][:16]}…)."))
    blocks += [("heading", "IX. Signature")]
    if signature:
        blocks.append(("meta", [("Signed with key", signature.get("key_id", "")), ("Signature", signature.get("status", ""))]))
    else:
        blocks.append(("note", f"Entering your passphrase signs this {word.lower()} with your key. The signature covers "
                               "every section above exactly as shown."))
    return blocks


OUTCOME_TEXT = {"ok": "Sent", "error": "Send attempted; the search service failed", "denied": "Blocked before sending"}


def compact_json(value, indent: int = 2, width: int = 96) -> str:
    """Pretty JSON, but lists of plain values (like 200 country codes) wrap across lines instead of one per line.
    Same content as json.dumps; only the layout differs."""
    def render(v, level):
        pad, inner = " " * (indent * level), " " * (indent * (level + 1))
        if isinstance(v, dict):
            if not v:
                return "{}"
            items = [f"{inner}{json.dumps(k, ensure_ascii=False)}: {render(x, level + 1)}" for k, x in v.items()]
            return "{\n" + ",\n".join(items) + "\n" + pad + "}"
        if isinstance(v, list) and v and all(not isinstance(x, (dict, list)) for x in v):
            parts = [json.dumps(x, ensure_ascii=False) for x in v]
            lines, line = [], ""
            for part in parts:
                if line and len(inner) + len(line) + len(part) + 2 > width:
                    lines.append(line.rstrip())
                    line = ""
                line += part + ", "
            lines.append(line.rstrip(", "))
            return "[" + parts[0] + "]" if len(parts) == 1 else "[\n" + "\n".join(inner + l for l in lines) + "\n" + pad + "]"
        if isinstance(v, list):
            return "[]" if not v else "[\n" + ",\n".join(inner + render(x, level + 1) for x in v) + "\n" + pad + "]"
        return json.dumps(v, ensure_ascii=False)
    return render(value, 0)


def payload_fingerprint(payload: dict) -> str:
    """SHA-256 of the payload in canonical JSON (sorted keys), to check a printed copy against the saved file."""
    import hashlib

    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _elide_country_lists(payload: dict, lists: dict[tuple, str]) -> dict:
    """The payload for printing, with a long derived allow-list replaced by a reference to its printed list.
    `lists` maps each list (as a tuple) to its label and gains new lists as they're seen."""
    country = payload.get("country_filter")
    if not (isinstance(country, dict) and country.get("include_derived") and isinstance(country.get("include"), list)
            and isinstance(country.get("field"), str)):
        return payload
    codes = tuple(str(c) for c in country["include"])
    label = lists.setdefault(codes, f"List {chr(ord('A') + len(lists))}")
    marker = f"<{label}: {len(codes)} permitted country codes, printed at the start of this addendum>"
    printed = {**payload, "country_filter": {**country, "include": marker}}
    clause = "(" + " OR ".join(f'{country["field"]}:"{c}"' for c in codes) + ")"
    if isinstance(payload.get("query"), str) and clause in payload["query"]:
        printed["query"] = payload["query"].replace(clause, f"({country['field']}: any code in {label})")
    return printed


def _elide_query(query: str, lists: dict[tuple, str], field: str = "country") -> str:
    """A logged query with any printed permitted-country list replaced by its name."""
    for codes, label in lists.items():
        clause = "(" + " OR ".join(f'{field}:"{c}"' for c in codes) + ")"
        if clause in query:
            query = query.replace(clause, f"({field}: any code in {label})")
    return query


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def clean_run(entry, record: dict | None, problem: str | None, term_ids: list[str], summary: dict | None = None) -> dict:
    """One run as plain, safe values, whatever shape its log entry and payload record are in."""
    entry = entry if isinstance(entry, dict) else {}
    bindings = entry.get("bindings") if isinstance(entry.get("bindings"), dict) else {}
    payload = record.get("payload") if isinstance(record, dict) and isinstance(record.get("payload"), dict) else None
    count = entry.get("count")
    return {
        "run_id": _text(entry.get("run_id")) or "(no run ID)", "ts": _text(entry.get("ts")),
        "kind": _text(entry.get("kind")), "template_id": _text(entry.get("template_id")),
        "skeleton": _text(entry.get("skeleton")), "requested_limit": entry.get("requested_limit"),
        "lint": entry.get("lint") if isinstance(entry.get("lint"), dict) else None,
        "withheld": entry.get("withheld") if isinstance(entry.get("withheld"), int) else 0,
        "withheld_detail": entry.get("withheld_detail") if isinstance(entry.get("withheld_detail"), dict) else {},
        "storage_problem": str(entry.get("storage_problem") or "")[:500],
        "onetime_id": _text(entry.get("onetime_id")),
        "outcome": entry["outcome"] if isinstance(entry.get("outcome"), str) else repr(entry.get("outcome")),
        "term_ids": [t for t in term_ids if isinstance(t, str)],
        "bindings": {str(k): str(v) for k, v in bindings.items()},
        "count": count if isinstance(count, int) and not isinstance(count, bool) else None,
        "service_query_id": _text(entry.get("service_query_id")) or (_text(record.get("service_query_id")) if payload else ""),
        "reason": _text(entry.get("reason")), "query": _text(entry.get("query")),
        "payload": payload, "sent_at": _text(record.get("sent_at")) if payload else "",
        "send_error": _text(record.get("error")) if payload else "", "payload_problem": problem,
        "profile": record["profile"] if payload and isinstance(record.get("profile"), dict) else None,
        "summary": summary if isinstance(summary, dict) and isinstance(summary.get("text"), str) else None,
    }


def _outcome(run: dict) -> str:
    outcome = run["outcome"]
    if outcome == "ok":
        return "Sent" + (f"; {run['count']} record{'s' if run['count'] != 1 else ''} returned" if run["count"] is not None else "")
    return OUTCOME_TEXT.get(outcome) or f"Unknown outcome ({outcome!r})"


def runs_addendum(doc: dict, runs: list[dict], *, printed_at: str, terms: dict | None = None,
                  log_problems=()) -> list[tuple]:
    """Addendum A: every search attempted under this warrant, a summary and then each run on its own page, oldest
    first. Built from the run log and saved payloads at print time; not part of what was signed. Claims only what
    the records show: "Sent" comes from the run log, and a payload is the executor's saved copy of what it sent."""
    word = ((terms or {}).get("warrant") or {}).get("singular", "Warrant")
    hits = ((terms or {}).get("hit_term") or {}).get("plural", "Hit terms")
    hit = ((terms or {}).get("hit_term") or {}).get("singular", "Hit term")
    by_id = {t["term_id"]: term_label(t) for t in doc["terms"]}
    runs = sorted(runs, key=lambda r: r["ts"])

    def search_name(r):
        if r["kind"] == "template":
            return f"Template {r['template_id'] or '?'}"
        if r["kind"] == "onetime":
            return f"One-time approval {r['onetime_id'] or '?'}"
        if r["kind"] == "syntax_request":
            return f"Syntax request {r['skeleton'] or '?'} (refused)"
        return "Unknown"

    def literals(r):
        return ", ".join(by_id.get(t, f"{t} (not in this {word.lower()})") for t in r["term_ids"]) or "—"

    blocks: list[tuple] = [
        ("pagebreak",),
        ("heading", f"Addendum A. Searches Run Under This {word.title()}"),
        ("note", f"Generated from the run records when this copy was printed ({printed_at}). The addendum is not part of "
                 f"the signed {word.lower()}: the signature covers Sections I–IX only."),
    ]
    if log_problems:
        blocks.append(("warning", f"The run log has {len(log_problems)} unreadable line(s), so this addendum may be "
                                  f"incomplete: " + "; ".join(log_problems[:3]) + ("…" if len(log_problems) > 3 else "")))
    if not runs:
        return blocks + [("para", f"No searches are recorded under this {word.lower()}.")]
    tally = {key: sum(1 for r in runs if r["outcome"] == key) for key in ("ok", "error", "denied")}
    unknown = len(runs) - sum(tally.values())
    blocks += [
        ("para", f"{len(runs)} search{'es' if len(runs) != 1 else ''} attempted: {tally['ok']} sent, "
                 f"{tally['error']} where the search service failed, {tally['denied']} blocked before sending"
                 + (f", {unknown} with an unknown outcome" if unknown else "")
                 + ". Each is detailed below on its own page, oldest first."),
        ("heading", "Summary"),
        ("table", ["No.", "Time", "Run ID", "Search", hits, "Outcome"],
         [[str(i), local_time(r["ts"]), r["run_id"], search_name(r), literals(r), _outcome(r)]
          for i, r in enumerate(runs, start=1)]),
    ]
    lists: dict[tuple, str] = {}
    for r in runs:
        if r["payload"] is not None:
            _elide_country_lists(r["payload"], lists)  # label the lists in the order they first appear
        elif r["query"]:  # a blocked run has no payload, but its logged query carries the same list
            import re

            for clause in re.findall(r'\((country:"[A-Z]{2}"(?: OR country:"[A-Z]{2}"){19,})\)', r["query"]):
                codes = tuple(re.findall(r'country:"([A-Z]{2})"', clause))
                lists.setdefault(codes, f"List {chr(ord('A') + len(lists))}")
    for codes, label in lists.items():
        blocks += [("subheading", f"{label}. Permitted country codes ({len(codes)})"),
                   ("note", "Every recognized two-letter code except those excluded in Section V. Searches send this "
                            "list in full, both in the query and in country_filter; it's printed once here and referred "
                            "to by name on each run's page."),
                   ("pre", ", ".join(codes))]
    for i, r in enumerate(runs, start=1):
        meta = [("Run ID", r["run_id"]), ("Time", local_time(r["ts"])), ("Outcome", _outcome(r)), ("Search", search_name(r)),
                (hits, "; ".join(f"{by_id.get(t, f'not in this {word.lower()}')} ({t})" for t in r["term_ids"]) or "—")]
        if r["bindings"]:
            meta.append(("Bindings", ", ".join(f"{slot} = {term}" for slot, term in sorted(r["bindings"].items()))))
        if r["service_query_id"]:
            meta.append(("Service query ID", r["service_query_id"]))
        effective = r["payload"].get("limit") if r["payload"] else None
        if isinstance(r["requested_limit"], int) or effective is not None:
            meta.append(("Record limit", f"requested {r['requested_limit'] if isinstance(r['requested_limit'], int) else 'none (default)'}"
                                         + (f"; sent {effective}" if effective is not None else "")))
        if r["lint"]:
            codes = [f.get("code", "?") for f in (r["lint"].get("findings") or []) if isinstance(f, dict)]
            meta.append(("Syntax checks at firing", ", ".join(codes) if codes else "passed"))
        if r["withheld"]:
            meta.append(("Withheld", withheld_text(r["withheld"], r["withheld_detail"])))
        if r["storage_problem"]:
            meta.append(("Record incomplete", f"sent, but {r['storage_problem']}"))
        if r["reason"]:
            meta.append(("Reason", r["reason"]))
        body: list[tuple] = [("heading", f"Run {i} of {len(runs)}"), ("meta", meta)]
        sent = r["outcome"] in ("ok", "error")
        if r["outcome"] == "ok" and r["profile"] is not None:
            # What came back (a profile: there may be hundreds of thousands of records) and what the agent made
            # of it come first; the payload follows for checking.
            from ..results import describe_lines, pivot_caption, pivot_table

            body += [("subheading", "What came back"), ("meta", describe_lines(r["profile"]))]
            if r["profile"].get("count"):
                headers, rows = pivot_table(r["profile"], hit)
                body += [("note", pivot_caption(r["profile"], hit) + ":"), ("table", headers, rows),
                         ("note", f"Full results: lucid-warrant results {r['run_id']} --format parquet|csv|jsonl")]
            else:
                body.append(("note", "No records matched within the warrant's scope."))
            if r["summary"]:
                body += [("subheading", "Agent's summary (its words, not verified)"), ("para", r["summary"]["text"])]
            else:
                body.append(("note", "The agent didn't summarize this run."))
        if sent and r["payload"] is not None:
            elided = _elide_country_lists(r["payload"], lists)
            body += [("subheading", "Payload sent (the executor's saved copy)"),
                     ("pre", compact_json(elided)),
                     ("note", ("The permitted-country list is shown by name; the full list is printed at the start of "
                               "this addendum. " if elided is not r["payload"] else "")
                      + f"SHA-256 of the exact payload (canonical JSON): {payload_fingerprint(r['payload'])}"),
                     ("note", (f"Saved when sent, {local_time(r['sent_at'])}. " if r["sent_at"] else "")
                      + ("The search service acknowledged it with the service query ID above." if r["outcome"] == "ok"
                         and r["service_query_id"] else "")
                      + (f"The search service failed: {r['send_error']}" if r["outcome"] == "error" and r["send_error"] else ""))]
        elif sent:
            body.append(("warning", f"The saved payload record for this run is {r['payload_problem'] or 'missing'}, so "
                                    "the payload can't be shown."
                         + (f" Query recorded in the run log: {_elide_query(r['query'], lists)}" if r["query"] else "")))
        elif r["outcome"] == "denied":
            body.append(("note", "Nothing was sent to the search service."
                         + (f" The query that was checked: {_elide_query(r['query'], lists)}" if r["query"] else "")
                         + (" A payload file exists for this run, but the run log records it as blocked, so it is not "
                            "shown as sent." if r["payload"] is not None else "")))
        else:
            body.append(("warning", "The run log doesn't say whether anything was sent."
                         + (" A payload record exists for this run; it is shown below as found, not as confirmed sent."
                            if r["payload"] is not None else "")))
            if r["payload"] is not None:
                body.append(("pre", compact_json(r["payload"])))
        blocks.append(("run", body))
    return blocks


# --- screen rendering ------------------------------------------------------------------

def render_text(widget: tk.Text, blocks: list[tuple]) -> None:
    """Render blocks into a read-only Text widget styled like a document."""
    p = theme.PALETTE
    paper, ink = ("#ffffff", "#1a1a1a") if theme.MODE == "light" else ("#f4f1ea", "#1a1a1a")
    serif = _serif_family(widget)
    base = tkfont.nametofont("TkDefaultFont").cget("size") or 11
    widget.configure(state="normal", background=paper, foreground=ink, padx=36, pady=28, wrap="word",
                     font=(serif, base + 1), spacing1=2, spacing3=2, relief="flat", insertwidth=0)
    widget.delete("1.0", "end")
    widget.tag_configure("title", font=(serif, base + 12, "bold"), justify="center", spacing1=4, spacing3=2)
    widget.tag_configure("subtitle", font=(serif, base), justify="center", foreground="#555555", spacing3=14)
    widget.tag_configure("heading", font=(serif, base + 3, "bold"), spacing1=16, spacing3=6)
    widget.tag_configure("label", font=(serif, base + 1, "bold"))
    widget.tag_configure("note", font=(serif, base - 1, "italic"), foreground="#555555", spacing3=6)
    widget.tag_configure("rule", foreground="#999999", justify="center")
    widget.tag_configure("th", font=(serif, base, "bold"), background="#e8e4da")
    widget.tag_configure("td", font=(serif, base))
    widget.tag_configure("check", font=(serif, base + 1), lmargin1=8, lmargin2=34, spacing3=6)
    widget.tag_configure("accent", foreground=p.get("accent", "#005fb8"))
    widget.tag_configure("definition", lmargin1=8, lmargin2=24, spacing3=4)
    widget.tag_configure("pre", font=("Courier", base), background="#f4f4f4", lmargin1=8, lmargin2=8)
    render_text_blocks(widget, blocks, serif, base)
    widget.configure(state="disabled")


def render_text_blocks(widget: tk.Text, blocks: list[tuple], serif: str, base: int) -> None:
    for block in blocks:
        kind = block[0]
        if kind == "title":
            widget.insert("end", block[1].upper() + "\n", "title")
        elif kind == "subtitle":
            widget.insert("end", block[1] + "\n", "subtitle")
        elif kind == "rule":
            widget.insert("end", "─" * 60 + "\n", "rule")
        elif kind == "heading":
            widget.insert("end", block[1] + "\n", "heading")
        elif kind == "para":
            widget.insert("end", block[1] + "\n")
        elif kind == "note":
            widget.insert("end", block[1] + "\n", "note")
        elif kind == "meta":
            width = max(len(label) for label, _ in block[1])
            tabstop = tkfont.Font(family=serif, size=base + 1, weight="bold").measure("M" * min(width, 30)) + 24
            tag = f"meta{widget.index('end')}"
            widget.tag_configure(tag, tabs=(tabstop,), lmargin2=tabstop)
            for label, value in block[1]:
                widget.insert("end", label + "\t", ("label", tag))
                widget.insert("end", str(value) + "\n", tag)
        elif kind == "table":
            _render_table(widget, block[1], block[2], serif, base)
        elif kind == "checks":
            for text in block[1]:
                widget.insert("end", "☑  " + text + "\n", "check")
        elif kind == "pagebreak":
            widget.insert("end", "─" * 60 + "\n", "rule")
        elif kind == "run":
            widget.insert("end", "─" * 60 + "\n", "rule")
            render_text_blocks(widget, block[1], serif, base)
        elif kind == "warning":
            widget.insert("end", block[1] + "\n", "note")
        elif kind == "subheading":
            widget.insert("end", block[1] + "\n", "label")
        elif kind == "pre":
            widget.insert("end", block[1] + "\n", "pre")
        elif kind == "definitions":
            for term, meaning in block[1]:
                widget.insert("end", f"\u201c{term}\u201d ", ("label", "definition"))
                widget.insert("end", meaning + "\n", "definition")


def _render_table(widget, headers, rows, serif, base):
    measure = tkfont.Font(family=serif, size=base).measure
    bold = tkfont.Font(family=serif, size=base, weight="bold").measure
    widths = []
    for i, header in enumerate(headers):
        cells = [str(r[i]) for r in rows]
        natural = max([bold(header)] + [measure(c) for c in cells]) + 18
        widths.append(min(natural, 360))
    stops, x = [], 0
    for w in widths[:-1]:
        x += w
        stops.append(x)
    tag = f"table{widget.index('end')}"
    widget.tag_configure(tag, tabs=tuple(stops), lmargin2=stops[-1] if stops else 0)
    widget.insert("end", "\t".join(headers) + "\n", ("th", tag))
    for row in rows:
        widget.insert("end", "\t".join(str(c) for c in row) + "\n", ("td", tag))
    widget.insert("end", "\n")


def _serif_family(widget) -> str:
    families = set(tkfont.families(widget))
    for family in ("Georgia", "Cambria", "Times New Roman", "Times", "Liberation Serif", "DejaVu Serif"):
        if family in families:
            return family
    return "TkDefaultFont"


# --- HTML (print / save as PDF) -------------------------------------------------------

def render_html(blocks: list[tuple], footer: str = "") -> str:
    body = _html_body(blocks)
    title = next((b[1] for b in blocks if b[0] == "title"), "Warrant")
    return _html_page(title, body, footer)


def _html_body(blocks: list[tuple]) -> str:
    out = []
    for block in blocks:
        kind = block[0]
        if kind == "title":
            out.append(f"<h1>{html.escape(block[1])}</h1>")
        elif kind == "subtitle":
            out.append(f"<p class='subtitle'>{html.escape(block[1])}</p>")
        elif kind == "rule":
            out.append("<hr>")
        elif kind == "heading":
            out.append(f"<h2>{html.escape(block[1])}</h2>")
        elif kind == "para":
            out.append(f"<p>{html.escape(block[1])}</p>")
        elif kind == "note":
            out.append(f"<p class='note'>{html.escape(block[1])}</p>")
        elif kind == "meta":
            rows = "".join(f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>" for label, value in block[1])
            out.append(f"<table class='meta'>{rows}</table>")
        elif kind == "table":
            head = "".join(f"<th>{html.escape(h)}</th>" for h in block[1])
            body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in block[2])
            out.append(f"<table class='grid'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
        elif kind == "checks":
            out.append("<ul class='checks'>" + "".join(f"<li>&#9745; {html.escape(t)}</li>" for t in block[1]) + "</ul>")
        elif kind == "pagebreak":
            out.append("<div class='pagebreak'></div>")
        elif kind == "run":
            out.append("<section class='run'>" + _html_body(block[1]) + "</section>")
        elif kind == "warning":
            out.append(f"<p class='warning'>{html.escape(block[1])}</p>")
        elif kind == "footer":
            out.append(f"<p class='footer'>{html.escape(block[1])}</p>")
        elif kind == "subheading":
            out.append(f"<h3>{html.escape(block[1])}</h3>")
        elif kind == "pre":
            out.append(f"<pre>{html.escape(block[1])}</pre>")
        elif kind == "definitions":
            out.append("<div class='definitions'>" + "".join(
                f"<p><strong>&ldquo;{html.escape(term)}&rdquo;</strong> {html.escape(meaning)}</p>" for term, meaning in block[1])
                + "</div>")
    return "".join(out)


def _html_page(title: str, body: str, footer: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
  @page {{ size: letter; margin: 0.4in; }}
  @media print {{ body {{ max-width: none; margin: 0; }} }}
  body {{ font-family: Georgia, Cambria, "Times New Roman", serif; color: #1a1a1a; max-width: 7.2in; margin: 0.5in auto; line-height: 1.45; }}
  h1 {{ text-align: center; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.1em; }}
  .subtitle {{ text-align: center; color: #555; margin-top: 0; }}
  h2 {{ font-size: 1.05em; text-transform: uppercase; letter-spacing: 0.04em; border-bottom: 1px solid #999; padding-bottom: 2px; margin-top: 1.6em; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0.4em 0; }}
  .pagebreak {{ break-before: page; page-break-before: always; height: 0; }}
  @media screen {{ .pagebreak {{ border-top: 2px dashed #bbb; margin: 2.5em 0 1em; }} }}
  h3 {{ font-size: 0.95em; margin: 1.2em 0 0.3em; }}
  pre {{ font-family: Consolas, "Courier New", monospace; font-size: 0.72em; line-height: 1.3; background: #f4f4f4;
        border: 1px solid #ddd; padding: 5px 7px; white-space: pre-wrap; word-break: break-all; }}
  @media print {{ section.run pre {{ font-size: 0.58em; line-height: 1.15; padding: 3px 5px; margin: 0.3em 0; }}
                 section.run h2 {{ margin-top: 0; }} section.run table.meta {{ font-size: 0.82em; }}
                 section.run h3 {{ margin: 0.5em 0 0.2em; }} }}
  /* Each run starts a new page and is kept on one page when it fits. */
  section.run {{ break-before: page; page-break-before: always; break-inside: avoid; page-break-inside: avoid; }}
  section.run table.meta {{ font-size: 0.9em; }}
  @media screen {{ section.run {{ border-top: 2px dashed #bbb; margin-top: 2.5em; }} }}
  .warning {{ color: #8a4b00; background: #fff4ce; border-left: 4px solid #9d5d00; padding: 4px 8px; }}
  .definitions p {{ margin: 0.25em 0 0.25em 0.3in; text-indent: -0.2in; }}
  table.meta th {{ text-align: left; width: 40%; vertical-align: top; padding: 2px 8px 2px 0; }}
  table.grid th, table.grid td {{ border: 1px solid #bbb; padding: 4px 6px; text-align: left; vertical-align: top; }}
  table.grid th {{ background: #eee; }}
  .note {{ color: #555; font-style: italic; font-size: 0.92em; }}
  ul.checks {{ list-style: none; padding-left: 0; }} ul.checks li {{ margin: 0.5em 0; white-space: pre-wrap; }}
  .footer {{ margin-top: 2em; font-size: 0.85em; color: #555; border-top: 1px solid #999; padding-top: 0.5em; }}
  @media print {{ .noprint {{ display: none; }} }}
</style></head><body>
<p class="noprint note">Use your browser's Print command and choose “Save as PDF” to keep a PDF copy.</p>
{body}
{f'<p class="footer">{html.escape(footer)}</p>' if footer else ''}
</body></html>
"""

