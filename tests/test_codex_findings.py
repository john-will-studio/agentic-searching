"""Regression tests for the backend findings from Codex's independent smoke test (F5–F20)."""

import json
import os
import subprocess
import sys

import pytest

from lucid_warrant.executor import (
    ExecutionDenied,
    audit_problems,
    ledger_problems,
    read_audit,
    run_onetime,
    run_template,
    used_onetime_ids,
)
from lucid_warrant.onetime import approve_request, list_requests, reject_request, submit_request
from lucid_warrant.review import read_inbox
from lucid_warrant.signing import canonical_bytes, load_public_key, write_signed
from lucid_warrant.warrant import WarrantError, supersede_warrant


def _request(paths, warrant, skeleton="{a}", **bindings):
    return submit_request(paths, load_public_key(paths), warrant_id=warrant["warrant_id"], skeleton=skeleton,
                          bindings=bindings or {"a": "t-project"}, rationale="")


def test_f5_invalid_limit_is_audited(paths, warrant):
    with pytest.raises(ExecutionDenied, match="positive"):
        run_template(paths, warrant_id=warrant["warrant_id"], template_id="single-term", bindings={"term": "t-project"}, limit=0)
    assert read_audit(paths)[-1]["outcome"] == "denied" and read_audit(paths)[-1]["run_id"].startswith("run-")


def test_f6_resigned_warrant_with_changed_literal_blocks_onetime(paths, key, warrant):
    _request(paths, warrant)
    (request,) = list_requests(paths)
    approval = approve_request(paths, key, request)
    changed = {**warrant, "terms": [{**t, "literal": "DIFFERENT_TERM"} if t["term_id"] == "t-project" else t
                                    for t in warrant["terms"]]}
    write_signed(paths.warrants / f"{warrant['warrant_id']}.json", changed, key)
    with pytest.raises(ExecutionDenied, match="no longer approves"):
        run_onetime(paths, onetime_id=approval["onetime_id"])
    assert approval["onetime_id"] not in used_onetime_ids(paths)


def test_f7_approval_refused_if_warrant_changed_since_review(paths, key, warrant):
    _request(paths, warrant)
    (request,) = list_requests(paths)
    with pytest.raises(WarrantError, match="changed since you reviewed"):
        approve_request(paths, key, request, reviewed_warrant_digest="0" * 64)


def test_f8_terms_null_is_a_problem_not_a_crash(paths):
    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.nominations.write_text('{"terms": null}')
    paths.proposal.write_text('["not", "an", "object"]')
    candidates, purpose, problems = read_inbox(paths)
    assert candidates == [] and purpose is None
    assert any("must be a list" in p for p in problems) and any("not a JSON object" in p for p in problems)


def test_f9_malformed_request_with_odd_name_can_be_rejected(paths):
    paths.syntax_requests.mkdir(parents=True)
    (paths.syntax_requests / "req-malformed.json").write_text("{")
    (row,) = list_requests(paths)
    assert row["malformed"] and row["file"] == "req-malformed.json"
    reject_request(paths, row["request_id"], file=row["file"])
    assert list_requests(paths) == [] and (paths.syntax_requests / "rejected" / "req-malformed.json").exists()
    with pytest.raises(ValueError):
        reject_request(paths, "x", file="../../escape.json")


def test_f10_id_mismatch_is_malformed_and_reject_hits_only_that_file(paths, warrant):
    a = _request(paths, warrant)
    b = _request(paths, warrant, skeleton="{a} AND {b}", a="t-project", b="t-supplier")
    file_a = paths.syntax_requests / f"{a['request_id']}.json"
    doc = json.loads(file_a.read_text())
    doc["request_id"] = b["request_id"]
    file_a.write_text(json.dumps(doc))
    rows = {r["file"]: r for r in list_requests(paths)}
    assert rows[file_a.name]["malformed"] and "doesn't match" in rows[file_a.name]["problem"]
    reject_request(paths, rows[file_a.name]["request_id"], file=file_a.name)
    remaining = [r["request_id"] for r in list_requests(paths)]
    assert remaining == [b["request_id"]], "rejecting the bad file must not move a different request"


def test_f11_bad_timestamp_is_skipped_and_reported(paths, warrant):
    run_template(paths, warrant_id=warrant["warrant_id"], template_id="single-term", bindings={"term": "t-project"})
    with open(paths.audit_log, "a") as fh:
        fh.write(json.dumps({"ts": "not-a-date", "outcome": "ok", "run_id": "run-bad"}) + "\n")
    assert len(read_audit(paths)) == 1 and audit_problems(paths)


def test_f12_torn_ledger_fails_closed_without_crashing(paths, key, warrant):
    _request(paths, warrant)
    (request,) = list_requests(paths)
    approval = approve_request(paths, key, request)
    paths.state.mkdir(parents=True, exist_ok=True)
    with open(paths.used_onetime, "a") as fh:
        fh.write("{torn\n")
    assert used_onetime_ids(paths) == set() and ledger_problems(paths)
    with pytest.raises(ExecutionDenied, match="ledger is unreadable"):
        run_onetime(paths, onetime_id=approval["onetime_id"])


def test_f14_dates_outside_range_rejected():
    from lucid_warrant.dates import DateLimitError, parse_day
    with pytest.raises(DateLimitError, match="outside"):
        parse_day("9999-12-31")
    assert parse_day("2026-12-31").year == 2026


def test_f18_cannot_supersede_a_revoked_warrant(paths, warrant):
    from lucid_warrant.warrant import revoke_warrant
    revoke_warrant(paths, warrant["warrant_id"])
    with pytest.raises(WarrantError, match="no longer active"):
        supersede_warrant(paths, warrant["warrant_id"], "w-20261017-000000-abcdef")


@pytest.mark.parametrize("args", [
    ["run", "--template", "single-term", "--bind", "term"],
    ["run", "--template", "single-term", "--bind", "term=t-project", "--bind", "term=t-project"],
    ["request-syntax", "--skeleton", "{x}", "--bind", "x", "--rationale", "bad"],
])
def test_f20_cli_argument_errors_are_json(paths, warrant, args):
    args = [args[0], "--warrant", warrant["warrant_id"], *args[1:]]
    env = {**os.environ, "LUCID_WARRANT_KEYS": str(paths.keys), "LUCID_WARRANT_DATA": str(paths.data)}
    result = subprocess.run([sys.executable, "-m", "lucid_warrant.cli", *args], capture_output=True, timeout=60, text=True, env=env)
    assert result.returncode == 2 and "error" in json.loads(result.stdout)


# --- second round: gaps Codex found in the first fixes ------------------------------------

@pytest.mark.parametrize("record", ["{}", '{"onetime_id": null}', '{"onetime_id": []}', "[]", '{"onetime_id": "x"}'])
def test_ledger_malformed_records_fail_closed(paths, key, warrant, record):
    _request(paths, warrant)
    (request,) = list_requests(paths)
    approval = approve_request(paths, key, request)
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.used_onetime.write_text(record + "\n")
    assert ledger_problems(paths)
    with pytest.raises(ExecutionDenied, match="ledger is unreadable"):
        run_onetime(paths, onetime_id=approval["onetime_id"])


def test_replace_warrant_refuses_if_old_already_revoked(paths, key, warrant):
    from lucid_warrant.rbac import get_rbac
    from lucid_warrant.warrant import build_warrant, list_warrants, replace_warrant, revoke_warrant
    new = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="p", ttl_minutes=60, max_rows=10,
                        terms=warrant["terms"], rbac=get_rbac(paths), supersedes=warrant["warrant_id"])
    revoke_warrant(paths, warrant["warrant_id"])  # a concurrent revoke wins the race
    with pytest.raises(WarrantError, match="revoked or replaced"):
        replace_warrant(paths, warrant["warrant_id"], new, key)
    assert list_warrants(paths, load_public_key(paths)) == [], "nothing new was signed"


def test_replace_warrant_retires_then_signs(paths, key, warrant):
    from lucid_warrant.rbac import get_rbac
    from lucid_warrant.warrant import build_warrant, list_warrants, replace_warrant
    new = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="p", ttl_minutes=60, max_rows=10,
                        terms=warrant["terms"], rbac=get_rbac(paths), supersedes=warrant["warrant_id"])
    replace_warrant(paths, warrant["warrant_id"], new, key)
    rows = {w["warrant_id"]: w for w in list_warrants(paths, load_public_key(paths), include_archived=True)}
    assert rows[new["warrant_id"]]["status"] == "active"
    assert rows[warrant["warrant_id"]]["superseded_by"] == new["warrant_id"]


@pytest.mark.parametrize("field, value", [("lint", "bad"), ("submitted_at", "yesterday"), ("rationale", ["x"])])
def test_requests_with_bad_optional_fields_are_malformed(paths, warrant, field, value):
    req = _request(paths, warrant)
    f = paths.syntax_requests / f"{req['request_id']}.json"
    doc = json.loads(f.read_text())
    doc[field] = value
    f.write_text(json.dumps(doc))
    (row,) = list_requests(paths)
    assert row["malformed"]


# --- searches-per-hour limit --------------------------------------------------------------

def test_rate_limit_per_warrant(paths, key, library):
    from datetime import timedelta

    from lucid_warrant.executor import queries_in_last_hour
    from lucid_warrant.rbac import get_rbac
    from lucid_warrant.util import utc_now
    from lucid_warrant.warrant import build_warrant, save_warrant
    doc = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="p", ttl_minutes=600, max_rows=10,
                        terms=[{"term_id": "t-a", "literal": "PROJECT_CEDAR", "source": "script"}], rbac=get_rbac(paths),
                        max_queries_per_hour=2)
    save_warrant(paths, doc, key)
    now = utc_now()
    run = lambda at, **kw: run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term",
                                        bindings=kw.get("b", {"term": "t-a"}), now=at)
    run(now)
    with pytest.raises(ExecutionDenied):  # a refused search (unknown term) doesn't use quota
        run(now, b={"term": "t-nope"})
    run(now + timedelta(minutes=1))
    with pytest.raises(ExecutionDenied, match="searches-per-hour limit reached"):
        run(now + timedelta(minutes=2))
    assert read_audit(paths)[-1]["outcome"] == "denied"
    assert queries_in_last_hour(paths, doc["warrant_id"], now + timedelta(minutes=2)) == 2
    run(now + timedelta(minutes=61))  # the first slot has rolled out of the window


def test_rate_limit_validated(paths):
    from lucid_warrant.rbac import get_rbac
    from lucid_warrant.warrant import build_warrant
    for bad in (0, 101, True):
        with pytest.raises(WarrantError, match="per hour"):
            build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="p", ttl_minutes=60, max_rows=10,
                          terms=[{"term_id": "t-a", "literal": "A", "source": "script"}], rbac=get_rbac(paths),
                          max_queries_per_hour=bad)


# --- third round: Codex's smoke test of the new features (N-F01 … N-F19) --------------------

import datetime as _dt  # noqa: E402


def _rated(paths, key, per_hour=2, **kw):
    from lucid_warrant.rbac import get_rbac
    from lucid_warrant.warrant import build_warrant, save_warrant
    doc = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000", "ATTR_1200"], purpose="p", ttl_minutes=600,
                        max_rows=10, terms=[{"term_id": "t-a", "literal": "PROJECT_CEDAR", "source": "script"}],
                        rbac=get_rbac(paths), max_queries_per_hour=per_hour, **kw)
    save_warrant(paths, doc, key)
    return doc


def test_n_f02_certification_text_signed_verbatim(paths, key, library):
    text = "  I certify  the purpose.\nSecond paragraph.  "
    doc = _rated(paths, key, certifications=[{"id": "purpose", "text": text}])
    assert doc["certifications"][0]["text"] == text


def test_n_f04_refused_replay_uses_no_quota(paths, key, library):
    from lucid_warrant.executor import queries_in_last_hour
    doc = _rated(paths, key, per_hour=2)
    submit_request(paths, load_public_key(paths), warrant_id=doc["warrant_id"], skeleton="{a}", bindings={"a": "t-a"}, rationale="")
    (request,) = list_requests(paths)
    approval = approve_request(paths, key, request)
    run_onetime(paths, onetime_id=approval["onetime_id"])
    with pytest.raises(ExecutionDenied, match="already been used"):
        run_onetime(paths, onetime_id=approval["onetime_id"])
    assert queries_in_last_hour(paths, doc["warrant_id"]) == 1
    run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})


def test_n_f05_damaged_rate_ledger_fails_closed(paths, key, library):
    from lucid_warrant.executor import rate_ledger_path
    doc = _rated(paths, key, per_hour=1)
    run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})
    path = rate_ledger_path(paths)
    path.write_text(path.read_text().rstrip().rstrip("}") + "\n")
    with pytest.raises(ExecutionDenied, match="ledger is unreadable"):
        run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})


def test_n_f06_fractional_seconds_respect_blocked_days():
    from lucid_warrant.backend import _in_window
    window = {"field": "ts", "from": None, "exclude": [{"from": "2026-10-15T00:00:00Z", "to": "2026-10-16T00:00:00Z"}]}
    assert not _in_window(window, {"ts": "2026-10-15T00:00:00.000001Z"})
    assert _in_window(window, {"ts": "2026-10-16T00:00:00.000001Z"})
    assert not _in_window(window, {"ts": "2026-10-15T23:59:59.999Z"})


@pytest.mark.parametrize("bad", [
    {"source_labels": {"script": 1}}, {"ui": {"scheme": []}}, {"ui": {"schemes": {"x": {"label": "X", "light": "oops"}}}},
    {"ui": {"base_font_pt": False}}, {"defaults": {"blocked_ranges": False}},
    {"defaults": {"blocked_ranges": [{"start": "2026-01-01", "end": "2026-01-02", "oops": 1}]}},
    {"country_scope": {"exclude": [{"code": "DE", "reason": "GDPR", "oops": 1}]}},
    {"country_scope": {"exclude": [{"code": "DE", "reason": None}]}},
])
def test_n_f08_f09_bad_types_are_validation_errors(paths, bad):
    from lucid_warrant import org_config as oc
    with pytest.raises(oc.ConfigError):
        oc.validate(oc.merge(oc.DEFAULTS, {"schema_version": 1, **bad}), paths)


def test_n_f10_signed_fields_must_match_library(paths, key, library):
    from lucid_warrant import org_config as oc
    values = oc.authority_values(oc.merge(oc.DEFAULTS, {"query_fields": {"country": "nation"}}))
    doc = _rated(paths, key, config_context={"schema": "org-authority-v1", "sha256": "x", "values": values},
                 country_exclude=[{"code": "DE", "reason": "GDPR"}])
    with pytest.raises(ExecutionDenied, match="different field names"):
        run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})


@pytest.mark.parametrize("line", ['{"warrant_id": null, "run_id": "run-x", "at": "2026-10-17T00:00:00Z"}',
                                  '{"cancel": null}', '[]', '{"warrant_id": "w-x", "run_id": "run-1", "at": "2026-10-17T00:00:00Z", "x": 1}'])
def test_n_f05_malformed_ledger_records_fail_closed(paths, key, library, line):
    from lucid_warrant.executor import rate_ledger_path
    doc = _rated(paths, key, per_hour=5)
    rate_ledger_path(paths).parent.mkdir(parents=True, exist_ok=True)
    rate_ledger_path(paths).write_text(line + "\n")
    with pytest.raises(ExecutionDenied, match="ledger is unreadable"):
        run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})


def test_n_f02_printed_certifications_keep_spacing():
    from lucid_warrant.ui.document import render_html
    out = render_html([("title", "Search Warrant"), ("checks", ["A  b\nc"])])
    assert "white-space: pre-wrap" in out and "A  b\nc" in out


def test_searches_per_hour_capped_at_100(paths, key, library):
    from lucid_warrant import org_config as oc
    from lucid_warrant.warrant import WarrantError
    assert _rated(paths, key, per_hour=100)["max_queries_per_hour"] == 100
    with pytest.raises(WarrantError, match="1 to 100"):
        _rated(paths, key, per_hour=101)
    with pytest.raises(oc.ConfigError):
        oc.validate(oc.merge(oc.DEFAULTS, {"defaults": {"max_queries_per_hour": 101}}), paths)


# --- round 4: Codex's review of the color schemes and fixes (R4-1 … R4-12) ------------------

def test_r4_1_duplicate_ledger_keys_fail_closed(paths, key, library):
    from lucid_warrant.executor import rate_ledger_path
    doc = _rated(paths, key, per_hour=1)
    run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})
    path = rate_ledger_path(paths)
    line = path.read_text().strip()
    path.write_text(line[:-1] + ', "warrant_id": "w-20260101-000000-abcdef"}\n')
    with pytest.raises(ExecutionDenied, match="ledger is unreadable"):
        run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})


def test_r4_8_theme_save_uses_selected_org_file(paths, monkeypatch, tmp_path):
    from lucid_warrant import org_config as oc
    monkeypatch.setenv("LUCID_WARRANT_PREFS", str(tmp_path / "prefs.json"))
    override = tmp_path / "override.json"
    override.write_text(json.dumps({"schema_version": 1, "ui": {"schemes": {"brand": {"label": "Brand", "color": "#a4161a"}}}}))
    oc.save_user_ui(paths, {"scheme": "brand"}, str(override))
    assert json.loads((tmp_path / "prefs.json").read_text())["ui"]["scheme"] == "brand"


def test_r4_12_permit_wording_throughout_document(paths):
    from lucid_warrant.ui.document import warrant_blocks
    doc = {"warrant_id": "w-1", "created_at": "2026-10-17T00:00:00Z", "expires_at": "2026-10-17T02:00:00Z",
           "parameter_1": "0012", "parameter_2": ["ATTR_1000"], "purpose": "p", "max_rows_per_run": 10,
           "terms": [], "country_scope": {"include": [], "exclude": []}, "max_lookback_days": 30}
    blocks = warrant_blocks(doc, labels={}, countries={}, library_line="the library", status="Draft", replaces=None,
                            signature=None, new_literals=set(), removed=["X"], sources=None,
                            terms={"warrant": {"singular": "Permit", "plural": "Permits"},
                                   "hit_term": {"singular": "Keyword", "plural": "Keywords"}})
    text = json.dumps(blocks).lower()
    assert "warrant" not in text.replace("w-1", "") and "permit" in text


def test_r4_2_sprites_recolor_without_tk9_photo_api():
    """The PNG path must not depend on Tk 8.7+/9 RGBA photo data (absent from Tk 8.6 on Windows)."""
    import inspect

    from lucid_warrant.ui import pngdata, theme
    source = inspect.getsource(theme)
    assert "-colorformat" not in source and '"data"' not in source
    width, height, rows, regions = theme._sheet("light")
    assert pngdata.read_rgba(pngdata.write_rgba(width, height, rows))[2] == rows
    assert any(name == "button-accent-rest" for name, *_ in regions)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_r4_7_every_enabled_accent_state_keeps_contrast(mode):
    from lucid_warrant import org_config as oc
    from lucid_warrant.ui import theme
    for sid, scheme in oc.BUILTIN_SCHEMES.items():
        palette = oc.derive_palette(mode, scheme)
        width, height, rows, regions = theme._recolored_sheet(mode, palette, scheme.get("color"))
        for name, x, y, w, h in regions:
            if name.startswith("button-accent") and not name.endswith("-dis"):
                r, g, b = rows[y + h // 2][(x + w // 2) * 4:(x + w // 2) * 4 + 3]
                assert oc.contrast("#%02x%02x%02x" % (r, g, b), palette["accent_text"]) >= 4.5, (mode, sid, name)


# --- purpose in the payload, included-country reasons, runs addendum ------------------------

def test_payload_carries_signed_purpose_and_warrant_id(paths, key, library):
    from lucid_warrant.executor import load_payload
    doc = _rated(paths, key)
    run = run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})
    payload = load_payload(paths, run["run_id"])["payload"]
    assert payload["purpose"] == doc["purpose"] and payload["warrant_id"] == doc["warrant_id"]


def test_included_countries_carry_optional_reasons_and_old_form_still_reads(paths):
    from lucid_warrant.scope import country_filter, include_codes, normalize_country_scope
    scope = normalize_country_scope(paths, [{"code": "us", "reason": "Home country of client CL-100482"}, "CA"],
                                    [{"code": "DE", "reason": "GDPR"}])
    assert scope["include"] == [{"code": "CA", "reason": ""}, {"code": "US", "reason": "Home country of client CL-100482"}]
    assert include_codes(scope) == ["CA", "US"]
    assert country_filter({"country_scope": {"include": ["GB"], "exclude": []}}, "country")["include"] == ["GB"]
    with pytest.raises(Exception, match="longer than"):
        normalize_country_scope(paths, [{"code": "US", "reason": "x" * 61}], [])


def _runs(entries, payloads, term_ids=None):
    from lucid_warrant.ui.document import clean_run
    return [clean_run(e, *payloads.get(e.get("run_id") if isinstance(e, dict) else None, (None, "missing")),
                      (term_ids or {}).get(e.get("run_id") if isinstance(e, dict) else None, e.get("term_ids", [])
                                          if isinstance(e, dict) and isinstance(e.get("term_ids"), list) else []))
            for e in entries]


def test_runs_addendum_summary_then_each_run_oldest_first():
    from lucid_warrant.ui.document import render_html, runs_addendum
    doc = {"terms": [{"term_id": "t-a", "literal": "PROJECT_CEDAR"}]}
    entries = [
        {"ts": "2026-10-17T12:00:00Z", "run_id": "run-2", "kind": "template", "template_id": "single-term",
         "term_ids": ["t-a"], "outcome": "denied", "reason": "searches-per-hour limit reached"},
        {"ts": "2026-10-17T11:00:00Z", "run_id": "run-1", "kind": "template", "template_id": "single-term",
         "term_ids": ["t-a"], "bindings": {"term": "t-a"}, "outcome": "ok", "count": 3, "service_query_id": "svc-9"},
    ]
    payloads = {"run-1": ({"sent_at": "2026-10-17T11:00:00Z", "payload": {"query": 'content:"PROJECT_CEDAR"', "limit": 10}}, None)}
    blocks = runs_addendum(doc, _runs(entries, payloads), printed_at="now")
    headings = [b[1] for b in blocks if b[0] == "heading"]
    assert headings == ["Addendum A. Searches Run Under This Warrant", "Summary"]
    runs = [b[1] for b in blocks if b[0] == "run"]
    assert [r[0][1] for r in runs] == ["Run 1 of 2", "Run 2 of 2"]
    summary = next(b for b in blocks if b[0] == "table")
    assert [row[2] for row in summary[2]] == ["run-1", "run-2"], "chronological"
    out = render_html(blocks)
    assert out.count("<section class='run'>") == 2 and "break-inside: avoid" in out
    assert "&quot;limit&quot;: 10" in out and "Nothing was sent to the search service." in out and "svc-9" in out
    assert "not part of the signed warrant" in out and "received" not in out


@pytest.mark.parametrize("case", ["missing", "unreadable", "denied_with_payload", "unknown", "error"])
def test_addendum_claims_only_what_the_records_show(case):
    from lucid_warrant.ui.document import render_html, runs_addendum
    doc = {"terms": [{"term_id": "t-a", "literal": "A"}]}
    base = {"ts": "2026-10-17T11:00:00Z", "run_id": "run-1", "kind": "template", "template_id": "x", "term_ids": ["t-a"]}
    record = {"sent_at": "2026-10-17T11:00:00Z", "payload": {"query": "q"}, "error": "timeout"}
    entry, payloads = {
        "missing": ({**base, "outcome": "ok"}, {}),
        "unreadable": ({**base, "outcome": "ok"}, {"run-1": (None, "unreadable: bad JSON")}),
        "denied_with_payload": ({**base, "outcome": "denied"}, {"run-1": (record, None)}),
        "unknown": ({**base, "outcome": "weird"}, {}),
        "error": ({**base, "outcome": "error"}, {"run-1": (record, None)}),
    }[case]
    out = render_html(runs_addendum(doc, _runs([entry], payloads), printed_at="now"))
    if case in ("missing", "unreadable"):
        assert "Sent" in out and "can&#x27;t be shown" in out and "Nothing was sent" not in out
    elif case == "denied_with_payload":
        assert "Blocked before sending" in out and "not shown as sent" in out and "Payload sent" not in out
    elif case == "unknown":
        assert "Unknown outcome" in out and "doesn&#x27;t say whether anything was sent" in out and "blocked before sending" in out
        assert "0 blocked before sending" in out
    else:
        assert "service failed: timeout" in out and "Payload sent" in out


@pytest.mark.parametrize("bad", [
    {"term_ids": None}, {"term_ids": [["nested"]]}, {"bindings": [1]}, {"outcome": ["x"]}, {"run_id": 7},
    {"count": "many"}, {"service_query_id": {"x": 1}},
])
def test_addendum_survives_malformed_records(bad):
    from lucid_warrant.ui.document import clean_run, render_html, runs_addendum
    entry = {"ts": "2026-10-17T11:00:00Z", "run_id": "run-1", "kind": "template", "outcome": "ok", **bad}
    for record in (None, {"payload": [1]}, {"payload": {"query": None}}, [1]):
        run = clean_run(entry, record if isinstance(record, dict) and isinstance(record.get("payload"), dict) else None,
                        None, [])
        render_html(runs_addendum({"terms": []}, [run], printed_at="now"))


def test_legacy_run_terms_and_service_id_are_recovered(paths):
    from lucid_warrant.executor import run_term_ids
    from lucid_warrant.ui.document import clean_run
    entry = {"ts": "2026-10-17T11:00:00Z", "run_id": "run-1", "outcome": "ok", "bindings": {"term": "t-project"}}
    assert run_term_ids(paths, entry) == ["t-project"]
    run = clean_run(entry, {"payload": {"query": "q"}, "service_query_id": "svc-7"}, None, run_term_ids(paths, entry))
    assert run["service_query_id"] == "svc-7" and run["term_ids"] == ["t-project"]


# --- Türkiye scenario findings ---------------------------------------------------------------

def _corpus(tmp_path, records):
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps({"records": records}))
    return path


def _plan(literal, field="content"):
    from lucid_warrant.backend import Plan
    from lucid_warrant.syntax import Term
    return Plan(node=Term(slot="a"), literals={"a": literal}, default_field=field)


def test_scenario_country_allow_list_stops_bad_codes(paths, tmp_path):
    from lucid_warrant.backend import MockBackend
    from lucid_warrant.scope import apply_country_filter, countries, country_filter
    warrant = {"country_scope": {"include": [], "exclude": [{"code": "DE", "reason": "GDPR"}]}}
    country = country_filter(warrant, "country", countries(paths))
    assert country["include_derived"] and "DE" not in country["include"] and "TR" in country["include"]
    assert "NOT country" not in apply_country_filter("q", country)
    rows = [{"record_id": rid, "tags": ["A"], "content": "Bosphorus Logistics", **({"country": c} if c is not None else {})}
            for rid, c in (("tr", "TR"), ("lower", "de"), ("missing", None), ("alpha3", "DEU"), ("de", "DE"), ("blank", ""))]
    found = MockBackend(_corpus(tmp_path, rows)).search(
        {"parameter_1": "p", "parameter_2": ["A"], "limit": 10, "country_filter": country}, _plan("Bosphorus Logistics"))
    assert [r["record_id"] for r in found.results] == ["tr"]


def test_scenario_executor_withholds_out_of_scope_results(paths, key, library):
    from lucid_warrant.backend import SearchResponse
    doc = _rated(paths, key, country_exclude=[{"code": "DE", "reason": "GDPR"}])

    class Leaky:
        def search(self, payload, plan):
            return SearchResponse([{"record_id": "ok", "country": "TR"}, {"record_id": "leak", "country": "de"}], "svc-1")
    result = run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"},
                          backend=Leaky())
    assert [r["record_id"] for r in result["results"]] == ["ok"] and result["withheld_outside_geographic_constraint"] == 1


def test_scenario_mock_matches_words_tags_and_turkish_i(tmp_path):
    from lucid_warrant.backend import MockBackend
    rows = [
        {"record_id": "jv", "tags": ["CRM"], "content": "Caspian Anatolian Steel LLC opened a centre"},
        {"record_id": "ohio", "tags": ["CRM"], "content": "Anatolian Steelworks Supply Co. of Ohio"},
        {"record_id": "memo", "tags": ["CRM"], "content": "KAPADOKYA TEKSTİL A.Ş. YÖNETİM KURULU"},
        {"record_id": "mixed", "tags": ["CRM", "HR"], "content": "Kapadokya Tekstil salary bands"},
    ]
    backend = MockBackend(_corpus(tmp_path, rows))
    search = lambda literal: [r["record_id"] for r in backend.search(
        {"parameter_1": "p", "parameter_2": ["CRM"], "limit": 10}, _plan(literal)).results]
    assert search("Anatolian Steel") == ["jv"], "whole words: not Steelworks"
    assert search("Kapadokya Tekstil") == ["memo"], "dotted İ matches; a CRM+HR record needs HR granted too"


def test_scenario_window_ends_at_search_time():
    from datetime import datetime, timezone

    from lucid_warrant.backend import _in_window
    from lucid_warrant.dates import window
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    f = window({"max_lookback_days": 60, "blocked_date_ranges": []}, now, "ts")
    assert f["to"] == "2026-10-17T12:00:00Z"
    assert _in_window(f, {"ts": "2026-10-17T11:00:00Z"}) and not _in_window(f, {"ts": "2026-10-19T09:00:00Z"})


def test_scenario_refusals_carry_run_ids_and_refused_syntax_is_logged(paths, key, library):
    import subprocess
    import sys

    from lucid_warrant.executor import read_audit
    doc = _rated(paths, key)
    env = {**__import__("os").environ, "LUCID_WARRANT_KEYS": str(paths.keys), "LUCID_WARRANT_DATA": str(paths.data)}

    def cli(*args):
        out = subprocess.run([sys.executable, "-m", "lucid_warrant.cli", *args], capture_output=True, timeout=60, text=True, env=env)
        return out.returncode, json.loads(out.stdout)
    code, out = cli("run", "--warrant", doc["warrant_id"], "--template", "single-term", "--bind", "term=t-nope")
    assert code == 2 and out["run_id"].startswith("run-")
    code, out = cli("request-syntax", "--warrant", doc["warrant_id"], "--skeleton", "{a} AND country:DE",
                    "--bind", "a=t-a", "--rationale", "probe")
    assert code == 2 and out["run_id"].startswith("run-") and "lint failed" in out["denied"]
    logged = [e for e in read_audit(paths) if e.get("kind") == "syntax_request"]
    assert logged and logged[-1]["skeleton"] == "{a} AND country:DE" and logged[-1]["outcome"] == "denied"


def test_scenario_status_box_when_hourly_limit_is_used():
    from lucid_warrant.ui.warrants import status_message
    d = {"expires_at": "2099-01-01T00:00:00Z", "max_queries_per_hour": 20}
    assert status_message({"status": "active"}, d, 20)[0] == "Active · hourly limit reached"
    assert status_message({"status": "active"}, d, 19)[0] == "Active"


def test_scenario_replacing_a_warrant_keeps_its_hourly_usage(paths, key, library):
    from lucid_warrant.executor import queries_in_last_hour
    from lucid_warrant.rbac import get_rbac
    from lucid_warrant.warrant import build_warrant, replace_warrant
    doc = _rated(paths, key, per_hour=2)
    for _ in range(2):
        run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})
    new = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000", "ATTR_1200"], purpose="p", ttl_minutes=600,
                        max_rows=10, terms=[{"term_id": "t-a", "literal": "PROJECT_CEDAR", "source": "script"}],
                        rbac=get_rbac(paths), max_queries_per_hour=2, supersedes=doc["warrant_id"])
    replace_warrant(paths, doc["warrant_id"], new, key)
    assert queries_in_last_hour(paths, new["warrant_id"]) == 2
    with pytest.raises(ExecutionDenied, match="searches-per-hour limit"):
        run_template(paths, warrant_id=new["warrant_id"], template_id="single-term", bindings={"term": "t-a"})


def test_addendum_prints_the_country_list_once_and_fingerprints_payloads():
    from lucid_warrant.ui.document import payload_fingerprint, render_html, runs_addendum
    codes = [f"{a}{b}" for a in "ABCDEFGH" for b in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"][:50]
    clause = "(" + " OR ".join(f'country:"{c}"' for c in codes) + ")"
    payload = {"query": f'(content:"X") AND {clause}', "limit": 5,
               "country_filter": {"field": "country", "include": codes, "exclude": ["DE"], "include_derived": True}}
    runs = _runs([{"ts": "2026-10-17T11:00:00Z", "run_id": "run-1", "kind": "template", "outcome": "ok", "count": 1},
                  {"ts": "2026-10-17T12:00:00Z", "run_id": "run-2", "kind": "template", "outcome": "denied",
                   "query": f'(content:"X") AND {clause}'}],
                 {"run-1": ({"payload": payload, "sent_at": "2026-10-17T11:00:00Z"}, None)})
    out = render_html(runs_addendum({"terms": []}, runs, printed_at="now"))
    assert out.count('country:&quot;AA&quot;') == 0, "the long clause is replaced on every page"
    assert "List A. Permitted country codes (50)" in out and out.count("AA, AB, AC") == 1
    assert "any code in List A" in out and payload_fingerprint(payload) in out
    assert payload["country_filter"]["include"] == codes, "the saved payload itself is untouched"
