"""The agent's summary of a search's results, attached to that run.

Written with `lucid-warrant summarize RUN_ID --text ...`, shown in the Runs tab and on the run's page of the
printed warrant, below what the run returned and above the payload. It's the agent's own account, unsigned and
labelled as such: the records returned and the hit terms found in them are recorded by Lucid Warrant itself.
"""

from __future__ import annotations

import json

from .config import Paths
from .signing import atomic_write, canonical_bytes
from .util import check_id, to_iso, utc_now

MAX_SUMMARY = 2000


class SummaryError(ValueError):
    pass


def summary_path(paths: Paths, run_id: str):
    return paths.state / "summaries" / f"{check_id(run_id, 'run')}.json"


def write_summary(paths: Paths, run_id: str, text: str) -> dict:
    """Attach (or replace) the agent's summary of a run that was sent."""
    from .executor import read_audit

    text = (text or "").strip()
    if not text or len(text) > MAX_SUMMARY:
        raise SummaryError(f"a summary must be 1 to {MAX_SUMMARY} characters")
    entry = next((e for e in read_audit(paths, limit=None) if e.get("run_id") == run_id), None)
    if entry is None:
        raise SummaryError(f"no run {run_id}")
    if entry.get("outcome") != "ok":
        raise SummaryError(f"{run_id} didn't return results (it was {entry.get('outcome')}), so there's nothing to summarize")
    doc = {"run_id": run_id, "text": text, "at": to_iso(utc_now())}
    atomic_write(summary_path(paths, run_id), canonical_bytes(doc))
    return doc


def read_summary(paths: Paths, run_id) -> dict | None:
    try:
        doc = json.loads(summary_path(paths, run_id).read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("text"), str):
        return None
    return {"text": doc["text"][:MAX_SUMMARY], "at": doc.get("at") if isinstance(doc.get("at"), str) else None}
