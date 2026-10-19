"""Country scope and certifications recorded in a warrant.

Country scope (shown to people as the "geographic constraint"):
- include: countries the search is limited to, each {"code", "reason"} with an optional reason (for example
  "Home country of client CL-100482"); empty means all countries. Warrants signed before reasons existed
  store plain codes; include_codes() reads both;
- exclude: countries never searched, each with a required reason (for example "GDPR").

The executor adds both to every query, outside the agent's content query, and
sends them as a structured `country_filter`. Records tagged with no country
are excluded whenever an include list is set.

Certifications are statements the human ticks before signing. Their exact text
is signed into the warrant, so the record shows what was attested.
"""

from __future__ import annotations

import json
from functools import lru_cache

from .config import Paths

DEFAULT_COUNTRY_FIELD = "country"
MAX_REASON = 60  # fits the exclusion table at the minimum window size


class ScopeError(ValueError):
    """A country scope or certification is invalid."""


@lru_cache(maxsize=4)
def _load_countries(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)["countries"]
    return {r["code"]: r["name"] for r in rows}


def countries(paths: Paths) -> dict[str, str]:
    """Code -> name, from the country reference list."""
    return _load_countries(str(paths.seed / "countries.json"))


def country_label(paths: Paths, code: str) -> str:
    return f"{code} ({countries(paths).get(code, 'unknown code')})"


def normalize_country_scope(paths: Paths, include, exclude) -> dict:
    known = countries(paths)
    if include is not None and not isinstance(include, list) or exclude is not None and not isinstance(exclude, list):
        raise ScopeError("include and exclude must be lists")
    included = []
    for row in include or []:
        if isinstance(row, str):
            row = {"code": row, "reason": ""}
        if not isinstance(row, dict) or set(row) - {"code", "reason"} or not isinstance(row.get("code"), str) \
                or not isinstance(row.get("reason", ""), str):
            raise ScopeError('each included country must be "XX" or {"code": "XX", "reason": "text"}')
        code, reason = row["code"].strip().upper(), " ".join(row.get("reason", "").split())
        if code not in known:
            raise ScopeError(f"unknown country code {code!r}")
        if len(reason) > MAX_REASON:
            raise ScopeError(f"the reason for including {code} is longer than {MAX_REASON} characters")
        if code in {r["code"] for r in included}:
            raise ScopeError(f"{code} is included twice")
        included.append({"code": code, "reason": reason})
    include = sorted(included, key=lambda r: r["code"])
    seen, clean = set(), []
    for row in exclude or []:
        if not isinstance(row, dict) or set(row) - {"code", "reason"} or not isinstance(row.get("code"), str) \
                or not isinstance(row.get("reason"), str):
            raise ScopeError('each exclusion must be {"code": "XX", "reason": "text"}')
        code = row["code"].strip().upper()
        reason = " ".join(row["reason"].split())
        if code not in known:
            raise ScopeError(f"unknown country code {code!r}")
        if not reason:
            raise ScopeError(f"give a reason for excluding {code}")
        if len(reason) > MAX_REASON:
            raise ScopeError(f"the reason for excluding {code} is longer than {MAX_REASON} characters")
        if code in seen:
            raise ScopeError(f"{code} is excluded twice")
        seen.add(code)
        clean.append({"code": code, "reason": reason})
    overlap = sorted({r["code"] for r in include} & seen)
    if overlap:
        raise ScopeError(f"{', '.join(overlap)} can't be both included and excluded")
    return {"include": include, "exclude": sorted(clean, key=lambda r: r["code"])}


def include_codes(scope: dict | None) -> list[str]:
    """Included country codes, from either form ({"code", "reason"} rows, or plain codes in older warrants)."""
    return [r if isinstance(r, str) else r["code"] for r in (scope or {}).get("include") or []]


def include_rows(rows) -> list[dict]:
    """Included countries as {"code", "reason"} rows, from either form."""
    return [{"code": r, "reason": ""} if isinstance(r, str) else {"code": r["code"], "reason": r.get("reason", "")}
            for r in rows or []]


def country_filter(warrant: dict, country_field: str, known=None) -> dict | None:
    """The country filter for one search. With exclusions only, `include` becomes every recognized code except the
    excluded ones (an allow-list), so records whose country is missing, lowercase, three-letter or otherwise
    unrecognized can't slip past an exclusion: they match nothing in the list."""
    scope = warrant.get("country_scope") or {}
    include, exclude = include_codes(scope), [r["code"] for r in scope.get("exclude") or []]
    if not include and not exclude:
        return None
    if include or known is None:
        return {"field": country_field, "include": include, "exclude": exclude}
    allowed = sorted(set(known) - set(exclude))
    return {"field": country_field, "include": allowed, "exclude": exclude, "include_derived": True}


def summarize_country_filter(query: str, country: dict) -> tuple[str, dict]:
    """(query, filter) with a derived permitted-country allow-list named rather than listed in full."""
    if not country.get("include_derived"):
        return query, country
    clause = "(" + " OR ".join(f'{country["field"]}:"{c}"' for c in country["include"]) + ")"
    summary = {"field": country["field"], "excluded": country["exclude"],
               "permitted": f"every recognized two-letter code except the excluded ones ({len(country['include'])} codes)"}
    return query.replace(clause, f"({country['field']}: any permitted code)"), summary


def allowed_countries(country: dict | None) -> set[str] | None:
    """Codes a returned record may carry, or None when there's no constraint."""
    if not country:
        return None
    return set(country["include"]) - set(country["exclude"]) if country["include"] else None


def apply_country_filter(query: str, country: dict | None) -> str:
    if not country:
        return query
    field, clauses = country["field"], []
    if country["include"]:
        clauses.append("(" + " OR ".join(f'{field}:"{c}"' for c in country["include"]) + ")")
    if not country.get("include_derived"):  # an allow-list built from the exclusions already leaves them out
        clauses += [f'NOT {field}:"{c}"' for c in country["exclude"]]
    return f"({query}) AND " + " AND ".join(clauses)


def normalize_certifications(certifications) -> list[dict]:
    clean = []
    for c in certifications or []:
        cid, text = c.get("id"), c.get("text")
        if not isinstance(cid, str) or not cid.strip() or not isinstance(text, str) or not text.strip():
            raise ScopeError("each certification needs an id and its text")
        clean.append({"id": cid.strip(), "text": text})  # verbatim: exactly what the signer saw
    return clean
