"""The warrant directory: where one deployment's keys, warrants, inbox and run records live.

The software can live anywhere (for example Documents\\Lucid Warrant). A warrant directory is found the way git
finds a repository, so separate projects never mix:

1. LUCID_WARRANT_DATA and LUCID_WARRANT_KEYS (tests, scenarios, scripted setups), or LUCID_WARRANT_WORKSPACE;
2. walking up from the current folder: a warrant directory itself, or a project folder containing one named
   lucid-warrant/ (what `lucid-warrant init` creates), so an agent started in a project finds it with no setup;
3. the GUI's last choice (first launch asks where to create one), remembered here:

    Windows  %APPDATA%\\LucidWarrant\\workspace.json
    macOS    ~/Library/Application Support/LucidWarrant/workspace.json
    other    ~/.config/lucid-warrant/workspace.json

Layout of a warrant directory:

    <warrant directory>/
      .lucid-warrant-dir  marks the folder as a warrant directory
      README.txt        what each folder is for
      keys/             the encrypted signing key and its public key
      data/             signed warrants, the template library, approvals, the agent's inbox, run records
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WORKSPACE_ENV = "LUCID_WARRANT_WORKSPACE"
DEFAULT_NAME = "Lucid Warrant Workspace"
PROJECT_DIR = "lucid-warrant"  # the warrant directory inside a project folder
MARKER = ".lucid-warrant-dir"

README = """Lucid Warrant: warrant directory

This folder holds one deployment of Lucid Warrant. Back it up; don't edit its files by hand.

keys/   Your signing key, encrypted with your passphrase, and its public key. The passphrase is never stored.
        Losing it means creating a new key and re-signing the template library.
data/   Signed warrants, the signed template library and one-time approvals; the agent's inbox (nominations,
        proposals, syntax requests); and the run records (what was searched, when, under which warrant).

The agent works here too: its commands (lucid-warrant status, propose, wait, run, request-syntax) read and
write data/. Set up a project for an agent with `lucid-warrant init` in the folder where it works.
"""


def config_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "LucidWarrant"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LucidWarrant"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "lucid-warrant"


def workspace_file() -> Path:
    return config_dir() / "workspace.json"


def discover(start: Path | None = None) -> Path | None:
    """The nearest warrant directory at or above `start` (default: the current folder): a folder marked as one,
    or a project folder's lucid-warrant/ subfolder.

    The search stops at the top of the project: a repository root (a folder with .git) or the home folder. A
    warrant directory in some parent folder belongs to another project, and this one mustn't adopt it."""
    try:
        here = Path(start or Path.cwd()).resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return None
    for folder in (here, *here.parents):
        if (folder / MARKER).is_file():
            return folder
        if (folder / PROJECT_DIR / MARKER).is_file():
            return folder / PROJECT_DIR
        if (folder / ".git").exists() or folder == home:
            return None
    return None


def remembered() -> Path | None:
    try:
        value = json.loads(workspace_file().read_text("utf-8")).get("workspace")
    except (OSError, ValueError, AttributeError):
        return None
    return Path(value) if isinstance(value, str) and value else None


def resolve() -> tuple[Path | None, str]:
    """(warrant directory, how it was found). None when nothing is set up yet."""
    if os.environ.get(WORKSPACE_ENV):
        return Path(os.environ[WORKSPACE_ENV]), WORKSPACE_ENV
    found = discover()
    if found is not None:
        return found, "found from the current folder"
    chosen = remembered()
    if chosen is not None:
        return chosen, f"last opened in the GUI ({workspace_file()})"
    return None, "not set up"


def configured() -> Path | None:
    """The warrant directory to use (see the module docstring for the order), or None."""
    return resolve()[0]


def overridden_by_environment() -> bool:
    """Data and keys set directly (tests, scenarios, scripted setups): no warrant directory involved."""
    return bool(os.environ.get("LUCID_WARRANT_DATA") or os.environ.get("LUCID_WARRANT_KEYS"))


def needs_setup() -> bool:
    return not overridden_by_environment() and configured() is None


def default_location() -> Path:
    documents = Path.home() / "Documents"
    return (documents if documents.is_dir() else Path.home()) / DEFAULT_NAME


def create(path: Path) -> Path:
    """Create (or adopt) a warrant directory. Existing files are left as they are."""
    path = Path(path).expanduser().resolve()
    for sub in ("keys", "data", "data/inbox"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    marker = path / MARKER
    if not marker.exists():
        marker.write_text("This folder is a Lucid Warrant warrant directory.\n", "utf-8")
    readme = path / "README.txt"
    if not readme.exists():
        readme.write_text(README, "utf-8")
    return path


def remember(path: Path) -> None:
    from .signing import atomic_write

    atomic_write(workspace_file(), (json.dumps({"workspace": str(Path(path).resolve())}, indent=2) + "\n").encode("utf-8"))


def is_workspace(path: Path) -> bool:
    path = Path(path)
    return (path / MARKER).is_file() or ((path / "keys").is_dir() and (path / "data").is_dir())


def for_folder(folder: Path) -> Path:
    """The warrant directory to use for a folder someone picked: the folder itself if it's a warrant directory or
    empty, its lucid-warrant/ subfolder if it's a project that has one, otherwise a new lucid-warrant/ inside it."""
    folder = Path(folder).expanduser()
    if not folder.exists() or is_workspace(folder):
        return folder
    if is_workspace(folder / PROJECT_DIR):
        return folder / PROJECT_DIR
    try:
        empty = not any(folder.iterdir())
    except OSError:
        empty = False
    return folder if empty else folder / PROJECT_DIR
