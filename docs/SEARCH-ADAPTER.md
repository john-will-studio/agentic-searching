# Connecting Lucid Warrant to your search service

Lucid Warrant sends each search itself, after checking it against a signed warrant: approved terms, template, authorization, countries, dates, record cap and hourly limit. Out of the box it searches practice records (the `mock` backend). This guide connects it to your real search API, so the agent's searches go through those checks instead of straight to the API.

**Who does what.** An agent (or an administrator) can do steps 1–4: find out how the API works, write the configuration and check it. Step 5 signs the change and needs the analyst's passphrase, because where searches go is part of the organization configuration, which is signed. The agent hands the analyst one command to run.

## 1. Find out how your search API works

You need these answers. Look in the code or scripts that already call the API, the API's documentation, or an existing saved response. **Never copy a secret (token, password, key) into a file or into chat.**

| Question | Example answers |
|---|---|
| The URL that runs a search | `https://search.example.com/records/_search` |
| GET or POST | Elasticsearch/OpenSearch: POST. Solr: GET. |
| How the Lucene query is passed | Elasticsearch: `{"query": {"query_string": {"query": "…"}}}`. Solr: `q=…`. |
| How the record limit is passed | `size`, `rows`, `limit` |
| How it authenticates | `Authorization: Bearer …`, `Authorization: ApiKey …`, `X-API-Key: …` |
| Where the list of results is in the response | `hits.hits` (Elasticsearch), `response.docs` (Solr), `results` |
| Each result's ID, title, timestamp, country and text fields | `_id`, `_source.title`, `_source.timestamp`, `_source.country`, `_source.content` |
| The service's own ID for each request | a response header (`X-Request-Id`, `X-Opaque-Id`) or a field (`request_id`) |
| How it applies parameter 1 and 2 (authorization and visibility) | a header, a body field, or the credentials themselves |

Also check that the index's field names match `query_fields` in the configuration (content, date and country). `lucid-warrant config get query_fields` shows them; they must also match the signed template library.

## 2. Write the configuration

The `search_backend` section has three types: `mock`, `http` (a REST API described entirely in configuration) and `python` (an adapter class you write). Save your choice as `backend.json`.

### Elasticsearch / OpenSearch

```json
{
  "type": "http",
  "http": {
    "url": "https://search.example.com/records/_search",
    "method": "POST",
    "secret_headers": {"Authorization": "SEARCH_API_AUTH"},
    "headers": {"X-Opaque-Id": "lucid-warrant {warrant_id}"},
    "body": {
      "query": {"query_string": {"query": "{query}"}},
      "size": "{limit}",
      "_source": ["title", "timestamp", "country", "content"]
    },
    "results_path": "hits.hits",
    "fields": {"record_id": "_id", "title": "_source.title", "timestamp": "_source.timestamp",
               "country": "_source.country", "snippet": "_source.content"},
    "service_query_id": {"header": "X-Opaque-Id"}
  }
}
```

`SEARCH_API_AUTH` holds the whole header value, for example `ApiKey abc123…` or `Bearer eyJ…`.

### Solr

```json
{
  "type": "http",
  "http": {
    "url": "https://solr.example.com/solr/records/select",
    "method": "GET",
    "secret_headers": {"Authorization": "SOLR_AUTH"},
    "params": {"q": "{query}", "rows": "{limit}", "wt": "json", "fl": "id,title,timestamp,country,content"},
    "results_path": "response.docs",
    "fields": {"record_id": "id", "title": "title", "timestamp": "timestamp", "country": "country", "snippet": "content"},
    "service_query_id": {"path": "responseHeader.params.rid"}
  }
}
```

### A generic REST API

```json
{
  "type": "http",
  "http": {
    "url": "https://api.example.com/v2/search",
    "method": "POST",
    "secret_headers": {"X-API-Key": "RECORDS_API_KEY"},
    "body": {"lucene": "{query}", "max_results": "{limit}", "purpose": "{purpose}", "warrant": "{warrant_id}",
             "authorization": "{parameter_1}", "visibility": "{parameter_2}"},
    "results_path": "data.results",
    "fields": {"record_id": "id", "title": "name", "timestamp": "created", "country": "country_code", "snippet": "excerpt"},
    "service_query_id": {"path": "meta.request_id"}
  }
}
```

### Placeholders

Use these in `url`, `params` and `body`. A value that is exactly one placeholder (`"{limit}"`) keeps its type: a number, a list or an object. A placeholder inside longer text (`"lucid-warrant {warrant_id}"`) is inserted as text.

| Placeholder | What it is |
|---|---|
| `{query}` | The final Lucene query. It already includes the permitted-country list, the date window and blocked dates. |
| `{limit}` | The record cap for this search (never above the warrant's). |
| `{warrant_id}`, `{purpose}` | From the signed warrant. Send them if your service can log them; they make reconciliation easy. |
| `{parameter_1}`, `{parameter_2}`, `{parameter_2_csv}` | Authorization and visibility from the signed warrant (`parameter_2` is a list). |
| `{date_filter}`, `{country_filter}`, `{place_filter}` | The same limits as structured objects, for services that prefer filters to query clauses. `place_filter` lists each place's `lat`, `lon` and `radius_km` (relation `within`). |

**The request must carry `{query}`**: in `params` or `url` (or `body` for POST). A configuration without it is refused, because the service would run something other than the query Lucid Warrant checked. GET requests have no body, so a GET configuration with a `body` is refused too.

Keys starting with `_` (like Elasticsearch's `_source`) are kept in `body`, `params` and `headers`; elsewhere in the configuration they're comments.

Other options:
- `headers`: fixed headers.
- `timeout_seconds`: 1–300 (default 30).
- `verify_tls`: keep it `true`.

**Return coordinates when records have them** (`fields.lat` and `fields.lon`): under a warrant with places, Lucid Warrant releases only results it can show are inside an approved circle. A result without usable coordinates is withheld (and counted), so map these fields if you use places. The service URL may not contain a `#` fragment, since a fragment is never sent.

**Return the country with every result.** Lucid Warrant withholds any result from outside the permitted countries; without a country field it can't double-check.

### When to write a Python adapter instead

Write one if the API needs any of these:
- a token exchange (OAuth client credentials), signed requests or mutual TLS;
- several calls per search (pagination, scroll);
- a vendor SDK, or a non-JSON response.

```python
# mycompany/lucid_search.py
from lucid_warrant.backend import BackendError, SearchResponse


class RecordsAdapter:
    def __init__(self, base_url: str):          # options from the configuration
        self.base_url = base_url

    def search(self, payload: dict, plan) -> SearchResponse:
        # payload: warrant_id, purpose, parameter_1, parameter_2, query, limit, date_filter, country_filter, place_filter.
        # Send payload["query"] exactly as given; don't rewrite it. Return at most payload["limit"] results,
        # each a dict with record_id (required), title, timestamp, country and snippet.
        # Raise BackendError on failure. Read credentials from your environment; never log them.
        ...
        return SearchResponse(results=[...], service_query_id=request_id)
```

```json
{"type": "python", "python": {"adapter": "mycompany.lucid_search:RecordsAdapter",
                              "options": {"base_url": "https://records.internal"}}}
```

Install the module into the same Python environment as Lucid Warrant (`pip install -e path/to/mycompany`).

## 3. Set the secrets

Set each environment variable named in `secret_headers` wherever the agent's commands run. Use the OS's own mechanism, never a file in the project:
- **Windows:** System Properties → Environment Variables, or `setx SEARCH_API_AUTH "ApiKey …"` in a new terminal.
- **macOS:** your shell profile, or a secrets manager that exports it.

The GUI doesn't need them; only commands that search do.

## 4. Check it without searching

```sh
lucid-warrant backend check
lucid-warrant backend check --sample-response sample.json
```

`check` reports:
- whether the configuration is valid;
- whether each secret variable is set (never its value);
- the exact request an example search would send, with secrets redacted;
- with `--sample-response`, how a saved JSON response from your API maps to results, including any result without a country and whether the service's request ID can be found.

It never contacts the service: a test search would itself be a search without a warrant. For the sample, use a response you already have, such as one from API documentation, a log, or one the administrator captured under their own authority.

## 5. Sign it (the analyst)

Preview it first; this works without the passphrase:

```sh
lucid-warrant config path                      # note the org hash ("missing" if there's no file yet)
lucid-warrant config set search_backend --value-file backend.json --scope org \
    --reason "Connect to the records search API" --if-hash <hash> --dry-run
```

Then the analyst runs the same command without `--dry-run`. It asks for their passphrase and signs the change. Every new search then goes to the configured service.

## 6. Run the first real search through a warrant

Sign a warrant as usual and have the agent run one search. Then check:
- **Runs tab:** the run shows **Completed**, with a **service query ID** from your service (not `mock-…`).
- **The run's payload record:** the run inspector, or `data/state/payloads/<run_id>.json`. `payload` is what Lucid Warrant checked; `request_sent` is the actual HTTP request (method, URL, headers with credentials replaced by the variable's name, body).
- **Your service's own logs:** they show the same request, ideally with the warrant ID if you passed `{warrant_id}`.

The status bar then reads "Searches go to `<host>`" instead of PRACTICE MODE.

## Troubleshooting

| Message | Meaning |
|---|---|
| `… contains a line break or other control character` | The secret's environment variable has a stray newline (common when pasting a token). Set it again. The value is never printed. |
| `search service unavailable: environment variable … isn't set` | Set it where the agent's commands run (step 3). Nothing was sent, and the search didn't count against the hourly limit. |
| `the search service answered 401 / 403` | The credentials are wrong or lack permission. |
| `no list of results at …` | `results_path` doesn't match the response. Test with `backend check --sample-response`. |
| `a result has no record ID at …` | `fields.record_id` doesn't match the response. |
| `UNSIGNED_CONFIG` | The organization file was edited without signing. Review it, then `lucid-warrant config sign`. |
| `query_fields … but the library uses …` | The index field names in the configuration and the signed library disagree. |
