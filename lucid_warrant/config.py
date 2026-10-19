"""File locations.

Keys and data live in the warrant directory chosen on first launch (see workspace.py): <dir>/keys and
<dir>/data. LUCID_WARRANT_KEYS and LUCID_WARRANT_DATA override that (tests, scenarios, scripted setups).
Without either, keys default to ~/.lucid-warrant and data to ./data next to the package. Practice mode's
mock entitlements and search data: LUCID_WARRANT_RBAC_FILE and LUCID_WARRANT_CORPUS.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
SEED_DIR = PACKAGE_ROOT / "seed"


@dataclass(frozen=True)
class Paths:
    keys: Path
    data: Path
    seed: Path = SEED_DIR
    rbac_file: Path | None = None
    corpus_file: Path | None = None

    @classmethod
    def default(cls) -> "Paths":
        from .workspace import configured, overridden_by_environment

        workspace = None if overridden_by_environment() else configured()
        if workspace is not None:
            return cls.for_workspace(workspace)
        keys = os.environ.get("LUCID_WARRANT_KEYS") or Path.home() / ".lucid-warrant"
        data = os.environ.get("LUCID_WARRANT_DATA") or PACKAGE_ROOT.parent / "data"
        return cls._with_practice_files(Path(keys), Path(data))

    @classmethod
    def for_workspace(cls, workspace: Path) -> "Paths":
        return cls._with_practice_files(Path(workspace) / "keys", Path(workspace) / "data")

    @classmethod
    def _with_practice_files(cls, keys: Path, data: Path) -> "Paths":
        rbac = os.environ.get("LUCID_WARRANT_RBAC_FILE")
        corpus = os.environ.get("LUCID_WARRANT_CORPUS")
        return cls(keys=keys, data=data, rbac_file=Path(rbac) if rbac else None,
                   corpus_file=Path(corpus) if corpus else None)

    @property
    def private_key(self) -> Path:
        return self.keys / "signing_key.enc.json"

    @property
    def public_key(self) -> Path:
        return self.keys / "signing_key.pub"

    @property
    def library(self) -> Path:
        return self.data / "library.json"

    @property
    def warrants(self) -> Path:
        return self.data / "warrants"

    @property
    def onetime(self) -> Path:
        return self.data / "onetime"

    @property
    def inbox(self) -> Path:
        return self.data / "inbox"

    @property
    def nominations(self) -> Path:
        return self.inbox / "nominations.json"

    @property
    def proposal(self) -> Path:
        return self.inbox / "proposal.json"

    @property
    def syntax_requests(self) -> Path:
        return self.inbox / "syntax_requests"

    @property
    def state(self) -> Path:
        return self.data / "state"

    @property
    def used_onetime(self) -> Path:
        return self.state / "used_onetime.jsonl"

    @property
    def audit_log(self) -> Path:
        return self.state / "audit.jsonl"

    @property
    def rbac(self) -> Path:
        """Mock RBAC file (see rbac.py). Override with LUCID_WARRANT_RBAC_FILE."""
        return self.rbac_file or self.seed / "rbac_mock.json"

    @property
    def corpus(self) -> Path:
        """Mock search data for practice (see backend.MockBackend). Override with LUCID_WARRANT_CORPUS."""
        return self.corpus_file or self.seed / "mock_corpus.json"
