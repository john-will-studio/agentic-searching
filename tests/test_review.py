import json

import pytest

from lucid_warrant.executor import ExecutionDenied, run_template
from lucid_warrant.rbac import get_rbac
from lucid_warrant.review import load_rejected, merge, read_inbox, reject, unreject, warranted_terms
from lucid_warrant.signing import load_public_key
from lucid_warrant.warrant import build_warrant, save_warrant, supersede_warrant


def write_inbox(paths, nominations, proposal=None):
    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.nominations.write_text(json.dumps({"terms": nominations}))
    if proposal is not None:
        paths.proposal.write_text(json.dumps(proposal))


def test_merge_keeps_decisions_and_skips_hidden(paths):
    write_inbox(paths, [
        {"term_id": "t-a", "literal": "A", "provenance": "p"},
        {"term_id": "t-b", "literal": "B", "provenance": "p"},
        {"term_id": "t-c", "literal": "C", "provenance": "p"},
    ], {"purpose": "draft", "terms": [{"literal": "D", "reason": "seen in results"}]})
    queue = [{"term_id": "t-a", "literal": "A", "source": "script", "provenance": "p", "decision": "approved"}]
    candidates, purpose, problems = read_inbox(paths)
    added = merge(queue, candidates, hidden={"C"})
    assert [t["literal"] for t in added] == ["B", "D"]
    assert queue[0]["decision"] == "approved"  # untouched
    assert all(t["decision"] == "pending" for t in added)
    assert purpose == "draft" and problems == []
    assert merge(queue, candidates, hidden={"C"}) == []  # idempotent


def test_rejections_are_remembered_and_undoable(paths):
    reject(paths, {"literal": "NOISE", "term_id": "t-n", "source": "agent", "provenance": "x"})
    assert "NOISE" in load_rejected(paths)
    unreject(paths, "NOISE")
    assert load_rejected(paths) == {}


def test_warranted_terms_only_counts_active(paths, key, warrant):
    covered = warranted_terms(paths, load_public_key(paths))
    assert covered["PROJECT_CEDAR"]["warrant_id"] == warrant["warrant_id"]


def test_supersede_carries_terms_and_retires_old(paths, key, warrant):
    new = build_warrant(
        parameter_1=warrant["parameter_1"], parameter_2=warrant["parameter_2"], purpose=warrant["purpose"],
        terms=warrant["terms"] + [{"term_id": "t-new", "literal": "NEW_TERM", "source": "agent"}],
        ttl_minutes=60, max_rows=warrant["max_rows_per_run"], rbac=get_rbac(paths), supersedes=warrant["warrant_id"],
    )
    save_warrant(paths, new, key)
    supersede_warrant(paths, warrant["warrant_id"], new["warrant_id"])
    with pytest.raises(ExecutionDenied, match=f"superseded by {new['warrant_id']}"):
        run_template(paths, warrant_id=warrant["warrant_id"], template_id="single-term", bindings={"term": "t-project"})
    result = run_template(paths, warrant_id=new["warrant_id"], template_id="all-of-two",
                          bindings={"a": "t-project", "b": "t-new"})
    assert result["query"] == 'content:"PROJECT_CEDAR" AND content:"NEW_TERM"'
    assert new["supersedes"] == warrant["warrant_id"]


# --- client ID: the client a hit term is associated with ---------------------------------------

def test_client_id_flows_from_inbox_and_refreshes_pending(paths):
    import json as _json

    from lucid_warrant.review import merge, read_inbox
    paths.nominations.parent.mkdir(parents=True, exist_ok=True)
    paths.nominations.write_text(_json.dumps({"terms": [
        {"term_id": "t-a", "literal": "ALPHA", "client_id": "CL-1"},
        {"term_id": "t-b", "literal": "BRAVO"},
        {"term_id": "t-c", "literal": "CHARLIE", "client_id": "bad id with spaces"}]}))
    candidates, _purpose, problems = read_inbox(paths)
    assert [(c["literal"], c["client_id"]) for c in candidates] == [("ALPHA", "CL-1"), ("BRAVO", "")]
    assert any("CHARLIE" in p and "client ID" in p for p in problems)
    queue: list = []
    merge(queue, candidates, set())
    merge(queue, [{**candidates[1], "client_id": "CL-2"}], set())  # the script later adds BRAVO's client
    assert queue[1]["client_id"] == "CL-2" and len(queue) == 2


def test_client_id_signed_into_warrant_and_validated(paths):
    from lucid_warrant.rbac import get_rbac
    from lucid_warrant.warrant import WarrantError, build_warrant

    def build(client_id):
        return build_warrant(parameter_1="0012", parameter_2=["ATTR_1000"], purpose="p", ttl_minutes=60, max_rows=10,
                             terms=[{"term_id": "t-a", "literal": "ALPHA", "source": "script", "client_id": client_id}],
                             rbac=get_rbac(paths))
    assert build("CL-100482")["terms"][0]["client_id"] == "CL-100482"
    assert "client_id" not in build("")["terms"][0]
    with pytest.raises(WarrantError, match="client ID"):
        build("x" * 65)


def test_client_id_never_reaches_the_query(paths, key, library):
    from lucid_warrant.executor import load_payload, run_template
    from lucid_warrant.rbac import get_rbac
    from lucid_warrant.warrant import build_warrant, save_warrant

    doc = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000", "ATTR_1200"], purpose="p", ttl_minutes=60,
                        max_rows=10, terms=[{"term_id": "t-a", "literal": "PROJECT_CEDAR", "source": "script",
                                             "client_id": "CL-100482"}], rbac=get_rbac(paths))
    save_warrant(paths, doc, key)
    run = run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})
    assert "CL-100482" not in json.dumps(load_payload(paths, run["run_id"]))


def test_rejection_waits_hold_then_resurface_with_the_reason(paths):
    from datetime import timedelta

    from lucid_warrant.review import held_rejections, is_held, load_rejected, merge, reject
    from lucid_warrant.util import utc_now
    now = utc_now()
    reject(paths, {"literal": "ALPHA", "reject_reason": "Not linked to the case yet.", "reassess_minutes": 60}, now=now)
    reject(paths, {"literal": "BRAVO", "reject_reason": "Personal name.", "reassess_minutes": None}, now=now)
    entries = load_rejected(paths)
    assert entries["BRAVO"]["renominate_after"] is None and entries["ALPHA"]["reason"] == "Not linked to the case yet."
    assert set(held_rejections(paths, now)) == {"ALPHA", "BRAVO"}
    later = now + timedelta(minutes=61)
    assert set(held_rejections(paths, later)) == {"BRAVO"} and is_held({"literal": "OLD"}, later)
    queue: list = []
    candidates = [{"term_id": "a-alpha", "literal": "ALPHA", "source": "agent", "provenance": "agent: new context"},
                  {"term_id": "a-bravo", "literal": "BRAVO", "source": "agent", "provenance": ""}]
    merge(queue, candidates, set(held_rejections(paths, now)), entries)
    assert queue == [], "both held during the wait"
    merge(queue, candidates, set(held_rejections(paths, later)), entries)
    assert [t["literal"] for t in queue] == ["ALPHA"]
    assert queue[0]["rejected_before"]["reason"] == "Not linked to the case yet." and queue[0]["unreject_on_sign"]


def test_agent_status_lists_rejections(paths, key, library):
    import os
    import subprocess
    import sys

    from lucid_warrant.review import reject
    reject(paths, {"literal": "ALPHA", "reject_reason": "Out of scope.", "reassess_minutes": None})
    env = {**os.environ, "LUCID_WARRANT_KEYS": str(paths.keys), "LUCID_WARRANT_DATA": str(paths.data)}
    out = json.loads(subprocess.run([sys.executable, "-m", "lucid_warrant.cli", "status"], capture_output=True, timeout=60,
                                    text=True, env=env).stdout)
    (entry,) = out["rejected_terms"]
    assert entry["literal"] == "ALPHA" and entry["reason"] == "Out of scope." and entry["renominate_after"] is None
    assert entry["may_renominate_now"] is False
