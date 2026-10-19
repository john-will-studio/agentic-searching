"""Read a list of places from GeoJSON, KML/KMZ or CSV, working out which column holds what.

For each file the reader finds the coordinate (separate latitude and longitude columns, or one column holding a
coordinate in any form geo.parse_coordinate reads, or the file's own point geometry), and where present a label,
radius, country and reason. The person sees the detected columns, can change them, and every place arrives in
Compose as pending: it's reviewed (and its country checked) like any other.

Only points are read. Lines and areas (GeoJSON polygons, KML polygons) are reported and skipped.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import geo

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
FIELDS = ("coordinate", "lat", "lon", "label", "radius", "country", "reason")

# Column names people use, compared after lowercasing and dropping everything but letters and digits.
_NAMES = {
    "lat": ("lat", "latitude", "y", "latdd", "latdeg", "ycoord", "northing"),
    "lon": ("lon", "lng", "long", "longitude", "x", "londd", "londeg", "xcoord", "easting"),
    "coordinate": ("coordinate", "coordinates", "coord", "coords", "location", "position", "point", "latlon", "latlong",
                   "mgrs", "utm", "grid", "gridref", "geo", "geolocation", "lonlat", "lnglat", "longlat"),
    "label": ("label", "name", "title", "site", "sitename", "place", "placename", "facility", "id", "siteid"),
    "radius": ("radius", "radiuskm", "radiusm", "radiusmeters", "radiusmetres", "radiuskilometers", "buffer",
               "bufferkm", "bufferm", "distance", "distancekm", "distancem", "range", "rangekm", "rangem"),
    "country": ("country", "countrycode", "iso", "iso2", "isoa2", "cc", "countryiso", "nation"),
    "reason": ("reason", "notes", "note", "description", "desc", "justification", "comment", "comments", "purpose"),
}


FILE_TYPES = ".csv (comma, semicolon or tab separated; UTF-8 text), .geojson / .json, .kml, .kmz"
LIMITS = f"Up to {MAX_FILE_BYTES // (1024 * 1024)} MB and {MAX_ROWS:,} rows. Only points: lines and areas are skipped."
BEST_FORMAT = ("A CSV with a header row and one place per row:\n"
               "    name,lat,lon,radius_m,country,reason\n"
               "    Istanbul office,41.0082,28.9784,100,TR,Client's registered office\n"
               "lat and lon in decimal degrees (WGS84), latitude first.")
ACCEPTED_VALUES = (
    "Coordinates: lat, lon (41.0082, 28.9784) · separate latitude and longitude columns · degrees-minutes-seconds "
    "(41°00'29.5\"N 28°58'42.2\"E, or compact 410029.5N0285842.2E) · MGRS (35TPF6637041552) · UTM "
    "(35T 666370 4541552) · a GeoJSON or KML file's own points.\n"
    "Columns are recognized by name: name / label / site · lat / latitude · lon / lng / longitude · location / "
    "coordinate / mgrs / utm · radius / radius_m / radius_km / buffer (km unless the name or value says m, e.g. "
    "100m) · country / iso / country_code (two letters, e.g. TR) · reason / notes / description.")
FORMAT_HELP = f"Best format: {BEST_FORMAT}\n\nAccepted: {ACCEPTED_VALUES}\n\nFiles: {FILE_TYPES}. {LIMITS}"


class ImportError_(ValueError):
    """The file can't be read as a list of places: malformed, the wrong kind of file, or no usable points."""


@dataclass
class ImportResult:
    kind: str  # "GeoJSON", "KML", "CSV"
    columns: list[str]  # property / column names found
    mapping: dict[str, str | None]  # field -> column (None: not found)
    radius_unit: str  # "km" or "m"
    unit_stated: bool = True  # False: the file doesn't say; the review asks the person to confirm
    coordinate_form: str = "auto"  # how to read a single coordinate column (e.g. "lonlat" for a lon,lat column)
    places: list[dict] = field(default_factory=list)  # label, lat, lon, radius_km?, country?, reason?, given_as, line
    problems: list[tuple[str, str]] = field(default_factory=list)  # (where, what)
    warnings: list[str] = field(default_factory=list)  # things to check that don't stop the import


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


LON_FIRST = ("lonlat", "lnglat", "longlat", "xy")


def coordinate_form_for(column: str | None) -> str:
    """A column named lon/lat (or x,y) holds longitude first."""
    return "lonlat" if column and _key(column) in LON_FIRST else "auto"


def detect_mapping(columns: list[str], sample_rows: list[dict]) -> dict[str, str | None]:
    """Field -> column, by name first, then (for the coordinate) by which column's values read as coordinates."""
    mapping: dict[str, str | None] = {f: None for f in FIELDS}
    by_key = {_key(c): c for c in columns}
    for f, names in _NAMES.items():
        for n in names:
            if n in by_key and by_key[n] not in mapping.values():
                mapping[f] = by_key[n]
                break
    if mapping["lat"] and mapping["lon"]:
        mapping["coordinate"] = None  # separate columns win
    elif not mapping["coordinate"]:
        mapping["lat"] = mapping["lon"] = None
        best, score = None, 0
        for c in columns:
            if c in mapping.values():
                continue
            values = [str(r.get(c, "")).strip() for r in sample_rows if str(r.get(c, "")).strip()]
            ok = sum(1 for v in values if _reads(v))
            if values and ok / len(values) >= 0.8 and ok > score:
                best, score = c, ok
        mapping["coordinate"] = best
        if best is None and columns and all(re.fullmatch(r"column \d+", c) for c in columns):
            # Only a file without a header row: there, two neighbouring number columns are lat, lon.
            mapping["lat"], mapping["lon"] = _numeric_pair(columns, sample_rows, set(mapping.values()))
    return mapping


def _numeric_pair(columns, rows, taken) -> tuple[str | None, str | None]:
    """Two neighbouring all-number columns that fit latitude then longitude (a headerless lat,lon file)."""
    def numbers(c):
        try:
            return [float(r[c]) for r in rows if str(r.get(c, "")).strip()]
        except ValueError:
            return None

    free = [c for c in columns if c not in taken]
    for a, b in zip(free, free[1:]):
        lat, lon = numbers(a), numbers(b)
        if lat and lon and all(-90 <= v <= 90 for v in lat) and all(-180 <= v <= 180 for v in lon):
            return a, b
    return None, None


def _reads(text: str) -> bool:
    try:
        geo.parse_coordinate(text)
        return True
    except geo.CoordinateError:
        return False


def radius_unit_for(column: str | None) -> tuple[str, bool]:
    """(unit, stated): the unit the column name states (kilometres checked before metres, since "kilometers"
    contains "meters"), or km and False when the name doesn't say. Values never change the unit: one outlier
    mustn't reinterpret a whole column. The review asks the person to confirm an unstated unit."""
    if column:
        k = _key(column)
        if "km" in k or "kilomet" in k:
            return "km", True
        if k.endswith("m") or "meter" in k or "metre" in k:
            return "m", True
    return "km", False


def _radius_km(value, unit: str) -> float | None:
    text = str(value).strip().lower().replace(" ", "")
    if not text:
        return None
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)(km|m|meters?|metres?|kilometers?|kilometres?)?", text)
    if not m:
        raise ValueError(f"radius {value!r} isn't a number")
    number, suffix = float(m.group(1)), m.group(2)
    if suffix:
        unit = "km" if suffix.startswith("k") else "m"
    return number / 1000 if unit == "m" else number


# --- reading files -----------------------------------------------------------------------------------------------

def read_file(path: Path) -> tuple[str, list[dict], list[tuple[str, str]], dict[int, tuple[float, float]]]:
    try:
        return _read_file(path)
    except (TypeError, AttributeError, IndexError, KeyError, ValueError, RecursionError) as exc:
        if isinstance(exc, ImportError_):
            raise
        raise ImportError_(f"its structure isn't what a {path.suffix or 'places'} file should look like "
                           f"({type(exc).__name__}: {str(exc)[:80]})") from exc


def _read_file(path: Path) -> tuple[str, list[dict], list[tuple[str, str]], dict[int, tuple[float, float]]]:
    """(kind, rows of column -> value, problems, {row index: (lat, lon) from the file's own geometry})."""
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ImportError_(f"the file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    suffix = path.suffix.lower()
    data = path.read_bytes()
    if not data.strip():
        raise ImportError_("the file is empty")
    if suffix == ".kmz" or data[:4] == b"PK\x03\x04":
        if suffix != ".kmz":
            raise ImportError_("this is a zip archive (a spreadsheet workbook, for example), not a CSV, GeoJSON or "
                               "KML file: export the list as CSV")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                inner = next((i for i in z.infolist() if i.filename.lower().endswith(".kml")), None)
                if inner is None:
                    raise ImportError_("the KMZ file has no .kml inside")
                too_big = ImportError_(f"the KML inside the KMZ is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB "
                                       "once unzipped")
                if inner.file_size > MAX_FILE_BYTES:
                    raise too_big
                with z.open(inner) as member:  # bounded even if the size in the archive is wrong
                    data = member.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise too_big
        except zipfile.BadZipFile as exc:
            raise ImportError_("the KMZ file isn't a valid zip archive") from exc
        suffix = ".kml"
    text = _decode(data)
    if suffix in (".geojson", ".json") or text.lstrip().startswith("{"):
        return ("GeoJSON", *_geojson(text))
    if suffix == ".kml" or text.lstrip().startswith("<"):
        return ("KML", *_kml(text))
    return ("CSV", *_csv(text))


def _decode(data: bytes) -> str:
    """Text from the file's bytes: UTF-8 (with or without a BOM), UTF-16/32 with a BOM, or Windows-1252 (which
    some Windows tools still write). Binary files are refused."""
    for bom, codec in ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"), (b"\xff\xfe", "utf-16"),
                       (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            try:
                return data.decode(codec)
            except UnicodeDecodeError as exc:
                raise ImportError_("the file says it's Unicode text but can't be read as such") from exc
    if b"\x00" in data[:4096] or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"%PDF":
        raise ImportError_("this is a binary file (an image, PDF or spreadsheet?), not a text list of places")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _geojson(text: str):
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ImportError_(f"not valid GeoJSON ({exc})") from exc
    if isinstance(doc, dict) and doc.get("type") == "FeatureCollection":
        features = doc.get("features")
        if not isinstance(features, list):
            raise ImportError_("the FeatureCollection's \"features\" isn't a list")
    elif isinstance(doc, dict) and doc.get("type") == "Feature":
        features = [doc]
    else:
        raise ImportError_("expected a GeoJSON FeatureCollection or Feature")
    rows, problems, points = [], [], {}

    def point(c, where):
        if (not isinstance(c, list) or len(c) < 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                              for v in c[:2])):
            raise ImportError_(f"{where}: a point's coordinates must be [longitude, latitude] numbers")
        return c[1], c[0]  # GeoJSON is lon, lat

    for n, f in enumerate(features, start=1):
        where = f"feature {n}"
        if not isinstance(f, dict):
            raise ImportError_(f"{where} isn't a GeoJSON Feature object")
        g = f.get("geometry")
        if g is not None and not isinstance(g, dict):
            raise ImportError_(f"{where}: its geometry isn't an object")
        g = g or {}
        raw_props = f.get("properties") or {}
        if not isinstance(raw_props, dict):
            raise ImportError_(f"{where}: its properties aren't an object")
        props = {k: v for k, v in raw_props.items() if not isinstance(v, (dict, list))}
        coords = g.get("coordinates")
        if g.get("type") == "Point":
            points[len(rows)] = point(coords, where)
            rows.append({**props, "_line": where})
        elif g.get("type") == "MultiPoint":
            if not isinstance(coords, list):
                raise ImportError_(f"{where}: a MultiPoint's coordinates must be a list of points")
            for i, c in enumerate(coords, start=1):
                points[len(rows)] = point(c, f"{where}, point {i}")
                rows.append({**props, "_line": f"{where}, point {i}"})
        elif g:
            problems.append((where, f"{g.get('type')} skipped: only points can be places"))
        else:
            rows.append({**props, "_line": where})  # no geometry: maybe a coordinate column
        if len(rows) > MAX_ROWS:
            raise ImportError_(f"it has more than {MAX_ROWS:,} points, more than an import can hold; split the file")
    return rows, problems, points


def _kml(text: str):
    import xml.etree.ElementTree as ET

    if "<!ENTITY" in text or "<!DOCTYPE" in text:
        raise ImportError_("KML with a DOCTYPE or entities isn't accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ImportError_(f"not valid KML ({exc})") from exc

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    rows, problems, points = [], [], {}
    placemarks = [e for e in root.iter() if local(e.tag) == "Placemark"]
    if len(placemarks) > MAX_ROWS:
        raise ImportError_(f"it has {len(placemarks):,} placemarks, more than the {MAX_ROWS:,} an import can hold; "
                           "split the file")
    for n, pm in enumerate(placemarks, start=1):
        row = {"_line": f"placemark {n}"}
        geometry = None
        for e in pm.iter():
            name = local(e.tag)
            if name in ("name", "description") and e.text and name not in row:
                row[name] = e.text.strip()
            elif name == "Data" and e.get("name"):
                value = next((c.text for c in e if local(c.tag) == "value"), None)
                row[e.get("name")] = (value or "").strip()
            elif name == "SimpleData" and e.get("name"):
                row[e.get("name")] = (e.text or "").strip()
            elif name in ("Point", "LineString", "Polygon", "LinearRing") and geometry is None:
                geometry = (name, e)
        if geometry and geometry[0] == "Point":
            coords = next((c.text for c in geometry[1].iter() if local(c.tag) == "coordinates" and c.text), "")
            parts = coords.strip().split(",")
            try:
                points[len(rows)] = (float(parts[1]), float(parts[0]))  # KML is lon,lat[,alt]
            except (IndexError, ValueError):
                problems.append((row["_line"], f"unreadable coordinates {coords.strip()[:40]!r}"))
                continue
        elif geometry:
            problems.append((row["_line"], f"{geometry[0]} skipped: only points can be places"))
            continue
        rows.append(row)
    return rows, problems, points


def _csv(text: str):
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        lines = [r for r in csv.reader(io.StringIO(text), dialect) if any(cell.strip() for cell in r)]
    except csv.Error as exc:
        raise ImportError_(f"this isn't a readable CSV file ({exc})") from exc
    if not lines:
        raise ImportError_("the CSV file is empty")
    header = [h.strip() or f"column {i + 1}" for i, h in enumerate(lines[0])]
    seen = {}
    for i, h in enumerate(header):
        if _key(h) in seen:
            raise ImportError_(f"the column name {h!r} appears twice (columns {seen[_key(h)] + 1} and {i + 1}); "
                               "rename one so it's clear which to use")
        seen[_key(h)] = i
    known = sum(1 for h in header if any(_key(h) in names for names in _NAMES.values()))
    numeric = all(re.fullmatch(r"\s*[-+]?\d+(\.\d+)?\s*", c) for c in lines[0] if c.strip())
    if known == 0 and (numeric or any(_reads(c) for c in lines[0])):  # no header row: the first line is data
        header = [f"column {i + 1}" for i in range(len(lines[0]))]
        body, first = lines, 1
    else:
        body, first = lines[1:], 2
    if len(body) > MAX_ROWS:
        raise ImportError_(f"it has {len(body):,} rows, more than the {MAX_ROWS:,} an import can hold; split the file")
    rows = [{**dict(zip(header, cells)), "_line": f"line {first + i}"} for i, cells in enumerate(body)]
    return rows, [], {}


def build(kind: str, rows: list[dict], problems: list, points: dict, mapping: dict, radius_unit: str,
          columns: list[str], coordinate_form: str = "auto", unit_stated: bool = True) -> ImportResult:
    result = ImportResult(kind, columns, mapping, radius_unit, unit_stated, coordinate_form, problems=list(problems))
    for i, row in enumerate(rows):
        where = row.get("_line", f"row {i + 1}")
        try:
            if mapping.get("lat") and mapping.get("lon"):
                given = f"{row.get(mapping['lat'], '')}, {row.get(mapping['lon'], '')}"
                c = geo.parse_coordinate(given, "latlon")
            elif mapping.get("coordinate"):
                given = str(row.get(mapping["coordinate"], ""))
                c = geo.parse_coordinate(given, coordinate_form)
            elif i in points:
                lat, lon = points[i]
                given = geo.format_latlon(float(lat), float(lon))
                c = geo.parse_coordinate(given, "latlon")
            else:
                raise geo.CoordinateError("no coordinate (choose the column that holds it)")
        except (geo.CoordinateError, TypeError, ValueError) as exc:
            result.problems.append((where, str(exc)))
            continue
        place = {"lat": round(c.lat, 6), "lon": round(c.lon, 6), **geo.input_evidence(c, given),
                 "line": where}
        label = str(row.get(mapping["label"], "") if mapping.get("label") else "").strip()
        place["label"] = label  # blank stays blank: the person fills it in when reviewing the import
        if mapping.get("radius"):
            try:
                radius = _radius_km(row.get(mapping["radius"], ""), radius_unit)
            except ValueError as exc:
                result.problems.append((where, str(exc)))
                continue
            if radius is not None:
                place["radius_km"] = radius
        if mapping.get("country"):
            code = str(row.get(mapping["country"], "")).strip().upper()
            if re.fullmatch(r"[A-Z]{2}", code):
                place["country"] = code
            elif code:
                result.problems.append((where, f"country {code!r} isn't a two-letter code; choose it when reviewing"))
        if mapping.get("reason"):
            reason = " ".join(str(row.get(mapping["reason"], "")).split())
            if reason:
                place["reason"] = reason
        result.places.append(place)
    return result


def read_places(path: Path, *, mapping: dict | None = None, radius_unit: str | None = None,
                coordinate_form: str | None = None, max_radius_km: float = 25,
                require_places: bool = True) -> ImportResult:
    """Read and interpret a file. Without `mapping`, the columns are detected; pass one to override (and
    radius_unit / coordinate_form). With require_places, a file that yields no places is refused, with the reason."""
    kind, rows, problems, points = read_file(path)
    columns = list(dict.fromkeys(k for r in rows for k in r if k != "_line"))
    if mapping is None:
        mapping = detect_mapping(columns, rows[:50])
        if points:  # the file's own point geometry is the coordinate; property columns are only the fallback
            mapping.update(coordinate=None, lat=None, lon=None)
    detected_unit, stated = radius_unit_for(mapping.get("radius"))
    unit = radius_unit or detected_unit
    stated = stated or radius_unit is not None or not mapping.get("radius")
    form = coordinate_form or coordinate_form_for(mapping.get("coordinate"))
    result = build(kind, rows, problems, points, mapping, unit, columns, form, stated)
    if require_places and not result.places:
        raise ImportError_(_why_none(kind, rows, result, points))
    return result


def _why_none(kind: str, rows: list, result: ImportResult, points: dict) -> str:
    unit = {"CSV": "rows of data", "GeoJSON": "features", "KML": "placemarks"}[kind]
    if not rows and result.problems:
        return f"it has only lines or areas ({len(result.problems)} skipped); places must be points"
    if not rows:
        return f"it has no {unit}"
    m = result.mapping
    if not points and not m.get("coordinate") and not (m.get("lat") and m.get("lon")):
        shown = ", ".join(result.columns[:8]) + (" …" if len(result.columns) > 8 else "")
        return f"no column holds coordinates (its columns: {shown})"
    where, what = result.problems[0] if result.problems else ("", "")
    return f"none of its {len(rows)} {unit} has a readable coordinate (for example {where}: {what})"
