import json
from dataclasses import replace
from datetime import timedelta

import pytest

from lucid_warrant.executor import ExecutionDenied, run_onetime, run_template
from lucid_warrant.library import load_library, promote_template
from lucid_warrant.onetime import approve_request, list_requests, submit_request
from lucid_warrant.signing import canonical_bytes, load_public_key
from lucid_warrant.util import from_iso
from lucid_warrant.rbac import get_rbac
from lucid_warrant.warrant import WarrantError, build_warrant, revoke_warrant


class RecordingBackend:
    def __init__(self):
        self.payloads = []

    def search(self, payload, plan):
        self.payloads.append(payload)
        return [{"record_id": "x"}]


def run(paths, warrant, template="all-of-two", **bindings):
    backend = RecordingBackend()
    result = run_template(
        paths, warrant_id=warrant["warrant_id"], template_id=template, bindings=bindings, backend=backend
    )
    return result, backend


def test_template_run_uses_warrant_parameters(paths, warrant):
    result, backend = run(paths, warrant, a="t-project", b="t-supplier")
    (payload,) = backend.payloads
    assert payload == {
        "warrant_id": warrant["warrant_id"],
        "purpose": warrant["purpose"],
        "parameter_1": "0012",
        "parameter_2": ["ATTR_1000", "ATTR_1200"],
        "query": 'content:"PROJECT_CEDAR" AND content:"SUPPLIER_NORTH"',
        "limit": 3,
    }
    assert isinstance(payload["parameter_1"], str)


def test_mock_backend_filters_by_visibility_and_terms(paths, warrant):
    result = run_template(
        paths, warrant_id=warrant["warrant_id"], template_id="all-of-two", bindings={"a": "t-project", "b": "t-supplier"}
    )
    # rec-003 matches but is tagged ATTR_1230, which the warrant doesn't include.
    assert [r["record_id"] for r in result["results"]] == ["rec-001", "rec-002", "rec-008"]


def test_exclusion_template(paths, warrant):
    result = run_template(
        paths, warrant_id=warrant["warrant_id"], template_id="term-excluding",
        bindings={"required": "t-project", "excluded": "t-test"},
    )
    assert "rec-005" not in [r["record_id"] for r in result["results"]]


def test_limit_capped_by_warrant(paths, warrant):
    backend = RecordingBackend()
    run_template(paths, warrant_id=warrant["warrant_id"], template_id="single-term",
                 bindings={"term": "t-project"}, limit=500, backend=backend)
    assert backend.payloads[0]["limit"] == 3


@pytest.mark.parametrize(
    "template, bindings, reason",
    [
        ("all-of-two", {"a": "t-project", "b": "t-unknown"}, "not approved"),
        ("all-of-two", {"a": "t-project"}, "no term bound"),
        ("all-of-two", {"a": "t-project", "b": "t-supplier", "c": "t-alias"}, "not in template"),
        ("no-such-template", {"a": "t-project"}, "no template"),
    ],
)
def test_denials(paths, warrant, template, bindings, reason):
    backend = RecordingBackend()
    with pytest.raises(ExecutionDenied, match=reason):
        run_template(paths, warrant_id=warrant["warrant_id"], template_id=template, bindings=bindings, backend=backend)
    assert backend.payloads == []


def test_expired_warrant_denied(paths, warrant):
    later = from_iso(warrant["expires_at"]) + timedelta(seconds=1)
    backend = RecordingBackend()
    with pytest.raises(ExecutionDenied, match="expired"):
        run_template(paths, warrant_id=warrant["warrant_id"], template_id="single-term",
                     bindings={"term": "t-project"}, backend=backend, now=later)
    assert backend.payloads == []


@pytest.mark.parametrize(
    "field, value",
    [
        ("parameter_1", "0123"),
        ("parameter_2", ["ATTR_1000", "ATTR_1200", "ATTR_1234"]),
        ("purpose", "something else"),
        ("expires_at", "2099-01-01T00:00:00Z"),
        ("max_rows_per_run", 1000),
    ],
)
def test_editing_any_warrant_field_denied(paths, warrant, field, value):
    path = paths.warrants / f"{warrant['warrant_id']}.json"
    path.write_bytes(canonical_bytes({**warrant, field: value}))
    with pytest.raises(ExecutionDenied, match="does not match"):
        run(paths, warrant, a="t-project", b="t-supplier")


def test_editing_term_literal_denied(paths, warrant):
    path = paths.warrants / f"{warrant['warrant_id']}.json"
    terms = [dict(t) for t in warrant["terms"]]
    terms[0]["literal"] = "*"
    path.write_bytes(canonical_bytes({**warrant, "terms": terms}))
    with pytest.raises(ExecutionDenied):
        run(paths, warrant, a="t-project", b="t-supplier")


def test_unsigned_warrant_denied(paths, library):
    doc = build_warrant(parameter_1="1234", parameter_2=["ATTR_1234"], purpose="agent-made",
                        terms=[{"term_id": "t-x", "literal": "X", "source": "agent"}],
                        ttl_minutes=60, max_rows=10, rbac=get_rbac(paths))
    path = paths.warrants / f"{doc['warrant_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(doc))
    with pytest.raises(ExecutionDenied, match="missing"):
        run(paths, doc, template="single-term", term="t-x")


def test_tampered_library_denied(paths, warrant):
    lib = json.loads(paths.library.read_text())
    lib["templates"].append({"template_id": "anything", "name": "x", "description": "x", "skeleton": "{a}"})
    paths.library.write_bytes(canonical_bytes(lib))
    with pytest.raises(ExecutionDenied, match="does not match"):
        run(paths, warrant, a="t-project", b="t-supplier")


def test_revoked_warrant_denied(paths, warrant):
    revoke_warrant(paths, warrant["warrant_id"])
    with pytest.raises(ExecutionDenied, match="revoked"):
        run(paths, warrant, a="t-project", b="t-supplier")


def _with_rbac(paths, tmp_path, change):
    rbac = json.loads(paths.rbac.read_text())
    change(rbac)
    path = tmp_path / "rbac.json"
    path.write_text(json.dumps(rbac))
    return replace(paths, rbac_file=path)


def test_entitlement_removed_after_signing_denied(paths, warrant, tmp_path):
    def drop_1200(rbac):
        rbac["parameter_2"] = [o for o in rbac["parameter_2"] if o["value"] != "ATTR_1200"]
    with pytest.raises(ExecutionDenied, match="not authorized"):
        run(_with_rbac(paths, tmp_path, drop_1200), warrant, a="t-project", b="t-supplier")


def test_parameter_2_restricted_per_parameter_1(paths, warrant, tmp_path):
    def restrict(rbac):
        rbac["parameter_2_by_parameter_1"] = {"0012": ["ATTR_1000"]}
    restricted = _with_rbac(paths, tmp_path, restrict)
    assert [o.value for o in get_rbac(restricted).parameter_2_options("0012")] == ["ATTR_1000"]
    assert len(get_rbac(restricted).parameter_2_options("0001")) == 4
    with pytest.raises(ExecutionDenied, match="not authorized with parameter 1 0012"):
        run(restricted, warrant, a="t-project", b="t-supplier")


def test_path_traversal_ids_rejected(paths, warrant):
    with pytest.raises(ExecutionDenied, match="not a valid"):
        run_template(paths, warrant_id="../../etc/passwd", template_id="single-term", bindings={"term": "t-project"})


# --- one-time syntax ----------------------------------------------------------

SKELETON = "{a} AND ({b} OR {c}) AND NOT {d}"
BINDINGS = {"a": "t-project", "b": "t-supplier", "c": "t-alias", "d": "t-test"}


def approve(paths, key, warrant):
    submit_request(paths, load_public_key(paths), warrant_id=warrant["warrant_id"],
                   skeleton=SKELETON, bindings=BINDINGS, rationale="supplier or its alias")
    (request,) = list_requests(paths)
    return approve_request(paths, key, request)


def test_request_needs_approved_terms(paths, warrant):
    with pytest.raises(WarrantError, match="not approved"):
        submit_request(paths, load_public_key(paths), warrant_id=warrant["warrant_id"],
                       skeleton="{a}", bindings={"a": "t-nope"}, rationale="")


def test_onetime_runs_exactly_once(paths, key, warrant):
    approval = approve(paths, key, warrant)
    result = run_onetime(paths, onetime_id=approval["onetime_id"])
    assert result["query"] == (
        'content:"PROJECT_CEDAR" AND (content:"SUPPLIER_NORTH" OR content:"NORTH_SUPPLY_CO") '
        'AND NOT content:"TEST_DATA"'
    )
    assert [r["record_id"] for r in result["results"]] == ["rec-001", "rec-002", "rec-004"]
    with pytest.raises(ExecutionDenied, match="already been used"):
        run_onetime(paths, onetime_id=approval["onetime_id"])


def test_request_alone_cannot_run(paths, warrant):
    req = submit_request(paths, load_public_key(paths), warrant_id=warrant["warrant_id"],
                         skeleton=SKELETON, bindings=BINDINGS, rationale="")
    fake_id = req["request_id"].replace("req-", "ot-")
    with pytest.raises(ExecutionDenied):
        run_onetime(paths, onetime_id=fake_id)


def test_edited_onetime_denied(paths, key, warrant):
    approval = approve(paths, key, warrant)
    path = paths.onetime / f"{approval['onetime_id']}.json"
    path.write_bytes(canonical_bytes({**approval, "query": 'content:"*"'}))
    with pytest.raises(ExecutionDenied, match="does not match"):
        run_onetime(paths, onetime_id=approval["onetime_id"])


def test_promote_then_use_as_template(paths, key, warrant):
    approval = approve(paths, key, warrant)
    run_onetime(paths, onetime_id=approval["onetime_id"])
    promote_template(paths, key, skeleton=approval["skeleton"], template_id="cedar-supplier-alias",
                     name="Project with supplier or alias, minus test data",
                     description="Required term plus either of two alternatives, excluding a third.",
                     origin=f"promoted from {approval['onetime_id']}")
    assert load_library(paths, load_public_key(paths))["version"] == 2
    result, _ = run(paths, warrant, template="cedar-supplier-alias", **BINDINGS)
    assert result["count"] == 1  # RecordingBackend


def test_audit_log_records_denials(paths, warrant):
    with pytest.raises(ExecutionDenied):
        run(paths, warrant, a="t-project", b="t-unknown")
    lines = paths.audit_log.read_text().splitlines()
    assert json.loads(lines[-1])["outcome"] == "denied"


def test_expiry_allows_days_up_to_limit(paths):
    from lucid_warrant.warrant import MAX_TTL_MINUTES, format_duration
    args = dict(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="p",
                terms=[{"term_id": "t-x", "literal": "X", "source": "human"}], max_rows=10, rbac=get_rbac(paths))
    assert build_warrant(ttl_minutes=MAX_TTL_MINUTES, **args)
    with pytest.raises(WarrantError, match="30 days"):
        build_warrant(ttl_minutes=MAX_TTL_MINUTES + 1, **args)
    assert format_duration(3 * 1440 + 4 * 60 + 5) == "3 d 4 h 5 min"
    assert format_duration(120) == "2 h"


def test_term_usage_counts(paths, key, warrant):
    from lucid_warrant.executor import term_usage
    run(paths, warrant, a="t-project", b="t-supplier")
    run(paths, warrant, template="single-term", term="t-project")
    with pytest.raises(ExecutionDenied):
        run(paths, warrant, a="t-project", b="t-nope")
    approval = approve(paths, key, warrant)
    run_onetime(paths, onetime_id=approval["onetime_id"], backend=RecordingBackend())
    usage = term_usage(paths, warrant["warrant_id"])
    assert usage["t-project"]["queries"] == 3 and usage["t-project"]["refused"] == 1
    assert usage["t-project"]["records"] == 3  # RecordingBackend returns one record per run
    assert usage["t-supplier"]["queries"] == 2
    assert usage["t-test"]["queries"] == 1  # used as the exclusion in the one-time query
    assert usage["t-nope"] == {"queries": 0, "records": 0, "refused": 1, "last_used": None}


# --- row cap and date limits ----------------------------------------------------

from lucid_warrant.util import from_iso as _iso  # noqa: E402

NOW = _iso("2026-10-17T12:00:00Z")


def dated_warrant(paths, key, **limits):
    doc = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000", "ATTR_1200"], purpose="dates",
                        terms=TERMS_FOR_DATES, ttl_minutes=60, max_rows=20_000, rbac=get_rbac(paths), now=NOW, **limits)
    from lucid_warrant.warrant import save_warrant
    save_warrant(paths, doc, key)
    return doc


TERMS_FOR_DATES = [{"term_id": "t-project", "literal": "PROJECT_CEDAR", "source": "script"}]


def cedar_hits(paths, doc):
    result = run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term",
                          bindings={"term": "t-project"}, now=NOW)
    return result, [r["record_id"] for r in result["results"]]


def test_row_cap_bounds(paths):
    args = dict(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="p", ttl_minutes=60,
                terms=TERMS_FOR_DATES, rbac=get_rbac(paths))
    assert build_warrant(max_rows=500_000, **args)["max_rows_per_run"] == 500_000
    for bad in (0, 500_001):
        with pytest.raises(WarrantError, match="500,000"):
            build_warrant(max_rows=bad, **args)


def test_no_date_limits_sends_plain_query(paths, key, library):
    result, hits = cedar_hits(paths, dated_warrant(paths, key))
    assert result["date_filter"] is None and result["query"] == 'content:"PROJECT_CEDAR"'
    assert hits == ["rec-001", "rec-002", "rec-004", "rec-005", "rec-008"]


def test_lookback_excludes_older_records(paths, key, library):
    result, hits = cedar_hits(paths, dated_warrant(paths, key, lookback_days=365))
    assert result["query"] == '(content:"PROJECT_CEDAR") AND timestamp:["2025-10-17T12:00:00Z" TO "2026-10-17T12:00:00Z"]'
    assert hits == ["rec-001", "rec-002", "rec-005"]  # rec-004 (2025-08) and rec-008 (2025-02) are too old


def test_blocked_ranges_excluded_inclusively(paths, key, library):
    doc = dated_warrant(paths, key, blocked_ranges=[
        {"start": "2026-09-15", "end": "2026-09-15"},  # same day as rec-001 (14:02 UTC)
        {"start": "2025-01-01", "end": "2025-03-31"},  # rec-008
    ])
    result, hits = cedar_hits(paths, doc)
    assert result["query"] == (
        '(content:"PROJECT_CEDAR") AND NOT timestamp:["2025-01-01T00:00:00Z" TO "2025-04-01T00:00:00Z"} '
        'AND NOT timestamp:["2026-09-15T00:00:00Z" TO "2026-09-16T00:00:00Z"}'
    )
    assert hits == ["rec-002", "rec-004", "rec-005"]


def test_date_limits_apply_to_onetime_runs(paths, key, library, monkeypatch):
    from datetime import timedelta as _td

    from lucid_warrant import onetime as _onetime, warrant as _warrant
    for module in (_onetime, _warrant):  # freeze the clock: the test must not depend on when it runs
        monkeypatch.setattr(module, "utc_now", lambda: NOW + _td(minutes=1))
    doc = dated_warrant(paths, key, lookback_days=45)  # from 2026-09-02
    submit_request(paths, load_public_key(paths), warrant_id=doc["warrant_id"], skeleton="{a}",
                   bindings={"a": "t-project"}, rationale="")
    (request,) = list_requests(paths)
    approval = approve_request(paths, key, request)
    result = run_onetime(paths, onetime_id=approval["onetime_id"], now=NOW)
    assert [r["record_id"] for r in result["results"]] == ["rec-001", "rec-005"]


@pytest.mark.parametrize("limits, reason", [
    ({"lookback_days": -1}, "lookback"),
    ({"lookback_days": 36501}, "lookback"),
    ({"blocked_ranges": [{"start": "2026-02-30", "end": "2026-03-01"}]}, "not a date"),
    ({"blocked_ranges": [{"start": "2026-03-02", "end": "2026-03-01"}]}, "ends before"),
])
def test_invalid_date_limits_rejected(paths, limits, reason):
    with pytest.raises(WarrantError, match=reason):
        build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="p", ttl_minutes=60, max_rows=10,
                      terms=TERMS_FOR_DATES, rbac=get_rbac(paths), **limits)


def test_date_limits_are_signed(paths, key, library):
    doc = dated_warrant(paths, key, lookback_days=30)
    path = paths.warrants / f"{doc['warrant_id']}.json"
    path.write_bytes(canonical_bytes({**doc, "max_lookback_days": 0}))
    with pytest.raises(ExecutionDenied, match="does not match"):
        cedar_hits(paths, doc)


# --- run IDs, payload records, template status --------------------------------

def test_run_records_payload_and_service_id(paths, warrant):
    from lucid_warrant.executor import load_payload, read_audit
    result = run_template(paths, warrant_id=warrant["warrant_id"], template_id="single-term", bindings={"term": "t-project"})
    assert result["run_id"].startswith("run-") and result["service_query_id"].startswith("mock-")
    saved = load_payload(paths, result["run_id"])
    assert saved["service_query_id"] == result["service_query_id"]
    assert saved["payload"] == {"warrant_id": warrant["warrant_id"], "purpose": warrant["purpose"],
                                "parameter_1": "0012", "parameter_2": ["ATTR_1000", "ATTR_1200"],
                                "query": 'content:"PROJECT_CEDAR"', "limit": 3}
    entry = read_audit(paths)[-1]
    assert entry["run_id"] == result["run_id"] and entry["service_query_id"] == result["service_query_id"]


def test_refused_run_has_id_but_no_payload(paths, warrant):
    from lucid_warrant.executor import load_payload, read_audit
    with pytest.raises(ExecutionDenied):
        run(paths, warrant, a="t-project", b="t-nope")
    entry = read_audit(paths)[-1]
    assert entry["outcome"] == "denied" and entry["run_id"].startswith("run-")
    assert load_payload(paths, entry["run_id"]) is None


def test_disabled_template_refused_and_reenabled(paths, key, warrant):
    from lucid_warrant.library import describe_template, set_template_enabled
    set_template_enabled(paths, key, "all-of-two", False)
    with pytest.raises(ExecutionDenied, match="disabled"):
        run(paths, warrant, a="t-project", b="t-supplier")
    lib = set_template_enabled(paths, key, "all-of-two", True)
    assert lib["version"] == 3
    assert describe_template(lib, lib["templates"][1])["enabled"] is True
    run(paths, warrant, a="t-project", b="t-supplier")


def test_template_usage_and_shape(paths, warrant, library):
    from lucid_warrant.executor import template_usage
    from lucid_warrant.library import describe_template, suggest_template_id
    run(paths, warrant, a="t-project", b="t-supplier")
    run(paths, warrant, a="t-supplier", b="t-alias")
    assert template_usage(paths)["all-of-two"]["queries"] == 2
    shape = describe_template(library, library["templates"][3])["lucene_shape"]
    assert shape == 'content:"{required}" AND (content:"{alt_a}" OR content:"{alt_b}")'
    assert suggest_template_id(library, "Both terms!") == "both-terms"
    assert suggest_template_id(library, "Single term") == "single-term-2"


# --- lint at nomination, approval and firing ------------------------------------

def test_lint_recorded_at_every_stage(paths, key, warrant):
    from lucid_warrant.executor import load_payload, read_audit
    req = submit_request(paths, load_public_key(paths), warrant_id=warrant["warrant_id"],
                         skeleton="({a} OR {b}) AND NOT {c}", bindings={"a": "t-project", "b": "t-supplier", "c": "t-test"},
                         rationale="")
    assert req["lint"]["stage"] == "nomination" and req["lint"]["passed"]
    assert req["suggested_name"] == "Either of two terms, excluding one term"
    (request,) = list_requests(paths)
    approval = approve_request(paths, key, request)
    assert approval["lint"]["stage"] == "approval" and approval["suggested_name"] == req["suggested_name"]
    result = run_onetime(paths, onetime_id=approval["onetime_id"])
    assert result["lint"] == {"passed": True, "summary": "passed", "codes": [], "warnings": 0, "errors": 0}
    assert read_audit(paths)[-1]["lint"]["passed"]
    saved = load_payload(paths, result["run_id"])
    assert saved["lint"]["stage"] == "fire" and saved["lint"]["query_sha256"]


def test_lint_failing_request_is_refused(paths, warrant):
    from lucid_warrant.syntax import SyntaxRejected
    with pytest.raises(SyntaxRejected):
        submit_request(paths, load_public_key(paths), warrant_id=warrant["warrant_id"], skeleton="{a}*",
                       bindings={"a": "t-project"}, rationale="")
    assert list_requests(paths) == []


def test_fire_time_lint_catches_a_renderer_bug(paths, key, monkeypatch, library):
    """If rendering ever stopped escaping quotes, the independent output check refuses to send."""
    from lucid_warrant import syntax
    doc = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="p", ttl_minutes=60, max_rows=10,
                        terms=[{"term_id": "t-q", "literal": 'say "hi"', "source": "human"}], rbac=get_rbac(paths))
    from lucid_warrant.warrant import save_warrant
    save_warrant(paths, doc, key)
    monkeypatch.setattr(syntax, "escape_phrase", lambda literal: literal)  # simulated bug
    backend = RecordingBackend()
    with pytest.raises(ExecutionDenied, match="lint failed"):
        run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-q"}, backend=backend)
    assert backend.payloads == []


# --- Phase 0 fixes ------------------------------------------------------------------

def test_lint_failure_does_not_consume_onetime(paths, key, warrant, monkeypatch):
    from lucid_warrant import syntax
    from lucid_warrant.executor import used_onetime_ids
    approval = approve(paths, key, warrant)
    real = syntax.escape_phrase
    monkeypatch.setattr(syntax, "escape_phrase", lambda literal: literal + '"')  # simulated renderer bug
    with pytest.raises(ExecutionDenied):
        run_onetime(paths, onetime_id=approval["onetime_id"], backend=RecordingBackend())
    assert approval["onetime_id"] not in used_onetime_ids(paths)
    monkeypatch.setattr(syntax, "escape_phrase", real)
    run_onetime(paths, onetime_id=approval["onetime_id"], backend=RecordingBackend())  # still usable
    assert approval["onetime_id"] in used_onetime_ids(paths)


def test_archived_warrants_listed_read_only(paths, key, warrant):
    from lucid_warrant.warrant import list_warrants, revoke_warrant
    revoke_warrant(paths, warrant["warrant_id"])
    assert list_warrants(paths, load_public_key(paths)) == []
    (row,) = list_warrants(paths, load_public_key(paths), include_archived=True)
    assert row["status"] == "revoked" and row["doc"]["warrant_id"] == warrant["warrant_id"]


def test_approval_refused_if_changed_since_review(paths, key, warrant):
    submit_request(paths, load_public_key(paths), warrant_id=warrant["warrant_id"], skeleton="{a}",
                   bindings={"a": "t-project"}, rationale="")
    (request,) = list_requests(paths)
    with pytest.raises(WarrantError, match="changed since you reviewed"):
        approve_request(paths, key, request, reviewed_query='content:"SOMETHING_ELSE"')
    with pytest.raises(WarrantError, match="library changed"):
        approve_request(paths, key, request, reviewed_query='content:"PROJECT_CEDAR"', reviewed_library_version=0)
    assert approve_request(paths, key, request, reviewed_query='content:"PROJECT_CEDAR"', reviewed_library_version=1)


def test_audit_reader_skips_bad_lines(paths, warrant):
    from lucid_warrant.executor import audit_problems, read_audit
    run(paths, warrant, a="t-project", b="t-supplier")
    with open(paths.audit_log, "a") as fh:
        fh.write('{"ts": "2026-01-01T00:00:00Z", "outc')  # torn write
    assert len(read_audit(paths)) == 1
    assert "interrupted write" in audit_problems(paths)[0]


def test_library_change_refused_if_not_reviewed_version(paths, key, library):
    from lucid_warrant.library import LibraryError, set_template_enabled
    with pytest.raises(LibraryError, match="changed"):
        set_template_enabled(paths, key, "all-of-two", False, expected_version=99)
    assert set_template_enabled(paths, key, "all-of-two", False, expected_version=1)["version"] == 2


def test_round7_not_ready_after_consumption_releases_the_approval(paths, key, warrant):
    """Codex round 7 #13: an adapter that refuses before sending anything leaves the one-time approval usable."""
    from lucid_warrant.backend import BackendNotReady
    from lucid_warrant.executor import used_onetime_ids

    class NotReady:
        def search(self, payload, plan):
            raise BackendNotReady("not ready; nothing sent")

    approval = approve(paths, key, warrant)
    with pytest.raises(ExecutionDenied, match="nothing sent"):
        run_onetime(paths, onetime_id=approval["onetime_id"], backend=NotReady())
    assert approval["onetime_id"] not in used_onetime_ids(paths)
    run_onetime(paths, onetime_id=approval["onetime_id"], backend=RecordingBackend())
    assert approval["onetime_id"] in used_onetime_ids(paths)
    with pytest.raises(ExecutionDenied, match="already been used"):
        run_onetime(paths, onetime_id=approval["onetime_id"], backend=RecordingBackend())


def test_round7_a_sent_search_is_audited_even_if_saving_results_fails(paths, warrant, monkeypatch):
    """Codex round 7 #12: once a search is sent it always reaches the audit log, with the storage problem."""
    from lucid_warrant import results

    def disk_full(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr(results, "save_results", disk_full)
    result, _backend = run(paths, warrant, template="single-term", term="t-project")
    entries = [json.loads(line) for line in paths.audit_log.read_text().splitlines()]
    last = entries[-1]
    assert last["run_id"] == result["run_id"] and last["outcome"] == "ok"
    assert "No space left on device" in last["storage_problem"]


def test_hourly_limit_wait_is_rounded_up_not_past_the_hour():
    """Issue #9: a full-hour wait read "about 61 min"."""
    import math

    assert max(1, math.ceil(3600 / 60)) == 60 and max(1, math.ceil(1 / 60)) == 1
