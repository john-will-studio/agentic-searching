"""Places in a warrant's geographic constraint: circles (a point and a radius) every search is limited to.

Each place has a label, WGS84 latitude and longitude, its maximum search radius in km (100 m unless the analyst
sets another, up to the organization's geography.max_radius_km), the country it's in, and a reason. The radius is
signed with the place: every search under the warrant uses exactly that circle. A place
can't be approved (or signed) unless the analyst has chosen its country, that country is permitted by the
warrant's country constraint, and the point is inside that country's border (geo.check_place_country).

With places, the executor adds `(place 1 OR place 2 ...)` to every query, outside the agent's content query, using
the organization's place-clause template (geography.place_clause, default INTERSECTS({lat},{lon},{km})) as
signed into the warrant's config_context, and sends them as a structured `place_filter`. Returned records that
carry coordinates outside every circle are withheld, as records from outside the permitted countries are.
"""

from __future__ import annotations

import re

from . import geo
from .scope import include_codes

DEFAULT_PLACE_CLAUSE = "INTERSECTS({lat},{lon},{km})"
DEFAULT_RADIUS_KM = 0.1  # each place's own search radius, signed with it: 100 m unless the analyst sets another
DEFAULT_MAX_RADIUS_KM = 25.0  # the organization's cap on any one place's radius (geography.max_radius_km)
MIN_RADIUS_KM = 0.01
MAX_PLACES = 50  # each adds ~40 characters to every query (the limit is 8000)
MAX_LABEL = 60
MAX_REASON = 200
SAME_POINT_M = 1.0  # closer than this is the same point (one place written two ways, e.g. decimal and MGRS)
PLACE_ID_RE = re.compile(r"^p-[a-z0-9-]{1,40}$")
CLAUSE_TEMPLATE_RE = re.compile(r"^[A-Za-z0-9_(),.:{} ]{1,200}$")


class PlaceError(ValueError):
    """A place can't be approved or signed as given."""


def permitted_country(scope: dict | None, code: str) -> bool:
    """Whether the warrant's country constraint allows this country."""
    scope = scope or {}
    included = include_codes(scope)
    if included and code not in included:
        return False
    return code not in {r["code"] for r in scope.get("exclude") or []}


def check_place(place: dict, scope: dict | None, max_radius_km: float) -> dict:
    """A place as it's signed, or PlaceError saying what to fix."""
    label = " ".join(str(place.get("label") or "").split())
    if not 1 <= len(label) <= MAX_LABEL:
        raise PlaceError(f"a place needs a label of 1 to {MAX_LABEL} characters")
    try:
        lat, lon = float(place["lat"]), float(place["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlaceError(f"{label}: latitude and longitude must be numbers") from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise PlaceError(f"{label}: latitude must be -90 to 90 and longitude -180 to 180")
    try:
        radius = float(place["radius_km"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlaceError(f"{label}: the radius must be a number of kilometres") from exc
    if not MIN_RADIUS_KM <= radius <= max_radius_km:
        raise PlaceError(f"{label}: the radius must be {MIN_RADIUS_KM} to {max_radius_km:g} km")
    country = str(place.get("country") or "").strip().upper()
    if not country:
        raise PlaceError(f"{label}: choose the country it's in first")
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise PlaceError(f"{label}: the country must be a two-letter code")
    if not permitted_country(scope, country):
        raise PlaceError(f"{label}: {country} isn't permitted by this warrant's countries; include it (or stop "
                         "excluding it) first")
    try:
        geo.check_place_country(country, lat, lon)
    except geo.CoordinateError as exc:
        raise PlaceError(f"{label}: {exc}") from exc
    reason = " ".join(str(place.get("reason") or "").split())
    if not 1 <= len(reason) <= MAX_REASON:
        raise PlaceError(f"{label}: give a reason (1 to {MAX_REASON} characters)")
    source = place.get("source", "human")
    if source not in ("human", "agent"):
        raise PlaceError(f"{label}: unknown source {source!r}")
    place_id = place.get("place_id")
    if not isinstance(place_id, str) or not PLACE_ID_RE.match(place_id):
        raise PlaceError(f"{label}: invalid place id {place_id!r}")
    clean = {"place_id": place_id, "label": label, "lat": round(lat, 6), "lon": round(lon, 6),
             "radius_km": round(radius, 3), "country": country, "reason": reason, "source": source}
    if place.get("given_as"):
        if not isinstance(place["given_as"], str) or len(place["given_as"]) > 80:
            raise PlaceError(f"{label}: the original coordinate must be at most 80 characters")
        if not isinstance(place.get("read_as", ""), str) or len(place.get("read_as", "")) > 120:
            raise PlaceError(f"{label}: the coordinate format must be at most 120 characters")
        cell = place.get("cell_size_m")
        if cell is not None and (isinstance(cell, bool) or cell not in (1, 10, 100, 1000, 10000, 100000)):
            raise PlaceError(f"{label}: invalid MGRS square size")
        clean.update({k: place[k] for k in geo.EVIDENCE_FIELDS if k in place})
        if geo.precision_needs_review(clean):
            raise PlaceError(f"{label}: {geo.precision_warning(clean)}")
    return clean


def _label_key(label) -> str:
    return " ".join(str(label or "").split()).casefold()


def duplicate_of(place: dict, others) -> str | None:
    """Why `place` duplicates one of `others` (the same label, ignoring case and spacing, or a point within
    SAME_POINT_M of another), or None. A warrant can't hold two places that are the same place under two names,
    or two places called the same thing."""
    label = " ".join(str(place.get("label") or "").split())
    for other in others:
        if other is place or (place.get("place_id") and other.get("place_id") == place.get("place_id")):
            continue
        if _label_key(other.get("label")) == _label_key(label):
            return f"{label}: another place already has this label"
        try:
            apart_m = geo.distance_km(float(place["lat"]), float(place["lon"]), float(other["lat"]), float(other["lon"])) * 1000
        except (KeyError, TypeError, ValueError):
            continue
        if apart_m < SAME_POINT_M:
            return f"{label}: same coordinates as {other.get('label')!r}"
    return None


def normalize_places(places, scope: dict | None, max_radius_km: float) -> list[dict]:
    places = list(places or [])
    if len(places) > MAX_PLACES:
        raise PlaceError(f"at most {MAX_PLACES} places")
    clean, ids = [], set()
    for place in places:
        p = check_place(place, scope, max_radius_km)
        if p["place_id"] in ids:
            raise PlaceError(f"duplicate place id {p['place_id']}")
        duplicate = duplicate_of(p, clean)
        if duplicate:
            raise PlaceError(duplicate)
        ids.add(p["place_id"])
        clean.append(p)
    return clean


def _number(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def clause(template: str, place: dict) -> str:
    return (template.replace("{lat}", _number(place["lat"])).replace("{lon}", _number(place["lon"]))
            .replace("{km}", _number(place["radius_km"])))


def clause_template(warrant: dict) -> str:
    """The place-clause template signed into the warrant (its config_context), or the default."""
    values = (warrant.get("config_context") or {}).get("values") or {}
    return ((values.get("geography") or {}).get("place_clause")) or DEFAULT_PLACE_CLAUSE


def place_filter(warrant: dict) -> dict | None:
    places = warrant.get("places") or []
    if not places:
        return None
    template = clause_template(warrant)
    return {"relation": "within", "template": template,
            "places": [{k: p[k] for k in ("place_id", "label", "lat", "lon", "radius_km", "country")} for p in places],
            "clauses": [clause(template, p) for p in places]}


def apply_place_filter(query: str, places: dict | None) -> str:
    if not places:
        return query
    return f"({query}) AND (" + " OR ".join(places["clauses"]) + ")"


def place_check(places: dict, record) -> str:
    """"inside" an approved circle, "outside" all of them, or "unknown" when the record has no usable coordinates
    (missing, unreadable, infinite or out of range). Under a place warrant only "inside" is released: a record
    that can't be shown to be inside a signed circle is withheld, and counted separately."""
    import math

    if not isinstance(record, dict) or "lat" not in record or "lon" not in record:
        return "unknown"
    try:
        lat, lon = float(record["lat"]), float(record["lon"])
    except (TypeError, ValueError):
        return "unknown"
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return "unknown"
    inside = any(geo.distance_km(lat, lon, p["lat"], p["lon"]) <= p["radius_km"] for p in places["places"])
    return "inside" if inside else "outside"


def outside_places(places: dict | None, record: dict) -> bool:
    """True when a record would be withheld under these places (outside every circle, or not locatable)."""
    return bool(places) and place_check(places, record) != "inside"


# --- rejected agent places ----------------------------------------------------------------------------------------

REJECTED_FILE = "rejected_places.json"


def place_key(place) -> str:
    """The same place, however it's proposed again: its label (ignoring case and spacing) and its point."""
    return f"{_label_key(place.get('label'))}|{float(place['lat']):.5f}|{float(place['lon']):.5f}"


def load_rejected_places(paths) -> dict[str, dict]:
    """{place key: {"label", "reason", "rejected_at"}} for agent places rejected at a signing. They aren't offered
    for review again; the agent sees the reason in its status."""
    import json

    try:
        data = json.loads((paths.state / REJECTED_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, list):  # an earlier form: keys only
        return {str(k): {"label": str(k).split("|")[0], "reason": ""} for k in data}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def record_place_rejections(paths, rows, now_iso: str) -> None:
    import json

    from .signing import atomic_write

    data = load_rejected_places(paths)
    for r in rows:
        if r.get("decision") == "rejected" and r.get("source") == "agent":
            data[place_key(r)] = {"label": r["label"], "reason": r.get("reject_reason", ""), "rejected_at": now_iso}
    if data:
        paths.state.mkdir(parents=True, exist_ok=True)
        atomic_write(paths.state / REJECTED_FILE, json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"))
