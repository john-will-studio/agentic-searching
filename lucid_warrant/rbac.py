"""Entitlements: which parameter 1 and parameter 2 values the user may use.

Everything else (GUI dropdown and pills, warrant signing, executor re-checks)
goes through `get_rbac()`. To connect the real RBAC service, implement the
`RbacProvider` methods in a new class and return it from `get_rbac()`.

`MockRbacProvider` reads a JSON file and re-reads it on every call, so edits
to the mock take effect immediately and the executor's checks are "live".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Paths


class EntitlementError(Exception):
    """The user isn't entitled to a parameter value or combination."""


@dataclass(frozen=True)
class Option:
    value: str
    label: str


class RbacProvider(Protocol):
    def parameter_1_options(self) -> list[Option]:
        """Search authorizations the current user may use."""

    def parameter_2_options(self, parameter_1: str) -> list[Option]:
        """Visibility attributes the user may combine with this parameter 1."""

    def check(self, parameter_1: str, parameter_2: list[str]) -> None:
        """Raise EntitlementError unless this exact combination is allowed now."""


class MockRbacProvider:
    """JSON-backed stand-in for the entitlement service.

    File shape:
      parameter_1: [{"value": "0012", "label": "..."}]
      parameter_2: [{"value": "ATTR_1000", "label": "..."}]
      parameter_2_by_parameter_1 (optional): {"0012": ["ATTR_1000", ...]}
        If absent, or a parameter 1 value has no entry, every parameter 2 option is allowed.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        return json.loads(self.path.read_text("utf-8"))

    @staticmethod
    def _options(items) -> list[Option]:
        return [Option(str(i["value"]), str(i.get("label") or i["value"])) for i in items]

    def subject(self) -> str:
        doc = self._load()
        return f"{doc.get('subject_id', '?')} ({doc.get('entitlement_version', '?')})"

    def parameter_1_options(self) -> list[Option]:
        return self._options(self._load()["parameter_1"])

    def parameter_2_options(self, parameter_1: str) -> list[Option]:
        doc = self._load()
        options = self._options(doc["parameter_2"])
        allowed = doc.get("parameter_2_by_parameter_1", {}).get(parameter_1)
        return options if allowed is None else [o for o in options if o.value in allowed]

    def check(self, parameter_1, parameter_2) -> None:
        if not isinstance(parameter_1, str) or parameter_1 not in {o.value for o in self.parameter_1_options()}:
            raise EntitlementError(f"parameter 1 {parameter_1!r} is not authorized for this user")
        if not isinstance(parameter_2, list) or not parameter_2:
            raise EntitlementError("parameter 2 needs at least one visibility attribute")
        if len(set(parameter_2)) != len(parameter_2):
            raise EntitlementError("parameter 2 has duplicates")
        allowed = {o.value for o in self.parameter_2_options(parameter_1)}
        denied = [p for p in parameter_2 if p not in allowed]
        if denied:
            raise EntitlementError(
                f"parameter 2 values not authorized with parameter 1 {parameter_1}: {', '.join(map(str, denied))}"
            )


def get_rbac(paths: Paths) -> RbacProvider:
    return MockRbacProvider(paths.rbac)
