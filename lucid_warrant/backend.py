"""Search backends: where the executor sends a search once every warrant check has passed.

Chosen by the signed organization configuration's search_backend section (docs/SEARCH-ADAPTER.md):
- mock: the practice records, evaluated in memory;
- http: any REST search API, described entirely in configuration (URL, request template, how to read results);
- python: an adapter class you write, for anything the http type can't express.

A backend receives the payload the executor built (warrant ID and signed purpose, parameter 1 and 2, the final
Lucene query with the country and date limits already in it, the record limit, and the same date and country
filters as structured fields) plus the parsed plan, which only the mock uses. It returns the results and the ID
the search service gave the request, so runs can be matched to the service's own logs.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .syntax import Group, Node, Not, Term


@dataclass(frozen=True)
class Plan:
    node: Node
    literals: dict[str, str]
    default_field: str
    patterns: frozenset = frozenset()  # literals approved as patterns (PROJECT*): wildcards, not exact text


@dataclass(frozen=True)
class SearchResponse:
    results: list[dict]
    service_query_id: str | None = None  # the service's own tracking ID for this request


class Backend(Protocol):
    def search(self, payload: dict, plan: Plan) -> SearchResponse: ...


class MockBackend:
    """In-memory stand-in for the search service.

    Approximates Lucene: a phrase matches whole words in order, case-insensitively (Turkish İ folded to i);
    proximity is ignored. Visibility is a placeholder rule (a record is visible only if every tag on it is
    granted in parameter 2), not the company's tag policy.
    Date limits come from payload["date_filter"]; when present, records without
    a timestamp are excluded.
    """

    def __init__(self, corpus_path: Path):
        self.records = [_dated(r) for r in json.loads(Path(corpus_path).read_text("utf-8"))["records"]]

    def search(self, payload: dict, plan: Plan) -> SearchResponse:
        if not payload.get("parameter_1") or not payload.get("parameter_2"):
            raise ValueError("mock service: both authorization parameters are required")
        visible_tags = set(payload["parameter_2"])
        hits = []
        for record in self.records:
            if not set(record["tags"]) <= visible_tags:
                continue  # every tag on a record must be granted (a CRM+HR record needs both)
            if not _in_window(payload.get("date_filter"), record):
                continue
            if not _in_countries(payload.get("country_filter"), record):
                continue
            if not _in_places(payload.get("place_filter"), record):
                continue
            if _matches(plan.node, plan.literals, plan.default_field, record, plan.patterns):
                hits.append({
                    "record_id": record["record_id"],
                    "title": record.get("title", ""),
                    "timestamp": record.get("timestamp", ""),
                    "country": record.get("country", ""),
                    "snippet": _snippet(record.get("content", "")),
                    FULL_TEXT: str(record.get("content", "")),
                    **({"lat": record["lat"], "lon": record["lon"]} if "lat" in record and "lon" in record else {}),
                })
        return SearchResponse(hits[: payload["limit"]], f"mock-{uuid.uuid4().hex[:12]}")


def _in_places(place_filter: dict | None, record: dict) -> bool:
    """Practice search for places: a record with coordinates inside any circle (none without coordinates)."""
    if not place_filter:
        return True
    if "lat" not in record or "lon" not in record:
        return False
    from .geo import distance_km

    return any(distance_km(record["lat"], record["lon"], p["lat"], p["lon"]) <= p["radius_km"]
               for p in place_filter["places"])


def _in_countries(country_filter: dict | None, record: dict) -> bool:
    if not country_filter:
        return True
    code = record.get(country_filter["field"])
    if country_filter["include"] and code not in country_filter["include"]:
        return False  # untagged records are out when the search is limited to named countries
    return code not in country_filter["exclude"]


def _in_window(date_filter: dict | None, record: dict) -> bool:
    if not date_filter:
        return True
    try:
        stamp = _instant(record.get(date_filter["field"]))
    except (TypeError, ValueError):
        return False  # untimed records are out whenever a date limit applies
    if date_filter["from"] and stamp < _instant(date_filter["from"]):
        return False
    if date_filter.get("to") and stamp > _instant(date_filter["to"]):
        return False
    return not any(_instant(r["from"]) <= stamp < _instant(r["to"]) for r in date_filter["exclude"])  # half-open


def _dated(record: dict) -> dict:
    """Practice records may give days_ago (and time, HH:MM UTC) instead of a timestamp, so they stay recent."""
    if "timestamp" in record or not isinstance(record.get("days_ago"), int):
        return record
    from datetime import datetime, timedelta, timezone

    hour, minute = (int(x) for x in str(record.get("time", "12:00")).split(":"))
    day = datetime.now(timezone.utc).date() - timedelta(days=record["days_ago"])
    stamp = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)
    return {**record, "timestamp": stamp.strftime("%Y-%m-%dT%H:%M:%SZ")}


SNIPPET_LENGTH = 400
# The complete text of a returned record, attached by backends only so hit terms can be counted in all of it.
# The executor removes it before results are saved or handed back (they carry the shortened snippet).
FULL_TEXT = "_full_text"


def _snippet(text: str) -> str:
    return text if len(text) <= SNIPPET_LENGTH else text[:SNIPPET_LENGTH].rsplit(" ", 1)[0] + " …"


def _fold(text: str) -> str:
    """Case-insensitive text, with Turkish dotted capital İ folded to i (Python lowercases it to i + U+0307)."""
    return str(text).casefold().replace("i\u0307", "i")


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", _fold(text))


def _instant(text: str):
    """Parse an ISO-8601 time of any precision (compares fractional seconds correctly)."""
    from datetime import datetime, timezone

    value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _matches(node: Node, literals: dict, default_field: str, record: dict, patterns=frozenset()) -> bool:
    if isinstance(node, Term) and literals[node.slot] in patterns:
        # A pattern matches a single word (like Lucene's wildcard query on an analyzed field), case-insensitively.
        from .syntax import pattern_matches

        return any(pattern_matches(literals[node.slot], word) for word in _tokens(record.get(node.field or default_field, "")))
    if isinstance(node, Term):
        # A phrase matches whole words in order, like Lucene's standard analyzer: "Anatolian Steel" matches
        # "Caspian Anatolian Steel LLC" but not "Anatolian Steelworks".
        haystack, phrase = _tokens(record.get(node.field or default_field, "")), _tokens(literals[node.slot])
        return bool(phrase) and any(haystack[i:i + len(phrase)] == phrase for i in range(len(haystack) - len(phrase) + 1))
    if isinstance(node, Not):
        return not _matches(node.child, literals, default_field, record, patterns)
    assert isinstance(node, Group)
    results = (_matches(c, literals, default_field, record, patterns) for c in node.children)
    return all(results) if node.op == "AND" else any(results)


# --- configured backends -------------------------------------------------------------------

class BackendError(RuntimeError):
    """The search service couldn't be reached or answered with something unusable."""


class BackendNotReady(BackendError):
    """The search can't be sent at all (for example a secret isn't set). Nothing reached the service."""


def _get_path(value, path: str):
    """Follow a dotted path (hits.hits, _source.title, results.0.id) into parsed JSON; None if it isn't there."""
    if not path:
        return value
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def fill_template(template, values: dict):
    """Placeholders like "{query}" in a request template. A string that is exactly one placeholder takes the value
    with its JSON type (a number, a list); placeholders inside longer text are inserted as text."""
    from .org_config import _PLACEHOLDER

    if isinstance(template, dict):
        return {k: fill_template(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [fill_template(v, values) for v in template]
    if isinstance(template, str):
        whole = _PLACEHOLDER.fullmatch(template)
        if whole:
            return values[whole.group(1)]
        return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), template)
    return template


def template_values(payload: dict) -> dict:
    return {"query": payload["query"], "limit": payload["limit"], "warrant_id": payload.get("warrant_id", ""),
            "purpose": payload.get("purpose", ""), "parameter_1": payload["parameter_1"],
            "parameter_2": list(payload["parameter_2"]), "parameter_2_csv": ",".join(payload["parameter_2"]),
            "date_filter": payload.get("date_filter"), "country_filter": payload.get("country_filter"),
            "place_filter": payload.get("place_filter")}


def map_results(document, spec: dict) -> list[dict]:
    """Turn the service's response into Lucid Warrant's result records, using the configured paths."""
    rows = _get_path(document, spec.get("results_path", ""))
    if not isinstance(rows, list):
        raise BackendError(f"no list of results at {spec.get('results_path') or 'the top level'} in the response")
    fields = spec["fields"]
    results = []
    for row in rows:
        record = {}
        for name, path in fields.items():
            value = _get_path(row, path)
            if value is not None:
                if name == "snippet":
                    record[FULL_TEXT] = str(value)
                    value = _snippet(str(value))
                record[name] = value
        if "record_id" not in record:
            raise BackendError(f"a result has no record ID at {fields['record_id']}")
        results.append(record)
    return results


class HttpBackend:
    """A REST search API described in configuration. Secrets come from environment variables named there."""

    def __init__(self, spec: dict):
        self.spec = spec

    def check_ready(self) -> None:
        """Before anything is reserved or sent: every secret the request needs is available."""
        import os

        missing = self.missing_secrets()
        if missing:
            raise BackendNotReady(f"environment variable {missing[0]} isn't set (the search service's credentials)")
        for header, env in self.spec["secret_headers"].items():
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in os.environ[env]):
                # Python would reject the header and quote the whole value in its error; name the variable instead.
                raise BackendNotReady(f"environment variable {env} (for the {header} header) contains a line break or "
                                      "other control character; set it again without one")

    def prepare(self, payload: dict) -> None:
        """Build the request now (filling every placeholder and header) so any problem is a refusal before quota
        or a one-time approval is used."""
        self.check_ready()
        self.request(payload)

    def scrub(self, text: str) -> str:
        """Remove credential values (and their escaped forms) from text before it's printed or saved."""
        import os

        for env in self.spec["secret_headers"].values():
            secret = os.environ.get(env)
            if secret and len(secret) >= 4:
                for form in {secret, secret.strip(), repr(secret)[1:-1], repr(secret.encode("utf-8"))[2:-1]}:
                    if form:
                        text = text.replace(form, f"[{env}]")
        return text

    def missing_secrets(self) -> list[str]:
        import os

        return [env for env in self.spec["secret_headers"].values() if not os.environ.get(env)]

    def request(self, payload: dict):
        """(url, method, headers, body bytes or None): exactly what will be sent, for checking and for sending."""
        import os
        from urllib.parse import urlencode

        values = template_values(payload)
        spec = self.spec
        from urllib.parse import urlsplit, urlunsplit

        url = fill_template(spec["url"], values)
        parts = urlsplit(url)
        if parts.fragment:
            raise BackendError("the service URL has a # fragment, which is never sent; remove it from the configuration")
        params = fill_template(spec["params"], values)
        if params:
            # Into the URL's query component itself (never appended as text after something else).
            extra = urlencode({k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()})
            url = urlunsplit(parts._replace(query=f"{parts.query}&{extra}" if parts.query else extra))
        headers = {k: str(fill_template(v, values)) for k, v in spec["headers"].items()}
        for header, env in spec["secret_headers"].items():
            secret = os.environ.get(env)
            if not secret:
                raise BackendNotReady(f"environment variable {env} (for the {header} header) isn't set")
            headers[header] = secret
        body = None
        if spec["method"] == "POST":
            body = json.dumps(fill_template(spec["body"], values)).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        return url, spec["method"], headers, body

    def search(self, payload: dict, plan: Plan) -> SearchResponse:
        import ssl
        import urllib.error
        import urllib.request

        url, method, headers, body = self.request(payload)
        secret = set(self.spec["secret_headers"])
        self.last_request = {"method": method, "url": url,
                             "headers": {k: (f"[from {self.spec['secret_headers'][k]}]" if k in secret else v)
                                         for k, v in headers.items()},
                             "body": json.loads(body) if body else None}
        context = None if self.spec["verify_tls"] else ssl._create_unverified_context()  # noqa: S323 - explicit opt-out
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method=method),
                                        timeout=self.spec["timeout_seconds"], context=context) as response:
                raw, response_headers = response.read(), response.headers
        except urllib.error.HTTPError as exc:
            raise BackendError(f"the search service answered {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise BackendError(f"couldn't reach the search service: {exc}") from exc
        except ValueError as exc:  # e.g. a header value Python refuses; its message can quote the value
            raise BackendError(self.scrub(f"couldn't build the request: {exc}")) from None
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackendError("the search service didn't answer with JSON") from exc
        return SearchResponse(map_results(document, self.spec), self._service_id(document, response_headers))

    def _service_id(self, document, headers) -> str | None:
        rule = self.spec["service_query_id"]
        if rule.get("header"):
            return headers.get(rule["header"])
        if rule.get("path"):
            value = _get_path(document, rule["path"])
            return None if value is None else str(value)
        return None


def load_python_backend(spec: dict):
    """An adapter class named in configuration ("module.path:ClassName"), created with its options."""
    import importlib

    module_name, _, class_name = spec["adapter"].partition(":")
    try:
        cls = getattr(importlib.import_module(module_name), class_name)
    except (ImportError, AttributeError) as exc:
        raise BackendError(f"can't load the search adapter {spec['adapter']}: {exc}") from exc
    adapter = cls(**spec["options"]) if spec["options"] else cls()
    if not callable(getattr(adapter, "search", None)):
        raise BackendError(f"{spec['adapter']} has no search(payload, plan) method")
    return adapter


def backend_from_settings(settings: dict, corpus_path: Path):
    """The backend the signed organization configuration chooses."""
    spec = settings["search_backend"]
    if spec["type"] == "http":
        return HttpBackend(spec["http"])
    if spec["type"] == "python":
        return load_python_backend(spec["python"])
    return MockBackend(corpus_path)
