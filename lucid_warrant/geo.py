"""Places: coordinates for a warrant's geographic constraint.

A place is a point (WGS84 latitude and longitude, in that order, in decimal degrees) and a radius in kilometres.
People get coordinates in many forms, so parse_coordinate() reads the common ones, spaced or not:

    decimal degrees           41.0082, 28.9784   ·  41.0082 28.9784  ·  41.0082N 28.9784E  ·  N41.0082 E28.9784
    degrees, decimal minutes  41 00.492 N 028 58.704 E  ·  41°00.492'N 28°58.704'E  ·  4100.492N02858.704E
    degrees, minutes, seconds 41°00'29.5"N 28°58'42.2"E  ·  41 00 29.5 N 28 58 42.2 E  ·  410029.5N0285842.2E
    UTM                       35T 666370 4541552  ·  35T 666370mE 4541552mN
    MGRS                      35TPF6637041552  ·  35T PF 66370 41552

Every conversion is shown to the person as lat, lon with a reminder to check it: a misread coordinate puts the
circle somewhere else entirely, without any error.

Country borders come from Natural Earth's 1:10m admin-0 countries (public domain), bundled as
geo_data/borders_10m.json.gz. They're accurate to roughly a kilometre at borders and coasts.
"""

from __future__ import annotations

import gzip
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BORDERS = Path(__file__).resolve().parent / "geo_data" / "borders_10m.json.gz"
EARTH_RADIUS_KM = 6371.0088  # mean radius, for distances along the surface

# WGS84 and UTM
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996
_BANDS = "CDEFGHJKLMNPQRSTUVWX"  # 8° latitude bands from 80°S; X covers 72°–84°N


class CoordinateError(ValueError):
    """Text that can't be read as a coordinate, with a reason a person can act on."""


@dataclass(frozen=True)
class Coordinate:
    lat: float
    lon: float
    form: str  # how the text was read, in words
    note: str = ""  # anything the person should know (precision, an assumption made)
    detail: str = ""  # a few words to show beside the form, e.g. "10 m square, centre used"
    cell_size_m: float | None = None  # MGRS square width; decimal places don't imply surveyed accuracy

    def text(self) -> str:
        return format_latlon(self.lat, self.lon)


def format_latlon(lat: float, lon: float) -> str:
    return f"{lat:.6f}, {lon:.6f}"


MAP_LIMITATION = "Bundled borders are approximate (about 1 km). This checks the point, not the whole circle or the site."
EVIDENCE_FIELDS = ("given_as", "read_as", "cell_size_m", "precision_acknowledged")


def input_evidence(c: Coordinate, given: str, previous: dict | None = None) -> dict:
    """Keep source precision when an unchanged canonical point is reopened for review."""
    previous = previous or {}
    same = (previous.get("lat"), previous.get("lon")) == (round(c.lat, 6), round(c.lon, 6))
    if same and previous.get("given_as"):
        evidence = {k: previous.get(k) for k in EVIDENCE_FIELDS}
        # Older proposals kept the source text but not its precision. Only infer MGRS here;
        # re-reading an unlabelled degree pair could reverse its original order.
        if evidence["cell_size_m"] is None:
            try:
                original = parse_coordinate(previous["given_as"], "mgrs")
            except CoordinateError:
                pass
            else:
                evidence.update(read_as=original.form, cell_size_m=original.cell_size_m)
        evidence["read_as"] = evidence["read_as"] or c.form
        return evidence
    return {"given_as": given.strip(), "read_as": c.form, "cell_size_m": c.cell_size_m,
            "precision_acknowledged": None}


def evidence_text(place: dict) -> str:
    text = f"Given as {place.get('given_as', '')} · {place.get('read_as', '')}"
    cell = place.get("cell_size_m")
    if cell:
        text += f" · {cell:g} m square, centre used"
    return text


def precision_key(place: dict) -> list:
    return [place.get(k) for k in ("lat", "lon", "radius_km", "cell_size_m")]


def precision_warning(place: dict) -> str:
    """The half diagonal bounds the distance from a square's centre to any point in it."""
    cell = place.get("cell_size_m")
    if not cell or place["radius_km"] * 1000 >= cell / math.sqrt(2):
        return ""
    return (f"The {place['radius_km'] * 1000:g} m radius is smaller than the input's uncertainty "
            f"(up to {cell / math.sqrt(2):.0f} m from the centre of a {cell:g} m MGRS square). "
            "Refine the point from your source, or acknowledge this limitation.")


def precision_needs_review(place: dict) -> bool:
    return bool(precision_warning(place)) and place.get("precision_acknowledged") != precision_key(place)


# --- reading coordinates ---------------------------------------------------------------------------------------

_UTM = re.compile(r"^(\d{1,2})\s*([C-HJ-NP-X])\s+(\d{1,7}(?:\.\d+)?)\s*(?:M?E)?\s*[,;]?\s*(\d{1,8}(?:\.\d+)?)\s*(?:M?N)?$")
_MGRS = re.compile(r"^(\d{1,2})\s*([C-HJ-NP-X])\s*([A-HJ-NP-Z])\s*([A-HJ-NP-V])\s*(\d*)\s*(\d*)$")


FORMS = {"auto": "Detect automatically", "latlon": "Lat, lon", "lonlat": "Lon, lat", "mgrs": "MGRS", "utm": "UTM"}


def parse_coordinate(text: str, form: str = "auto") -> Coordinate:
    """Read a coordinate in any of the forms in the module docstring (form="auto"), or only as the form chosen:
    "latlon" or "lonlat" (decimal or degrees-minutes-seconds, in that order), "mgrs" or "utm". Choosing removes
    any guessing. Raises CoordinateError."""
    if form not in FORMS:
        raise CoordinateError(f"unknown coordinate form {form!r}")
    if not isinstance(text, str) or not text.strip():
        raise CoordinateError("enter a coordinate")
    raw = " ".join(text.strip().upper().split())
    if len(raw) > 80:
        raise CoordinateError("that's too long to be a coordinate")
    if re.match(r"^[ABYZ]\s*[A-Z]{2}\s*\d", raw):
        raise CoordinateError("polar grid references (UPS, bands A, B, Y and Z) aren't supported; give lat, lon")
    m = _MGRS.match(raw)
    if form == "mgrs" and not m:
        raise CoordinateError("not an MGRS reference: zone, band, two square letters, then digits, e.g. 35TPF6637041552")
    if m and form in ("auto", "mgrs"):
        return _from_mgrs(*m.groups())
    m = _UTM.match(raw)
    if form == "utm" and not m:
        raise CoordinateError("not UTM: zone and band, easting, northing, e.g. 35T 666370 4541552")
    if form == "lonlat":
        c = _from_degrees(_swap_unlabelled(raw))
        return Coordinate(c.lat, c.lon, c.form + " (given as lon, lat)", c.note)
    if m and form in ("auto", "utm"):
        zone, band, easting, northing = int(m.group(1)), m.group(2), float(m.group(3)), float(m.group(4))
        lat, lon = utm_to_latlon(zone, band >= "N", easting, northing)
        _check_band(band, lat, f"{zone}{band}")
        return Coordinate(lat, lon, "UTM", _band_note(band))
    return _from_degrees(raw)


def _swap_unlabelled(raw: str) -> str:
    """Lon, lat text reordered to lat, lon (numbers only; text with N/S/E/W letters is already unambiguous)."""
    if re.search(r"[NSEW]", raw):
        return raw
    if "," in raw or ";" in raw:
        first, _, second = re.split(r"([,;])", raw, maxsplit=1)
        return f"{second.strip()}, {first.strip()}"
    numbers = raw.split()
    if len(numbers) not in (2, 4, 6):
        raise CoordinateError("give longitude then latitude, e.g. 28.9784, 41.0082")
    half = len(numbers) // 2
    return " ".join(numbers[half:] + numbers[:half])


def _band_note(band: str) -> str:
    if band == "S":
        return "S was read as latitude band S (32°N–40°N). For the southern hemisphere, use the band letter (C–M)."
    if band == "N":
        return "N was read as latitude band N (0°–8°N)."
    return ""


def _check_band(band: str, lat: float, label: str) -> None:
    south = -80 + 8 * _BANDS.index(band)
    north = 84 if band == "X" else south + 8
    if not south - 0.5 <= lat <= north + 0.5:
        raise CoordinateError(f"{label} doesn't match its northing: that point is at latitude {lat:.2f}, outside band "
                              f"{band} ({south}° to {north}°). Check the band letter (C–M are south of the equator).")


def _from_degrees(raw: str) -> Coordinate:
    normalized = raw
    for symbols, replacement in (("°º˚D", " "), ("'’′ʹM", " "), ('"”″ʺ', " ")):
        for ch in symbols:
            if ch in ("D", "M"):
                normalized = re.sub(rf"(?<=[\d.]){ch}", " ", normalized)  # 41D 00M as degree/minute marks
            else:
                normalized = normalized.replace(ch, " ")
    normalized = normalized.replace("''", " ")
    leftovers = re.sub(r"[NSEW\d.+\-,;\s]", "", normalized)
    if leftovers:
        shown = raw if len(raw) <= 40 else raw[:40] + "…"
        raise CoordinateError(f"couldn't read {shown!r} as a coordinate; use lat, lon (e.g. 41.0082, 28.9784), "
                              "degrees minutes seconds, UTM or MGRS")
    tokens = _lex(normalized, raw)
    letters = [t for t in tokens if t in "NSEW"]
    if not letters:
        return _unlabelled(tokens)
    groups = _groups_by_letter(tokens)
    if len(groups) != 2:
        raise CoordinateError("give exactly two parts, latitude and longitude, each with one of N/S and E/W")
    parts = {}
    for letter, numbers in groups:
        axis = "lat" if letter in "NS" else "lon"
        if axis in parts:
            raise CoordinateError("both parts are marked " + ("N/S" if axis == "lat" else "E/W")
                                  + "; one must be latitude (N/S) and the other longitude (E/W)")
        if any(n.startswith(("-", "+")) for n in numbers):
            raise CoordinateError("use either a minus sign or a hemisphere letter, not both")
        value, form = _degrees(numbers, axis)
        parts[axis] = (-value if letter in "SW" else value, form)
    return _finish(parts["lat"], parts["lon"])


_TOKEN = re.compile(r"\s*([NSEW]|[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?![\d.])|[,;])")


def _lex(text: str, raw: str) -> list[str]:
    """Every character must belong to a number, a hemisphere letter or a separator: nothing is skipped, so
    "41..5" or "4 1.2.3" are refused instead of being read as something else."""
    tokens, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            shown = raw if len(raw) <= 40 else raw[:40] + "…"
            raise CoordinateError(f"couldn't read {shown!r} as a coordinate (check the decimal points and separators); "
                                  "use lat, lon (e.g. 41.0082, 28.9784), degrees minutes seconds, UTM or MGRS")
        token = m.group(1)
        if token.startswith((".", "-.", "+.")):  # .5 is 0.5; -.5 is -0.5
            token = token[:-len(token.lstrip("+-"))] + "0" + token.lstrip("+-")
        tokens.append(token)
        pos = m.end()
    return tokens


def _groups_by_letter(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """Split [numbers and N/S/E/W letters] into (letter, numbers) pairs, letters before or after their numbers."""
    tokens = [t for t in tokens if t not in ",;"]
    prefix = tokens[0] in "NSEW"
    groups, numbers, letter = [], [], None
    for t in tokens:
        if t in "NSEW":
            if prefix:
                if letter is not None:
                    groups.append((letter, numbers))
                letter, numbers = t, []
            else:
                groups.append((t, numbers))
                numbers = []
        else:
            numbers.append(t)
    if prefix and letter is not None:
        groups.append((letter, numbers))
    elif numbers:
        raise CoordinateError("every part needs its own N/S or E/W letter")
    if any(not nums for _l, nums in groups):
        raise CoordinateError("a hemisphere letter has no numbers with it")
    return groups


def _unlabelled(tokens: list[str]) -> Coordinate:
    halves = _split_on_separator(tokens)
    if halves is None:
        numbers = [t for t in tokens if t not in ",;"]
        if len(numbers) not in (2, 4, 6):
            raise CoordinateError("give latitude then longitude, e.g. 41.0082, 28.9784")
        halves = numbers[: len(numbers) // 2], numbers[len(numbers) // 2:]
    lat_parts, lon_parts = halves
    lat, lat_form = _signed(lat_parts, "lat")
    lon, lon_form = _signed(lon_parts, "lon")
    if abs(lat) > 90 and abs(lon) <= 90:
        raise CoordinateError(f"{lat_parts[0]} can't be a latitude. Lucid Warrant reads lat, lon; this looks like "
                              "lon, lat (the order GeoJSON and WKT use). Swap them.")
    return _finish((lat, lat_form), (lon, lon_form))


def _split_on_separator(tokens: list[str]):
    if not any(t in ",;" for t in tokens):
        return None
    first, second, current = [], [], None
    current = first
    for t in tokens:
        if t in ",;":
            if current is second:
                raise CoordinateError("use one comma, between latitude and longitude")
            current = second
        else:
            current.append(t)
    if not first or not second:
        raise CoordinateError("give latitude, then a comma, then longitude")
    return first, second


def _signed(numbers: list[str], axis: str) -> tuple[float, str]:
    negative = numbers[0].startswith("-")
    if any(n.startswith(("-", "+")) for n in numbers[1:]):
        raise CoordinateError("only the degrees can carry a minus sign")
    # Unlabelled: never compact (that needs a hemisphere letter), and no range check yet, so lon, lat order can
    # be recognized and named before "out of range" is.
    value, form = _degrees([numbers[0].lstrip("+-")] + numbers[1:], axis, compact=False, check_range=False)
    return (-value if negative else value), form


def _degrees(numbers: list[str], axis: str, *, compact: bool = True, check_range: bool = True) -> tuple[float, str]:
    """Degrees from 1–3 numbers (d, d m, d m s), or one compact number like 410029.5 (DDMMSS) when labelled."""
    limit = 90 if axis == "lat" else 180
    whole = numbers[0].lstrip("+-").split(".")[0]
    if compact and len(numbers) == 1 and len(whole) >= 4:
        # Recognized by its width, not its size: 0030N is 00°30'N (DDMM), not 30 degrees.
        return _compact(numbers[0], axis)
    if not 1 <= len(numbers) <= 3:
        raise CoordinateError("each part is degrees, degrees and minutes, or degrees, minutes and seconds")
    if any("." in n for n in numbers[:-1]):
        raise CoordinateError("only the last number of each part can have decimals (e.g. 41 00 29.5)")
    values = [float(n) for n in numbers]
    for v, name in zip(values[1:], ("minutes", "seconds")):
        if v >= 60:
            raise CoordinateError(f"{name} must be less than 60")
    degrees = values[0] + sum(v / 60 ** i for i, v in enumerate(values[1:], start=1))
    if check_range and degrees > limit:
        raise CoordinateError(f"{'latitude' if axis == 'lat' else 'longitude'} must be at most {limit}°")
    return degrees, ("decimal degrees", "degrees and decimal minutes", "degrees, minutes and seconds")[len(values) - 1]


def _compact(number: str, axis: str) -> tuple[float, str]:
    whole, _, fraction = number.partition(".")
    width = 2 if axis == "lat" else 3
    if len(whole) == width + 2:
        d, m = whole[:width], whole[width:] + ("." + fraction if fraction else "")
        return _degrees([d, m], axis)[0], "compact degrees and minutes"
    if len(whole) == width + 4:
        d, m, s = whole[:width], whole[width:width + 2], whole[width + 2:] + ("." + fraction if fraction else "")
        return _degrees([d, m, s], axis)[0], "compact degrees, minutes and seconds"
    expect = "DDMM or DDMMSS" if axis == "lat" else "DDDMM or DDDMMSS"
    raise CoordinateError(f"{number} isn't a valid compact {'latitude' if axis == 'lat' else 'longitude'} ({expect})")


def _finish(lat_part, lon_part) -> Coordinate:
    (lat, lat_form), (lon, lon_form) = lat_part, lon_part
    if not -90 <= lat <= 90:
        raise CoordinateError("latitude must be between -90 and 90")
    if not -180 <= lon <= 180:
        raise CoordinateError("longitude must be between -180 and 180")
    form = lat_form if lat_form == lon_form else f"{lat_form} / {lon_form}"
    return Coordinate(lat, lon, form)


# --- UTM and MGRS ----------------------------------------------------------------------------------------------

def utm_to_latlon(zone: int, northern: bool, easting: float, northing: float) -> tuple[float, float]:
    """WGS84 latitude and longitude from UTM (Snyder's series; well under a metre within a zone)."""
    if not 1 <= zone <= 60:
        raise CoordinateError("UTM zones are 1 to 60")
    if not 100_000 <= easting <= 900_000:
        raise CoordinateError("a UTM easting is between 100000 and 900000 metres")
    if not 0 <= northing <= 10_000_000:
        raise CoordinateError("a UTM northing is between 0 and 10000000 metres")
    x = easting - 500_000
    y = northing if northern else northing - 10_000_000
    mu = (y / _K0) / (_A * (1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    n1 = _A / math.sqrt(1 - _E2 * sin1 ** 2)
    t1, c1 = tan1 ** 2, _EP2 * cos1 ** 2
    r1 = _A * (1 - _E2) / (1 - _E2 * sin1 ** 2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * tan1 / r1) * (d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * _EP2) * d ** 4 / 24
                                     + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * _EP2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6
           + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * _EP2 + 24 * t1 ** 2) * d ** 5 / 120) / cos1
    lon_deg = (zone - 1) * 6 - 180 + 3 + math.degrees(lon)
    return math.degrees(lat), (lon_deg + 180) % 360 - 180  # zone 60's eastern edge is past 180°: wrap it


def latlon_to_utm(lat: float, lon: float, zone: int | None = None) -> tuple[int, bool, float, float]:
    """(zone, northern, easting, northing) for a WGS84 point (the standard zone unless one is given)."""
    zone = zone or int((lon + 180) // 6) % 60 + 1
    phi, lam = math.radians(lat), math.radians(lon - ((zone - 1) * 6 - 180 + 3))
    sin, cos, tan = math.sin(phi), math.cos(phi), math.tan(phi)
    n = _A / math.sqrt(1 - _E2 * sin ** 2)
    t, c, a = tan ** 2, _EP2 * cos ** 2, cos * lam
    m = _A * ((1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256) * phi
              - (3 * _E2 / 8 + 3 * _E2 ** 2 / 32 + 45 * _E2 ** 3 / 1024) * math.sin(2 * phi)
              + (15 * _E2 ** 2 / 256 + 45 * _E2 ** 3 / 1024) * math.sin(4 * phi)
              - (35 * _E2 ** 3 / 3072) * math.sin(6 * phi))
    easting = _K0 * n * (a + (1 - t + c) * a ** 3 / 6 + (5 - 18 * t + t ** 2 + 72 * c - 58 * _EP2) * a ** 5 / 120) + 500_000
    northing = _K0 * (m + n * tan * (a ** 2 / 2 + (5 - t + 9 * c + 4 * c ** 2) * a ** 4 / 24
                                     + (61 - 58 * t + t ** 2 + 600 * c - 330 * _EP2) * a ** 6 / 720))
    return zone, lat >= 0, easting, northing + (0 if lat >= 0 else 10_000_000)


_COLUMNS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
_ROWS = "ABCDEFGHJKLMNPQRSTUV"


def _from_mgrs(zone_text, band, column, row, first, second) -> Coordinate:
    zone = int(zone_text)
    if not 1 <= zone <= 60:
        raise CoordinateError("MGRS zones are 1 to 60")
    if first and second and len(first) != len(second):
        raise CoordinateError("an MGRS reference has the same number of easting and northing digits; "
                              f"{first} and {second} differ")
    digits = first + second
    if len(digits) % 2 or len(digits) > 10:
        raise CoordinateError("an MGRS reference has the same number of easting and northing digits (up to 5 each)")
    columns = _COLUMNS[(zone - 1) % 3]
    if column not in columns:
        raise CoordinateError(f"column letter {column} isn't used in zone {zone}")
    half = len(digits) // 2
    scale = 10 ** (5 - half)
    east = int(digits[:half] or 0) * scale
    north = int(digits[half:] or 0) * scale
    cell = scale / 2  # the centre of the square the reference names
    easting = (columns.index(column) + 1) * 100_000 + east + cell
    row_offset = 5 if zone % 2 == 0 else 0
    northing = ((_ROWS.index(row) - row_offset) % 20) * 100_000 + north + cell
    northern = band >= "N"
    # The row letters repeat every 2000 km: add whole cycles until the point is inside the latitude band.
    south_edge = -80 + 8 * _BANDS.index(band)
    band_min = latlon_to_utm(south_edge, (zone - 1) * 6 - 180 + 3, zone)[3]  # lowest at the central meridian
    while northing < band_min - 100_000:
        northing += 2_000_000
    lat, lon = utm_to_latlon(zone, northern, easting, min(northing, 10_000_000))
    _check_band(band, lat, f"{zone}{band}{column}{row}")
    precision = {0: "100 km", 1: "10 km", 2: "1 km", 3: "100 m", 4: "10 m", 5: "1 m"}[half]
    return Coordinate(lat, lon, "MGRS", f"An MGRS reference names a {precision} square; its centre is used.",
                      f"{precision} square, centre used", scale)


# --- borders ---------------------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _borders() -> dict[str, dict]:
    with gzip.open(BORDERS, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    out = {}
    for country in doc["countries"]:
        polygons = []
        for rings in country["polygons"]:
            xs = [p[0] for p in rings[0]]
            ys = [p[1] for p in rings[0]]
            polygons.append(((min(xs), min(ys), max(xs), max(ys)), rings))
        entry = out.setdefault(country["code"], {"name": country["name"], "polygons": []})
        entry["polygons"] += polygons
    return out


def _in_ring(lon: float, lat: float, ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _in_country(entry: dict, lat: float, lon: float) -> bool:
    for (x0, y0, x1, y1), rings in entry["polygons"]:
        if x0 <= lon <= x1 and y0 <= lat <= y1 and _in_ring(lon, lat, rings[0]):
            if not any(_in_ring(lon, lat, hole) for hole in rings[1:]):
                return True
    return False


def _same_meridians(lon: float) -> tuple[float, ...]:
    """180°E and 180°W are the same line; the map draws it on one side or the other."""
    return (lon, -lon) if abs(lon) == 180 else (lon,)


def country_at(lat: float, lon: float) -> str | None:
    """The two-letter code of the country containing the point, per the bundled borders, or None (sea, or an area
    with no ISO code such as a disputed territory)."""
    for code, entry in _borders().items():
        if any(_in_country(entry, lat, x) for x in _same_meridians(lon)):
            return code
    return None


def in_country(code: str, lat: float, lon: float) -> bool:
    entry = _borders().get(code)
    return bool(entry) and any(_in_country(entry, lat, x) for x in _same_meridians(lon))


def country_name(code: str) -> str:
    entry = _borders().get(code)
    return entry["name"] if entry else code


def check_place_country(code: str, lat: float, lon: float) -> None:
    """Raise CoordinateError unless the point is inside the country chosen for it."""
    if in_country(code, lat, lon):
        return
    found = country_at(lat, lon)
    where = (f"it's in {country_name(found)} ({found})" if found
             else "it isn't inside any country's border in the bundled map (at sea, or in an area without an ISO code)")
    raise CoordinateError(f"{format_latlon(lat, lon)} isn't inside {country_name(code)} ({code}): {where}. Check the "
                          "coordinate and the country. Borders are accurate to about a kilometre.")


def where_is(lat: float, lon: float) -> str:
    """Which country the point is in, per the bundled borders, in words."""
    code = country_at(lat, lon)
    return (f"bundled map: {country_name(code)} ({code})" if code
            else "bundled map: not inside any country (at sea, or an area without an ISO code)")


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))
