"""Connecting a real search service: the configured http and python adapters, chosen by the signed configuration."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from lucid_warrant import org_config as oc
from lucid_warrant.backend import BackendError, HttpBackend, SearchResponse, fill_template, map_results
from lucid_warrant.executor import ExecutionDenied, load_payload, run_template
from lucid_warrant.rbac import get_rbac
from lucid_warrant.warrant import build_warrant, save_warrant
from tests.conftest import sign_org_file

ES_RESPONSE = {"took": 3, "hits": {"hits": [
    {"_id": "doc-1", "_source": {"title": "Kickoff", "timestamp": "2026-10-01T10:00:00Z", "country": "US",
                                 "content": "PROJECT_CEDAR kickoff with SUPPLIER_NORTH"}},
    {"_id": "doc-2", "_source": {"title": "Leak", "timestamp": "2026-10-02T10:00:00Z", "country": "DE",
                                 "content": "PROJECT_CEDAR in Germany"}},
    {"_id": "doc-3", "_source": {"title": "Third", "timestamp": "2026-10-03T10:00:00Z", "country": "US",
                                 "content": "PROJECT_CEDAR again"}},
]}}


@pytest.fixture
def server():
    """A local search API that records what it receives and answers like Elasticsearch."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                         "body": json.loads(self.rfile.read(length)) if length else None})
            if self.headers.get("Authorization") != "ApiKey s3cret":
                self.send_response(401)
                self.end_headers()
                return
            data = json.dumps(ES_RESPONSE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Request-Id", "svc-123")
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _answer

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    httpd.shutdown()


def es_config(url, **over):
    http = {"url": f"{url}/records/_search", "method": "POST", "secret_headers": {"Authorization": "SEARCH_API_AUTH"},
            "headers": {"X-Opaque-Id": "lucid-warrant {warrant_id}"},
            "body": {"query": {"query_string": {"query": "{query}"}}, "size": "{limit}", "who": "{parameter_2}"},
            "results_path": "hits.hits",
            "fields": {"record_id": "_id", "title": "_source.title", "timestamp": "_source.timestamp",
                       "country": "_source.country", "snippet": "_source.content"},
            "service_query_id": {"header": "X-Request-Id"}}
    http.update(over)
    return {"schema_version": 1, "search_backend": {"type": "http", "http": http}}


def warrant(paths, key, **kw):
    doc = build_warrant(parameter_1="0012", parameter_2=["ATTR_1000", "ATTR_1200"], purpose="Case DEMO-42", ttl_minutes=60,
                        max_rows=2, terms=[{"term_id": "t-a", "literal": "PROJECT_CEDAR", "source": "script"}],
                        rbac=get_rbac(paths), paths=paths, country_exclude=[{"code": "DE", "reason": "GDPR"}], **kw)
    save_warrant(paths, doc, key)
    return doc


def test_templates_keep_types_and_fill_text():
    values = {"query": "q", "limit": 5, "parameter_2": ["A", "B"], "warrant_id": "w-1"}
    assert fill_template({"size": "{limit}", "who": "{parameter_2}", "tag": "lw {warrant_id}", "n": [1, "{query}"]},
                         values) == {"size": 5, "who": ["A", "B"], "tag": "lw w-1", "n": [1, "q"]}


def test_http_backend_end_to_end_through_the_executor(paths, key, library, server, monkeypatch):
    url, seen = server
    monkeypatch.setenv("SEARCH_API_AUTH", "ApiKey s3cret")
    sign_org_file(paths, es_config(url))
    doc = warrant(paths, key)
    result = run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})
    (request,) = seen
    assert request["method"] == "POST" and request["path"] == "/records/_search"
    assert request["body"]["size"] == 2 and request["body"]["who"] == ["ATTR_1000", "ATTR_1200"]
    assert 'content:"PROJECT_CEDAR"' in request["body"]["query"]["query_string"]["query"]
    assert 'country:"DE"' not in request["body"]["query"]["query_string"]["query"], "the allow-list leaves DE out"
    assert request["headers"]["X-Opaque-Id"] == f"lucid-warrant {doc['warrant_id']}"
    assert [r["record_id"] for r in result["results"]] == ["doc-1", "doc-3"], "DE withheld, then capped at the warrant's 2"
    assert result["withheld_outside_geographic_constraint"] == 1 and result["service_query_id"] == "svc-123"
    assert load_payload(paths, result["run_id"])["service_query_id"] == "svc-123"


def test_get_requests_carry_params(paths, server, monkeypatch):
    url, seen = server
    monkeypatch.setenv("SOLR_AUTH", "ApiKey s3cret")
    backend = HttpBackend(oc.merge(oc.DEFAULTS, {"search_backend": {"http": {
        "url": f"{url}/solr/records/select", "method": "GET", "secret_headers": {"Authorization": "SOLR_AUTH"},
        "params": {"q": "{query}", "rows": "{limit}"}, "results_path": "hits.hits",
        "fields": {"record_id": "_id"}}}})["search_backend"]["http"])
    response = backend.search({"query": 'content:"X"', "limit": 7, "parameter_1": "p", "parameter_2": ["A"]}, None)
    assert seen[0]["method"] == "GET" and "q=content%3A%22X%22" in seen[0]["path"] and "rows=7" in seen[0]["path"]
    assert [r["record_id"] for r in response.results] == ["doc-1", "doc-2", "doc-3"]


def test_failures_are_refusals_that_send_nothing_and_use_no_quota(paths, key, library, server, monkeypatch):
    from lucid_warrant.executor import queries_in_last_hour
    url, seen = server
    sign_org_file(paths, es_config(url))
    doc = warrant(paths, key, max_queries_per_hour=5)
    monkeypatch.delenv("SEARCH_API_AUTH", raising=False)
    with pytest.raises(ExecutionDenied, match="SEARCH_API_AUTH"):
        run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})
    assert seen == [] and queries_in_last_hour(paths, doc["warrant_id"]) == 0
    monkeypatch.setenv("SEARCH_API_AUTH", "wrong")
    with pytest.raises(BackendError, match="401"):
        run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})


def test_an_unsigned_backend_change_stops_searches(paths, key, library, server):
    url, _ = server
    sign_org_file(paths, es_config(url))
    org = paths.data / "organization.json"
    org.write_text(org.read_text().replace("records/_search", "elsewhere/_search"))  # redirected without signing
    doc = warrant(paths, key)
    with pytest.raises(ExecutionDenied, match="search service unavailable.*isn't signed"):
        run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})


@pytest.mark.parametrize("bad", [
    {"type": "http", "http": {"url": "ftp://x"}},
    {"type": "http", "http": {"url": "https://x", "secret_headers": {"Authorization": "Bearer abc123"}}},
    {"type": "http", "http": {"url": "https://x", "body": {"q": "{querry}"}}},
    {"type": "python", "python": {"adapter": "not a path"}},
    {"type": "elastic"},
])
def test_bad_backend_configs_are_refused(paths, bad):
    with pytest.raises(oc.ConfigError):
        oc.validate(oc.merge(oc.DEFAULTS, {"search_backend": bad}), paths)


class FakeAdapter:
    def __init__(self, tag="x"):
        self.tag = tag

    def search(self, payload, plan):
        return SearchResponse([{"record_id": f"{self.tag}-{i}", "country": "US"} for i in range(5)], "fake-1")


def test_python_adapter_is_loaded_from_configuration(paths, key, library):
    sign_org_file(paths, {"schema_version": 1, "search_backend": {"type": "python", "python": {
        "adapter": "tests.test_search_backend:FakeAdapter", "options": {"tag": "rec"}}}})
    doc = warrant(paths, key)
    result = run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-a"})
    assert [r["record_id"] for r in result["results"]] == ["rec-0", "rec-1"] and result["service_query_id"] == "fake-1"


def test_map_results_reports_mismatched_paths():
    spec = es_config("http://x")["search_backend"]["http"]
    assert [r["record_id"] for r in map_results(ES_RESPONSE, spec)] == ["doc-1", "doc-2", "doc-3"]
    with pytest.raises(BackendError, match="no list of results"):
        map_results({"other": []}, spec)


def test_backend_check_previews_without_sending(paths, key, server, tmp_path, monkeypatch, capsys):
    from lucid_warrant import cli
    url, seen = server
    sign_org_file(paths, es_config(url))
    monkeypatch.setenv("LUCID_WARRANT_KEYS", str(paths.keys))
    monkeypatch.setenv("LUCID_WARRANT_DATA", str(paths.data))
    monkeypatch.delenv("SEARCH_API_AUTH", raising=False)
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps(ES_RESPONSE))
    code = cli.main(["backend", "check", "--sample-response", str(sample)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["secrets"]["Authorization"] == {"environment_variable": "SEARCH_API_AUTH", "set": False}
    assert out["example_request"]["headers"]["Authorization"] == "<secret from SEARCH_API_AUTH>"
    assert [r["record_id"] for r in out["sample_results"]] == ["doc-1", "doc-2", "doc-3"]
    assert seen == [], "a check never sends a search"
    monkeypatch.setenv("SEARCH_API_AUTH", "ApiKey s3cret")
    assert cli.main(["backend", "check"]) == 0


def test_a_missing_secret_doesnt_use_up_a_one_time_approval(paths, key, library, server, monkeypatch):
    from lucid_warrant.executor import run_onetime, used_onetime_ids
    from lucid_warrant.onetime import approve_request, list_requests, submit_request
    from lucid_warrant.signing import load_public_key
    url, seen = server
    sign_org_file(paths, es_config(url))
    doc = warrant(paths, key)
    submit_request(paths, load_public_key(paths), warrant_id=doc["warrant_id"], skeleton="{a}", bindings={"a": "t-a"},
                   rationale="r")
    (req,) = list_requests(paths)
    ot = approve_request(paths, key, req)
    monkeypatch.delenv("SEARCH_API_AUTH", raising=False)
    with pytest.raises(ExecutionDenied, match="isn't set"):
        run_onetime(paths, onetime_id=ot["onetime_id"])
    assert ot["onetime_id"] not in used_onetime_ids(paths) and seen == []
    monkeypatch.setenv("SEARCH_API_AUTH", "ApiKey s3cret")
    assert run_onetime(paths, onetime_id=ot["onetime_id"])["count"] == 2, "still usable once the secret is set"


def test_round7_http_config_must_send_the_checked_query():
    """Codex round 7 #3: a GET body is never sent, and a request without {query} would run something else."""
    from lucid_warrant import org_config as oc

    base = {"url": "https://s.example/search", "results_path": "hits", "fields": {"record_id": "id"}}
    for http, message in (({**base, "method": "GET", "body": {"q": "{query}"}}, "GET request has no body"),
                          ({**base, "method": "POST", "body": {"query": {"match_all": {}}}}, "must send the checked query")):
        with pytest.raises(oc.ConfigError, match=message):
            oc._validate_backend(oc.merge(oc.DEFAULTS, {"search_backend": {"type": "http", "http": http}})["search_backend"])
    ok = {**base, "method": "GET", "params": {"q": "{query}"}}
    oc._validate_backend(oc.merge(oc.DEFAULTS, {"search_backend": {"type": "http", "http": ok}})["search_backend"])


def test_round7_underscore_keys_in_a_request_body_are_kept():
    """Codex round 7 #9: Elasticsearch's _source is part of the request, not a comment."""
    from lucid_warrant import org_config as oc

    body = {"query": {"query_string": {"query": "{query}"}}, "_source": ["title", "country"]}
    merged = oc.merge(oc.DEFAULTS, {"search_backend": {"type": "http", "http": {"body": body}}})
    assert merged["search_backend"]["http"]["body"]["_source"] == ["title", "country"]


def test_round7_a_malformed_secret_never_reaches_records(monkeypatch):
    """Codex round 7 #4: a token with a stray newline is refused by name, and errors never quote it."""
    from lucid_warrant.backend import BackendNotReady, HttpBackend

    spec = {"url": "https://s.example/search", "method": "POST", "body": {"q": "{query}"}, "params": {}, "headers": {},
            "secret_headers": {"Authorization": "LW_TEST_SECRET"}, "results_path": "hits", "fields": {"record_id": "id"},
            "service_query_id": {}, "timeout_seconds": 5, "verify_tls": True}
    monkeypatch.setenv("LW_TEST_SECRET", "Bearer DUMMY_SECRET_123\n")
    backend = HttpBackend(spec)
    with pytest.raises(BackendNotReady) as err:
        backend.prepare({"query": "x", "limit": 1, "warrant_id": "w", "purpose": "p", "parameter_1": "a",
                         "parameter_2": []})
    assert "LW_TEST_SECRET" in str(err.value) and "DUMMY_SECRET_123" not in str(err.value)
    assert "DUMMY_SECRET_123" not in backend.scrub("ValueError: Invalid header value b'Bearer DUMMY_SECRET_123\\n'")


def test_round8_url_fragments_are_refused_and_params_land_in_the_query():
    """Codex round 8 A3: parameters appended after a # never reached the server."""
    from lucid_warrant import org_config as oc
    from lucid_warrant.backend import HttpBackend

    base = {"results_path": "hits", "fields": {"record_id": "id"}, "method": "GET"}
    for url, params in (("https://s.example/search#overview", {"q": "{query}"}), ("https://s.example/search#{query}", {})):
        with pytest.raises(oc.ConfigError, match="fragment"):
            oc._validate_backend(oc.merge(oc.DEFAULTS, {"search_backend": {"type": "http", "http": {
                **base, "url": url, "params": params}}})["search_backend"])
    spec = oc.merge(oc.DEFAULTS, {"search_backend": {"type": "http", "http": {
        **base, "url": "https://s.example/search?index=records", "params": {"q": "{query}"}}}})["search_backend"]["http"]
    url, _m, _h, _b = HttpBackend(spec).request({"query": 'a:"b"', "limit": 1, "parameter_1": "x", "parameter_2": []})
    assert url.startswith("https://s.example/search?index=records&q=") and "%22b%22" in url
