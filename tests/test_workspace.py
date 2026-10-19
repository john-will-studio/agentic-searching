"""The warrant directory: chosen once, remembered, used by the GUI and the agent's CLI."""

import json
import os
import subprocess
import sys

import pytest

from lucid_warrant import workspace
from lucid_warrant.config import Paths


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "config_dir", lambda: tmp_path / "config")
    for var in ("LUCID_WARRANT_DATA", "LUCID_WARRANT_KEYS", "LUCID_WARRANT_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_first_launch_needs_setup_then_remembers(config_home):
    assert workspace.needs_setup() and workspace.configured() is None
    ws = workspace.create(config_home / "My Warrants")
    workspace.remember(ws)
    assert not workspace.needs_setup() and workspace.configured() == ws
    assert (ws / "keys").is_dir() and (ws / "data" / "inbox").is_dir() and "keys/" in (ws / "README.txt").read_text()
    paths = Paths.default()
    assert paths.keys == ws / "keys" and paths.data == ws / "data"


def test_create_adopts_an_existing_directory_without_touching_it(config_home):
    ws = workspace.create(config_home / "ws")
    (ws / "README.txt").write_text("mine")
    (ws / "data" / "note.txt").write_text("keep")
    workspace.create(ws)
    assert (ws / "README.txt").read_text() == "mine" and (ws / "data" / "note.txt").read_text() == "keep"
    assert workspace.is_workspace(ws)


def test_environment_overrides_the_remembered_directory(config_home, monkeypatch, tmp_path):
    workspace.remember(workspace.create(config_home / "ws"))
    monkeypatch.setenv("LUCID_WARRANT_DATA", str(tmp_path / "d"))
    monkeypatch.setenv("LUCID_WARRANT_KEYS", str(tmp_path / "k"))
    assert not workspace.needs_setup()
    assert Paths.default().data == tmp_path / "d"
    monkeypatch.delenv("LUCID_WARRANT_DATA")
    monkeypatch.delenv("LUCID_WARRANT_KEYS")
    monkeypatch.setenv("LUCID_WARRANT_WORKSPACE", str(tmp_path / "other"))
    assert Paths.default().data == tmp_path / "other" / "data"


def test_a_damaged_workspace_file_means_choose_again(config_home):
    workspace.workspace_file().parent.mkdir(parents=True)
    workspace.workspace_file().write_text("{not json")
    assert workspace.configured() is None and workspace.needs_setup()


def test_where_command_reports_the_folders(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LUCID_WARRANT")}
    env["LUCID_WARRANT_WORKSPACE"] = str(tmp_path / "ws")
    out = json.loads(subprocess.run([sys.executable, "-m", "lucid_warrant.cli", "where"], capture_output=True, timeout=60,
                                    text=True, env=env).stdout)
    assert out["warrant_directory"] == str(tmp_path / "ws") and out["chosen_by"] == "LUCID_WARRANT_WORKSPACE"
    assert out["keys"] == str(tmp_path / "ws" / "keys")


def test_cli_output_is_utf8_through_a_pipe(tmp_path):
    """Windows writes pipes in cp1252 unless told otherwise: "ü" came out as invalid UTF-8 and "→" crashed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LUCID_WARRANT") and k != "PYTHONIOENCODING"}
    env["PYTHONUTF8"] = "0"
    folder = tmp_path / "Türkiye → İstanbul Θεσσαλονίκη"
    env["LUCID_WARRANT_WORKSPACE"] = str(folder)
    raw = subprocess.run([sys.executable, "-m", "lucid_warrant.cli", "where"], capture_output=True, timeout=60,
                         env=env).stdout
    assert json.loads(raw.decode("utf-8"))["warrant_directory"] == str(folder)


def test_signing_key_format_never_changes():
    """The key file's format string is also the encryption's associated data: changing it locks out every key."""
    from lucid_warrant.signing import KEY_FORMAT
    assert KEY_FORMAT == "lucene-warrant-key-v1"


def test_round7_discovery_stops_at_the_repository_root(tmp_path, monkeypatch):
    """Codex round 7 #7: a warrant directory above another project's repository isn't adopted."""
    from lucid_warrant import workspace

    monkeypatch.setenv("HOME", str(tmp_path))
    parent = tmp_path / "parent"
    workspace.create(parent / "lucid-warrant")
    child = parent / "unrelated-project"
    (child / ".git").mkdir(parents=True)
    assert workspace.discover(child) is None
    assert workspace.discover(parent / "lucid-warrant" / "data") == parent / "lucid-warrant"
    (child / "src").mkdir()
    workspace.create(child / "lucid-warrant")
    assert workspace.discover(child / "src") == child / "lucid-warrant", "subfolders of a project still find it"


def test_round7_switching_projects_restarts_in_the_chosen_one(tmp_path, monkeypatch):
    """Codex round 7 #8: the new app is told which project; the old one can't win again."""
    from lucid_warrant import workspace
    from lucid_warrant.ui import dialogs

    monkeypatch.setenv(workspace.WORKSPACE_ENV, str(tmp_path / "old"))
    _command, options = dialogs.restart_in(tmp_path / "new")
    assert options["env"][workspace.WORKSPACE_ENV] == str(tmp_path / "new") and options["cwd"] == str(tmp_path / "new")
    (tmp_path / "new").mkdir()
    probe = [sys.executable, "-c", f"import os; print(os.environ[{workspace.WORKSPACE_ENV!r}], os.getcwd(), sep=chr(10))"]
    monkeypatch.setattr(dialogs, "restart_in", lambda chosen: (probe, {**options, "stdout": subprocess.PIPE, "text": True}))
    launched = dialogs.relaunch_in(tmp_path / "new")  # a real launch, so the options must suit Popen
    assert launched.communicate()[0].splitlines() == [str(tmp_path / "new"), str(tmp_path / "new")]
