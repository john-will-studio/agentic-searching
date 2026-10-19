"""Pattern hit terms (PROJECT*): approved as patterns, sent unquoted so the wildcard works, and never broader than
signed: at least three ordinary characters come first, so a pattern can't mean "everything"."""

import pytest

from lucid_warrant.executor import run_template
from lucid_warrant.lint import lint_query
from lucid_warrant.rbac import get_rbac
from lucid_warrant.syntax import SyntaxRejected, compile_query, validate_pattern
from lucid_warrant.warrant import WarrantError, build_warrant, save_warrant

TERMS = [{"term_id": "t-project", "literal": "PROJECT*", "source": "human", "match": "pattern"},
         {"term_id": "t-supplier", "literal": "SUPPLIER_NORTH", "source": "human"}]


def make(paths, terms=TERMS):
    return build_warrant(parameter_1="0012", parameter_2=["ATTR_1000", "ATTR_1200"], purpose="p", ttl_minutes=60,
                         max_rows=50, terms=terms, rbac=get_rbac(paths), paths=paths)


@pytest.mark.parametrize("bad", ["*", "P*", "PR?", "*CEDAR", "PROJECT", "PRO JECT*", 'PRO"JECT*', "PROJECT:*"])
def test_a_pattern_must_extend_real_text(bad):
    with pytest.raises(SyntaxRejected):
        validate_pattern(bad)


@pytest.mark.parametrize("good", ["PROJECT*", "SUPPLIER_N?RTH", "CL-10048*", "PRO*_CEDAR"])
def test_patterns_people_use(good):
    assert validate_pattern(good) == good


def test_the_warrant_records_which_terms_are_patterns(paths):
    doc = make(paths)
    assert doc["terms"][0]["match"] == "pattern" and "match" not in doc["terms"][1]
    with pytest.raises(WarrantError, match="must be one word that starts with at least 3"):
        make(paths, [{"term_id": "t-all", "literal": "*", "source": "human", "match": "pattern"}])


def test_a_signed_pattern_searches_with_its_wildcard(paths, key, library):
    doc = make(paths)
    save_warrant(paths, doc, key)
    result = run_template(paths, warrant_id=doc["warrant_id"], template_id="single-term", bindings={"term": "t-project"})
    assert result["query"].startswith("(content:PROJECT*)") or "content:PROJECT*" in result["query"]
    ids = {r["record_id"] for r in result["results"]}
    assert ids, "the practice records include PROJECT_CEDAR and PROJECT_BIRCH"
    assert all("PROJECT_" in (r.get("snippet", "") + r.get("title", "")) for r in result["results"])


def test_the_final_check_accepts_only_the_signed_pattern():
    approved = {"PROJECT*", "SUPPLIER_NORTH"}
    ok = lint_query('content:PROJECT* AND content:"SUPPLIER_NORTH"', approved, allowed_fields={"content"},
                    patterns={"PROJECT*"})
    assert ok.passed
    for wider in ("content:PRO*", "content:PROJECT*S*", "content:*"):
        assert not lint_query(f'{wider} AND content:"SUPPLIER_NORTH"', approved, allowed_fields={"content"},
                              patterns={"PROJECT*"}).passed, wider
    exact_only = lint_query("content:PROJECT*", {"PROJECT*"}, allowed_fields={"content"})
    assert not exact_only.passed, "approved as exact text, it can't be used as a pattern"


def test_an_exact_term_with_a_star_stays_exact():
    c = compile_query("{a}", {"a": "PROJECT*"}, default_field="content", allowed_fields={"content"})
    assert c.query == 'content:"PROJECT*"'


def test_profile_counts_pattern_matches():
    from lucid_warrant.results import profile

    rows = [{"record_id": "1", "country": "US", "title": "x", "snippet": "PROJECT_CEDAR kickoff"},
            {"record_id": "2", "country": "US", "title": "x", "snippet": "PROJECT_BIRCH update"},
            {"record_id": "3", "country": "US", "title": "x", "snippet": "unrelated"}]
    prof = profile(rows, ["t-project"], {"terms": TERMS})
    assert prof["pivot"]["rows"][0]["total"] == 2


@pytest.mark.parametrize("query", ['content:"PROJECT*"', 'content:"PROJECT*"~9', 'content:PROJECT*~9',
                                   'content:PROJECT*^2', 'title:PROJECT*', 'content:PRO*'])
def test_final_lint_refuses_changed_pattern_semantics(query):
    assert not lint_query(query, {"PROJECT*"}, allowed_fields={"content"}, patterns={"PROJECT*"}).passed


def test_final_lint_requires_a_valid_bound_pattern():
    assert not lint_query('content:PROJECT*', set(), allowed_fields={"content"}, patterns={"PROJECT*"}).passed
    assert not lint_query('content:P*', {"P*"}, allowed_fields={"content"}, patterns={"P*"}).passed


def test_one_time_pattern_review_and_execution(paths, key, library):
    from lucid_warrant.onetime import preview, submit_request, approve_request
    from lucid_warrant.executor import run_onetime

    doc = make(paths)
    save_warrant(paths, doc, key)
    bindings = {"a": "t-project", "b": "t-supplier"}
    skeleton = "{a} AND NOT {b}"
    _, compiled = preview(paths, key.public_key(), doc["warrant_id"], skeleton, bindings)
    assert "any word matching the pattern PROJECT*" in compiled.plain_english
    request = submit_request(paths, key.public_key(), warrant_id=doc["warrant_id"], skeleton=skeleton,
                             bindings=bindings, rationale="check the whole project family")
    approval = approve_request(paths, key, request)
    result = run_onetime(paths, onetime_id=approval["onetime_id"])
    assert "content:PROJECT*" in result["query"]
    with pytest.raises(SyntaxRejected, match="proximity"):
        preview(paths, key.public_key(), doc["warrant_id"], "{a}~3", {"a": "t-project"})


def test_rejected_patterns_keep_their_mode(paths):
    from lucid_warrant.review import reject, load_rejected

    reject(paths, TERMS[0])
    assert load_rejected(paths)["PROJECT*"]["match"] == "pattern"


def test_only_undecided_proposals_follow_source_mode_changes():
    from lucid_warrant.review import merge

    candidate = {**TERMS[0], "source": "agent"}
    queue = []
    merge(queue, [{**candidate, "match": "exact"}], set())
    merge(queue, [candidate], set())
    assert queue[0]["match"] == "pattern"
    queue[0]["decision"] = "approved"
    merge(queue, [{**candidate, "match": "exact"}], set())
    assert queue[0]["match"] == "pattern"
    queue[0].update(decision="pending", source="human-edited")
    merge(queue, [{**candidate, "match": "exact"}], set())
    assert queue[0]["match"] == "pattern"


@pytest.mark.parametrize("pattern", ["PRO+*", "PRO-*", "PRO&*", "PRO/*", "PRO.?", "PRO@*", "PRO#*"])
def test_pattern_escaping_roundtrips_without_broadening(pattern):
    compiled = compile_query("{a}", {"a": pattern}, default_field="content", allowed_fields={"content"},
                             patterns={pattern})
    assert lint_query(compiled.query, {pattern}, allowed_fields={"content"}, patterns={pattern}).passed


def test_existing_exact_approval_does_not_cover_pattern(paths, key, library):
    from lucid_warrant.review import warranted_terms

    doc = make(paths, [{**TERMS[0], "match": "exact"}])
    save_warrant(paths, doc, key)
    assert "PROJECT*" in warranted_terms(paths, key.public_key(), match="exact")
    assert "PROJECT*" not in warranted_terms(paths, key.public_key(), match="pattern")


@pytest.mark.parametrize("before,after", [("exact", "pattern"), ("pattern", "exact")])
def test_replacement_document_marks_both_mode_changes(paths, before, after):
    from lucid_warrant.ui.document import warrant_blocks, render_html

    old_terms = [{**TERMS[0], "match": before}]
    doc = make(paths, [{**TERMS[0], "match": after}])
    html = render_html(warrant_blocks(doc, labels={}, countries={}, library_line="library", replaces="old-warrant",
                                      old_terms=old_terms))
    assert f"PROJECT* ({before}) → PROJECT* ({after})" in html
    assert "none to terms or places" not in html
