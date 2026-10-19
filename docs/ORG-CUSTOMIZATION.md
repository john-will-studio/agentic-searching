# Adapting lucid-warrant to an organization

Written for an agent (or person) setting this up for a new organization. **Don't edit Python for any of this.** Everything below is configuration, plus two clearly bounded code adapters.

## 1. Start from the example

```sh
cp examples/organization.example.json data/organization.json        # Windows: copy examples\organization.example.json data\organization.json
lucid-warrant config validate
```

Or keep the file elsewhere and set `LUCID_WARRANT_ORG_CONFIG` to its path. For example, on Windows `%PROGRAMDATA%\LucidWarrant\organization.json`, writable only by administrators.

## 2. Change what's organization-specific

| You want to change… | Setting(s) |
| --- | --- |
| Organization name in the title bar and on warrants | `organization.name` |
| What parameter 1 and 2 are called | `parameters.parameter_1.label`, `parameters.parameter_2.label` (+ `description`) |
| Tab and document wording (Warrants, Hit terms, Templates, Syntax requests, Runs) | `terms.*` |
| How term origins are labelled | `source_labels.script / agent / human / human-edited` |
| Default limits for new warrants | `defaults.expiry_minutes`, `max_rows_per_run`, `max_queries_per_hour`, `lookback_days`, `blocked_ranges` |
| Which countries are searched or excluded by default, and why | `country_scope.mode`, `include`, `exclude`, `reason_label`, `default_reason` |
| The certification statements signers must accept | `certification.purpose`, `certification.limits` |
| Colors | `ui.scheme`, and your own schemes under `ui.schemes` (see THEMING-WINDOWS.md) |
| Window size, text size, density | `ui.window`, `ui.base_font_pt`, `ui.density` |

Use the CLI rather than hand-editing, so every change is validated, hash-checked and logged (docs/AGENT-CLI.md):

```sh
lucid-warrant config path
lucid-warrant config set organization.name --value "Acme Business Development" --scope org --dry-run
lucid-warrant config set organization.name --value "Acme Business Development" --scope org \
    --reason "Initial setup" --if-hash <hash from config path>       # asks for your passphrase, then signs
```

The organization file is signed with your key; the app refuses it unsigned or changed after signing. If you do edit it by hand, run `lucid-warrant config validate`, then `lucid-warrant config sign --reason … --if-hash …` to sign the version you reviewed.

The signing review shows **Changes from built-in defaults** and calls out destination, place clause, field mappings, certification wording, country mode and adapter options. In GUI setup, expand **Show full configuration** for every setting. In the CLI, `config path` returns `org.review` and `org.full_config` with the hash of the same captured file. `config sign` prints the review before asking for the passphrase.

## 3. Match your data

- **Field names:** `query_fields` and the signed template library (`data/library.json`: `default_field`, `allowed_fields`, `date_field`, `country_field`) must name your index's fields. The library is signed, so changing it means re-signing it in the GUI. For a new installation, edit `lucid_warrant/seed/seed_library.json` before first launch.
- **Country codes:** trim `lucid_warrant/seed/countries.json` to the codes your index uses.
- **Hit-term nominations:** your script writes `data/inbox/nominations.json`:
  ```json
  {"terms": [{"term_id": "t-project-cedar", "literal": "PROJECT_CEDAR", "client_id": "CL-100482", "provenance": "where it came from"}]}
  ```
  Term IDs start with `t-`. `client_id` is optional (up to 64 letters, digits and `_ . : / -`); it's recorded and printed, never searched.

## 4. The two code adapters (only when going live)

| Adapter | File | Implement |
| --- | --- | --- |
| Entitlements (parameter 1/2 options and checks) | `lucid_warrant/rbac.py` | A class with `parameter_1_options()`, `parameter_2_options(p1)`, `check(p1, p2)`; return it from `get_rbac()`. |
| Search service | the `search_backend` configuration (most REST APIs need no code), or a Python adapter class | Follow [SEARCH-ADAPTER.md](SEARCH-ADAPTER.md): describe the API in configuration, check it with `lucid-warrant backend check`, and sign it. Return each record's country so the executor can double-check the geographic constraint; store codes as ISO alpha-2, and make sure your analyzer handles Turkish dotted/dotless I if you index Turkish text. |

Leave everything else alone: signing, the executor, lint, warrant validation. Those are the controls.

## 5. Check it

```sh
lucid-warrant config validate
python -m pytest            # 190+ tests, including GUI smoke tests (each runs in its own process)
lucid-warrant-gui
```

Sign a practice warrant, print it (Warrants → Print / Save as PDF), and confirm the wording, certifications, geographic constraint and limits read correctly for your organization.

## Rules that keep authority honest

- Changing configuration never changes existing warrants. Each one records the configuration it was signed under.
- Personal preferences can only change appearance.
- Certifications are accepted by a person ticking them in the GUI, never by configuration or the CLI.
