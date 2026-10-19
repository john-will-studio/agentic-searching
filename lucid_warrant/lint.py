"""Deterministic lint for queries: run at nomination, approval and firing.

Two layers:
1. Structure: the skeleton parses under the restricted syntax, every slot is
   bound, every literal is valid, fields are allowed (errors), plus advisory
   warnings (proximity, contradictions, redundancy, duplicate templates).
2. Output: the rendered Lucene string is re-read with an independent tokenizer.
   Every quoted phrase must decode to an approved literal, ranges may only
   target the date field, parentheses balance and operators alternate with
   operands. This checks the string actually sent, not just the template.

Errors block (nomination is refused, the executor won't fire). Warnings are
shown to the human but don't block. No agent judgment is involved.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

from .syntax import (
    Group,
    Not,
    SyntaxRejected,
    Term,
    parse_skeleton,
    render,
    slots,
    uses_proximity,
    validate_literal,
    validate_pattern,
)
from .util import to_iso, utc_now

LINTER = "lucid-warrant-lint/1"
MAX_QUERY_LENGTH = 8000


@dataclass(frozen=True)
class Finding:
    code: str
    level: str  # "error" or "warning"
    message: str


@dataclass
class LintReport:
    findings: list[Finding] = field(default_factory=list)
    stage: str = ""
    query_sha256: str | None = None

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.errors:
            return f"FAILED ({len(self.errors)} error{'s' if len(self.errors) != 1 else ''})"
        if self.warnings:
            return f"passed with {len(self.warnings)} warning{'s' if len(self.warnings) != 1 else ''}"
        return "passed"

    def to_dict(self) -> dict:
        return {
            "linter": LINTER,
            "stage": self.stage,
            "checked_at": to_iso(utc_now()),
            "passed": self.passed,
            "summary": self.summary(),
            "query_sha256": self.query_sha256,
            "findings": [asdict(f) for f in self.findings],
        }

    def add(self, code: str, level: str, message: str) -> None:
        self.findings.append(Finding(code, level, message))


# --- structure ----------------------------------------------------------------

def _shape(node, names: dict) -> str:
    """Skeleton with slots renamed in order of appearance, to spot duplicate templates."""
    if isinstance(node, Term):
        names.setdefault(node.slot, f"s{len(names)}")
        return f"{node.field or ''}:{{{names[node.slot]}}}{'~' + str(node.slop) if node.slop is not None else ''}"
    if isinstance(node, Not):
        return "NOT " + _shape(node.child, names)
    return "(" + f" {node.op} ".join(_shape(c, names) for c in node.children) + ")"


def _polarity(node, negated=False, out=None) -> list[tuple[str, bool]]:
    out = [] if out is None else out
    if isinstance(node, Term):
        out.append((node.slot, negated))
    elif isinstance(node, Not):
        _polarity(node.child, not negated, out)
    else:
        for child in node.children:
            _polarity(child, negated, out)
    return out


def lint(
    skeleton: str,
    literals: dict[str, str] | None,
    *,
    default_field: str,
    allowed_fields,
    library: dict | None = None,
    stage: str = "",
    patterns=frozenset(),
) -> LintReport:
    """Lint a skeleton and, if literals are given, the query it renders to."""
    report = LintReport(stage=stage)
    try:
        node = parse_skeleton(skeleton, allowed_fields)
    except SyntaxRejected as exc:
        report.add("E001", "error", f"syntax: {exc}")
        return report

    needed = slots(node)
    if uses_proximity(node):
        report.add("W101", "warning", "uses proximity (~N): matches more than the exact phrase")
    if library:
        shape = _shape(node, {})
        for t in library.get("templates", []):
            try:
                if _shape(parse_skeleton(t["skeleton"]), {}) == shape:
                    report.add("W104", "warning", f"same structure as library template {t['name']!r} ({t['template_id']})")
            except SyntaxRejected:
                continue

    if literals is None:
        return report

    missing = [s for s in needed if s not in literals]
    extra = sorted(set(literals) - set(needed))
    if missing:
        report.add("E005", "error", f"no term bound to slot(s): {', '.join(missing)}")
    if extra:
        report.add("E005", "error", f"bound slot(s) not in template: {', '.join(extra)}")
    for slot, literal in literals.items():
        try:
            validate_literal(literal)
        except SyntaxRejected as exc:
            report.add("E007", "error", f"slot {slot}: {exc}")
    if report.errors:
        return report

    values = [literals[s] for s in needed]
    if len(set(values)) < len(values):
        report.add("W102", "warning", "the same term fills more than one slot")
    included = {literals[s] for s, neg in _polarity(node) if not neg}
    excluded = {literals[s] for s, neg in _polarity(node) if neg}
    for literal in sorted(included & excluded):
        report.add("W103", "warning", f'"{literal}" is both required and excluded; that part can never match')
    for literal in sorted(set(values) - set(patterns)):
        if re.search(r'[*?~^\[\]{}]', literal):
            report.add("W107", "warning", f'"{literal}" contains wildcard-like characters; they are matched literally, not as wildcards')

    try:
        query = render(node, literals, default_field, patterns=patterns)
    except SyntaxRejected as exc:
        report.add("E007", "error", str(exc))
        return report
    report.findings += lint_query(query, set(literals.values()), allowed_fields=allowed_fields,
                                  patterns=patterns).findings
    report.query_sha256 = hashlib.sha256(query.encode()).hexdigest()
    return report


# --- output -------------------------------------------------------------------

_PHRASE = r'"((?:[^"\\]|\\.)*)"'
_OUT_TOKEN = re.compile(
    rf"""
      (?P<ws>\s+)
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<op>AND|OR|NOT)(?=[\s(]|$)
    | (?P<range>(?P<rfield>[A-Za-z_][A-Za-z0-9_.]*):\[{_PHRASE}\ TO\ (?:\*|{_PHRASE})[\]}}])
    | (?P<phrase>(?P<pfield>[A-Za-z_][A-Za-z0-9_.]*):{_PHRASE}(?:~(?P<slop>[0-9]{{1,2}}))?)
    """,
    re.VERBOSE,
)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# field:PATTERN, unquoted, with at least one wildcard (escaped operators allowed): see syntax.validate_pattern.
_PATTERN_TOKEN = re.compile(r"(?P<field>[A-Za-z_][A-Za-z0-9_.]*):(?P<pat>(?:\\[+\-&/]|[A-Za-z0-9_.@#*?])*[*?]"
                            r"(?:\\[+\-&/]|[A-Za-z0-9_.@#*?])*)(?=[\s)]|$)")


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def lint_query(query: str, approved: set[str], *, allowed_fields, date_field: str | None = None,
               country_field: str | None = None, country_codes: set[str] | None = None,
               place_clauses: list[str] | None = None, patterns=frozenset(), stage: str = "") -> LintReport:
    """Re-read a final Lucene string independently of the renderer."""
    report = LintReport(stage=stage, query_sha256=hashlib.sha256(query.encode()).hexdigest())
    if len(query) > MAX_QUERY_LENGTH:
        report.add("E013", "error", f"query is longer than {MAX_QUERY_LENGTH} characters")
    pos, depth, expect_operand, positives = 0, 0, True, 0
    places = sorted(place_clauses or [], key=len, reverse=True)
    while pos < len(query):
        # A place clause added by the executor from the signed warrant: accepted only exactly as built.
        place = next((c for c in places if query.startswith(c, pos)), None)
        if place is not None:
            if not expect_operand:
                report.add("E014", "error", f"missing operator before place clause at position {pos}")
            pos += len(place)
            expect_operand = False
            continue
        # A pattern term: unquoted, so only exactly a pattern the warrant approved as one is accepted.
        p = _PATTERN_TOKEN.match(query, pos)
        if p and not query.startswith('"', p.start("pat")):
            pattern = re.sub(r"\\(.)", r"\1", p.group("pat"))
            if not expect_operand:
                report.add("E014", "error", f"missing operator before term at position {pos}")
            if p.group("field") not in allowed_fields:
                report.add("E011", "error", f"field {p.group('field')!r} is not an allowed search field")
            if pattern not in patterns or pattern not in approved:
                report.add("E010", "error", f"{p.group(0)!r} is not a pattern this warrant approves")
            try:
                validate_pattern(pattern)
            except SyntaxRejected as exc:
                report.add("E010", "error", str(exc))
            positives += 1
            expect_operand = False
            pos = p.end()
            continue
        m = _OUT_TOKEN.match(query, pos)
        if m is None:
            report.add("E008", "error", f"unrecognized text in rendered query at position {pos}: {query[pos:pos + 20]!r}")
            return report
        pos = m.end()
        if m.group("ws"):
            continue
        if m.group("lparen"):
            if not expect_operand:
                report.add("E014", "error", f"missing operator before '(' at position {m.start()}")
            depth += 1
        elif m.group("rparen"):
            if expect_operand:
                report.add("E014", "error", f"empty group or dangling operator before ')' at position {m.start()}")
            depth -= 1
            if depth < 0:
                report.add("E009", "error", "unbalanced parentheses")
                return report
        elif m.group("op"):
            op = m.group("op")
            if op == "NOT":
                if not expect_operand:
                    report.add("E014", "error", f"NOT must follow AND (position {m.start()})")
            elif expect_operand:
                report.add("E014", "error", f"operator {op} without a term before it (position {m.start()})")
            expect_operand = True
        elif m.group("range"):
            if not expect_operand:
                report.add("E014", "error", f"missing operator before range at position {m.start()}")
            field_name = m.group("rfield")
            if field_name != date_field:
                report.add("E012", "error", f"range on {field_name!r}; only the date field {date_field!r} may use ranges")
            bounds = re.findall(_PHRASE, m.group("range"))
            if not all(_ISO.match(_unescape(b)) for b in bounds):
                report.add("E012", "error", f"date range bounds must be ISO-8601 UTC timestamps: {m.group('range')}")
            expect_operand = False
        elif m.group("phrase"):
            if not expect_operand:
                report.add("E014", "error", f"missing operator before term at position {m.start()}")
            field_name = m.group("pfield")
            literal = _unescape(m.group(m.re.groupindex["pfield"] + 1))
            if country_field and field_name == country_field:
                # Country scope added by the executor: only the warrant's own country codes.
                if literal not in (country_codes or set()):
                    report.add("E016", "error", f"country {literal!r} isn't part of the warrant's country scope")
            else:
                if field_name not in allowed_fields:
                    report.add("E011", "error", f"field {field_name!r} is not an allowed search field")
                if literal in patterns:
                    report.add("E010", "error", f"pattern {literal!r} must be used unquoted, without proximity")
                if literal not in approved:
                    report.add("E010", "error", f'phrase "{literal}" is not one of the approved terms')
                positives += 1
            expect_operand = False
    if depth != 0:
        report.add("E009", "error", "unbalanced parentheses")
    if expect_operand and query.strip():
        report.add("E014", "error", "query ends with an operator")
    if positives == 0:
        report.add("E015", "error", "query contains no search terms")
    return report
