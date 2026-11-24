"""Observational ARI discrepancy stage engine (0.11).

Maps the generic discrepancy graph onto whatever the existing ``ari`` task
produced (raw artifacts + anchors). Does **not** change verdict meanings or
replace ``pt_ari``; it only records structured stage history for operators.

Stage ids (stable, generic):
  DISCOVERY → SENT → RECEIVED → OTA_FETCH → ACTOR → DERIVATION
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import ModeSpec
from .runner import append_stage_history

DISCREPANCY_STAGE_IDS: Tuple[str, ...] = (
    "DISCOVERY",
    "SENT",
    "RECEIVED",
    "OTA_FETCH",
    "ACTOR",
    "DERIVATION",
)

_RAW_HINTS: Dict[str, Tuple[str, ...]] = {
    "SENT": (
        "t_ari_push_",
        "t_ari_push_lgcy_",
        "t_ari_bridge_values_",
    ),
    "RECEIVED": (
        "t_ari_ingest_",
        "t_ari_bridge_",
    ),
    "OTA_FETCH": (
        "t_ari_booking_xml_",
        "t_ari_series_",
    ),
    "ACTOR": (
        "t_ari_attribute_",
        "t_guiattrib_",
        "f_gui_",
    ),
    "DERIVATION": (
        "t_ari_filter_",
        "t_ari_billing_",
    ),
}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _raw_files(run_dir: Path) -> List[Path]:
    raw = Path(run_dir) / "raw"
    if not raw.is_dir():
        return []
    return sorted(p for p in raw.iterdir() if p.is_file())


def _file_has_data(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("[fetch]") or s.startswith("#"):
            continue
        return True
    return False


def _match_hints(files: Sequence[Path], prefixes: Sequence[str]) -> List[str]:
    hits: List[str] = []
    for p in files:
        for pref in prefixes:
            if p.name.startswith(pref):
                hits.append(p.name)
                break
    return hits


def assess_discrepancy_stages(
    run_dir: Path,
    *,
    anchors_present: Optional[Dict[str, bool]] = None,
    mode_id: str = "ari-discrepancy",
) -> Dict[str, Any]:
    """Build an observational stage assessment (no proven outcomes).

    Status values:
      - ``planned``: stage is part of the graph; no evidence yet
      - ``anchored``: required inputs for this stage look present
      - ``evidence``: at least one matching raw artifact has data lines
      - ``empty``: matching artifacts exist but no data lines
      - ``skipped``: not evaluable yet (upstream missing and no artifacts)
    """
    anchors_present = anchors_present or {}
    files = _raw_files(run_dir)
    stages: List[Dict[str, Any]] = []

    discovery_anchored = bool(
        anchors_present.get("PT_CHANNEL")
        or anchors_present.get("PT_PROPERTY_ID")
        or anchors_present.get("PT_HOTEL_UUID")
    )
    stages.append(
        {
            "stage_id": "DISCOVERY",
            "status": "anchored" if discovery_anchored else "planned",
            "note": "route/context anchors"
            + (" present" if discovery_anchored else " incomplete"),
            "artifacts": [],
        }
    )

    for stage_id in DISCREPANCY_STAGE_IDS[1:]:
        prefixes = _RAW_HINTS.get(stage_id, ())
        matched = _match_hints(files, prefixes)
        with_data = [n for n in matched if _file_has_data(Path(run_dir) / "raw" / n)]
        if with_data:
            status = "evidence"
            note = f"{len(with_data)} artifact(s) with data"
        elif matched:
            status = "empty"
            note = f"{len(matched)} artifact(s), no data lines"
        else:
            if stage_id == "SENT" and (
                anchors_present.get("PT_STAY_DATES") or anchors_present.get("PT_TARGET_VALUES")
            ):
                status = "anchored"
                note = "stay/target anchors present; no SENT artifacts yet"
            elif stage_id == "ACTOR" and (
                anchors_present.get("PT_AT") or anchors_present.get("PT_FROM")
            ):
                status = "anchored"
                note = "time window present; no actor artifacts yet"
            else:
                status = "planned"
                note = "no matching artifacts yet"
        stages.append(
            {
                "stage_id": stage_id,
                "status": status,
                "note": note,
                "artifacts": matched[:20],
            }
        )

    return {
        "mode_id": mode_id,
        "assessed_at": _utc_now(),
        "stages": stages,
        "policy": "observational-only; no proven outcome labels in 0.11",
    }


def write_discrepancy_assessment(
    run_dir: Path,
    assessment: Dict[str, Any],
) -> Path:
    """Persist assessment JSON and append stage_history events."""
    run_dir = Path(run_dir)
    state = run_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "discrepancy_stages.json"
    path.write_text(json.dumps(assessment, indent=2) + "\n", encoding="utf-8")

    events = [
        {
            "stage_id": s["stage_id"],
            "status": s["status"],
            "note": s.get("note", ""),
            "source": "discrepancy_assess",
        }
        for s in assessment.get("stages", [])
    ]
    append_stage_history(
        run_dir,
        mode_id=str(assessment.get("mode_id") or "ari-discrepancy"),
        events=events,
    )
    return path


def append_discrepancy_section(run_dir: Path, assessment: Dict[str, Any]) -> None:
    """Append an observational discrepancy table to REPORT.md."""
    run_dir = Path(run_dir)
    lines = [
        "",
        f"## DISCREPANCY STAGES (`{assessment.get('mode_id', '')}`)",
        "",
        "| stage | status | note |",
        "|---|---|---|",
    ]
    for s in assessment.get("stages", []):
        note = str(s.get("note") or "").replace("|", "/")
        lines.append(f"| `{s['stage_id']}` | {s['status']} | {note} |")
    lines.append("")
    lines.append(
        "_Observational only — status reflects anchors/artifacts, not a final root-cause label._"
    )
    lines.append("")
    with (run_dir / "REPORT.md").open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def run_discrepancy_observation(
    run_dir: Path,
    mode: ModeSpec,
    *,
    anchors_present: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """Full observational pass for discrepancy-capable modes."""
    assessment = assess_discrepancy_stages(
        run_dir,
        anchors_present=anchors_present,
        mode_id=mode.mode_id,
    )
    write_discrepancy_assessment(run_dir, assessment)
    append_discrepancy_section(run_dir, assessment)
    return assessment
