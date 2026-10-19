"""Shared descriptions of what a warrant enforces."""

from __future__ import annotations

from ..scope import include_rows
from ..warrant import format_duration
from .widgets import Inspector, local_time, plural


def window_text(doc: dict) -> str:
    lookback = doc.get("max_lookback_days", 0)
    return f"Last {lookback} days, up to the time of each search" if lookback else "No date restriction"


def blocked_text(doc: dict) -> str:
    ranges = doc.get("blocked_date_ranges", [])
    return "; ".join(f"{r['start']} to {r['end']}" for r in ranges) + " (UTC, inclusive)" if ranges else "None"


def scope_fields(view: Inspector, doc: dict, rbac=None, settings: dict | None = None) -> None:
    """The enforced scope of a warrant, as label/value fields, in the wording it was signed under
    (falling back to the current configuration for warrants signed before configuration snapshots)."""
    wording = (doc.get("config_context") or {}).get("values") or settings or {}
    params = wording.get("parameters", {})
    p1_label = params.get("parameter_1", {}).get("label", "Search authorization")
    p2_label = params.get("parameter_2", {}).get("label", "Data visibility")
    p1 = doc["parameter_1"]
    labels = {}
    if rbac is not None:
        try:
            labels = {o.value: o.label for o in rbac.parameter_1_options()}
            labels.update({o.value: o.label for o in rbac.parameter_2_options(p1)})
        except Exception:  # noqa: BLE001 - labels are decoration only
            labels = {}

    def named(code: str) -> str:
        return f"{code} ({labels[code]})" if labels.get(code) and labels[code] != code else code

    view.field(p1_label, named(p1))
    view.field(p2_label, ", ".join(named(v) for v in doc["parameter_2"]))
    view.field("Expires", local_time(doc["expires_at"]))
    view.field("Max records per search", f"{doc['max_rows_per_run']:,}")
    view.field("Max searches per hour", f"{doc['max_queries_per_hour']:,}" if doc.get("max_queries_per_hour")
               else "Not limited (signed before this limit existed)")
    view.field("Search window", window_text(doc))
    view.field("Blocked dates", blocked_text(doc))
    scope = doc.get("country_scope")
    if scope is None:
        view.field("Countries", "Not recorded (signed before country scope existed): all countries")
    else:
        view.field("Countries included", "; ".join(f"{r['code']} ({r['reason']})" if r["reason"] else r["code"]
                                                for r in include_rows(scope["include"])) or "All, except exclusions")
        view.field("Countries excluded", "; ".join(f"{r['code']} ({r['reason']})" for r in scope["exclude"]) or "None")
    places = doc.get("places") or []
    view.field("Places", "; ".join(f"{p['label']} ({p['lat']:.6f}, {p['lon']:.6f}, {p['radius_km'] * 1000:g} m, "
                                   f"{p['country']})" for p in places)
               if places else "No site restriction")
    hit_terms = wording.get("terms", {}).get("hit_term", {})
    view.field(hit_terms.get("plural", "Hit terms").capitalize(),
               plural(len(doc["terms"]), "included " + hit_terms.get("singular", "term").lower()))


def duration_until(doc: dict, now) -> str:
    from ..util import from_iso

    minutes = int((from_iso(doc["expires_at"]) - now).total_seconds() // 60)
    return f"in {format_duration(minutes)}" if minutes > 0 else "expired"
