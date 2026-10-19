"""Places in the geographic constraint: each signed with its own radius, checked against its country, added to
every search as the organization's place clause, and enforced again on what comes back."""

import pytest

from lucid_warrant.executor import ExecutionDenied, run_template
from lucid_warrant.places import (DEFAULT_RADIUS_KM, PlaceError, check_place, clause, duplicate_of,
                                  outside_places, place_filter)
from lucid_warrant.rbac import get_rbac
from lucid_warrant.warrant import WarrantError, build_warrant, save_warrant

ISTANBUL = {"place_id": "p-istanbul-office", "label": "Istanbul office", "lat": 41.0082, "lon": 28.9784,
            "radius_km": DEFAULT_RADIUS_KM, "country": "TR", "reason": "Client's registered office", "source": "agent"}
TERMS = [{"term_id": "t-project", "literal": "PROJECT_CEDAR", "source": "human"}]


def make(paths, places, **kw):
    return build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="p", ttl_minutes=60, max_rows=50,
                         terms=TERMS, rbac=get_rbac(paths), paths=paths, places=places, **kw)


def test_default_radius_is_100_m_and_signed_per_place(paths):
    doc = make(paths, [ISTANBUL, {**ISTANBUL, "place_id": "p-ankara", "label": "Ankara branch", "lat": 39.9334,
                                  "lon": 32.8597, "radius_km": 2}])
    assert [p["radius_km"] for p in doc["places"]] == [0.1, 2.0]
    assert clause("INTERSECTS({lat},{lon},{km})", doc["places"][0]) == "INTERSECTS(41.0082,28.9784,0.1)"


@pytest.mark.parametrize("change,message", [
    ({"country": ""}, "choose the country it's in first"),
    ({"country": "GR"}, "isn't inside Greece"),
    ({"radius_km": 30}, "the radius must be 0.01 to 25 km"),
    ({"radius_km": 0.001}, "the radius must be"),
    ({"reason": ""}, "give a reason"),
    ({"lat": 141.0}, "latitude must be"),
])
def test_a_place_is_refused_until_it_is_right(paths, change, message):
    with pytest.raises(WarrantError, match=message):
        make(paths, [{**ISTANBUL, **change}])


def test_a_place_must_be_in_a_permitted_country(paths):
    with pytest.raises(WarrantError, match="TR isn't permitted"):
        make(paths, [ISTANBUL], country_exclude=[{"code": "TR", "reason": "test"}])
    with pytest.raises(PlaceError, match="TR isn't permitted"):
        check_place(ISTANBUL, {"include": [{"code": "GB", "reason": ""}], "exclude": []}, 25)


class Recording:
    def __init__(self, results):
        self.payloads, self.results = [], results

    def search(self, payload, plan):
        self.payloads.append(payload)
        return list(self.results)


def test_every_search_is_limited_to_the_places_and_results_are_checked(paths, key, library):
    doc = make(paths, [ISTANBUL])
    save_warrant(paths, doc, key)
    inside = {"record_id": "a", "lat": 41.0085, "lon": 28.9786, "country": "TR"}  # ~40 m away
    outside = {"record_id": "b", "lat": 41.02, "lon": 28.98, "country": "TR"}  # ~1.3 km away
    no_coords = {"record_id": "c", "country": "TR"}
    backend = Recording([inside, outside, no_coords])
    result = run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term",
                          bindings={"term": "t-project"}, backend=backend)
    sent = backend.payloads[0]
    assert sent["query"].endswith("AND (INTERSECTS(41.0082,28.9784,0.1))") or "AND (INTERSECTS(41.0082,28.9784,0.1))" in sent["query"]
    assert sent["place_filter"]["places"][0]["radius_km"] == 0.1 and sent["place_filter"]["relation"] == "within"
    assert [r["record_id"] for r in result["results"]] == ["a"], "outside the circle, or not locatable, is withheld"
    assert result["withheld_outside_geographic_constraint"] == 2


def test_the_signed_template_is_used_and_lint_accepts_only_exact_clauses(paths):
    doc = {"places": [ISTANBUL], "config_context": {"values": {"geography": {"place_clause": "loc:WITHIN({lon} {lat} {km})"}}}}
    assert place_filter(doc)["clauses"] == ["loc:WITHIN(28.9784 41.0082 0.1)"]
    from lucid_warrant.lint import lint_query

    ok = lint_query('(body:"PROJECT_CEDAR") AND (INTERSECTS(41.0082,28.9784,0.1))', {"PROJECT_CEDAR"},
                    allowed_fields={"body"}, place_clauses=["INTERSECTS(41.0082,28.9784,0.1)"])
    assert ok.passed, ok.summary()
    widened = lint_query('(body:"PROJECT_CEDAR") AND (INTERSECTS(41.0082,28.9784,50))', {"PROJECT_CEDAR"},
                         allowed_fields={"body"}, place_clauses=["INTERSECTS(41.0082,28.9784,0.1)"])
    assert not widened.passed, "a clause that isn't exactly the signed one is refused"


def test_mock_search_honours_places():
    from lucid_warrant.backend import _in_places

    pf = place_filter({"places": [ISTANBUL]})
    assert _in_places(pf, {"lat": 41.0085, "lon": 28.9786}) and not _in_places(pf, {"lat": 41.02, "lon": 28.98})
    assert not _in_places(pf, {}) and _in_places(None, {})
    assert outside_places(pf, {"lat": "x", "lon": 1}), "unreadable coordinates aren't shown as inside"


# --- duplicates: the same label, or the same point under another name, can't both be in a warrant ------------

ANKARA = {**ISTANBUL, "place_id": "p-ankara", "label": "Ankara branch", "lat": 39.9334, "lon": 32.8597}


def test_duplicate_labels_ignore_case_and_spacing():
    assert duplicate_of({**ANKARA, "label": "  istanbul   OFFICE "}, [ISTANBUL]) == \
        "istanbul OFFICE: another place already has this label"
    assert duplicate_of(ANKARA, [ISTANBUL]) is None


def test_the_same_point_written_another_way_is_a_duplicate():
    from lucid_warrant.geo import parse_coordinate

    mgrs = parse_coordinate("35TPF6637041552")  # Istanbul office, as MGRS: well under a metre from the decimal form
    same = {**ANKARA, "label": "Old town desk", "lat": mgrs.lat, "lon": mgrs.lon}
    assert duplicate_of(same, [ISTANBUL]) == "Old town desk: same coordinates as 'Istanbul office'"
    near = {**same, "lat": ISTANBUL["lat"] + 0.00005}  # about 5.5 m away: a different point
    assert duplicate_of(near, [ISTANBUL]) is None


def test_a_place_is_not_its_own_duplicate():
    assert duplicate_of(ISTANBUL, [ISTANBUL]) is None
    assert duplicate_of(dict(ISTANBUL), [ISTANBUL]) is None, "same place_id: the same place, edited"


@pytest.mark.parametrize("twin,message", [
    ({"label": "ISTANBUL OFFICE", "lat": 39.9334, "lon": 32.8597}, "another place already has this label"),
    ({"label": "Old town desk"}, "same coordinates as 'Istanbul office'"),
])
def test_a_warrant_cant_be_signed_with_duplicate_places(paths, twin, message):
    with pytest.raises(WarrantError, match=message):
        make(paths, [ISTANBUL, {**ISTANBUL, "place_id": "p-twin", **twin}])


def test_round8_unlocatable_results_are_withheld_and_counted_by_cause(paths, key, library):
    """Codex round 8 A14/A18/A4: no coordinates, impossible or infinite ones are withheld (not trusted), counted
    separately from records outside the circle, and nothing crashes after sending."""
    doc = make(paths, [ISTANBUL])
    save_warrant(paths, doc, key)
    rows = [{"record_id": "in", "lat": 41.0085, "lon": 28.9786, "country": "TR"},
            {"record_id": "out", "lat": 41.02, "lon": 28.98, "country": "TR"},
            {"record_id": "none", "country": "TR"},
            {"record_id": "wrapped", "lat": 401.0082, "lon": 28.9784, "country": "TR"},
            {"record_id": "inf", "lat": "Infinity", "lon": 29, "country": "TR"}]
    result = run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term",
                          bindings={"term": "t-project"}, backend=Recording(rows))
    assert [r["record_id"] for r in result["results"]] == ["in"]
    assert result["withheld_detail"] == {"countries": 0, "places": 1, "no_location": 3}
    import json as _json
    last = _json.loads(paths.audit_log.read_text().splitlines()[-1])
    assert last["outcome"] == "ok" and last["withheld_detail"] == {"places": 1, "no_location": 3}
    from lucid_warrant.ui.document import withheld_text
    assert "1 outside every included place; 3 without a usable location" in withheld_text(4, last["withheld_detail"])


def test_round8_a_response_that_cant_be_processed_is_still_audited(paths, key, library):
    doc = make(paths, [ISTANBUL])
    save_warrant(paths, doc, key)

    class Odd:
        def search(self, payload, plan):
            return 42  # not iterable

    from lucid_warrant.backend import BackendError
    with pytest.raises(BackendError, match="couldn't be processed"):
        run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-project"},
                     backend=Odd())
    import json as _json
    assert _json.loads(paths.audit_log.read_text().splitlines()[-1])["outcome"] == "error"


def test_round9_signing_preserves_precision_and_requires_acknowledgment(paths):
    from lucid_warrant import geo

    c = geo.parse_coordinate("35TPF6641")
    place = {**ISTANBUL, "lat": round(c.lat, 6), "lon": round(c.lon, 6), **geo.input_evidence(c, "35TPF6641")}
    with pytest.raises(PlaceError, match="smaller than the input's uncertainty"):
        check_place(place, {}, 25)
    place["precision_acknowledged"] = geo.precision_key(place)
    signed = make(paths, [place])["places"][0]
    assert signed["given_as"] == "35TPF6641" and signed["cell_size_m"] == 1000
    assert signed["precision_acknowledged"] == geo.precision_key(signed)
    with pytest.raises(PlaceError, match="uncertainty"):
        check_place({**signed, "radius_km": .05}, {}, 25)
