"""Mode foundation for pms-trace 0.11+.

A *mode* is a named investigation graph: required anchors, ordered stages,
optional branch rules, and which task implementation runs the work.

0.11 keeps existing task modules (trace, ari, ...) and routes goals through
this registry so later patches can add modes without forking the runner.

Verdict *meanings* stay observational in 0.11: reports and verdict.json record
what was reached, skipped, and seen. Proven outcome taxonomies land in later
releases once operators validate them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class StageSpec:
    """One named stage in a mode graph.

    ``stage_id`` is stable (snake/upper identifiers). Display labels belong in
    report renderers, not here.
    """

    stage_id: str
    description: str = ""
    # If set, stage is only meaningful when this prior stage reached a truthy key
    # in the run ledger / mode state. Empty = always eligible.
    requires: Tuple[str, ...] = ()
    # Observational only in 0.11 — branch engines arrive with discrepancy/lifecycle modes.
    on_empty: str = "record"  # record | skip_downstream | branch:*


@dataclass(frozen=True)
class ModeSpec:
    """Declarative investigation mode.

    ``task_ids`` maps to existing task handlers in cli (trace, ari, ...).
    ``required_anchors`` use the PT_* env/CLI names already in the product.
    """

    mode_id: str
    task_ids: Tuple[str, ...]
    required_anchors: Tuple[str, ...] = ()
    optional_anchors: Tuple[str, ...] = ()
    stages: Tuple[StageSpec, ...] = ()
    description: str = ""
    # When true, mode is a thin alias over tasks (current goals). When false,
    # a dedicated mode runner may take over in a later release.
    legacy_task_bridge: bool = True


class ModeRegistry:
    """In-process registry of modes. Single source for goal → tasks + anchors."""

    def __init__(self) -> None:
        self._modes: Dict[str, ModeSpec] = {}

    def register(self, mode: ModeSpec) -> None:
        if mode.mode_id in self._modes:
            raise ValueError(f"mode already registered: {mode.mode_id}")
        self._modes[mode.mode_id] = mode

    def get(self, mode_id: str) -> Optional[ModeSpec]:
        return self._modes.get(mode_id)

    def require(self, mode_id: str) -> ModeSpec:
        m = self.get(mode_id)
        if m is None:
            known = ", ".join(sorted(self._modes)) or "(none)"
            raise KeyError(f"unknown mode '{mode_id}'; known: {known}")
        return m

    def ids(self) -> List[str]:
        return sorted(self._modes)

    def goal_specs(self) -> Dict[str, Tuple[List[str], List[str]]]:
        """Compatibility shape used by cli.GOAL_SPECS consumers."""
        out: Dict[str, Tuple[List[str], List[str]]] = {}
        for mid, m in self._modes.items():
            if mid == "custom":
                continue
            out[mid] = (list(m.task_ids), list(m.required_anchors))
        return out

    def all(self) -> Sequence[ModeSpec]:
        return tuple(self._modes[k] for k in sorted(self._modes))
