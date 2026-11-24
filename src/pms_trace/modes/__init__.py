"""Investigation modes — declarative graphs on top of shared backends/KB/verdict."""

from .base import ModeRegistry, ModeSpec, StageSpec
from .catalog import DEFAULT_REGISTRY, build_default_registry
from .discrepancy import assess_discrepancy_stages, run_discrepancy_observation
from .runner import (
    append_mode_section_to_report,
    append_stage_history,
    record_mode_start,
    write_mode_plan,
)

__all__ = [
    "ModeRegistry",
    "ModeSpec",
    "StageSpec",
    "DEFAULT_REGISTRY",
    "build_default_registry",
    "write_mode_plan",
    "append_stage_history",
    "record_mode_start",
    "append_mode_section_to_report",
    "assess_discrepancy_stages",
    "run_discrepancy_observation",
]
