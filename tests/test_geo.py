"""Places: reading coordinates in the forms people have them, and checking them against country borders.

Reference values were computed with pyproj (UTM, EPSG:326xx/327xx) and the mgrs package, not with this code."""

import pytest

from lucid_warrant import geo
from lucid_warrant.geo import CoordinateError, parse_coordinate

REFERENCES = [  # name, lat, lon, zone, northern, easting, northing, MGRS
    ("Istanbul", 41.0082, 28.9784, 35, True, 666370.51, 4541552.49, "35TPF6637041552"),
    ("Sydney", -33.8688, 151.2093, 56, False, 334368.63, 6250948.35, "56HLH3436850948"),
    ("Buenos Aires", -34.6037, -58.3816, 21, False, 373317.5, 6170036.17, "21HUB7331770036"),
    ("Anchorage", 61.2181, -149.9003, 6, True, 344247.21, 6790536.87, "06VUN4424790536"),
    ("Quito", -0.1807, -78.4678, 17, False, 781861.46, 9980007.57, "17MQV8186180007"),
    ("Svalbard", 78.2232, 15.6267, 33, True, 514278.72, 8683355.47, "33XWG1427883355"),
]


def close(a, b, tol=1e-5):  # 1e-5° is about a metre
    return abs(a - b) <= tol


@pytest.mark.parametrize("name,lat,lon,zone,northern,easting,northing,ref_mgrs", REFERENCES)
def test_utm_and_mgrs_match_reference_conversions(name, lat, lon, zone, northern, easting, northing, ref_mgrs):
    z, n, e, north = geo.latlon_to_utm(lat, lon)
    assert (z, n) == (zone, northern) and abs(e - easting) < 0.05 and abs(north - northing) < 0.05, name
    band = geo._BANDS[int((lat + 80) // 8)] if lat < 72 else "X"
    utm = parse_coordinate(f"{zone}{band} {easting} {northing}")
    assert close(utm.lat, lat) and close(utm.lon, lon) and utm.form == "UTM", name
    m = parse_coordinate(ref_mgrs)
    assert close(m.lat, lat, 2e-5) and close(m.lon, lon, 2e-5) and m.form == "MGRS", (name, m)
    spaced = parse_coordinate(f"{ref_mgrs[:3]} {ref_mgrs[3:5]} {ref_mgrs[5:10]} {ref_mgrs[10:]}")
    assert (spaced.lat, spaced.lon) == (m.lat, m.lon)


@pytest.mark.parametrize("text", [
    "41.0082, 28.9784", "41.0082 28.9784", "41.0082,28.9784", "41.0082N 28.9784E", "41.0082N, 28.9784E",
    "N41.0082 E28.9784", "N 41.0082, E 28.9784", "41.0082 N 28.9784 E",
])
def test_decimal_degrees_spaced_or_not(text):
    c = parse_coordinate(text)
    assert close(c.lat, 41.0082) and close(c.lon, 28.9784) and c.form == "decimal degrees"


@pytest.mark.parametrize("text,form", [
    ('41°00\'29.52"N 28°58\'42.24"E', "degrees, minutes and seconds"),
    ("41 00 29.52 N 028 58 42.24 E", "degrees, minutes and seconds"),
    ("41 00 29.52 28 58 42.24", "degrees, minutes and seconds"),
    ("410029.52N0285842.24E", "compact degrees, minutes and seconds"),
    ("410029.52N 0285842.24E", "compact degrees, minutes and seconds"),
    ("N410029.52 E0285842.24", "compact degrees, minutes and seconds"),
    ("41°00.492'N 28°58.704'E", "degrees and decimal minutes"),
    ("41 00.492 N 028 58.704 E", "degrees and decimal minutes"),
    ("4100.492N02858.704E", "compact degrees and minutes"),
])
def test_minutes_and_seconds_forms(text, form):
    c = parse_coordinate(text)
    assert close(c.lat, 41.0082) and close(c.lon, 28.9784) and c.form == form, c


def test_southern_and_western_hemispheres():
    for text in ("-34.6037, -58.3816", "34.6037S 58.3816W", "S34.6037 W58.3816", "34 36 13.32 S 58 22 53.76 W"):
        c = parse_coordinate(text)
        assert close(c.lat, -34.6037) and close(c.lon, -58.3816), text


def test_longitude_first_is_allowed_only_when_labelled():
    c = parse_coordinate("28.9784E 41.0082N")
    assert close(c.lat, 41.0082) and close(c.lon, 28.9784)


@pytest.mark.parametrize("text,message", [
    ("", "enter a coordinate"),
    ("151.2093, -33.8688", "looks like lon, lat"),
    ("91, 100", "latitude must be between"),
    ("91, 10", "looks like lon, lat"),
    ("41.0082N 28.9784N", "both parts are marked N/S"),
    ("-41.0082S 28.9784E", "minus sign or a hemisphere letter"),
    ("41 61 00 N 28 0 0 E", "minutes must be less than 60"),
    ("41.5 30 N 28 0 E", "only the last number"),
    ("Istanbul", "couldn't read"),
    ("41.0082", "latitude then longitude"),
    ("35S 666370 4541552", "doesn't match its northing"),
    ("35TPF663704155", "same number of easting and northing digits"),
    ("4100492N02858704E", "isn't a valid compact"),
])
def test_unreadable_coordinates_say_why(text, message):
    with pytest.raises(CoordinateError, match=message):
        parse_coordinate(text)


def test_s_band_is_noted_and_checked():
    """35S is latitude band S (32°–40°N), not 'south'; a southern northing under it is refused."""
    c = parse_coordinate("35S 666370 4541552".replace("4541552", "4100000"))
    assert c.lat > 0 and "band S" in c.note


def test_places_are_checked_against_the_chosen_country():
    geo.check_place_country("TR", 41.0082, 28.9784)  # Istanbul
    with pytest.raises(CoordinateError, match=r"isn't inside Greece \(GR\): it's in Turkey \(TR\)"):
        geo.check_place_country("GR", 41.0082, 28.9784)
    with pytest.raises(CoordinateError, match="isn't inside any country"):
        geo.check_place_country("TR", 34.0, 30.0)  # the Mediterranean
    assert geo.country_at(-34.6037, -58.3816) == "AR" and geo.country_at(48.8566, 2.3522) == "FR"
    assert geo.country_at(51.5074, -0.1278) == "GB" and geo.country_at(-33.8688, 151.2093) == "AU"


def test_distance():
    assert abs(geo.distance_km(41.0082, 28.9784, 39.9334, 32.8597) - 351) < 3  # Istanbul to Ankara


def test_choosing_the_form_removes_guessing():
    c = parse_coordinate("28.9784, 41.0082", "lonlat")
    assert close(c.lat, 41.0082) and close(c.lon, 28.9784) and "lon, lat" in c.form
    assert close(parse_coordinate("28 58 42.24 41 00 29.52", "lonlat").lat, 41.0082)
    assert parse_coordinate("35TPF6637041552", "mgrs").form == "MGRS"
    with pytest.raises(CoordinateError, match="not an MGRS reference"):
        parse_coordinate("41.0082, 28.9784", "mgrs")
    with pytest.raises(CoordinateError, match="not UTM"):
        parse_coordinate("35TPF6637041552", "utm")
    assert parse_coordinate("41.0082, 28.9784", "latlon").form == "decimal degrees"


@pytest.mark.parametrize("text,lat,lon", [
    (".5, 30", 0.5, 30.0), ("-.5, 30", -0.5, 30.0), ("0030N00030E", 0.5, 0.5),
    ("0.0001, -0.0001", 0.0001, -0.0001), ("0 0 0 N 0 0 0 E", 0.0, 0.0)])
def test_round8_near_zero_and_leading_decimals(text, lat, lon):
    """Codex round 8 A8: shorthand near the equator and prime meridian is read exactly."""
    c = parse_coordinate(text)
    assert close(c.lat, lat) and close(c.lon, lon), (text, c)


@pytest.mark.parametrize("text,message", [
    ("41..5,28", "check the decimal points"), ("4 1.2.3, 4", "check the decimal points"),
    ("35TPF 6637 041552", "same number of easting and northing digits"), ("ZGC1234", "polar grid references"),
])
def test_round8_malformed_input_is_refused_not_reinterpreted(text, message):
    with pytest.raises(CoordinateError, match=message):
        parse_coordinate(text)


def test_round8_longitudes_wrap_and_the_antimeridian_is_one_line():
    c = parse_coordinate("60N 900000 0")
    assert -180 <= c.lon <= 180 and close(c.lon, -179.407673, 1e-4)
    assert geo.country_at(66, -180) == geo.country_at(66, 180) == "RU"
    assert geo.country_at(-16.5, 180) == geo.country_at(-16.5, -180) == "FJ"


@pytest.mark.parametrize("text,cell", [("35TPF", 100000), ("35TPF64", 10000), ("35TPF6641", 1000),
                                      ("35TPF663415", 100), ("35TPF66374155", 10), ("35TPF6637041552", 1)])
def test_round9_mgrs_precision_is_retained(text, cell):
    c = parse_coordinate(text)
    assert c.cell_size_m == cell
    place = {"lat": round(c.lat, 6), "lon": round(c.lon, 6), "radius_km": .1, **geo.input_evidence(c, text)}
    reopened = geo.input_evidence(parse_coordinate(c.text()), c.text(), place)
    assert reopened["given_as"] == text and reopened["cell_size_m"] == cell
    assert geo.precision_needs_review(place) == (cell >= 1000)
    place["precision_acknowledged"] = geo.precision_key(place)
    assert not geo.precision_needs_review(place)
    place["radius_km"] = .01
    assert geo.precision_needs_review(place) == (cell >= 100)


def test_round9_decimal_places_do_not_claim_accuracy():
    c = parse_coordinate("41.008200, 28.978400")
    assert c.cell_size_m is None
    assert "square" not in geo.evidence_text(geo.input_evidence(c, c.text()))
