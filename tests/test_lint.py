import pytest

from lucid_warrant.lint import lint, lint_query
from lucid_warrant.syntax import parse_skeleton, suggest_name

FIELDS = ["content", "title"]


def codes(report):
    return sorted(f.code for f in report.findings)


def run(skeleton, literals=None, **kw):
    return lint(skeleton, literals, default_field="content", allowed_fields=FIELDS, **kw)


def test_clean_query_passes():
    r = run("{a} AND ({b} OR {c})", {"a": "A", "b": "B", "c": "C"})
    assert r.passed and r.findings == [] and r.summary() == "passed" and len(r.query_sha256) == 64


@pytest.mark.parametrize("skeleton, literals, code", [
    ("{a}*", {"a": "A"}, "E001"),
    ("secret:{a}", {"a": "A"}, "E001"),
    ("{a} AND {b}", {"a": "A"}, "E005"),
    ("{a}", {"a": "A", "b": "B"}, "E005"),
    ("{a}", {"a": "two\nlines"}, "E007"),
])
def test_errors(skeleton, literals, code):
    r = run(skeleton, literals)
    assert not r.passed and code in codes(r)


def test_warnings_do_not_block():
    r = run("{a} AND NOT {b} AND {c}~2", {"a": "X", "b": "X", "c": "WILD*"})
    assert r.passed
    assert codes(r) == ["W101", "W102", "W103", "W107"]


def test_duplicate_of_library_template():
    library = {"templates": [{"template_id": "all-of-two", "name": "Both terms", "skeleton": "{a} AND {b}"}]}
    r = run("{project} AND {supplier}", library=library)
    assert codes(r) == ["W104"]


@pytest.mark.parametrize("query, approved, code", [
    ('content:"A" AND content:"B"', {"A", "B"}, None),
    ('content:"A" AND content:"EVIL"', {"A"}, "E010"),
    ('content:"A" AND content:A*', {"A"}, "E010"),  # an unquoted pattern the warrant didn't approve
    ('(content:"A"', {"A"}, "E009"),
    ('content:"A" AND AND content:"B"', {"A", "B"}, "E014"),
    ('content:"A" content:"B"', {"A", "B"}, "E014"),
    ('secret:"A"', {"A"}, "E011"),
    ('content:"A" AND content:["x" TO *]', {"A"}, "E012"),
    ('content:"A" AND timestamp:["2025-01-01T00:00:00Z" TO *]', {"A"}, None),
    ('(content:"A") AND NOT timestamp:["2025-01-01T00:00:00Z" TO "2025-03-31T23:59:59Z"]', {"A"}, None),
    ('(content:"A") AND NOT timestamp:["2025-01-01T00:00:00Z" TO "2025-04-01T00:00:00Z"}', {"A"}, None),
    ('content:"say \\"hi\\""', {'say "hi"'}, None),
    ('content:"A" AND', {"A"}, "E014"),
])
def test_output_reader(query, approved, code):
    r = lint_query(query, approved, allowed_fields=FIELDS, date_field="timestamp")
    assert (codes(r) == [] if code is None else code in codes(r)), codes(r)


@pytest.mark.parametrize("skeleton, name", [
    ("{a}", "Term"),
    ("{a} AND {b}", "Both terms"),
    ("{a} OR {b}", "Either of two terms"),
    ("{a} OR {b} OR {c}", "Any of three terms"),
    ("({p} OR {s}) AND NOT {n}", "Either of two terms, excluding one term"),
    ("{r} AND ({a} OR {b})", "Term plus either of two terms"),
    ("{a} AND NOT ({b} OR {c})", "Term, excluding two terms"),
    ("title:{a} AND {b}", "Title term plus term"),
])
def test_suggested_names(skeleton, name):
    assert suggest_name(parse_skeleton(skeleton, FIELDS)) == name
