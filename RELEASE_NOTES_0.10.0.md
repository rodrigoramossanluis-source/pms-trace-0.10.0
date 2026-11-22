# pms-trace 0.10.0 — release notes

## What this is

Standalone, pip-installable Python package that investigates OTA → channel-manager → PMS
reservation and ARI flows from logs. Behavioural parity with the original `pms_trace.sh`
tool, plus an optional OpenSearch transport.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install .

# Optional OpenSearch support
pip install 'pms-trace[opensearch]'
```

```bash
pms-trace --version
# pms-trace 0.10.0
```

## Backends

| Mode | When | What it searches |
|------|------|------------------|
| **s3search** (default) | `PT_BACKEND` unset/`auto` and no `OS_URL`, or `PT_BACKEND=s3search` | Raw log **blobs** via local dir, `ssh://`, or `s3://` (`S3S_ROOT`) |
| **OpenSearch** | `OS_URL` set with `auto`, or `PT_BACKEND=opensearch` | Indexed documents in an OpenSearch cluster |

Both expose the same investigation API (`list` / `grep` / `query`). Investigation logic is unchanged.

### OpenSearch quick config

```bash
export PT_BACKEND=opensearch
export OS_URL=https://opensearch.example:9200
export OS_INDEX_PATTERN='logs-{sys}-{comp}-*'
export OS_TIME_FIELD=@timestamp
export OS_MESSAGE_FIELD=message
export OS_USER=admin OS_PASSWORD=secret
# or: export OS_API_KEY='...'
```

## Knowledge base

```bash
mkdir -p "$HOME/pms_tools/kb"
cp -n connector_kb.json "$HOME/pms_tools/kb/connector_kb.json"
```

Override with `PT_KB_DIR` / `PT_KB_FILE`. Live runs update the KB under an exclusive lock.

## Quick start (file/S3/SSH backend)

```bash
export S3S_ROOT=/path/to/log/archive

pms-trace \
  --case case-001 \
  --ref OTA2-1000000001 \
  --channel OTA2 \
  --pms CLOUDPMS \
  --at 2026-01-10T12:00:00 \
  --tasks trace
```

Results under `~/pms_tools/runs/<CASE>/` (`REPORT.md`, `verdict.json`, `NEXT_ACTIONS.md`, …).

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
# optional full parity harness (needs bash + fixtures layout):
# bash ./run_mock_test_py.sh -y
```

## Versioning

Single source of truth: `src/pms_trace/__init__.py` → `__version__`.  
Do not re-declare the version in `setup.cfg` or other modules.

## Contents of this release tarball / zip

- `src/pms_trace/` — package source
- `backends/` — reference s3search/s3grep scripts
- `tests/` — unit tests
- `connector_kb.json` — seed KB (sanitize history before publishing if it ever held live data)
- `pyproject.toml`, `setup.cfg`, `LICENSE`, `README.md`, `CHANGELOG.md`
- `run_mock_test_py.sh` — end-to-end parity harness

## Breaking changes

None for 0.9.9 → 0.10.0. Existing env vars and stage/verdict contracts are unchanged.
New env vars are additive (`PT_BACKEND`, `OS_*`).
