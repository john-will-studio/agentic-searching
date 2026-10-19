# Agent CLI: configuration commands

`lucid-warrant config …` reads and changes the organization configuration. It never opens a window and prints exactly one JSON object to stdout. Signing organization changes asks for the analyst's passphrase; review text goes to stderr.

**Who it's for:** administrators, and setup agents acting for an administrator. It is not for the agent that searches under warrants: changing defaults would let that agent shape its own scope, so the skill tells it not to run `config` at all. Organization-level changes are **signed with the analyst's passphrase** (the same key that signs warrants), need a `--reason` and `--if-hash`, and are logged; they change only what new warrants are prefilled with, never a warrant already signed. An agent running commands can't make them: there's no terminal to ask for the passphrase, and it doesn't know it.

```
lucid-warrant config [--org-config PATH] COMMAND
```

| Command | What it does |
| --- | --- |
| `path` | Where the organization and preference files are, how they were chosen, and their current file hashes. `org.review` shows changes and authority-affecting settings; `org.full_config` contains the full effective organization configuration. These use the same captured bytes as `org.hash`. |
| `show [--layer effective\|builtin\|org\|user] [--sources]` | The configuration. `effective` also gives the authority hash; `--sources` says which layer each value comes from. |
| `get KEY` | One value and where it comes from, e.g. `get parameters.parameter_1.label`. |
| `set KEY (--value TEXT \| --value-json JSON \| --value-file PATH)` | Change one value. `--value` is always text; use `--value-json` for numbers, lists and objects; `--value-file -` reads stdin. |
| `reset (KEY \| --all)` | Remove an override so the lower layer's value shows through. |
| `validate [--file PATH] [--scope org\|user]` | Check a file, or the effective configuration, without writing. Warns about low-contrast colors. |
| `schema` | Every setting: type, default, rule, and whether it affects authority. |
| `theme list` / `theme set ID [--mode system\|light\|dark]` | Color schemes (personal preference). |

`set` and `reset` options:

| Option | Meaning |
| --- | --- |
| `--scope user` (default) | Personal preferences: only `ui.*` keys. |
| `--scope org` | The organization file. Needs `--reason` and `--if-hash`, and asks for the analyst's passphrase to sign the change. |
| `--passphrase-stdin` | Read the passphrase from stdin instead of prompting (for scripted setups run by the analyst). |
| `--reason TEXT` | Recorded in `<data>/config-history.jsonl`. |
| `--if-hash HASH` | The organization file's hash from `config path` or a dry run (`missing` if it doesn't exist yet). Refuses if the file changed since. |
| `--dry-run` | Validate and show the diff, the hashes and whether authority changes, without writing. |

## The organization file is signed

`organization.json` carries a signature (`organization.json.sig`) from the analyst's key, like the template library. The GUI and the CLI refuse an organization file that isn't signed, or that changed after it was signed (`UNSIGNED_CONFIG`): it sets what new warrants start with (exclusions, limits, certification wording), so an unsigned edit could quietly weaken them. When a signing key is first created, the GUI offers **Review and sign organization configuration**, with changes from built-in defaults, authority-affecting settings and an expandable full configuration. It signs only if the analyst accepts that review. CLI key creation leaves a preexisting file unsigned.

If you edit the file by hand, review it and sign it. `config sign` shows the same review before requesting the passphrase. It calls out the destination, place clause, query and response field mappings, certification wording, country mode and adapter options. Inspect `org.full_config` from `config path` for every setting:

```sh
lucid-warrant config path                                              # note org.hash
lucid-warrant config sign --reason "Added NO and IS exclusions" --if-hash <org.hash>   # asks for your passphrase
```

## Safe workflow for an organization change

```sh
lucid-warrant config path                                   # note org.hash (or "missing")
lucid-warrant config set parameters.parameter_1.label --value "Case authorization" --scope org --dry-run
lucid-warrant config set parameters.parameter_1.label --value "Case authorization" --scope org \
    --reason "Adopt Acme terminology" --if-hash <org.hash>    # asks for your passphrase, then signs
lucid-warrant config validate
```

PowerShell is the same with a backtick (`` ` ``) for line continuation. Lists and objects:

```sh
lucid-warrant config set country_scope.include --value-json '["CA","GB","US"]' --scope org --dry-run
lucid-warrant config set country_scope.mode --value include --scope org --dry-run
lucid-warrant config set defaults.max_queries_per_hour --value-json 30 --scope org --dry-run
lucid-warrant config set ui.base_font_pt --value-json 12          # personal; no acknowledgement needed
```

Changing two related keys (for example `country_scope.mode` and `country_scope.include`) may be invalid one at a time. Write the whole object instead: `set country_scope --value-file scope.json`.

## Responses

Success:
```json
{"ok": true, "command": "config.get", "data": {"key": "ui.scheme", "value": "teal", "source": "user"}, "warnings": []}
```
Failure:
```json
{"ok": false, "command": "config.set", "error": {"code": "INVALID_VALUE", "path": "defaults.lookback_days", "message": "must be a whole number from 0 to 36500"}}
```

Every organization change increases `organization.revision`. The history entry is written before the change; if it can't be written, nothing changes.

`set` and `reset` report: `changed`, `diff`, `authority_changed`, `old_file_hash` / `new_file_hash`, `old_authority_hash` / `new_authority_hash`, `written` and `requires_review`.

| Exit code | Meaning |
| --- | --- |
| 0 | Success (including dry runs and no-op changes) |
| 1 | Internal error |
| 2 | Usage error (also reported as JSON, code `USAGE`) |
| 3 | Invalid configuration (code `INVALID_VALUE`, `UNKNOWN_KEY`, `INVALID_CONFIG`, `INVALID_JSON`, `FIELD_MISMATCH`) |
| 4 | Refused: wrong scope, CLI-managed key (`schema_version`, `organization.revision`), missing/blank `--reason` / `--if-hash`, no passphrase available (`PASSPHRASE_REQUIRED`), or a wrong one (`BAD_PASSPHRASE`) |
| 5 | File or permission problem |
| 6 | Concurrent change: the file hash differs from `--if-hash`, or another write holds the lock |

## What the CLI can't do

- It can't sign anything or attest that a person accepted something. Certifications are ticked by a human in the GUI and signed with their passphrase.
- It can't change existing warrants. Those keep the configuration they were signed under.
- It doesn't connect a real entitlement service or search backend. Those are code adapters: `lucid_warrant/rbac.py` (`RbacProvider`) and `lucid_warrant/backend.py` (`Backend`, returning `SearchResponse`).

The other agent commands (`init`, `status`, `propose`, `wait`, `run`, `run-onetime`, `request-syntax`, `where`) are summarized in the README, under "The agent's side", and documented in full in the skill that `lucid-warrant init` installs (`lucid_warrant/agent_files/SKILL.md`).
