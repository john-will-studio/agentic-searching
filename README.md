# Lucid Warrant

Lucid Warrant puts a human signature between an AI agent and a search index. The agent proposes what it wants to search for and why. An analyst reviews the proposal and signs a **warrant** that fixes what may be searched, for what purpose and within which limits. Lucid Warrant then sends only the searches that fit a signed warrant, and keeps a record of each one.

Signatures use Ed25519. The signing key is encrypted with the analyst's passphrase and unlocked only to sign; the parts that check and send searches need nothing but the public key. An agent that can read and write the project's files still can't create, widen or alter a warrant.

## Install

Python 3.11 or newer is required, with Tkinter for the desktop app (the python.org installers include it).

- **Windows:** double-click `Lucid Warrant.bat`. The first run creates a private Python environment beside it and installs the app.
- **macOS:** double-click `Lucid Warrant.command`.
- **Manually:** `uv venv --python 3.13 --managed-python .venv`, then `uv pip install --python .venv/bin/python -e '.[test]'`, then run `.venv/bin/lucid-warrant-gui` (`.venv\Scripts\` on Windows).

On first launch the app asks where to keep the **warrant directory**, the folder holding a project's key, signed records, the agent's inbox and the run history. It then walks the analyst through creating a signing key, and opens a short tutorial that can be reopened at any time. `lucid-warrant demo-inbox` loads practice nominations.

## How a warrant is made

The analyst works in the **Compose** tab. Proposals from the agent, and from an optional nomination script, arrive as pending rows. The analyst includes, rejects or sets aside each one with the A, R and P keys or the matching buttons. Nothing takes effect until the warrant is signed.

A warrant records:

- **Hit terms.** The exact text the agent may search for. A term can instead be approved as a **pattern**: `PROJECT*` matches any word that begins with PROJECT. A pattern must start with at least three ordinary characters, so it can never mean "everything", and it is marked as a pattern wherever it appears. Each term can carry a client ID for the record; client IDs are never added to searches.
- **Authorization.** Parameter 1 (the authority to search) and parameter 2 (which categories of record are visible), chosen from what the analyst's entitlement service allows.
- **Purpose.** The reason for the work, sent with every search. It is reviewed by a person, not checked by software; text drafted by the agent stays labeled as the agent's.
- **Geographic constraint.** The countries searched or excluded, each with a reason, and optionally **places**: circles around specific sites. Every place carries its own signed radius, 100 meters unless the analyst sets another, and cannot be approved until the analyst has confirmed the country it lies in and the point falls inside that country. Places can be entered by hand, converted from MGRS, UTM or degrees-minutes-seconds, or imported from CSV, GeoJSON, KML or KMZ.
- **Limits.** Expiry, records per search, searches per hour, the date window and any blocked date ranges.

Before signing, the analyst ticks two certifications and reviews the warrant as a document. It opens with an **At a glance** summary and, when replacing an earlier warrant, lists what changed. Signing asks for the passphrase every time.

Under a signed warrant, the agent may combine the approved terms with any template in the signed **template library**. Query syntax outside the library needs a one-time approval. A query that proves useful can be promoted into the library.

## What happens on every search

Lucid Warrant, not the agent, builds and sends each query. It checks the warrant's signature, expiry and limits, and the analyst's entitlements as they stand at that moment. It wraps the agent's query in the warrant's country, place and date limits, and confirms that the finished string contains only approved terms and the signed constraints. Records that come back from outside the constraint are withheld and counted.

Each search, sent or refused, gets a run ID. The exact payload, the search service's own query ID and a profile of what came back are saved to the run: counts, date ranges, and how many records contain each term in each country. Full results are kept, compressed, for export with `lucid-warrant results RUN_ID --format parquet|csv|jsonl`. The **Runs** tab shows this history, and **Print / Save as PDF** produces the warrant with an addendum covering every search run under it.

## The agent's side

```sh
cd my-case && lucid-warrant init      # once per project: warrant directory, agent skill, AGENTS.md section
lucid-warrant status                  # active warrants, approved terms, templates, limits, rejections
lucid-warrant propose --purpose "What the research is for."
lucid-warrant propose --term "PROJECT_CEDAR" --reason "Why it matters."
lucid-warrant propose --term "PROJECT*" --pattern --reason "Every project code starts with PROJECT_."
lucid-warrant propose --place "41.0082, 28.9784" --label "Istanbul office" --reason "Why searches stop here."
lucid-warrant wait --for warrant
lucid-warrant run --warrant W --template single-term --bind term=TERM_ID
lucid-warrant summarize RUN_ID --text "What the records show."
```

The agent passes warrant, template and term IDs only. No command accepts a query string or the authorization parameters; those come from signed files. Output is JSON. `init` installs a skill that teaches an agent this workflow, and the command line and the app find a project's warrant directory by walking up from the current folder.

## Connecting a search service

Searches go to practice records until the organization configuration names a real service. Most REST search APIs, including Elasticsearch, OpenSearch and Solr, need configuration only; anything unusual takes a small Python adapter. Credentials stay in environment variables, and `lucid-warrant backend check` previews the exact request without sending it. Because the configuration is signed, only the analyst can change where searches go. See [docs/SEARCH-ADAPTER.md](docs/SEARCH-ADAPTER.md).

## Configuration and entitlements

One signed organization file sets labels, defaults, certification wording, country rules, the place-search clause and colors. Each warrant records the configuration it was signed under, and searches refuse an older configuration than the one in force. Administrators change it with `lucid-warrant config`, which asks for the analyst's passphrase.

- [docs/ORG-CUSTOMIZATION.md](docs/ORG-CUSTOMIZATION.md): adapting the app to an organization
- [docs/CONFIG-REFERENCE.md](docs/CONFIG-REFERENCE.md): every setting
- [docs/AGENT-CLI.md](docs/AGENT-CLI.md): configuration commands, output and exit codes
- [docs/THEMING-WINDOWS.md](docs/THEMING-WINDOWS.md): color schemes and display checks
- [examples/organization.example.json](examples/organization.example.json): a complete example

Authorization options come from an entitlement provider (`lucid_warrant/rbac.py`). The included mock reads `lucid_warrant/seed/rbac_mock.json`; connecting a real service means implementing three methods: `parameter_1_options`, `parameter_2_options` and `check`.

## What it protects, and what it doesn't

**Signed and verified:** warrants, the template library, one-time approvals and the organization configuration. An edit to any of them fails verification and is refused. A configuration file placed in the folder before setup is shown to the analyst, never signed silently.

**Recorded but not signed:** the inbox, which carries no authority, and the run history. An agent with access to the files could delete or alter that history. The search service's own logs, matched against Lucid Warrant's run and service IDs, are the check that survives.

**Not prevented when the agent runs as the analyst's user:** querying the search service directly, editing this code, or operating the app through automation. It can't sign without the passphrase. Real enforcement means running the executor as a separate account that alone holds the search credentials and the warrant data, with the code and public key read-only to the agent. The About dialog warns when other accounts on the computer can reach the signing key, and offers to restrict access. [docs/PKI.md](docs/PKI.md) describes how an organization's smart cards or certificates could be added alongside the passphrase.

## Files

Inside a warrant directory:

| Path | Contents |
| --- | --- |
| `keys/signing_key.enc.json`, `keys/signing_key.pub` | the encrypted private key and its public key |
| `data/warrants/`, `data/library.json`, `data/onetime/` | signed warrants, template library and one-time approvals |
| `data/organization.json` | the signed organization configuration, when used |
| `data/inbox/` | proposals, nominations and syntax requests |
| `data/state/` | audit log, payloads, results and usage ledgers |

`LUCID_WARRANT_WORKSPACE`, or `LUCID_WARRANT_KEYS` with `LUCID_WARRANT_DATA`, override where these live.

## Development

`.venv/bin/python -m pytest -q` runs the test suite. [AGENTS.md](AGENTS.md) has the conventions for working on the code.
