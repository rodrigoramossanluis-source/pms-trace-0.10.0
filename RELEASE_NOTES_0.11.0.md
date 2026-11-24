# pms-trace 0.11.0 — release notes

## What this is

Same product as 0.10.0 (pip-installable OTA → CM → PMS investigator), plus a **mode foundation**
so goals and stage graphs can grow without forking the tool.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
pms-trace --version   # pms-trace 0.11.0

# Optional OpenSearch
pip install 'pms-trace[opensearch]'
```

## What changed vs 0.10.0

### Added
- Mode registry (`pms_trace.modes`): `ModeSpec`, stage lists, goal → tasks + anchors
- Mode plan artifacts: `state/mode_plan.json`, `state/stage_history.jsonl`, MODE PLAN on REPORT.md
- Observational **ari-discrepancy** assessment: `state/discrepancy_stages.json` + REPORT section
  (statuses: planned / anchored / evidence / empty — not proven root-cause labels)
- `REVIEW_0.11.md`, `SANITIZE_SCAN_0.11.txt`

### Unchanged (by design)
- Task implementations (`trace`, `ari`, `fidelity`, …)
- Verdict field meanings and synthesizer behaviour
- Backend contracts (file / S3 / SSH / optional OpenSearch)
- Zero default runtime dependencies

### Goals / modes
Existing 0.10 goals remain. New / explicit:
- `ari-discrepancy` — same `ari` task bridge + discrepancy stage observation
- `rate-mismatch` — still maps to `ari`; also runs discrepancy observation

## Upgrade notes
- Drop-in for 0.10 installs: `pip install .` from this tree
- No required env var changes
- New files under `state/` appear when a known goal/mode is selected

## Policy
Verdicts stay **observational** in 0.11. Proven outcome taxonomies can land in later
releases after operator validation.
