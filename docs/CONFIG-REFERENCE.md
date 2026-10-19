# Configuration reference

All organization-specific behavior is set in one JSON file; nothing requires editing Python. The table below is generated from the same definitions the program validates against. Get it as JSON with `lucid-warrant config schema`.

## Layers and files

Later layers win:
1. **Built-in defaults** (`lucid_warrant/org_config.py`, `DEFAULTS`).
2. **The organization file.** It's selected by `--org-config PATH`, else the `LUCID_WARRANT_ORG_CONFIG` environment variable, else `<data>/organization.json`. The working directory is never searched. A file you name explicitly must exist; a missing default file just means built-in defaults.
3. **Personal preferences.** `%APPDATA%\LucidWarrant\preferences.json` on Windows, `~/Library/Application Support/LucidWarrant/preferences.json` on macOS, `~/.config/lucid-warrant/preferences.json` elsewhere; override with `LUCID_WARRANT_PREFS`. Preferences may contain **only** `ui`; anything else is refused.

Merge rules:
- Objects merge key by key. Lists (country lists, blocked ranges) replace the lower layer's list entirely.
- Unknown keys, duplicate keys, `null` and wrongly typed values are errors, reported with the file and the key path.
- Keys starting with `_` are comments.

## Settings

| Key | Default | Allowed values | Affects authority |
| --- | --- | --- | --- |
| `schema_version` | `1` | managed by the CLI | yes |
| `organization.id` | `"practice"` | lowercase letters, digits, dashes; starts with a letter | yes |
| `organization.name` | `"Practice organization"` | — | yes |
| `organization.revision` | `1` | managed by the CLI; increases on every organization change | yes |
| `parameters.parameter_1.label` | `"Search authorization"` | 1–80 characters; the two labels must differ | yes |
| `parameters.parameter_1.description` | `"Authorization identifier checked by the entitlement serv…` | 1–1000 characters | yes |
| `parameters.parameter_2.label` | `"Data visibility"` | 1–80 characters; the two labels must differ | yes |
| `parameters.parameter_2.description` | `"Visibility attributes checked by the entitlement service."` | 1–1000 characters | yes |
| `terms.warrant.singular` | `"Warrant"` | 1–80 characters | yes |
| `terms.warrant.plural` | `"Warrants"` | 1–80 characters | yes |
| `terms.hit_term.singular` | `"Hit term"` | 1–80 characters | yes |
| `terms.hit_term.plural` | `"Hit terms"` | 1–80 characters | yes |
| `terms.client_id.singular` | `"Client ID"` | 1–80 characters; the column heading for the client a hit term is associated with | yes |
| `terms.client_id.plural` | `"Client IDs"` | 1–80 characters | yes |
| `terms.template.singular` | `"Template"` | 1–80 characters | yes |
| `terms.template.plural` | `"Templates"` | 1–80 characters | yes |
| `terms.syntax_request.singular` | `"Syntax request"` | 1–80 characters | yes |
| `terms.syntax_request.plural` | `"Syntax requests"` | 1–80 characters | yes |
| `terms.run.singular` | `"Run"` | 1–80 characters | yes |
| `terms.run.plural` | `"Runs"` | 1–80 characters | yes |
| `source_labels.script` | `"Script nomination"` | 1–80 characters, all distinct | yes |
| `source_labels.agent` | `"Agent proposal"` | 1–80 characters, all distinct | yes |
| `source_labels.human` | `"Analyst"` | 1–80 characters, all distinct | yes |
| `source_labels.human-edited` | `"Analyst-edited nomination"` | 1–80 characters, all distinct | yes |
| `query_fields.content` | `"content"` | field names; content, date and country must differ; must match the signed template library | yes |
| `query_fields.date` | `"timestamp"` | field names; content, date and country must differ; must match the signed template library | yes |
| `query_fields.country` | `"country"` | field names; content, date and country must differ; must match the signed template library | yes |
| `defaults.expiry_minutes` | `120` | 1–43200 | yes |
| `defaults.max_rows_per_run` | `20000` | 1–500000 | yes |
| `defaults.max_queries_per_hour` | `60` | 1–100; searches sent per warrant in any rolling hour | yes |
| `defaults.lookback_days` | `30` | 0–36500 (0 = no date restriction) | yes |
| `defaults.blocked_ranges` | `[]` | list of {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, years 1900–2999, at most 50 | yes |
| `country_scope.mode` | `"all"` | "all" or "include" | yes |
| `country_scope.include` | `[]` | list of {"code": "US", "reason": "optional text"} (plain "US" codes also accepted); empty when mode is all, non-empty when include | yes |
| `country_scope.exclude` | `[{"code": "DE", "reason": "GDPR"}, {"code": "FR", "reason…` | list of {"code": "DE", "reason": "GDPR"}; no overlap with include | yes |
| `country_scope.reason_label` | `"Reason"` | — | yes |
| `country_scope.default_reason` | `"GDPR"` | — | yes |
| `certification.purpose` | `"I certify that I have reviewed and validated the approve…` | 1–4000 characters; shown verbatim and signed into each warrant | yes |
| `certification.limits` | `"I certify that these limits and constraints are appropri…` | 1–4000 characters; shown verbatim and signed into each warrant | yes |
| `ui.theme_mode` | `"system"` | "system", "light" or "dark" | no (personal) |
| `ui.scheme` | `"sun-valley"` | a scheme ID from `config theme list` | no (personal) |
| `ui.theme_color` | `""` | `#rrggbb` for the `custom` scheme | no (personal) |
| `ui.schemes` | `{}` | map of id -> {"label", "color": "#rrggbb", "light": {token: "#rrggbb"}, "dark": {...}}; tokens: bg, surface, text, muted, border, accent, accent_text, accent_soft, ok, warn, danger, info, warn_bg, danger_bg, info_bg, agent_bg, agent_border, code_bg | no (personal) |
| `ui.window.width` | `1280` | 640–8192 | no (personal) |
| `ui.window.height` | `860` | 480–8192 | no (personal) |
| `ui.base_font_pt` | `0` | 0 (system default) or 8–24 | no (personal) |
| `ui.density` | `"comfortable"` | "compact", "comfortable" or "spacious" | no (personal) |
## What "affects authority" means

Everything outside `ui` shapes what a signer sees and approves: wording included, since a label can change what someone believes they're signing. So:

- **Changes are signed.** The organization file carries the analyst's signature; the app refuses it unsigned or edited after signing. Changing those settings through the CLI requires `--scope org --reason … --if-hash …` and the analyst's passphrase, increments `organization.revision`, and is logged in `<data>/config-history.jsonl`. Hand edits must be signed with `lucid-warrant config sign`.
- **Each warrant records its configuration.** Every signed warrant stores the configuration it was signed under (`config_context`: the values and their SHA-256). Printed warrants and the Warrants tab show it. Changing the configuration never changes the meaning of an existing warrant; replace or revoke warrants that should change.
- **Defaults only prefill the form.** `defaults` and `country_scope` prefill new warrants; the signer can change them, and what's actually signed is what counts.
- **Field names must match the library.** `query_fields` must agree with the signed template library's `default_field`, `date_field` and `country_field`, because the executor uses the library's values.

## Search service (`search_backend`)

Where searches go: `mock` (the practice records, the default), `http` (a REST search API described entirely in configuration: URL, method, headers, secret headers named by environment variable, request template with placeholders, where to find results and their fields, and the service's request ID), or `python` (`module:Class` adapter plus options). It affects authority, so it's signed like the rest of the organization file. Check it with `lucid-warrant backend check`. The full guide, with Elasticsearch, Solr and generic REST examples, is [SEARCH-ADAPTER.md](SEARCH-ADAPTER.md).

## Geographic constraint (`country_scope`)

Records carry a two-letter ISO 3166-1 code in the country field.

- `mode: "all"`: search every country except the exclusions. `mode: "include"`: only the listed countries; records with no country are then excluded too.
- Exclusions always apply, each with a reason (for example `GDPR`).
- The executor adds the scope to every query outside the agent's syntax, and also sends it as a structured `country_filter`.
- The list of valid codes is `lucid_warrant/seed/countries.json`; trim it to the codes your index uses.

Reasons are at most 60 characters, so they stay readable in the tables at the smallest window size. Exclusions need a reason; for included countries it's optional.

## Places (`geography`)

- `place_clause`: how the search service writes "within this circle", with `{lat}`, `{lon}` and `{km}` (default `INTERSECTS({lat},{lon},{km})`). Check the argument order and units against your service: Solr's `geofilt` takes `pt=lat,lon` and `d` in km; WKT and GeoJSON put longitude first. The template is signed into each warrant with the rest of the configuration, so changing it doesn't change existing warrants.
- `max_radius_km`: the largest radius any one place may have (default 25). Each place's own radius (100 m unless the analyst changes it) is signed with the place.

Places are checked against the bundled country borders (`lucid_warrant/geo_data/`, Natural Earth 1:10m, public domain).

## Searches per hour

`defaults.max_queries_per_hour` prefills each new warrant's signed limit. The executor counts searches *sent* under the warrant in any rolling hour, reserving a slot under a lock just before sending. Refused searches don't count. If the rate ledger (`<data>/state/rate_ledger.jsonl`) can't be read, searches are refused until it's repaired.

## Field names must match the library

If `query_fields` disagrees with the signed template library, the app shows the problem in the status bar and won't sign. `config validate` and `config set` refuse such a configuration, and the executor refuses to run a warrant that was signed for different field names than the library uses.

## Dates

Blocked ranges are inclusive UTC calendar days, enforced as half-open intervals: from midnight on the first day up to midnight after the last.
