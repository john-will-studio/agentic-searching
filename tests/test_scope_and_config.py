"""Country scope, certifications, configuration and the config CLI."""

import json
import os
import subprocess
import sys

import pytest

from tests.conftest import PASSPHRASE, sign_org_file

from lucid_warrant import org_config as oc
from lucid_warrant.executor import ExecutionDenied, run_onetime, run_template
from lucid_warrant.lint import lint_query
from lucid_warrant.onetime import approve_request, list_requests, submit_request
from lucid_warrant.rbac import get_rbac
from lucid_warrant.signing import load_public_key
from lucid_warrant.warrant import WarrantError, build_warrant, save_warrant


@pytest.fixture(autouse=True)
def isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setenv("LUCID_WARRANT_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.delenv("LUCID_WARRANT_ORG_CONFIG", raising=False)


def scoped(paths, key, **kw):
    doc = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000", "ATTR_1200"], purpose="p", ttl_minutes=60,
                        max_rows=50, terms=[{"term_id": "t-a", "literal": "PROJECT_CEDAR", "source": "script"},
                                            {"term_id": "t-b", "literal": "SUPPLIER_NORTH", "source": "script"}],
                        rbac=get_rbac(paths), paths=paths, **kw)
    save_warrant(paths, doc, key)
    return doc


def ids(result):
    return [r["record_id"] for r in result["results"]]


def test_country_exclusions_enforced(paths, key, library):
    doc = scoped(paths, key, country_exclude=[{"code": "DE", "reason": "GDPR"}])
    result = run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})
    assert result["country_filter"]["exclude"] == ["DE"] and result["country_filter"]["include_derived"]
    assert 'country:"DE"' not in result["query"] and 'country:"TR"' in result["query"], "an allow-list without DE"
    assert "rec-002" not in ids(result)  # the DE record


def test_country_include_only(paths, key, library):
    doc = scoped(paths, key, country_include=["US"])
    result = run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})
    assert '(country:"US")' in result["query"]
    assert set(ids(result)) <= {"rec-001", "rec-005", "rec-008"}


def test_country_scope_applies_to_onetime_runs(paths, key, library):
    doc = scoped(paths, key, country_exclude=[{"code": "US", "reason": "test"}])
    submit_request(paths, load_public_key(paths), warrant_id=doc["warrant_id"], skeleton="{a}", bindings={"a": "t-a"}, rationale="")
    (request,) = list_requests(paths)
    approval = approve_request(paths, key, request)
    result = run_onetime(paths, onetime_id=approval["onetime_id"])
    assert 'country:"US"' not in result["query"] and not {"rec-001", "rec-005", "rec-008"} & set(ids(result))


@pytest.mark.parametrize("kw, message", [
    ({"country_include": ["ZZ"]}, "unknown country"),
    ({"country_exclude": [{"code": "DE", "reason": ""}]}, "reason"),
    ({"country_include": ["DE"], "country_exclude": [{"code": "DE", "reason": "x"}]}, "both included and excluded"),
    ({"country_exclude": [{"code": "DE", "reason": "x"}, {"code": "de", "reason": "y"}]}, "twice"),
])
def test_country_scope_validation(paths, key, kw, message):
    with pytest.raises(WarrantError, match=message):
        scoped(paths, key, **kw)


def test_lint_rejects_foreign_country_clause():
    query = '(content:"A") AND NOT country:"DE"'
    ok = lint_query(query, {"A"}, allowed_fields=["content"], country_field="country", country_codes={"DE"})
    bad = lint_query(query, {"A"}, allowed_fields=["content"], country_field="country", country_codes={"FR"})
    assert ok.passed and "E016" in [f.code for f in bad.findings]


def test_certifications_and_config_context_signed(paths, key):
    config = oc.load(paths)
    doc = scoped(paths, key, certifications=[{"id": "purpose", "text": config["certification"]["purpose"]},
                                             {"id": "limits", "text": config["certification"]["limits"]}],
                 config_context=oc.config_context(config))
    assert [c["id"] for c in doc["certifications"]] == ["purpose", "limits"]
    assert all(c["accepted"] and c["accepted_at"] for c in doc["certifications"])
    assert doc["config_context"]["sha256"] == oc.authority_hash(config)


def test_defaults_match_requirements(paths):
    c = oc.load(paths)
    assert c["defaults"]["lookback_days"] == 30 and c["defaults"]["max_queries_per_hour"] == 60
    assert c["country_scope"]["exclude"] == [{"code": "DE", "reason": "GDPR"}, {"code": "FR", "reason": "GDPR"},
                                             {"code": "IT", "reason": "GDPR"}]


def test_org_file_layers_and_refusals(paths):
    sign_org_file(paths, {"schema_version": 1, "terms": {"warrant": {"plural": "Authorizations"}}})
    assert oc.load(paths)["terms"]["warrant"] == {"singular": "Warrant", "plural": "Authorizations"}
    sign_org_file(paths, '{"schema_version": 1, "schema_version": 1}')
    with pytest.raises(oc.ConfigError, match="duplicate"):
        oc.load(paths)
    sign_org_file(paths, {"schema_version": 1, "defaults": {"lookback_days": True}})
    with pytest.raises(oc.ConfigError, match="whole number"):
        oc.load(paths)


def test_signed_wording_survives_config_change(paths, key):
    config = oc.load(paths)
    doc = scoped(paths, key, config_context=oc.config_context(config))
    sign_org_file(paths, {"schema_version": 1, "parameters": {"parameter_1": {"label": "Something else"}}})
    assert doc["config_context"]["values"]["parameters"]["parameter_1"]["label"] == "Search authorization"


def cli(paths, *args, stdin=None):
    if stdin is None and "--passphrase-stdin" in args:
        stdin = PASSPHRASE + "\n"  # organization changes are signed with the analyst's passphrase
    env = {**os.environ, "LUCID_WARRANT_KEYS": str(paths.keys), "LUCID_WARRANT_DATA": str(paths.data)}
    result = subprocess.run([sys.executable, "-m", "lucid_warrant.cli", "config", *args], capture_output=True, timeout=60,
                            text=True, env=env, input=stdin)
    return result.returncode, json.loads(result.stdout)


def test_config_cli_contract(paths):
    code, out = cli(paths, "path")
    assert code == 0 and out["data"]["org"]["hash"] == "missing"
    code, out = cli(paths, "set", "parameters.parameter_1.label", "--value", "Case authorization")
    assert code == 4 and out["error"]["code"] == "SCOPE_REFUSED"
    code, out = cli(paths, "set", "parameters.parameter_1.label", "--value", "Case authorization", "--scope", "org")
    assert code == 4 and out["error"]["code"] == "ACK_REQUIRED"
    code, out = cli(paths, "set", "parameters.parameter_1.label", "--value", "Case authorization", "--scope", "org",
                    "--passphrase-stdin", "--reason", "test", "--if-hash", "missing")
    assert code == 0 and out["data"]["written"] and out["data"]["authority_changed"]
    code, out = cli(paths, "set", "defaults.lookback_days", "--value-json", "14", "--scope", "org",
                    "--passphrase-stdin", "--reason", "test", "--if-hash", "missing")
    assert code == 6 and out["error"]["code"] == "CONFLICT"
    code, out = cli(paths, "set", "nonsense.key", "--value", "x", "--scope", "org", "--dry-run")
    assert code == 3 and out["error"]["code"] == "UNKNOWN_KEY"
    code, out = cli(paths, "get", "organization.revision")
    assert code == 0 and out["data"]["value"] == 2
    code, out = cli(paths, "theme", "set", "violet", "--mode", "light")
    assert code == 0
    code, out = cli(paths, "show", "--sources")
    assert out["data"]["sources"]["ui.scheme"] == "user" and out["data"]["sources"]["parameters.parameter_1.label"] == "org"
    history = [json.loads(line) for line in (paths.data / "config-history.jsonl").read_text().splitlines()]
    assert [h["status"] for h in history] == ["pending", "committed"] and history[0]["change_id"] == history[1]["change_id"]


def test_low_contrast_scheme_warned(paths):
    assert oc.contrast_problems({**__import__("lucid_warrant.ui.theme", fromlist=["x"]).PALETTES["light"], "accent": "#eeeeee"})


# --- config CLI contract (Codex N-F11 … N-F19) ---------------------------------------------

def test_cli_unknown_keys_usage_and_missing_files(paths):
    for args in (["get", "nonexistent"], ["set", "ui.nonexistent", "--value", "x"], ["reset", "ui.nonexistent"]):
        code, out = cli(paths, *args)
        assert code == 3 and out["error"]["code"] == "UNKNOWN_KEY", args
    code, out = cli(paths, "set")
    assert code == 2 and out["error"]["code"] == "USAGE"
    code, out = cli(paths, "validate", "--file", str(paths.data / "missing.json"))
    assert code == 5 and out["error"]["code"] == "FILE_ERROR"
    code, out = cli(paths, "set", "ui", "--value-json", '{"density":"compact","density":"spacious"}')
    assert code == 3 and out["error"]["code"] == "INVALID_JSON"


def test_cli_revision_reason_and_history(paths):
    code, out = cli(paths, "set", "organization.name", "--value", "Acme", "--scope", "org", "--passphrase-stdin",
                    "--reason", "   ", "--if-hash", "missing")
    assert code == 4 and "--reason" in out["error"]["message"]
    code, out = cli(paths, "set", "organization.name", "--value", "Acme", "--scope", "org", "--passphrase-stdin",
                    "--reason", "setup", "--if-hash", "missing")
    assert code == 0
    code, out = cli(paths, "reset", "organization.revision", "--scope", "org", "--passphrase-stdin", "--reason", "x",
                    "--if-hash", out["data"]["new_file_hash"])
    assert code == 4
    _, path = cli(paths, "path")
    code, out = cli(paths, "set", "ui.density", "--value", "compact", "--scope", "org", "--passphrase-stdin",
                    "--reason", "org default", "--if-hash", path["data"]["org"]["hash"])
    assert code == 0 and oc.load(paths)["organization"]["revision"] == 3, "every org change bumps the revision"


def test_cli_history_failure_leaves_config_unchanged(paths):
    (paths.data / "config-history.jsonl").mkdir(parents=True)  # history can't be written
    code, out = cli(paths, "set", "organization.name", "--value", "Unlogged", "--scope", "org", "--passphrase-stdin",
                    "--reason", "x", "--if-hash", "missing")
    assert code == 5 and not (paths.data / "organization.json").exists()


def test_cli_concurrent_user_writes_keep_both(paths):
    import threading

    results = []

    def run(args):
        results.append(cli(paths, *args)[0])
    threads = [threading.Thread(target=run, args=(a,)) for a in (["set", "ui.density", "--value", "compact"],
                                                                  ["set", "ui.base_font_pt", "--value-json", "14"])]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    prefs = json.loads(oc.user_path().read_text())
    assert results == [0, 0] and prefs["ui"]["density"] == "compact" and prefs["ui"]["base_font_pt"] == 14


def test_cli_failed_write_is_marked_aborted(paths, monkeypatch):
    import argparse

    from lucid_warrant import config_cli  # in-process, so the failure can be injected

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(oc, "write_signed_config", boom)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(PASSPHRASE + "\n"))
    parser = argparse.ArgumentParser()
    config_cli.add_parser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["config", "set", "organization.name", "--value", "X", "--scope", "org",
                              "--passphrase-stdin", "--reason", "x", "--if-hash", "missing"])
    code = config_cli.run(args, paths)
    history = [json.loads(line) for line in (paths.data / "config-history.jsonl").read_text().splitlines()]
    assert code == 5 and [h["status"] for h in history] == ["pending", "aborted"]


def test_appearance_save_uses_the_shared_lock(paths):
    target = oc.user_path()
    with oc.FileLock(target, timeout=0.2):
        with pytest.raises(oc.ConfigError):
            with oc.FileLock(target, timeout=0.2):
                pass
    oc.save_user_ui(paths, {"density": "compact"})
    assert json.loads(target.read_text())["ui"]["density"] == "compact"


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_every_preset_tints_the_background_and_keeps_contrast(mode):
    for sid, scheme in oc.BUILTIN_SCHEMES.items():
        palette = oc.derive_palette(mode, scheme)
        assert not oc.contrast_problems(palette), (mode, sid)
        if scheme.get("color") and oc.theme_hue_chroma(scheme["color"])[1] > 0.3:
            assert palette["bg"] != oc.BASE_PALETTES[mode]["bg"], (mode, sid)


def test_custom_theme_color_validation(paths):
    ok = oc.merge(oc.DEFAULTS, {"ui": {"scheme": "custom", "theme_color": "#336699"}})
    oc.validate(ok, paths)
    assert oc.schemes(ok)["custom"]["color"] == "#336699"
    for bad in ({"scheme": "custom"}, {"theme_color": "blue"}):
        with pytest.raises(oc.ConfigError):
            oc.validate(oc.merge(oc.DEFAULTS, {"ui": bad}), paths)
    oc.validate(oc.merge(oc.DEFAULTS, {"ui": {"scheme": "violet"}}), paths)  # earlier names still load


def test_cli_theme_lock_contention_exits_6(paths):
    with oc.FileLock(oc.user_path()):
        code, out = cli(paths, "theme", "set", "blue")
    assert code == 6 and out["error"]["code"] == "LOCKED"


# --- the organization file carries the analyst's signature -------------------------------------

def test_unsigned_or_edited_org_file_is_refused(paths):
    org = paths.data / "organization.json"
    org.parent.mkdir(parents=True, exist_ok=True)
    org.write_text(json.dumps({"schema_version": 1, "defaults": {"lookback_days": 3650}}))
    with pytest.raises(oc.ConfigError, match="isn't signed with your key") as exc:
        oc.load(paths)
    assert exc.value.code == "UNSIGNED_CONFIG"
    sign_org_file(paths, {"schema_version": 1, "defaults": {"lookback_days": 60}})
    assert oc.load(paths)["defaults"]["lookback_days"] == 60
    org.write_text(org.read_text().replace("60", "3650"))  # an edit after signing
    with pytest.raises(oc.ConfigError, match="changed after it was signed"):
        oc.load(paths)


def test_org_change_needs_the_passphrase(paths):
    base = ("set", "defaults.lookback_days", "--value-json", "45", "--scope", "org", "--reason", "r", "--if-hash", "missing")
    code, out = cli(paths, *base)  # no terminal, no passphrase: what an agent running commands gets
    assert code == 4 and out["error"]["code"] == "PASSPHRASE_REQUIRED"
    code, out = cli(paths, *base, "--passphrase-stdin", stdin="wrong guess\n")
    assert code == 4 and out["error"]["code"] == "BAD_PASSPHRASE"
    assert not (paths.data / "organization.json").exists(), "nothing written without the passphrase"
    code, out = cli(paths, *base, "--passphrase-stdin")
    assert code == 0 and oc.load(paths)["defaults"]["lookback_days"] == 45, "signed, so it loads"


def test_config_sign_after_a_reviewed_hand_edit(paths):
    org = paths.data / "organization.json"
    org.parent.mkdir(parents=True, exist_ok=True)
    org.write_text(json.dumps({"schema_version": 1, "organization": {"name": "Acme"}}))
    _, out = cli(paths, "path")
    reviewed = out["data"]["org"]["hash"]
    code, out = cli(paths, "sign", "--reason", "hand edit reviewed", "--if-hash", reviewed, "--passphrase-stdin")
    assert code == 0 and oc.load(paths)["organization"]["name"] == "Acme"
    code, out = cli(paths, "sign", "--reason", "x", "--if-hash", "stale", "--passphrase-stdin")
    assert code == 6 and out["error"]["code"] == "CONFLICT", "only the version you reviewed gets signed"


def test_a_preexisting_org_file_is_never_signed_by_creating_the_key(tmp_path):
    """Codex round 7 #6: it's shown for review and signed only on the analyst's say-so."""
    from lucid_warrant.config import Paths
    from lucid_warrant.signing import generate_keypair, load_private_key
    p = Paths(keys=tmp_path / "k", data=tmp_path / "d")
    org = p.data / "organization.json"
    org.parent.mkdir(parents=True)
    org.write_text(json.dumps({"schema_version": 1, "search_backend": {"type": "http", "http": {
        "url": "https://unreviewed.invalid/search", "body": {"q": "{query}"}, "results_path": "r",
        "fields": {"record_id": "id"}}}}))
    oc.load(p)  # no key yet: nothing to sign with, and nothing can be searched
    generate_keypair(p, PASSPHRASE)
    assert oc.unsigned_org_file(p) == org
    with pytest.raises(oc.ConfigError) as err:
        oc.load(p)
    assert err.value.code == "UNSIGNED_CONFIG"
    assert "Searches go to: POST https://unreviewed.invalid/search" in oc.review_lines(oc.read_file(org))
    oc.sign_reviewed(p, org, oc.read_file(org), load_private_key(p, PASSPHRASE))
    assert oc.load(p)["organization"]["revision"] == 2, "signed as a new revision"


def test_public_key_alone_still_requires_a_signed_org_file(tmp_path):
    """Codex round 7 #1: a search machine may hold only the public key; that is no reason to trust an edit."""
    from lucid_warrant.config import Paths
    from lucid_warrant.signing import generate_keypair
    p = Paths(keys=tmp_path / "k", data=tmp_path / "d")
    generate_keypair(p, PASSPHRASE)
    p.private_key.unlink()
    org = p.data / "organization.json"
    org.parent.mkdir(parents=True, exist_ok=True)
    org.write_text(json.dumps({"schema_version": 1}))
    with pytest.raises(oc.ConfigError) as err:
        oc.load(p)
    assert err.value.code == "UNSIGNED_CONFIG"


def test_the_bytes_verified_are_the_bytes_used(tmp_path, monkeypatch):
    """Codex round 7 #2: swapping the file after the signature check must not change what's loaded."""
    from lucid_warrant import signing
    from lucid_warrant.config import Paths
    from lucid_warrant.signing import generate_keypair, load_private_key
    p = Paths(keys=tmp_path / "k", data=tmp_path / "d")
    generate_keypair(p, PASSPHRASE)
    org = p.data / "organization.json"
    org.parent.mkdir(parents=True, exist_ok=True)
    oc.write_signed_config(org, {"schema_version": 1, "organization": {"name": "Signed"}}, load_private_key(p, PASSPHRASE))
    real = signing.verify_bytes

    def verify_then_swap(path, data, key):
        real(path, data, key)
        org.write_text(json.dumps({"schema_version": 1, "organization": {"name": "UNSIGNED swap"}}))

    monkeypatch.setattr(signing, "verify_bytes", verify_then_swap)
    assert oc.load(p, user=False)["organization"]["name"] == "Signed"


def test_an_older_signed_config_is_refused_for_searches(tmp_path):
    """Codex round 7 #5 and round 8 A6: neither an older revision nor different content under the same revision."""
    from lucid_warrant.config import Paths
    from lucid_warrant.executor import _check_config_is_current

    p = Paths(keys=tmp_path / "k", data=tmp_path / "d")

    def conf(n, name="A"):
        return oc.merge(oc.DEFAULTS, {"organization": {"revision": n, "name": name}})

    warrant = {"config_context": oc.config_context(conf(3))}
    with pytest.raises(RuntimeError, match="older than revision 3 that this warrant was signed under"):
        _check_config_is_current(p, conf(2), warrant)
    with pytest.raises(RuntimeError, match="different content under the same number"):
        _check_config_is_current(p, conf(3, "B"), warrant)
    _check_config_is_current(p, conf(4), warrant)  # newer than the warrant's: a later, signed change
    with pytest.raises(RuntimeError, match="already used revision 4"):
        _check_config_is_current(p, conf(3), {})
    with pytest.raises(RuntimeError, match="different revision 4"):
        _check_config_is_current(p, conf(4, "B"), {})
    _check_config_is_current(p, conf(4), {})
    assert oc.next_revision(p, 1) == 5, "a re-signed old file gets a number no configuration has used"
    (p.state / "config_revision.json").write_text("not json")
    with pytest.raises(RuntimeError, match="can't be read"):
        _check_config_is_current(p, conf(4), {})

def test_round8_config_sign_signs_the_bytes_that_were_reviewed(paths, monkeypatch):
    """Codex round 8 A2: replacing the file after its hash is checked must not get the replacement signed."""
    import hashlib
    from types import SimpleNamespace

    from lucid_warrant import config_cli
    from lucid_warrant.signing import load_private_key

    org = paths.data / "organization.json"
    org.parent.mkdir(parents=True, exist_ok=True)
    reviewed = json.dumps({"schema_version": 1, "organization": {"name": "Reviewed"}}).encode()
    org.write_bytes(reviewed)
    real_parse = oc.parse_bytes

    swapped = []

    def swap_then_parse(path, raw):
        if not swapped:  # once: right after the reviewed bytes were read and their hash checked
            swapped.append(True)
            org.write_text(json.dumps({"schema_version": 1, "organization": {"name": "UNREVIEWED"}}))
        return real_parse(path, raw)

    monkeypatch.setattr(oc, "parse_bytes", swap_then_parse)
    monkeypatch.setattr(config_cli, "_signing_key", lambda _p, _a: load_private_key(paths, PASSPHRASE))
    config_cli._sign_existing(SimpleNamespace(if_hash=hashlib.sha256(reviewed).hexdigest(), reason="r"), paths, None)
    monkeypatch.setattr(oc, "parse_bytes", real_parse)
    assert oc.load(paths)["organization"]["name"] == "Reviewed"


def test_round8_a_fractional_radius_cap_is_accepted():
    """Codex round 8 A17: a 100 m organizational cap (0.1 km) was rejected as "must be a whole number"."""
    merged = oc.merge(oc.DEFAULTS, {"geography": {"max_radius_km": 0.1}})
    oc._check_types(merged, oc.DEFAULTS, "")  # was: "must be a whole number"
    with pytest.raises(oc.ConfigError, match="whole number"):
        oc._check_types(oc.merge(oc.DEFAULTS, {"defaults": {"lookback_days": 1.5}}), oc.DEFAULTS, "")
    from lucid_warrant.places import PlaceError, check_place
    place = {"place_id": "p-x", "label": "x", "lat": 41.0082, "lon": 28.9784, "radius_km": 0.15, "country": "TR",
             "reason": "r", "source": "human"}
    with pytest.raises(PlaceError, match="0.01 to 0.1 km"):
        check_place(place, {}, 0.1)


def test_round9_review_exposes_authority_and_changes(paths, monkeypatch, capsys):
    import hashlib
    from types import SimpleNamespace
    from lucid_warrant import config_cli

    candidate = {"schema_version": 1, "certification": {"purpose": "Custom certification"},
                 "geography": {"place_clause": "CIRCLE({lon},{lat},{km})"},
                 "country_scope": {"mode": "include", "include": ["TR"]},
                 "search_backend": {"type": "python", "python": {"adapter": "tests.fake_search_adapter:Adapter",
                                                                             "options": {"prefix": "review"}}}}
    path = paths.data / "organization.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate))
    raw = path.read_bytes()
    review, _ = config_cli._dispatch(SimpleNamespace(config_cmd="path", org_config=None), paths)
    assert review["org"]["hash"] == hashlib.sha256(raw).hexdigest()
    assert review["org"]["full_config"]["certification"]["purpose"] == "Custom certification"
    lines = "\n".join(review["org"]["review"])
    for text in ("Changes from built-in defaults", "Destination:", "Place clause:", "Field mappings",
                 "Certification wording", "Country mode: include", "Adapter options", '"prefix"', "Custom certification"):
        assert text in lines
    assert "None." in oc.review_lines({})

    def unlock(*_):
        assert "Changes from built-in defaults" in capsys.readouterr().err, "review precedes unlocking"
        return load_private_key(paths, PASSPHRASE)

    from lucid_warrant.signing import load_private_key
    monkeypatch.setattr(config_cli, "_signing_key", unlock)
    config_cli._sign_existing(SimpleNamespace(if_hash=review["org"]["hash"], reason="review"), paths, None)
    assert oc.load(paths)["certification"]["purpose"] == "Custom certification"


def test_round9_revision_check_is_one_locked_transaction(tmp_path, monkeypatch):
    """Codex round 9 #2: a concurrent check can't write back an older record over a newer one."""
    import threading

    from lucid_warrant.config import Paths
    from lucid_warrant.executor import _check_config_is_current

    p = Paths(keys=tmp_path / "k", data=tmp_path / "d")
    conf = lambda n: oc.merge(oc.DEFAULTS, {"organization": {"revision": n}})  # noqa: E731
    real_read = oc.used_revisions
    started, release = threading.Event(), threading.Event()

    def slow_read(paths):
        state = real_read(paths)
        if threading.current_thread().name == "rev2":
            started.set()
            release.wait(2)
        return state

    monkeypatch.setattr(oc, "used_revisions", slow_read)
    t = threading.Thread(target=_check_config_is_current, args=(p, conf(2), {}), name="rev2")
    t.start()
    started.wait(2)
    t3 = threading.Thread(target=_check_config_is_current, args=(p, conf(3), {}))
    t3.start()
    release.set()
    t.join(5)
    t3.join(5)
    monkeypatch.setattr(oc, "used_revisions", real_read)
    assert oc.used_revisions(p)["newest"] == 3, "the newer revision isn't lost"
