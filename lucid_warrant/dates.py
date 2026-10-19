"""Date limits that the executor adds to every search under a warrant.

A warrant can set:
- max_lookback_days: only records from the last N days (0 = no limit). The
  window is computed at each run, so it slides for a long-lived warrant.
- blocked_date_ranges: inclusive UTC calendar-day ranges no search may touch.

The executor appends these to the query as required clauses outside the
agent's content query, and also sends them as a structured `date_filter` so a
real backend adapter can apply them as a filter instead of query text. The
range syntax and date format must be confirmed against the real index's
timestamp field type.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .util import to_iso

DEFAULT_DATE_FIELD = "timestamp"
MAX_LOOKBACK_DAYS = 36500
MAX_BLOCKED_RANGES = 50


class DateLimitError(ValueError):
    """A lookback or blocked date range is invalid."""


MIN_YEAR, MAX_YEAR = 1900, 2999


def parse_day(text: str) -> date:
    try:
        day = datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise DateLimitError(f"{text!r} is not a date in YYYY-MM-DD form") from exc
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise DateLimitError(f"{text!r} is outside {MIN_YEAR}–{MAX_YEAR}")
    return day


def validate_lookback(days) -> int:
    if not isinstance(days, int) or not 0 <= days <= MAX_LOOKBACK_DAYS:
        raise DateLimitError(f"lookback must be 0 (no limit) to {MAX_LOOKBACK_DAYS} days")
    return days


def normalize_ranges(ranges) -> list[dict]:
    """Validate, de-duplicate and sort blocked ranges as {"start", "end"} ISO dates."""
    if ranges is not None and not isinstance(ranges, list):
        raise DateLimitError("blocked ranges must be a list")
    clean = set()
    for r in ranges or []:
        if not isinstance(r, dict) or set(r) != {"start", "end"}:
            raise DateLimitError('each blocked range must be exactly {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}')
        start, end = parse_day(r["start"]), parse_day(r["end"])
        if start > end:
            raise DateLimitError(f"blocked range {start} to {end} ends before it starts")
        clean.add((start.isoformat(), end.isoformat()))
    if len(clean) > MAX_BLOCKED_RANGES:
        raise DateLimitError(f"at most {MAX_BLOCKED_RANGES} blocked ranges")
    return [{"start": s, "end": e} for s, e in sorted(clean)]


def window(warrant: dict, now: datetime, date_field: str) -> dict | None:
    """The concrete date filter for one run, or None if the warrant sets no date limits."""
    lookback = warrant.get("max_lookback_days", 0)
    ranges = warrant.get("blocked_date_ranges", [])
    if not lookback and not ranges:
        return None
    return {
        "field": date_field,
        "from": to_iso(now - timedelta(days=lookback)) if lookback else None,
        # "The last N days" ends now: records dated in the future (scheduled events, clock errors) are out.
        "to": to_iso(now) if lookback else None,
        # Inclusive calendar days as half-open intervals: from midnight on the first day up to (not
        # including) midnight after the last, so no fraction of the final second slips through.
        "exclude": [{"from": f"{r['start']}T00:00:00Z", "to": f"{(parse_day(r['end']) + timedelta(days=1)).isoformat()}T00:00:00Z"}
                    for r in ranges],
    }


def apply_window(query: str, date_filter: dict | None) -> str:
    if not date_filter:
        return query
    field = date_filter["field"]
    clauses = []
    if date_filter["from"]:
        clauses.append(f'{field}:["{date_filter["from"]}" TO "{date_filter["to"]}"]' if date_filter.get("to")
                       else f'{field}:["{date_filter["from"]}" TO *]')
    clauses += [f'NOT {field}:["{r["from"]}" TO "{r["to"]}"}}' for r in date_filter["exclude"]]  # upper bound exclusive
    return f"({query}) AND " + " AND ".join(clauses)


def describe(lookback: int, ranges: list[dict]) -> str:
    window_text = f"last {lookback} days (up to the time of each search)" if lookback else "no limit"
    blocked = "; ".join(f"{r['start']} to {r['end']}" for r in ranges) or "none"
    return f"Search window: {window_text}\nBlocked dates (UTC, inclusive): {blocked}"
