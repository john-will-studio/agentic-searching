"""The agent's onboarding: `init` sets up a project, `propose` sends terms to the analyst, `wait` blocks for them."""

import json
import threading
import time

import pytest

from lucid_warrant import cli, project, workspace
from lucid_warrant.rbac import get_rbac
from lucid_warrant.review import reject
from lucid_warrant.warrant import build_warrant, save_warrant


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    try:
        return code, json.loads(out)
    except ValueError:
        return code, out


@pytest.fixture
def agent_env(paths, monkeypatch):
    monkeypatch.setenv("LUCID_WARRANT_KEYS", str(paths.keys))
    monkeypatch.setenv("LUCID_WARRANT_DATA", str(paths.data))
    monkeypatch.setattr(cli, "WAIT_INTERVAL", 0.05)
    return paths


def sign(paths, key, literals=("PROJECT_CEDAR",), **kw):
    doc = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="p", ttl_minutes=60, max_rows=10,
                        terms=[{"term_id": f"t-{i}", "literal": lit, "source": "agent"} for i, lit in enumerate(literals)],
                        rbac=get_rbac(paths), paths=paths, **kw)
    save_warrant(paths, doc, key)
    return doc


# --- init --------------------------------------------------------------------------------

def test_init_sets_up_a_project_and_is_idempotent(tmp_path):
    proj = tmp_path / "my-case"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("# Team notes\n\nKeep these.\n")
    result = project.init_project(proj)
    wd = proj / workspace.PROJECT_DIR
    assert result["warrant_directory"] == str(wd) and (wd / workspace.MARKER).is_file()
    skill = (proj / project.SKILL_PATH).read_text()
    assert skill.startswith("---\nname: lucid-warrant") and "{command}" not in skill and result["command"] in skill
    agents = (proj / "AGENTS.md").read_text()
    assert agents.startswith("# Team notes") and "Keep these." in agents and ".claude/skills/lucid-warrant/SKILL.md" in agents
    assert result["instructions"] == {"AGENTS.md": "added", "CLAUDE.md": "created"}
    (proj / "AGENTS.md").write_text(agents + "\nMore notes after.\n")
    again = project.init_project(proj)
    assert again["instructions"] == {"AGENTS.md": "updated", "CLAUDE.md": "updated"}
    text = (proj / "AGENTS.md").read_text()
    assert text.count("lucid-warrant:start") == 1 and "More notes after." in text


def test_discovery_walks_up_from_any_subfolder(tmp_path, monkeypatch):
    for var in ("LUCID_WARRANT_DATA", "LUCID_WARRANT_KEYS", "LUCID_WARRANT_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(workspace, "config_dir", lambda: tmp_path / "config")
    proj = tmp_path / "case"
    project.init_project(proj)
    deep = proj / "notes" / "2026"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    found, how = workspace.resolve()
    assert found == (proj / workspace.PROJECT_DIR).resolve() and how == "found from the current folder"
    from lucid_warrant.config import Paths
    assert Paths.default().data == (proj / workspace.PROJECT_DIR / "data").resolve()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert workspace.resolve() == (None, "not set up"), "nothing leaks in from another project"


def test_init_inside_an_existing_warrant_directory_reuses_it(tmp_path):
    wd = workspace.create(tmp_path / "proj" / workspace.PROJECT_DIR)
    result = project.init_project(tmp_path / "proj")
    assert result["warrant_directory"] == str(wd)


# --- propose -----------------------------------------------------------------------------

def test_propose_validates_and_writes_the_inbox(agent_env, capsys):
    code, out = run_cli(capsys, "propose", "--purpose", "  Find  supplier links  ", "--term", "SUPPLIER_NORTH",
                        "--reason", "Counterparty on the intake form.", "--client-id", "CL-207715")
    assert code == 0 and out["proposal"]["purpose"] == "Find supplier links"
    saved = json.loads(agent_env.proposal.read_text())
    assert saved["terms"] == [{"literal": "SUPPLIER_NORTH", "reason": "Counterparty on the intake form.", "client_id": "CL-207715"}]
    code, out = run_cli(capsys, "propose", "--term", "SUPPLIER_NORTH", "--reason", "Updated reason.")
    assert [t["reason"] for t in out["proposal"]["terms"]] == ["Updated reason."], "re-proposing replaces, not duplicates"
    assert run_cli(capsys, "propose", "--term", "X")[0] == 2, "a reason is required"
    assert run_cli(capsys, "propose", "--term", " padded ", "--reason", "r")[0] == 2
    assert run_cli(capsys, "propose", "--term", "X", "--reason", "r", "--client-id", "bad id!")[0] == 2
    code, out = run_cli(capsys, "propose", "--withdraw", "SUPPLIER_NORTH")
    assert code == 0 and out["proposal"]["terms"] == []


def test_propose_refuses_a_rejected_term_until_its_wait_is_over(agent_env, capsys):
    reject(agent_env, {"literal": "MEHMET", "reject_reason": "Personal name.", "reassess_minutes": None})
    code, out = run_cli(capsys, "propose", "--term", "MEHMET", "--reason", "CEO")
    assert code == 2 and "was rejected on" in out["error"] and "Personal name." in out["error"]
    assert "may not be proposed again" in out["error"]
    reject(agent_env, {"literal": "BIRCH", "reject_reason": "Not yet.", "reassess_minutes": 60})
    code, out = run_cli(capsys, "propose", "--term", "BIRCH", "--reason", "new evidence")
    assert code == 2 and "may be proposed again after" in out["error"]


def test_propose_notes_terms_already_approved(agent_env, key, capsys):
    sign(agent_env, key)
    code, out = run_cli(capsys, "propose", "--term", "PROJECT_CEDAR", "--reason", "r")
    assert code == 0 and "already approved" in out["warnings"][0]


# --- wait --------------------------------------------------------------------------------

def test_wait_returns_when_the_analyst_signs(agent_env, key, library, capsys):
    sign(agent_env, key, ("OLD",))
    threading.Timer(0.3, lambda: sign(agent_env, key, ("PROJECT_CEDAR", "SUPPLIER_NORTH"))).start()
    started = time.monotonic()
    code, out = run_cli(capsys, "wait", "--timeout", "10")
    assert code == 0 and out["event"] == "warrant_signed" and time.monotonic() - started < 5
    assert [t["literal"] for t in out["warrants"][0]["terms"]] == ["PROJECT_CEDAR", "SUPPLIER_NORTH"]


def test_wait_times_out_with_exit_3(agent_env, key, capsys):
    code, out = run_cli(capsys, "wait", "--timeout", "1")
    assert code == 3 and out["event"] == "timeout"
    assert run_cli(capsys, "wait", "--timeout", "0")[0] == 2
    assert run_cli(capsys, "wait", "--for", "request")[0] == 2, "needs --request"


def test_wait_for_a_syntax_request_decision(agent_env, key, library, capsys):
    from lucid_warrant.onetime import approve_request, list_requests, reject_request, submit_request
    from lucid_warrant.signing import load_public_key
    doc = sign(agent_env, key)
    pub = load_public_key(agent_env)
    req = submit_request(agent_env, pub, warrant_id=doc["warrant_id"], skeleton="{a}", bindings={"a": "t-0"}, rationale="r")
    (pending,) = list_requests(agent_env)
    threading.Timer(0.3, lambda: approve_request(agent_env, key, pending)).start()
    code, out = run_cli(capsys, "wait", "--for", "request", "--request", req["request_id"], "--timeout", "10")
    assert code == 0 and out["event"] == "request_approved" and out["onetime_id"].startswith("ot-")
    req2 = submit_request(agent_env, pub, warrant_id=doc["warrant_id"], skeleton="{a}", bindings={"a": "t-0"}, rationale="r")
    reject_request(agent_env, req2["request_id"])
    code, out = run_cli(capsys, "wait", "--for", "request", "--request", req2["request_id"], "--timeout", "5")
    assert code == 0 and out["event"] == "request_rejected"
    code, out = run_cli(capsys, "wait", "--for", "request", "--request", "req-20260101-000000-abcdef", "--timeout", "5")
    assert code == 2 and out["event"] == "request_not_found"


def test_propose_works_before_the_analyst_creates_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LUCID_WARRANT_KEYS", str(tmp_path / "keys"))
    monkeypatch.setenv("LUCID_WARRANT_DATA", str(tmp_path / "data"))
    code, out = run_cli(capsys, "propose", "--term", "PROJECT_CEDAR", "--reason", "named in the brief")
    assert code == 0 and out["proposal"]["terms"][0]["literal"] == "PROJECT_CEDAR"
    code, out = run_cli(capsys, "status")
    assert code == 2 and "no signing key yet" in out["error"] and "hasn't finished setting up" in out["error"]


def test_propose_note_reaches_the_analyst(agent_env, capsys):
    from lucid_warrant.review import read_agent_note
    code, out = run_cli(capsys, "propose", "--note", "Invoices before August would need a 90-day window.")
    note = read_agent_note(agent_env)
    assert code == 0 and note["text"] == "Invoices before August would need a 90-day window." and note["at"]
    run_cli(capsys, "propose", "--note", "")
    assert read_agent_note(agent_env) is None


def test_status_shows_country_rules_and_window(agent_env, key, library, capsys):
    sign(agent_env, key, lookback_days=30, country_exclude=[{"code": "DE", "reason": "GDPR"}])
    code, out = run_cli(capsys, "status")
    (w,) = out["warrants"]
    assert w["search_window"].startswith("last 30 days")
    assert w["countries"]["excluded"] == [{"code": "DE", "reason": "GDPR"}] and "all countries" in w["countries"]["searched"]


def test_run_output_names_the_country_list_and_explains_empty_results(agent_env, key, library, capsys):
    doc = sign(agent_env, key, ("NOTHING_MATCHES_THIS",), country_exclude=[{"code": "DE", "reason": "GDPR"}])
    code, out = run_cli(capsys, "run", "--warrant", doc["warrant_id"], "--template", "single-term", "--bind", "term=t-0")
    assert code == 0 and out["count"] == 0 and "deliberately doesn't say" in out["note"]
    assert out["country_filter"]["excluded"] == ["DE"] and "codes" in out["country_filter"]["permitted"]
    assert "any permitted code" in out["query"] and len(out["query"]) < 400
    from lucid_warrant.executor import load_payload
    assert len(load_payload(agent_env, out["run_id"])["payload"]["country_filter"]["include"]) > 200, "the record keeps everything"


def test_status_shows_the_proposal_state_and_brief_templates(agent_env, key, library, capsys):
    run_cli(capsys, "propose", "--term", "PROJECT_CEDAR", "--reason", "r")
    run_cli(capsys, "propose", "--term", "LATER", "--reason", "r")
    sign(agent_env, key, ("PROJECT_CEDAR",))
    code, out = run_cli(capsys, "status", "--brief")
    states = {t["literal"]: t["state"] for t in out["proposal"]["terms"]}
    assert states == {"PROJECT_CEDAR": "approved", "LATER": "pending review"}
    assert set(out["templates"][0]) == {"template_id", "slots", "plain_english"}
    code, out = run_cli(capsys, "propose", "--purpose", "p")
    assert "warnings" not in out, "no empty warnings field"


def test_messages_are_short_and_point_to_summarize(agent_env, capsys):
    code, out = run_cli(capsys, "propose", "--note", "x" * 281)
    assert code == 2 and "summarize" in out["error"]
    assert run_cli(capsys, "propose", "--note", "x" * 280)[0] == 0


def test_summarize_attaches_to_a_sent_run(agent_env, key, library, capsys):
    from lucid_warrant.executor import load_payload
    from lucid_warrant.summaries import read_summary
    doc = sign(agent_env, key, ("PROJECT_CEDAR", "NOT_IN_ANY_RECORD"))
    code, out = run_cli(capsys, "run", "--warrant", doc["warrant_id"], "--template", "any-of-two",
                        "--bind", "a=t-0", "--bind", "b=t-1")
    run_id = out["run_id"]
    prof = load_payload(agent_env, run_id)["profile"]
    assert prof["count"] == len(out["results"])
    totals = {row["term_id"]: row["total"] for row in prof["pivot"]["rows"]}
    assert totals["t-0"] == prof["count"] and totals["t-1"] == 0, "counts come from the returned text itself"
    code, out = run_cli(capsys, "summarize", run_id, "--text", "rec-001: kickoff. Nothing else relevant.")
    assert code == 0 and read_summary(agent_env, run_id)["text"] == "rec-001: kickoff. Nothing else relevant."
    assert run_cli(capsys, "summarize", run_id, "--text", "x" * 2001)[0] == 2
    assert run_cli(capsys, "summarize", "run-20260101-000000-abcdef", "--text", "x")[0] == 2
    code, out = run_cli(capsys, "run", "--warrant", doc["warrant_id"], "--template", "single-term", "--bind", "term=t-9")
    code, out = run_cli(capsys, "summarize", out["run_id"], "--text", "x")
    assert code == 2 and "nothing to summarize" in out["error"]


def test_results_command_shows_the_profile_and_exports(agent_env, key, library, capsys, tmp_path):
    doc = sign(agent_env, key)
    code, out = run_cli(capsys, "run", "--warrant", doc["warrant_id"], "--template", "single-term", "--bind", "term=t-0")
    run_id = out["run_id"]
    code, out = run_cli(capsys, "results", run_id)
    assert code == 0 and out["profile"]["count"] >= 1
    code, out = run_cli(capsys, "results", run_id, "--format", "csv", "--out", str(tmp_path / "r.csv"))
    assert code == 0 and out["records"] >= 1 and (tmp_path / "r.csv").exists()
    assert run_cli(capsys, "results", "run-20260101-000000-abcdef")[0] == 2


def test_propose_a_place(agent_env, capsys):
    code, out = run_cli(capsys, "propose", "--place", "35TPF6637041552", "--label", "Istanbul office",
                        "--reason", "Client's registered office", "--country", "tr")
    assert code == 0
    place = out["proposal"]["places"][0]
    assert abs(place["lat"] - 41.0082) < 1e-4 and place["radius_km"] == 0.1 and place["read_as"] == "MGRS"
    assert place["country_suggested"] == "TR"
    assert run_cli(capsys, "propose", "--place", "41.0082, 28.9784", "--reason", "r")[0] == 2, "needs a label"
    assert run_cli(capsys, "propose", "--place", "28.9784, 41.0082x", "--label", "x", "--reason", "r")[0] == 2
    assert run_cli(capsys, "propose", "--place", "41.0082, 28.9784", "--label", "x", "--reason", "r",
                   "--radius-km", "500")[0] == 2
    code, out = run_cli(capsys, "propose", "--withdraw-place", "Istanbul office")
    assert "places" not in out["proposal"]


def test_status_shows_rejected_places_with_their_reason(agent_env, key, library, capsys):
    from lucid_warrant.config import Paths
    from lucid_warrant.places import record_place_rejections

    run_cli(capsys, "propose", "--place", "39.9334, 32.8597", "--label", "Ankara branch", "--reason", "branch")
    paths = Paths.default()
    record_place_rejections(paths, [{"label": "Ankara branch", "lat": 39.9334, "lon": 32.8597, "source": "agent",
                                     "decision": "rejected", "reject_reason": "Not part of this case"}], "2026-10-18T00:00:00Z")
    code, out = run_cli(capsys, "status")
    assert out["proposal"]["places"] == [{"label": "Ankara branch", "state": "rejected", "reason": "Not part of this case"}]


def test_the_agent_can_propose_a_pattern(agent_env, capsys):
    code, out = run_cli(capsys, "propose", "--term", "PROJECT*", "--pattern", "--reason", "every project code")
    assert code == 0 and out["proposal"]["terms"][-1]["match"] == "pattern"
    assert run_cli(capsys, "propose", "--term", "*", "--pattern", "--reason", "everything")[0] == 2
