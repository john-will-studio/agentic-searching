"""Importing a list of places: the coordinate, label, radius (with units), country and reason are found by
column name or content, in CSV, GeoJSON and KML/KMZ."""

from pathlib import Path

import pytest

from lucid_warrant.place_import import ImportError_, read_places

HERE = Path(__file__).parent / "fixtures" / "places"


def test_csv_with_separate_lat_lon_and_a_radius_in_metres():
    r = read_places(HERE / "sites.csv")
    assert r.kind == "CSV" and r.mapping["lat"] == "Latitude" and r.mapping["lon"] == "Longitude"
    assert r.mapping["label"] == "Site Name" and r.mapping["radius"] == "Radius (m)" and r.radius_unit == "m"
    assert [(p["label"], p["radius_km"], p.get("country")) for p in r.places] == [
        ("Istanbul office", 0.1, "TR"), ("Ankara branch", 0.25, "TR")]
    assert r.places[0]["reason"] == "Registered office"
    assert r.problems and r.problems[0][0] == "line 4", "the bad row is reported by line, not dropped silently"


def test_csv_with_one_coordinate_column_in_mixed_forms():
    r = read_places(HERE / "grid.csv")
    assert r.mapping["coordinate"] == "location" and r.radius_unit == "km"
    ny, london = r.places
    assert abs(ny["lat"] - 40.7128) < 1e-3 and ny["read_as"] == "MGRS" and ny["radius_km"] == 0.2
    assert abs(london["lat"] - 51.5074) < 1e-3 and london["lon"] < 0


def test_csv_without_a_header_is_detected_by_content():
    r = read_places(HERE / "noheader.csv")
    assert len(r.places) == 2 and r.places[0]["label"] == "", "a missing label is left for the person to fill in"


def test_geojson_points_multipoints_and_skipped_areas():
    r = read_places(HERE / "sites.geojson")
    assert r.kind == "GeoJSON" and [p["label"] for p in r.places] == ["Istanbul office", "Branches", "Branches"]
    first = r.places[0]
    assert abs(first["lat"] - 41.0082) < 1e-6 and abs(first["lon"] - 28.9784) < 1e-6, "GeoJSON lon, lat is swapped"
    assert first["radius_km"] == 0.15 and first["country"] == "TR" and first["reason"] == "Registered office"
    assert any("Polygon skipped" in what for _w, what in r.problems)


@pytest.mark.parametrize("name", ["sites.kml", "sites.kmz"])
def test_kml_and_kmz(name):
    r = read_places(HERE / name)
    assert r.kind == "KML" and len(r.places) == 1
    p = r.places[0]
    assert p["label"] == "Istanbul office" and abs(p["lat"] - 41.0082) < 1e-6 and p["radius_km"] == 0.3
    assert p["country"] == "TR" and any("LineString skipped" in what for _w, what in r.problems)


def test_the_mapping_can_be_overridden(tmp_path):
    r = read_places(HERE / "sites.csv", mapping={"lat": "Latitude", "lon": "Longitude", "label": "Notes",
                                                 "radius": None, "country": None, "reason": None, "coordinate": None})
    assert r.places[0]["label"] == "Registered office" and "radius_km" not in r.places[0]


def test_unsafe_or_oversized_files_are_refused(tmp_path):
    bomb = tmp_path / "x.kml"
    bomb.write_text('<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaa">]><kml>&a;</kml>')
    with pytest.raises(ImportError_, match="DOCTYPE"):
        read_places(bomb)
    big = tmp_path / "big.csv"
    big.write_bytes(b"a" * (5 * 1024 * 1024 + 1))
    with pytest.raises(ImportError_, match="larger than"):
        read_places(big)


MALFORMED = HERE / "malformed"


@pytest.mark.parametrize("name,message", [
    ("empty.csv", "the file is empty"),
    ("header_only.csv", "it has no rows of data"),
    ("image_renamed.csv", "binary file"),
    ("zip_renamed.csv", "export the list as CSV"),
    ("no_coordinates.csv", "no column holds coordinates (its columns: name, city, notes)"),
    ("unreadable_coordinates.csv", "none of its 2 rows of data has a readable coordinate"),
    ("truncated.geojson", "not valid GeoJSON"),
    ("not_geojson.json", "expected a GeoJSON FeatureCollection"),
    ("areas_only.geojson", "only lines or areas"),
    ("broken.kml", "not valid KML"),
    ("no_placemarks.kml", "it has no placemarks"),
    ("not_a_zip.kmz", "isn't a valid zip archive"),
    ("zip_without_kml.kmz", "has no .kml inside"),
    ("too_big.csv", "larger than 5 MB"),
])
def test_malformed_files_say_what_is_wrong(name, message):
    """Smoke: genuinely unusable files are refused with the reason (never a crash, never a silent empty import)."""
    with pytest.raises(ImportError_) as err:
        read_places(MALFORMED / name)
    assert message in str(err.value)


def test_utf16_text_is_read():
    r = read_places(MALFORMED / "utf16.csv")
    assert r.places[0]["label"] == "Istanbul office" and abs(r.places[0]["lat"] - 41.0082) < 1e-6


def test_format_help_covers_the_basics():
    from lucid_warrant.place_import import FORMAT_HELP

    for text in ("name,lat,lon,radius_m,country,reason", "MGRS", "UTM", ".kmz", "5 MB"):
        assert text in FORMAT_HELP


def test_a_failed_tkdnd_load_is_retried(monkeypatch):
    """On Windows, Tcl sometimes can't open tkdnd's pkgIndex.tcl on the first try (a briefly locked file), and drag
    and drop went missing in about one launch in five. A failed load is retried; an unsupported platform isn't."""
    from tkinterdnd2 import TkinterDnD

    from lucid_warrant.ui import places_panel

    monkeypatch.setattr(places_panel.time, "sleep", lambda _s: None)
    calls = []

    def flaky(_root):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("Unable to load tkdnd library.")
        return "2.10.3"

    monkeypatch.setattr(TkinterDnD, "_require", flaky)
    places_panel._load_tkdnd(None)
    assert len(calls) == 3
    calls.clear()
    monkeypatch.setattr(TkinterDnD, "_require", lambda _r: calls.append(1) or (_ for _ in ()).throw(
        RuntimeError("Plaform not supported.")))
    with pytest.raises(RuntimeError, match="not supported"):
        places_panel._load_tkdnd(None)
    assert len(calls) == 1, "not retried"


def test_round8_column_order_units_duplicates_and_limits(tmp_path):
    """Codex round 8 A10-A12: what a file means is never guessed silently."""
    lonlat = tmp_path / "lonlat.csv"
    lonlat.write_text('name,lonlat\nOffice,"28.9784,41.0082"\n')
    r = read_places(lonlat)
    assert r.coordinate_form == "lonlat" and abs(r.places[0]["lat"] - 41.0082) < 1e-6
    dup = tmp_path / "dup.csv"
    dup.write_text("name,lat,lon,lat\nOffice,41.0082,28.9784,39.9334\n")
    with pytest.raises(ImportError_, match="appears twice"):
        read_places(dup)
    km = tmp_path / "km.csv"
    km.write_text("name,lat,lon,radius_kilometers\nOffice,41.0082,28.9784,10\n")
    r = read_places(km)
    assert r.radius_unit == "km" and r.places[0]["radius_km"] == 10 and r.unit_stated
    unstated = tmp_path / "unstated.csv"
    unstated.write_text("name,lat,lon,radius\nA,41.0082,28.9784,0.1\nB,39.9334,32.8597,500\n")
    r = read_places(unstated)
    assert r.radius_unit == "km" and not r.unit_stated, "one large value doesn't reinterpret the whole column"
    named = tmp_path / "named.csv"
    named.write_text("site,elevation,population\nA,41.0,28.9\n")
    with pytest.raises(ImportError_, match="no column holds coordinates"):
        read_places(named)  # named columns aren't guessed as lat, lon
    many = tmp_path / "many.csv"
    many.write_text("name,lat,lon\n" + "".join(f"P{i},41.0,28.9\n" for i in range(2001)))
    with pytest.raises(ImportError_, match="split the file"):
        read_places(many)


@pytest.mark.parametrize("doc", ['{"type":"FeatureCollection","features":{}}', '{"type":"FeatureCollection","features":[3]}',
                                 '{"type":"Feature","geometry":{"type":"MultiPoint","coordinates":[[1]]},"properties":{}}',
                                 '{"type":"Feature","geometry":{"type":"Point","coordinates":["a","b"]}}'])
def test_round8_structurally_malformed_geojson_is_reported(tmp_path, doc):
    f = tmp_path / "x.geojson"
    f.write_text(doc)
    with pytest.raises(ImportError_):
        read_places(f)


def test_round8_kmz_size_is_checked_after_unzipping(tmp_path):
    import zipfile

    kmz = tmp_path / "big.kmz"
    with zipfile.ZipFile(kmz, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", '<?xml version="1.0"?><kml><!--' + "x" * (6 * 1024 * 1024) + "--></kml>")
    assert kmz.stat().st_size < 100_000
    with pytest.raises(ImportError_, match="once unzipped"):
        read_places(kmz)

def test_round9_import_keeps_original_format_and_cell_size(tmp_path):
    path = tmp_path / "grid.csv"
    path.write_text('name,coordinate,radius_m,country,reason\nOffice,35TPF6641,100,TR,Review\n')
    place = read_places(path).places[0]
    assert (place["given_as"], place["read_as"], place["cell_size_m"]) == ("35TPF6641", "MGRS", 1000)
