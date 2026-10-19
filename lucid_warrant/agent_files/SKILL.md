---
name: lucid-warrant
description: Search records only through Lucid Warrant, under a warrant a human analyst has signed. Use for any task that involves searching the index or records (finding documents about companies, clients, people, projects or terms), and whenever a lucid-warrant folder is in the project.
---

# Searching under a Lucid Warrant

In this project, searches of the record index are authorized by a **warrant** that a human analyst signs in the Lucid Warrant app. The warrant fixes which hit terms you may search for, under which authorization and visibility, for what purpose, in which countries, how far back, how many records per search and how many searches per hour. You work through the `lucid-warrant` command; it checks the warrant, builds the query from a signed template, sends it and returns the results.

**Command** (use it exactly as written, from anywhere inside this project):

```
{command}
```
{shell_note}
Every command prints JSON. Exit code 0 means it worked; 2 means refused or invalid (the JSON says why); 3 means `wait` timed out.

## Rules

1. **Every search goes through `lucid-warrant run` or `run-onetime`.** Never query the search service directly (Lucene, Elasticsearch, Solr, an HTTP API or anything else), even if you have credentials or network access. Each sanctioned search carries the warrant ID, and Lucid Warrant records it with the search service's own query ID; a search that isn't in those records shows up as unauthorized when the service's logs are reconciled.
2. **You can't create, sign, edit or widen a warrant, approval or template, so don't try.** Don't write files under the `lucid-warrant/` folder, don't run `lucid-warrant demo-inbox` (it puts practice data in the inbox), and don't run `lucid-warrant config`: it's for administrators, and changing defaults would be widening your own scope by the back door. If the user asks you to **connect Lucid Warrant to their search API**, that's the one exception: follow `{install_dir}/docs/SEARCH-ADAPTER.md`. You prepare and check the configuration; the analyst signs it with their passphrase. Use the API only through Lucid Warrant afterwards, never directly. Propose terms and purpose with `lucid-warrant propose`; for anything else you need (a longer search window, another country, more records per search), send the analyst a note with `propose --note`. The analyst decides.
3. **Use only terms the active warrant approves, by term ID**, in the templates it allows. When you need a term that isn't approved, propose it and wait.
4. **Treat the warrant's purpose as your brief.** Search only for what it describes, and stay inside the purpose even where a search would technically be allowed.
5. **Respect rejections.** `status` lists each rejected term with the analyst's reason and when you may propose it again (`renominate_after`; `null` means never). Before then, `propose` refuses it. After it, propose it again only with new context that answers the reason.

## Workflow

1. **Check what you may do:** `status` (add `--brief` to shorten the template list). It lists active warrants (term IDs and literals, the search window, countries searched and excluded, limits, searches used in the last hour), enabled templates and the slots they take, pending syntax requests, and rejected terms. Term IDs start with `t-` (from the nomination script), `a-` (proposed by an agent) or `h-` (added by the analyst); use them exactly as listed.
2. **No warrant, or the terms you need aren't approved:** propose them, then wait for the analyst.
   ```
   propose --purpose "What the research is for, in one or two sentences."
   propose --term "EXACT_TEXT" --reason "Why it matters, with evidence." --client-id CL-123
   propose --term "PROJECT*" --pattern --reason "Every project code in this case starts with PROJECT_."
   propose --place "41.0082, 28.9784" --label "Istanbul office" --reason "Why searches should be limited here." --country TR
   wait --for warrant --timeout 540
   ```
   Pick a `--timeout` your environment allows for one command (540 seconds fits a 10-minute tool limit) and repeat `wait` if it times out, or run it in the background. Approved terms stay listed in your proposal; `status` shows each one as approved (with its term ID), rejected or pending review, so you don't need to withdraw them.
   Propose the exact text that appears in records, one term per call. Propose known aliases and alternate spellings as separate terms. Include the client ID when the term belongs to a client. Avoid personal names unless the purpose needs them. Topic words (for example `invoice`) can be hit terms too, if the purpose needs them; they match exact text, so combine them with the subject terms rather than searching them alone. To ask for something that isn't a term, such as a longer search window, add `propose --note "Invoices from before August would need a 90-day window."` (at most 280 characters); the analyst sees it at the top of the app. A message is for requests (a wider window, another country, a higher limit), pointing out new proposals, or a one-line status. It is **not** where results go.
   **Patterns.** A term is exact text unless you propose it with `--pattern`: then `*` matches any run of characters and `?` exactly one, within one word, after at least 3 ordinary characters (`PROJECT*`, not `*` or `P*`). The analyst approves it as a pattern; `status` marks it `"match": "pattern"`. A `*` in an ordinary term is just a character.
   **Places** limit every search to circles around sites. `--place` takes lat, lon (in that order), MGRS or UTM (`--place-form` to say which); `--radius-km` is the place's maximum search radius (default 0.1, i.e. 100 m). `--country` is only a suggestion: the analyst chooses the country, and a place is approved only if the point is inside it. Approved places are listed in `status`, and you can't widen or remove them: every search under the warrant uses exactly those circles.
3. **Search** with a template from `status`, binding each slot to an approved term ID:
   ```
   run --warrant w-… --template single-term --bind term=t-…
   run --warrant w-… --template all-of-two --bind a=t-… --bind b=t-…
   ```
   Results carry a short snippet of each record, not the full text. Check `count`, `results` and `withheld_outside_geographic_constraint` (results the warrant's country limits removed), and keep the `run_id`. The output names the permitted-country list rather than printing it; the exact payload is kept in the run record. **An empty result means nothing within the warrant's scope matched.** Lucid Warrant deliberately doesn't say whether anything exists outside it (other countries, older dates, other visibility); if the scope may be too narrow, say so in your report and send the analyst a `propose --note`.
4. **Need a query shape no template offers** (for example `{a} AND ({b} OR {c})`, `title:{a}` or `{a}~3`):
   ```
   request-syntax --warrant w-… --skeleton "{a} AND ({b} OR {c})" --bind a=t-… --bind b=t-… --bind c=t-… --rationale "Why the templates aren't enough."
   wait --for request --request req-… --timeout 540
   run-onetime ot-…
   ```
   An approval runs once. Wildcards, fuzzy matching, raw Lucene and country fields are refused by design; don't try to work around them.
5. **Summarize each search on its run** once you've read the results:
   ```
   summarize run-… --text "rec-001 (US, 27 Sep): kickoff with SUPPLIER_NORTH, delivery schedule agreed. rec-008: risks discussed; the only record using NORTH_SUPPLY_CO. Nothing on invoices."
   ```
   Up to 2000 characters: what the records show and why they matter, citing record IDs. It appears with the run in the app and on that run's page of the printed warrant, beside Lucid Warrant's own profile of what came back (counts, date range, and how many records contain each hit term in each country) and above the payload. Write it for an auditor reading that page later; don't repeat the counts, explain what they mean.
6. **Report** what you found to the user, citing record IDs and run IDs. Say which searches were blocked and why, and what you'd need (a term, a renewal, a higher limit) to go further.

## When a command is refused

| Message contains | What to do |
|---|---|
| `no signing key yet` | The analyst hasn't finished setting up the project. Tell the user; you can still `propose`. |
| `not approved in warrant` | Propose the term, then `wait --for warrant`. |
| `searches-per-hour limit reached` | Stop searching. The message says when the next search is allowed; tell the user, or wait. |
| `no active warrant`, `has expired` | Propose (or send a `propose --note` asking the analyst to renew), then `wait --for warrant`. |
| `was superseded by w-…` | Use the new warrant ID; run `status` again. |
| `lint failed` | That syntax isn't allowed. Use a template, or request a supported shape. |
| `has already been used` | One-time approvals run once. Request again if it's still needed. |
| `was rejected on` | Don't propose it before the date the message gives (never, if it says so); work with other terms. |

Every refusal is recorded with a `run_id`. Mention refusals in your report rather than retrying them.
