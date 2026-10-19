"""The nomination queue behind the New warrant tab.

Nominations arrive in the inbox (script and agent). A term leaves the queue
when it is decided:
- rejected: remembered in data/review/rejected_terms.json with the analyst's reason (for the agent) and
  when it may be renominated: after a wait, or never. Until then renominations are held; after it, the
  term comes back for review if it's nominated again, marked as rejected before (undo is available);
- approved and signed: covered by an active warrant, so re-nominations are
  skipped while that warrant is active.

Terms are matched by exact literal text. The rejection list is review
convenience, not a security control: editing it can only make a term show up
for review again, never approve it.
"""

from __future__ import annotations

import json
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .config import Paths
from .signing import atomic_write, canonical_bytes
from .syntax import SyntaxRejected, validate_literal, validate_pattern
from datetime import timedelta

from .util import from_iso, to_iso, utc_now
from .warrant import TERM_ID_RE, list_warrants, normalize_client_id


def rejected_path(paths: Paths):
    return paths.data / "review" / "rejected_terms.json"


def load_rejected(paths: Paths) -> dict[str, dict]:
    path = rejected_path(paths)
    if not path.exists():
        return {}
    try:
        entries = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {e["literal"]: e for e in entries if isinstance(e, dict) and isinstance(e.get("literal"), str)}


def _save_rejected(paths: Paths, entries: dict[str, dict]) -> None:
    atomic_write(rejected_path(paths), canonical_bytes(sorted(entries.values(), key=lambda e: e["literal"])))


MAX_REJECT_REASON = 300


def reject(paths: Paths, term: dict, now=None) -> None:
    """Remember a rejection. term["reject_reason"] is the analyst's note to the agent; term["reassess_minutes"] is
    how long before it may be renominated (None: never)."""
    now = now or utc_now()
    minutes = term.get("reassess_minutes")
    entries = load_rejected(paths)
    entries[term["literal"]] = {
        "literal": term["literal"],
        "term_id": term.get("term_id", ""),
        "source": term.get("source", ""),
        "provenance": term.get("provenance", ""),
        **({"client_id": term["client_id"]} if term.get("client_id") else {}),
        **({"match": "pattern"} if term.get("match") == "pattern" else {}),
        "rejected_at": to_iso(now),
        "reason": " ".join(str(term.get("reject_reason") or "").split())[:MAX_REJECT_REASON],
        "renominate_after": to_iso(now + timedelta(minutes=minutes)) if isinstance(minutes, int) and minutes > 0 else None,
    }
    _save_rejected(paths, entries)


def is_held(entry: dict, now=None) -> bool:
    """Whether a rejected term is still kept out of the queue. Rejections without a wait (including older ones,
    recorded before waits existed) hold until undone."""
    after = entry.get("renominate_after")
    if not after:
        return True
    try:
        return (now or utc_now()) < from_iso(after)
    except (TypeError, ValueError):
        return True


def held_rejections(paths: Paths, now=None) -> dict[str, dict]:
    """Rejected terms still inside their wait (or rejected for good)."""
    return {literal: e for literal, e in load_rejected(paths).items() if is_held(e, now)}


def unreject(paths: Paths, literal: str) -> None:
    entries = load_rejected(paths)
    if entries.pop(literal, None) is not None:
        _save_rejected(paths, entries)


def warranted_terms(paths: Paths, public_key: Ed25519PublicKey, *, match: str | None = None) -> dict[str, dict]:
    """Literal -> {term, warrant_id} for every term in a verified, unexpired warrant."""
    covered = {}
    for w in list_warrants(paths, public_key):
        if w["status"] != "active":
            continue
        for term in w["doc"]["terms"]:
            if match is None or term.get("match", "exact") == match:
                covered.setdefault(term["literal"], {"term": term, "warrant_id": w["warrant_id"]})
    return covered


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:48] or "term"


def read_inbox(paths: Paths) -> tuple[list[dict], str | None, list[str]]:
    """(candidate terms, agent's draft purpose, problems). Candidates are undecided."""
    problems: list[str] = []

    def load(path):
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"could not read {path.name}: {exc}")
            return {}

    nominations, proposal = load(paths.nominations), load(paths.proposal)
    for name, doc in (("nominations.json", nominations), ("proposal.json", proposal)):
        if not isinstance(doc, dict):
            problems.append(f"{name} is not a JSON object")
        elif not isinstance(doc.get("terms", []), list):
            problems.append(f"{name}: \"terms\" must be a list")
    nominations = nominations if isinstance(nominations, dict) and isinstance(nominations.get("terms", []), list) else {}
    proposal = proposal if isinstance(proposal, dict) and isinstance(proposal.get("terms", []), list) else (
        {"purpose": proposal.get("purpose")} if isinstance(proposal, dict) else {})
    candidates = []
    for t in nominations.get("terms", []) if isinstance(nominations, dict) else []:
        try:
            validate_literal(t["literal"])
            if not TERM_ID_RE.match(t["term_id"]):
                raise ValueError("bad term id")
            client_id = normalize_client_id(t.get("client_id"))
        except (KeyError, TypeError, ValueError, SyntaxRejected, AttributeError) as exc:
            problems.append(f"script term {t!r}: {exc}")
            continue
        candidates.append({"term_id": t["term_id"], "literal": t["literal"], "source": "script",
                           "provenance": str(t.get("provenance", "")), "client_id": client_id})
    for t in proposal.get("terms", []) if isinstance(proposal, dict) else []:
        literal = t.get("literal") if isinstance(t, dict) else None
        pattern = isinstance(t, dict) and t.get("match") == "pattern"
        try:
            (validate_pattern if pattern else validate_literal)(literal)
            client_id = normalize_client_id(t.get("client_id"))
        except (SyntaxRejected, ValueError) as exc:
            problems.append(f"agent term {literal!r}: {exc}")
            continue
        candidates.append({"term_id": "a-" + _slug(literal), "literal": literal, "source": "agent",
                           "provenance": "agent: " + str(t.get("reason", "")), "client_id": client_id,
                           **({"match": "pattern"} if pattern else {})})
    purpose = proposal.get("purpose") if isinstance(proposal, dict) else None
    return candidates, purpose if isinstance(purpose, str) and purpose.strip() else None, problems


MAX_AGENT_NOTE = 280  # a request or a one-line status; result write-ups go on the run (summarize)


def read_agent_note(paths: Paths) -> dict | None:
    """The agent's message to the analyst (proposal.json "note", and "note_at" when it was sent): a request or
    report that isn't a term or the purpose. Shown at the top of Compose; it has no authority of its own."""
    try:
        doc = json.loads(paths.proposal.read_text("utf-8"))
        note = doc.get("note")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(note, str) or not note.strip():
        return None
    at = doc.get("note_at") if isinstance(doc.get("note_at"), str) else None
    return {"text": " ".join(note.split())[:MAX_AGENT_NOTE], "at": at}


def clear_agent_note(paths: Paths) -> None:
    """Remove the agent's message from the inbox once the analyst has read it; the rest is left as it is."""
    try:
        doc = json.loads(paths.proposal.read_text("utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(doc, dict) and ("note" in doc or "note_at" in doc):
        doc.pop("note", None)
        doc.pop("note_at", None)
        atomic_write(paths.proposal, (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def unique_id(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def merge(queue: list[dict], candidates: list[dict], hidden: set[str], rejected_before: dict | None = None) -> list[dict]:
    """Add new candidates to the queue as pending. Returns the newly added terms.

    Skips literals already in the queue, in `hidden` (rejected and still within their wait, or covered by an
    active warrant). A term in `rejected_before` whose wait is over comes back marked with its earlier
    rejection. Decisions are never touched; a still-pending entry picks up a matching mode or client ID added or corrected by
    the same source.
    """
    present = {t["literal"]: t for t in queue}
    taken = {t["term_id"] for t in queue}
    added = []
    for c in candidates:
        queued = present.get(c["literal"])
        if queued is not None and queued.get("decision") == "pending" and queued.get("source") == c["source"]:
            # The same nomination, refreshed by its source: pick up a client ID added or corrected since.
            queued["client_id"] = c.get("client_id", "")
            # Only undecided nominations follow the source. A human edit changes the source, and a
            # decision freezes the mode until the analyst explicitly edits it.
            if c.get("match") == "pattern":
                queued["match"] = "pattern"
            else:
                queued.pop("match", None)
        if c["literal"] in present or c["literal"] in hidden:
            continue
        term = {**c, "term_id": unique_id(c["term_id"], taken), "decision": "pending"}
        earlier = (rejected_before or {}).get(c["literal"])
        if earlier:  # its wait is over and it was nominated again: show why it was rejected last time
            term["rejected_before"] = {"at": earlier.get("rejected_at", ""), "reason": earlier.get("reason", "")}
            term["unreject_on_sign"] = True
        queue.append(term)
        added.append(term)
        present[term["literal"]] = term
        taken.add(term["term_id"])
    return added
