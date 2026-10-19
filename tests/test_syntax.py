import pytest

from lucid_warrant.syntax import SyntaxRejected, compile_query, parse_skeleton

FIELDS = ["content", "title"]


def compile_(skeleton, **literals):
    return compile_query(skeleton, literals, default_field="content", allowed_fields=FIELDS)


def test_renders_explicit_fields_and_parentheses():
    c = compile_("{a} AND ({b} OR {c}) AND NOT {d}", a="A", b="B", c="C", d="D")
    assert c.query == 'content:"A" AND (content:"B" OR content:"C") AND NOT content:"D"'
    assert c.gloss == 'ALL of: "A"; [ANY of: "B"; "C"]; NOT "D"'
    assert c.slots == ("a", "b", "c", "d")


def test_field_prefix_and_proximity_flag():
    c = compile_("title:{a} AND {b}~3", a="Kickoff", b="cedar north")
    assert c.query == 'title:"Kickoff" AND content:"cedar north"~3'
    assert c.expands_matching


@pytest.mark.parametrize(
    "literal, expected",
    [
        ('say "hi"', r'content:"say \"hi\""'),
        ("back\\slash", r'content:"back\\slash"'),
        ("AND", 'content:"AND"'),
        ("field:value", 'content:"field:value"'),
        ("a OR b) OR (c", 'content:"a OR b) OR (c"'),
        ("CEDAR*", 'content:"CEDAR*"'),
    ],
)
def test_literals_are_data_not_syntax(literal, expected):
    assert compile_("{a}", a=literal).query == expected


@pytest.mark.parametrize("literal", ["", "  ", " padded", "line\nbreak", "tab\there", "x" * 201])
def test_bad_literals_rejected(literal):
    with pytest.raises(SyntaxRejected):
        compile_("{a}", a=literal)


@pytest.mark.parametrize(
    "skeleton, reason",
    [
        ("cedar", "unsupported"),
        ("{a} and {b}", "unsupported"),
        ("{a} {b}", "missing AND/OR"),
        ("{a} AND {b} OR {c}", "mixing AND and OR"),
        ("{a} OR NOT {b}", "NOT may only"),
        ("NOT {a}", "at least one positive"),
        ("{a} AND (NOT {b} AND NOT {c})", "at least one positive"),
        ("{a}*", "unsupported"),
        ("{a}~", "unsupported"),
        ("{a}^2", "unsupported"),
        ("{a} && {b}", "unsupported"),
        ("+{a}", "unsupported"),
        ("secret:{a}", "not allowed"),
        ("({a} AND {b}", "ends unexpectedly"),
        ("NOT NOT {a}", "double NOT"),
        ("{a} NOT {b}", "missing AND/OR"),
        ("", "empty"),
    ],
)
def test_unsupported_skeletons_rejected(skeleton, reason):
    with pytest.raises(SyntaxRejected, match=reason):
        parse_skeleton(skeleton, FIELDS)


def test_binding_mismatches_rejected():
    with pytest.raises(SyntaxRejected, match="no term bound"):
        compile_("{a} AND {b}", a="A")
    with pytest.raises(SyntaxRejected, match="not in template"):
        compile_("{a}", a="A", b="B")


@pytest.mark.parametrize(
    "skeleton, expected",
    [
        ("{term}", "Finds records that mention [term]."),
        ("{a} AND {b}", "Finds records that mention [a] and [b]."),
        ("{a} OR {b}", "Finds records that mention either [a] or [b]."),
        ("{a} OR {b} OR {c}", "Finds records that mention at least one of [a], [b], or [c]."),
        ("{required} AND ({alt_a} OR {alt_b})", "Finds records that mention [required] and either [alt_a] or [alt_b]."),
        ("{required} AND NOT {excluded}", "Finds records that mention [required], but not [excluded]."),
        ("{a} AND NOT ({b} OR {c})", "Finds records that mention [a], but neither [b] nor [c]."),
        ("{a} AND NOT {b} AND NOT {c} AND NOT {d}", "Finds records that mention [a], but none of [b], [c], or [d]."),
        ("({a} AND {b}) OR {c}", "Finds records that mention either both [a] and [b] or [c]."),
        ("({a} OR {b}) AND ({c} OR {d})",
         "Finds records that mention either [a] or [b], and also either [c] or [d]."),
        ("title:{a} AND {b}~3", "Finds records that mention [a] in the title and [b] (its words within 3 positions of each other)."),
    ],
)
def test_plain_english(skeleton, expected):
    from lucid_warrant.syntax import explain
    assert explain(parse_skeleton(skeleton, FIELDS), None, "content") == expected


def test_plain_english_with_terms():
    from lucid_warrant.syntax import explain
    node = parse_skeleton("({p} OR {s}) AND NOT {n}", FIELDS)
    assert explain(node, {"p": "PROJECT_CEDAR", "s": "SUPPLIER_NORTH", "n": "1234<term>"}, "content") == (
        'Finds records that mention either "PROJECT_CEDAR" or "SUPPLIER_NORTH", but not "1234<term>".'
    )
