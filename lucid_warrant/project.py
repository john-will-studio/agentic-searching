"""`lucid-warrant init`: set up a project folder so an agent started there finds Lucid Warrant on its own.

    <project>/
      lucid-warrant/                       the warrant directory (keys, data), found by discovery
      .claude/skills/lucid-warrant/SKILL.md how to work under a warrant (Claude Code loads skills from here)
      AGENTS.md                            a short section pointing to the skill (Codex and most agents read it)
      CLAUDE.md                            the same section (Claude Code reads it)

Running it again updates the skill and the sections in place (between their markers) and leaves everything
else, including existing warrants and keys, untouched.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

from . import workspace

AGENT_FILES = Path(__file__).resolve().parent / "agent_files"
SKILL_PATH = Path(".claude") / "skills" / "lucid-warrant" / "SKILL.md"
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
_SECTION = re.compile(r"<!-- lucid-warrant:start.*?<!-- lucid-warrant:end -->\n?", re.S)


def command_path() -> Path:
    """The lucid-warrant executable that belongs to this installation."""
    folder = Path(sys.executable).parent
    for name in ("lucid-warrant.exe", "lucid-warrant"):
        if (folder / name).exists():
            return folder / name
    return folder / ("lucid-warrant.exe" if os.name == "nt" else "lucid-warrant")


def command_text(path: Path | None = None) -> tuple[str, str]:
    """(how to run it, a note for PowerShell users or "")."""
    path = path or command_path()
    if os.name == "nt":
        quoted = f'"{path}"'
        return quoted, f"In PowerShell, put `&` in front: `& {quoted} status`.\n"
    return shlex.quote(str(path)), ""


def write_skill(project: Path, command: str, shell_note: str) -> Path:
    target = project / SKILL_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    text = (AGENT_FILES / "SKILL.md").read_text("utf-8")
    install_dir = Path(__file__).resolve().parents[1]
    target.write_text(text.replace("{command}", command).replace("{shell_note}", shell_note)
                      .replace("{install_dir}", install_dir.as_posix()), "utf-8")
    return target


def write_section(file: Path, command: str) -> str:
    """Add (or refresh) the Lucid Warrant section in an instruction file. Returns "created", "added" or "updated"."""
    section = (AGENT_FILES / "AGENTS_SECTION.md").read_text("utf-8").replace("{command}", command).replace(
        "{skill_path}", SKILL_PATH.as_posix())
    if not file.exists():
        file.write_text(section, "utf-8")
        return "created"
    text = file.read_text("utf-8")
    if _SECTION.search(text):
        file.write_text(_SECTION.sub(lambda _m: section, text, count=1), "utf-8")
        return "updated"
    file.write_text(text.rstrip("\n") + "\n\n" + section, "utf-8")
    return "added"


def init_project(project: Path) -> dict:
    project = Path(project).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    existing = workspace.discover(project)
    warrant_dir = existing if existing is not None and project in (existing, *existing.parents) else project / workspace.PROJECT_DIR
    warrant_dir = workspace.create(warrant_dir)
    command, note = command_text()
    skill = write_skill(project, command, note)
    files = {name: write_section(project / name, command) for name in INSTRUCTION_FILES}
    return {"project": str(project), "warrant_directory": str(warrant_dir), "skill": str(skill),
            "instructions": files, "command": command}
