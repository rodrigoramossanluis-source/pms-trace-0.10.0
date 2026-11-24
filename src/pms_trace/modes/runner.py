"""Mode plan + observational stage history (0.11).

Writes machine-readable mode context beside existing task output without
changing task behaviour or verdict meanings.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .base import ModeSpec, StageSpec


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_mode_plan(
    run_dir: Path,
    mode: ModeSpec,
    *,
    anchors_present: Optional[Dict[str, bool]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write ``state/mode_plan.json`` describing the selected mode graph."""
    run_dir = Path(run_dir)
    state = run_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    plan = {
        "mode_id": mode.mode_id,
        "description": mode.description,
        "task_ids": list(mode.task_ids),
        "required_anchors": list(mode.required_anchors),
        "optional_anchors": list(mode.optional_anchors),
        "legacy_task_bridge": mode.legacy_task_bridge,
        "stages": [
            {
                "stage_id": s.stage_id,
                "description": s.description,
                "requires": list(s.requires),
                "on_empty": s.on_empty,
                "status": "planned",
            }
            for s in mode.stages
        ],
        "anchors_present": anchors_present or {},
        "written_at": _utc_now(),
    }
    if extra:
        plan["extra"] = extra
    path = state / "mode_plan.json"
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return path


def append_stage_history(
    run_dir: Path,
    *,
    mode_id: str,
    events: Sequence[Dict[str, Any]],
) -> Path:
    """Append observational stage events to ``state/stage_history.jsonl"."""
    run_dir = Path(run_dir)
    state = run_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "stage_history.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for ev in events:
            row = {"mode_id": mode_id, "ts": _utc_now(), **ev}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def record_mode_start(run_dir: Path, mode: ModeSpec) -> None:
    """Mark all declared stages as started (observational; tasks still own evidence)."""
    events: List[Dict[str, Any]] = [
        {
            "stage_id": s.stage_id,
            "status": "bridged" if mode.legacy_task_bridge else "running",
            "note": (
                "legacy task bridge — evidence produced by task handlers; "
                "stage semantics recorded for history only"
                if mode.legacy_task_bridge
                else "mode runner owns this stage"
            ),
        }
        for s in mode.stages
    ]
    if not events:
        events = [
            {
                "stage_id": "_mode",
                "status": "bridged",
                "note": f"mode {mode.mode_id} has no declared stages; tasks={list(mode.task_ids)}",
            }
        ]
    append_stage_history(run_dir, mode_id=mode.mode_id, events=events)


def append_mode_section_to_report(run_dir: Path, mode: ModeSpec) -> None:
    """Append a short mode/stage plan section to REPORT.md if stages exist."""
    if not mode.stages:
        return
    run_dir = Path(run_dir)
    lines = [
        "",
        f"## MODE PLAN (`{mode.mode_id}`)",
        f"- {mode.description or '(no description)'}",
        f"- tasks: {', '.join(mode.task_ids)}",
        f"- legacy_task_bridge: {mode.legacy_task_bridge}",
        "",
        "| stage | description | status |",
        "|---|---|---|",
    ]
    for s in mode.stages:
        status = "bridged (observational)" if mode.legacy_task_bridge else "planned"
        desc = (s.description or "").replace("|", "/")
        lines.append(f"| `{s.stage_id}` | {desc} | {status} |")
    lines.append("")
    lines.append(
        "_Stage outcomes remain observational in 0.11; proven verdict labels arrive in later releases._"
    )
    lines.append("")
    report = run_dir / "REPORT.md"
    with report.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
