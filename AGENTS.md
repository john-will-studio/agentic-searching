# Notes for agents developing Lucid Warrant

**Using Lucid Warrant to search in a project, rather than working on its code?** This file isn't for you. Run `lucid-warrant init` in the project folder and follow the skill it installs (`.claude/skills/lucid-warrant/SKILL.md`), plus the section it adds to that project's AGENTS.md.

## Working on the code

- Install: `uv venv --python 3.13 --managed-python .venv` and `uv pip install --python .venv/bin/python -e '.[test]'` (on Windows, `.venv\Scripts\`).
- Test: `.venv/bin/python -m pytest -q`. GUI tests run each case in its own process, because Tk allows one interpreter per process on macOS.
- After editing the GUI, close any running `lucid-warrant-gui` and start it again with `.venv/bin/lucid-warrant-gui`.
- Windows is the target platform. Don't add macOS-only behavior without a Windows path.
- Never touch the user's real keys or data from tests or harnesses. Point `LUCID_WARRANT_KEYS`, `LUCID_WARRANT_DATA` and `LUCID_WARRANT_PREFS` at temporary folders.

## Rules learned the hard way

1. **Sweep after a behavior change.** Remove or rewrite every button, note, doc line and test that existed because of the old behavior, in the same change.
2. **Dialog buttons say what they do.** No "OK" that cancels, no restart that isn't needed, and Enter acts on the focused button.
3. **Test what the analyst sees.** For anything visual, assert on real widget state after the event loop has run: colors, geometry, mapped and visible. Not on the code's own variables.
4. **Read every new screen as the analyst would**, at real volumes, on a small and a maximized window. Plain phrases, names before internal IDs, summaries for big data, and only the warnings that matter.
5. **Test through the real final step.** A feature isn't done until a test drives it through what the analyst actually does (signing, printing, running a search), not an internal API that skips the screens.
6. **Measurements must confirm the thing happened** (the page moved, the row appeared) before the number counts.
7. **No broad `try/except` around the main path**, and no debug output or harness files left behind.
8. **Freeze the clock** (`utc_now`) in tests that build dated warrants.

## Where things are

- `lucid_warrant/`: the package. `executor.py` sends searches (the only path that does); `lint.py` checks every query; `warrant.py`, `signing.py` and `org_config.py` hold the signed records; `ui/` is the Tk GUI; `agent_files/` is the skill that `init` installs.
- `docs/`: guides for administrators and agents: the search adapter, configuration reference, agent CLI, organization setup, theming, and a PKI design note.
- `examples/`: an example organization file, and sample place lists (also used by tests).
