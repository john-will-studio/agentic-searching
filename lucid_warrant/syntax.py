"""Restricted template syntax that renders to Lucene classic query strings.

A template ("skeleton") contains placeholders, never literal terms:

    {project} AND ({supplier} OR {alias}) AND NOT {noise}

Supported: `{slot}`, `field:{slot}`, `{slot}~N` (phrase proximity, flagged as
expanding matching), AND, OR, NOT, and parentheses. Everything else (bare
words, wildcards, fuzzy, ranges, regex, boosts, +/-, &&/||) is rejected.

Rules that keep the rendered query reading the way it looks:
- operators must be explicit (no reliance on the parser's default operator);
- AND and OR can't be mixed in one group without parentheses;
- NOT only appears inside an AND group (`{a} AND NOT {b}`);
- every group must require at least one positive term, so no query or
  sub-query consists only of exclusions.

Approved literals are inserted as quoted, escaped phrases with an explicit
field. Whether a quoted phrase matches literally depends on the index's
analyzer; this module does not and cannot promise that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_SKELETON_LENGTH = 500
MAX_TERMS = 20
MAX_LITERAL_LENGTH = 200

_HELP = (
    "Templates may contain only {slot} placeholders (optionally field:{slot} or "
    "{slot}~N), the operators AND, OR, NOT, and parentheses."
)


class SyntaxRejected(ValueError):
    """A skeleton or literal falls outside the supported, reviewable syntax."""


@dataclass(frozen=True)
class Term:
    slot: str
    field: str | None = None
    slop: int | None = None


@dataclass(frozen=True)
class Not:
    child: "Node"


@dataclass(frozen=True)
class Group:
    op: str  # "AND" or "OR"
    children: tuple


Node = Term | Not | Group


@dataclass(frozen=True)
class Compiled:
    node: Node
    query: str
    gloss: str
    plain_english: str
    slots: tuple[str, ...]
    expands_matching: bool


_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<op>AND|OR|NOT)(?![A-Za-z0-9_{])
    | (?P<term>
          (?:(?P<field>[A-Za-z_][A-Za-z0-9_]*):)?
          \{(?P<slot>[a-z][a-z0-9_]*)\}
          (?:~(?P<slop>[0-9]{1,2}))?
      )(?![A-Za-z0-9_{}~*?^"\\:])
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    value: str
    pos: int
    term: Term | None = None


def _tokenize(text: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if m is None:
            raise SyntaxRejected(f"unsupported syntax at position {pos}: {text[pos:pos + 20]!r}. {_HELP}")
        if m.group("lparen"):
            tokens.append(_Tok("(", "(", pos))
        elif m.group("rparen"):
            tokens.append(_Tok(")", ")", pos))
        elif m.group("op"):
            tokens.append(_Tok("op", m.group("op"), pos))
        elif m.group("term"):
            slop = m.group("slop")
            term = Term(m.group("slot"), m.group("field"), int(slop) if slop else None)
            tokens.append(_Tok("term", m.group("term"), pos, term))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Tok]):
        self.tokens = tokens
        self.i = 0

    def peek(self) -> _Tok | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> _Tok:
        tok = self.peek()
        if tok is None:
            raise SyntaxRejected("expression ends unexpectedly (missing term or ')'?)")
        self.i += 1
        return tok

    def parse(self) -> Node:
        if not self.tokens:
            raise SyntaxRejected("template is empty")
        node = self.group()
        tok = self.peek()
        if tok is not None:
            raise SyntaxRejected(f"unexpected {tok.value!r} at position {tok.pos}")
        return node

    def group(self) -> Node:
        items = [self.operand()]
        op = None
        while True:
            tok = self.peek()
            if tok is None or tok.kind == ")":
                break
            if tok.kind != "op" or tok.value == "NOT":
                raise SyntaxRejected(
                    f"missing AND/OR before {tok.value!r} at position {tok.pos}; operators must be "
                    "explicit because the parser's default operator is configurable"
                )
            self.take()
            if op is None:
                op = tok.value
            elif tok.value != op:
                raise SyntaxRejected(
                    f"mixing AND and OR at position {tok.pos}; add parentheses so the grouping is explicit"
                )
            items.append(self.operand())
        if op is None:
            return items[0]
        if op == "OR" and any(isinstance(item, Not) for item in items):
            raise SyntaxRejected("NOT may only be used inside an AND group, e.g. {a} AND NOT {b}")
        return Group(op, tuple(items))

    def operand(self) -> Node:
        tok = self.take()
        if tok.kind == "op" and tok.value == "NOT":
            child = self.operand()
            if isinstance(child, Not):
                raise SyntaxRejected("double NOT is not supported")
            return Not(child)
        if tok.kind == "(":
            node = self.group()
            close = self.take()
            if close.kind != ")":
                raise SyntaxRejected(f"expected ')' at position {close.pos}")
            return node
        if tok.kind == "term":
            return tok.term
        raise SyntaxRejected(f"unexpected {tok.value!r} at position {tok.pos}")


def requires_positive_term(node: Node) -> bool:
    if isinstance(node, Term):
        return True
    if isinstance(node, Not):
        return False
    if node.op == "AND":
        return any(requires_positive_term(c) for c in node.children if not isinstance(c, Not))
    return all(requires_positive_term(c) for c in node.children)


def _check_positive_everywhere(node: Node) -> None:
    if isinstance(node, Not):
        _check_positive_everywhere(node.child)
    elif isinstance(node, Group):
        if not requires_positive_term(node):
            raise SyntaxRejected("a group must require at least one positive term; exclusions alone match too much")
        for child in node.children:
            _check_positive_everywhere(child)


def _terms(node: Node):
    if isinstance(node, Term):
        yield node
    elif isinstance(node, Not):
        yield from _terms(node.child)
    else:
        for child in node.children:
            yield from _terms(child)


def slots(node: Node) -> tuple[str, ...]:
    return tuple(dict.fromkeys(t.slot for t in _terms(node)))


def fields(node: Node) -> set[str]:
    return {t.field for t in _terms(node) if t.field}


def uses_proximity(node: Node) -> bool:
    return any(t.slop is not None for t in _terms(node))


def parse_skeleton(skeleton: str, allowed_fields=None) -> Node:
    if not isinstance(skeleton, str) or not skeleton.strip():
        raise SyntaxRejected("template is empty")
    if len(skeleton) > MAX_SKELETON_LENGTH:
        raise SyntaxRejected(f"template is longer than {MAX_SKELETON_LENGTH} characters")
    node = _Parser(_tokenize(skeleton)).parse()
    if not requires_positive_term(node):
        raise SyntaxRejected("query must require at least one positive term; exclusions alone match too much")
    _check_positive_everywhere(node)
    if sum(1 for _ in _terms(node)) > MAX_TERMS:
        raise SyntaxRejected(f"template has more than {MAX_TERMS} terms")
    if allowed_fields is not None:
        bad = sorted(fields(node) - set(allowed_fields))
        if bad:
            raise SyntaxRejected(f"field(s) not allowed by the library: {', '.join(bad)}")
    return node


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def validate_literal(literal: str) -> str:
    if not isinstance(literal, str) or not literal.strip():
        raise SyntaxRejected("term is empty")
    if literal != literal.strip():
        raise SyntaxRejected(f"term {literal!r} has leading or trailing whitespace")
    if len(literal) > MAX_LITERAL_LENGTH:
        raise SyntaxRejected(f"term is longer than {MAX_LITERAL_LENGTH} characters")
    if _CONTROL_RE.search(literal):
        raise SyntaxRejected(f"term {literal!r} contains control characters or line breaks")
    return literal


# Pattern hit terms: a word with wildcards (* any run of characters, ? exactly one), approved as a pattern. It
# always starts with at least three ordinary characters, so a pattern extends real text (PROJECT*) and can never
# mean "everything" (*, *X or P*). One word: no spaces, quotes or Lucene operators.
PATTERN_MIN_PREFIX = 3
MAX_PATTERN_LENGTH = 100
_PATTERN_RE = re.compile(r"^[A-Za-z0-9_.@#&+/\-]{%d,}[*?][A-Za-z0-9_.@#&+/\-*?]*$" % PATTERN_MIN_PREFIX)
_PATTERN_SPECIALS = "+-&/"  # Lucene operators allowed in a pattern's text, escaped when rendered


def validate_pattern(literal: str) -> str:
    validate_literal(literal)
    if len(literal) > MAX_PATTERN_LENGTH:
        raise SyntaxRejected(f"a pattern is at most {MAX_PATTERN_LENGTH} characters")
    if not _PATTERN_RE.match(literal):
        raise SyntaxRejected(
            f"pattern {literal!r} must be one word that starts with at least {PATTERN_MIN_PREFIX} letters or digits, "
            "then * (any characters) or ? (one character), e.g. PROJECT* or SUPPLIER_N?RTH; a wildcard alone or at "
            "the start isn't allowed")
    return literal


def escape_pattern(literal: str) -> str:
    """A pattern as Lucene classic syntax: unquoted (so * and ? are wildcards), other operators escaped."""
    validate_pattern(literal)
    return "".join("\\" + ch if ch in _PATTERN_SPECIALS else ch for ch in literal)


def pattern_matches(pattern: str, word: str) -> bool:
    """Whether one word matches a pattern (case-insensitive, like an analyzed field)."""
    import fnmatch

    return fnmatch.fnmatchcase(word.casefold(), pattern.casefold())


def escape_phrase(literal: str) -> str:
    """Escape a literal for use inside a Lucene classic double-quoted phrase."""
    validate_literal(literal)
    return literal.replace("\\", "\\\\").replace('"', '\\"')


def render(node: Node, literals: dict[str, str], default_field: str, *, _top: bool = True,
           patterns=frozenset()) -> str:
    """The Lucene query. Exact terms are quoted phrases; terms the warrant approved as patterns (`patterns`, by
    their text) are unquoted, so their wildcards work."""
    if isinstance(node, Term):
        literal = literals[node.slot]
        if literal in patterns:
            if node.slop is not None:
                raise SyntaxRejected(f"pattern {literal!r} can't be used in a proximity (~N) slot")
            return f"{node.field or default_field}:{escape_pattern(literal)}"
        text = f'{node.field or default_field}:"{escape_phrase(literal)}"'
        if node.slop is not None:
            text += f"~{node.slop}"
        return text
    if isinstance(node, Not):
        return "NOT " + render(node.child, literals, default_field, _top=False, patterns=patterns)
    inner = f" {node.op} ".join(render(c, literals, default_field, _top=False, patterns=patterns) for c in node.children)
    return inner if _top else f"({inner})"


def gloss(node: Node, literals: dict[str, str] | None, default_field: str, *, _top: bool = True,
          patterns=frozenset()) -> str:
    """Plain-language description generated from the parsed structure, not by the agent."""
    if isinstance(node, Term):
        if literals and literals[node.slot] in patterns:
            label = f"any word matching {literals[node.slot]}"
        else:
            label = f'"{literals[node.slot]}"' if literals else "{" + node.slot + "}"
        if node.field and node.field != default_field:
            label += f" in {node.field}"
        if node.slop is not None:
            label += f" (words within {node.slop} positions; expands matching)"
        return label
    if isinstance(node, Not):
        return "NOT " + gloss(node.child, literals, default_field, _top=False, patterns=patterns)
    word = "ALL of" if node.op == "AND" else "ANY of"
    text = f"{word}: " + "; ".join(gloss(c, literals, default_field, _top=False, patterns=patterns) for c in node.children)
    return text if _top else f"[{text}]"


def explain(node: Node, literals: dict[str, str] | None, default_field: str, *, patterns=frozenset()) -> str:
    """One plain-English sentence for a lay reader, generated from the parsed structure.

    With literals: 'Finds records that mention "A" and either "B" or "C".'
    Without (a template): slots appear as [slot].
    """
    return f"Finds records that mention {_say(node, literals, default_field, patterns)}."


def _name(term: Term, literals, default_field, patterns=frozenset()) -> str:
    text = f'"{literals[term.slot]}"' if literals else f"[{term.slot}]"
    if literals and literals[term.slot] in patterns:
        text = f"any word matching the pattern {literals[term.slot]}"
    if term.slop is not None:
        text += f" (its words within {term.slop} positions of each other)"
    if term.field and term.field != default_field:
        text += f" in the {term.field}"
    return text


def _list(parts: list[str], conjunction: str) -> str:
    if len(parts) == 1:
        return parts[0]
    if any(" or " in p for p in parts[:-1]):
        # "either A or B and C" is ambiguous; spell the grouping out.
        return f", {conjunction} also ".join(parts) if conjunction == "and" else ", or else ".join(parts)
    return ", ".join(parts[:-1]) + f"{',' if len(parts) > 2 else ''} {conjunction} " + parts[-1]


def _say(node: Node, literals, default_field, patterns=frozenset()) -> str:
    if isinstance(node, Term):
        return _name(node, literals, default_field, patterns)
    if isinstance(node, Not):  # only reachable inside AND; handled there
        return "not " + _say(node.child, literals, default_field, patterns)
    if node.op == "OR":
        parts = []
        for child in node.children:
            text = _say(child, literals, default_field, patterns)
            parts.append(f"both {text}" if isinstance(child, Group) and child.op == "AND" else text)
        return f"either {parts[0]} or {parts[1]}" if len(parts) == 2 else "at least one of " + _list(parts, "or")
    positives = [_say(c, literals, default_field, patterns) for c in node.children if not isinstance(c, Not)]
    excluded = []
    for child in node.children:
        if isinstance(child, Not):
            inner = child.child
            if isinstance(inner, Group) and inner.op == "OR":
                excluded += [_say(c, literals, default_field, patterns) for c in inner.children]
            elif isinstance(inner, Group):
                excluded.append("both " + _say(inner, literals, default_field, patterns))
            else:
                excluded.append(_say(inner, literals, default_field, patterns))
    text = _list(positives, "and")
    if len(excluded) == 1:
        text += f", but not {excluded[0]}"
    elif len(excluded) == 2:
        text += f", but neither {excluded[0]} nor {excluded[1]}"
    elif excluded:
        text += ", but none of " + _list(excluded, "or")
    return text


_COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}


def suggest_name(node: Node) -> str:
    """A short deterministic name from the structure, e.g. "Either of two terms, excluding one term"."""
    text = _name_part(node)
    return text[0].upper() + text[1:]


def _count(n: int) -> str:
    return _COUNT_WORDS.get(n, str(n))


def _name_part(node: Node) -> str:
    if isinstance(node, Term):
        label = "term"
        if node.field:
            label = f"{node.field} term"
        if node.slop is not None:
            label = f"near-phrase {label}"
        return label
    if isinstance(node, Not):
        return _name_part(node.child)
    if node.op == "OR":
        if all(isinstance(c, Term) and c.field is None and c.slop is None for c in node.children):
            n = len(node.children)
            return "either of two terms" if n == 2 else f"any of {_count(n)} terms"
        return "any of: " + " / ".join(_name_part(c) for c in node.children)
    positives = [c for c in node.children if not isinstance(c, Not)]
    exclusions = [c.child for c in node.children if isinstance(c, Not)]
    if all(isinstance(c, Term) and c.field is None and c.slop is None for c in positives):
        n = len(positives)
        main = "term" if n == 1 else "both terms" if n == 2 else f"all of {_count(n)} terms"
    else:
        main = " plus ".join(_name_part(c) for c in positives)
    if exclusions:
        excluded = sum(len(c.children) if isinstance(c, Group) and c.op == "OR" else 1 for c in exclusions)
        main += ", excluding " + ("one term" if excluded == 1 else f"{_count(excluded)} terms")
    return main


def compile_query(skeleton: str, literals: dict[str, str], *, default_field: str, allowed_fields,
                  patterns=frozenset()) -> Compiled:
    node = parse_skeleton(skeleton, allowed_fields)
    needed = slots(node)
    missing = [s for s in needed if s not in literals]
    if missing:
        raise SyntaxRejected(f"no term bound to slot(s): {', '.join(missing)}")
    extra = sorted(set(literals) - set(needed))
    if extra:
        raise SyntaxRejected(f"bound slot(s) not in template: {', '.join(extra)}")
    for literal in literals.values():
        (validate_pattern if literal in patterns else validate_literal)(literal)
    return Compiled(
        node=node,
        query=render(node, literals, default_field, patterns=patterns),
        gloss=gloss(node, literals, default_field, patterns=patterns),
        plain_english=explain(node, literals, default_field, patterns=patterns),
        slots=needed,
        expands_matching=uses_proximity(node),
    )
