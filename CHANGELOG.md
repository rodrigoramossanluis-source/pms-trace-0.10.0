# Changelog

All notable changes to this project are documented here.
Version lives in a single place: `src/pms_trace/__version__`.

## [0.11.0] - 2026-11-23

### Added
- **Mode foundation** (`pms_trace.modes`): `ModeSpec`, `StageSpec`, `ModeRegistry`.
- Default mode catalog: existing goals plus `ari-discrepancy` (stages declared; still bridges to `ari` task).
- `tests/test_modes.py`, `REVIEW_0.11.md` (operator checklist).
- Mode runner helpers: `state/mode_plan.json`, `state/stage_history.jsonl`, MODE PLAN section on REPORT.md.
- Observational **ari-discrepancy** stage assessment (`modes/discrepancy.py` → `state/discrepancy_stages.json`).


### Changed
- CLI `GOAL_SPECS` derived from the mode registry (single source of truth).
- Version **0.11.0**.

### Policy
- Verdicts remain **observational** (full history in REPORT / verdict.json).
- Proven outcome taxonomies deferred to later releases after operator review.

All notable changes to this project are documented here.
Version lives in a single place: `src/pms_trace/__version__`.

## [0.10.0] - 2026-11-20

### Added
- Optional **OpenSearch** log backend (`PT_BACKEND=opensearch` or `auto` when `OS_URL` is set).
  - Same `list` / `grep` / `query` contract as the file/S3/SSH backend.
  - Safety refusals (empty pattern, weak numerics, pms-hub full-day) preserved.
  - Install with `pip install 'pms-trace[opensearch]'` (pulls `opensearch-py`).
  - Core package remains zero runtime dependencies.
- `create_backend()` factory in `pms_trace.backend` — selects transport from env.
- Version consistency tests (`tests/test_version_and_factory.py`).

### Changed
- **Single source of truth for version**: only `src/pms_trace/__init__.py`.
  - `pyproject.toml` uses setuptools dynamic version (`attr = "pms_trace.__version__"`).
  - `setup.cfg` no longer declares a version (compatibility shim only).
  - `trace.py` no longer hard-codes `PT_VERSION` (was drifted at 0.9.7).
- Packaging metadata expanded (classifiers, optional-dependencies, pytest config).

### Fixed
- Version string drift between `__init__.py`, packaging files, and report headers.

## [0.9.9] - 2026-11-20

Initial pip-installable Python rewrite of `pms_trace.sh`:
- Zero external runtime dependencies, Python >= 3.9.
- Modules: cli, kb, backend, trace, verdict, fidelity, tasks (ari, guiattrib, bridgeflush, kb_test).
- External self-updating `connector_kb.json` with fcntl-locked write-back.
- Parity harness `run_mock_test_py.sh` (18 scenarios / 96 checks).
- 40+ unit tests.
