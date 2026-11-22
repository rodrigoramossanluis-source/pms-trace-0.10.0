# pms-trace

**Python (>=3.9) pip-installable rewrite** of `pms_trace.sh` — solver-driven channel (OTA) → channel manager → PMS reservation and ARI investigator.

- **Zero external runtime dependencies** by default
- Optional **OpenSearch** backend: `pip install 'pms-trace[opensearch]'`
- External self-updating knowledge base (`connector_kb.json`)
- Install: `pip install .` → console script `pms-trace`

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install .
pms-trace --version   # pms-trace 0.10.0
```

---

A log investigator for hotel reservations that travel **channel (OTA) -> channel manager -> PMS**.
Give it a booking reference and a rough time. It searches the logs of each hop, harvests join keys
(trace tokens, reservation UUIDs, PMS ids), follows them across the chain and writes a verdict on
where the booking stopped and why.

It is driven by an external, **self-updating knowledge base** (`connector_kb.json`). Every run makes
the next run faster.

```
ARRIVAL -> INVENTORY -> ACCEPTANCE -> CM_STORE -> PMS_RECEIVE -> CONVERSION -> DELIVERY
```

## What it does

| Stage | What happens |
|---|---|
| Resolve | Channel system from the KB module. If the module is thin: `cm-<prefix>` convention (verified against the live catalog), then catalog name-match as a last resort. |
| Sweep | Cost-ordered: KB apps first, then catalog-only apps, then system-wide `*` last. Hour fan-out around the given time, full-day and +/-1 day drift as fallbacks. |
| Anchor | Locks the real arrival hour from the evidence. Downstream scans use anchor +/-1h. |
| Harvest / follow | Extracts join keys and re-scans each hop with them. Low-selectivity keys (room/rate/hotel codes) are context-only, so other reservations are never counted as evidence. |
| Verdict | Lit / missing / failed / N/A gate per stage. `faultCode` is matched against KB `failure_signatures` (meaning, fix path, terminal flag). Writes `REPORT.md`, `verdict.json`, `NEXT_ACTIONS.md`. |
| Write-back | Records the run in the KB and caches the apps that produced evidence (see below). |

Extra modes (`PT_TASKS` / `PT_GOAL`): `fidelity` (field-level diff OTA vs PMS payload), `ari`
(rate/availability attribution), `guiattrib` (who changed it in the GUI), `bridgeflush` (goal `bridge-flush`),
`kb-test`. Every mode is exercised by the bundled fixture test (see [Test](#test)); the fixtures are
synthetic, so real-estate behaviour still depends on your log formats and KB.

### Task selection (`PT_TASKS`)

`PT_TASKS` is a space-separated list typed by the operator (default `trace`). Tasks run in the order
given. Valid names: `trace`, `fidelity`, `ari`, `guiattrib`, `kb-test`, `bridgeflush`.
`PT_GOAL=<playbook>` selects the tasks for you and refuses with exit 2, before any backend call, if
the playbook's required inputs are missing:

| `PT_GOAL` | Tasks |
|---|---|
| `missing-reservation` | `trace` |
| `delivery-failure` | `trace fidelity` |
| `rate-mismatch`, `overbooking`, `billing-trace` | `ari` |
| `bridge-flush` | `bridgeflush` |
| blank / `custom` | `PT_TASKS` as typed |

An unrecognised `PT_GOAL` is named in the output (`[goal] unknown PT_GOAL=...`) and the run falls
back to `PT_TASKS` as typed.

A name that is not in the list (usually a typo, e.g. `fidelty`) is **not silently dropped**: the run
prints `[tasks] unknown task 'fidelty' (trace|fidelity|kb-test|ari|guiattrib|bridgeflush)`, skips
it, still runs the remaining valid tasks, and exits 0. Check the console for that line if a mode's
output is missing. (`PT_GOAL` input contracts are strict; the free-text task list is lenient.)

### Stage gates (booking path)

```
ARRIVAL -> INVENTORY -> ACCEPTANCE -> CM_STORE -> PMS_RECEIVE -> CONVERSION -> DELIVERY
```

Each gate is classified **independently** from the collected evidence lines; the arrow is report
order, not an enforced sequence. The patterns live in `GATES` inside `pt_verdict` and are meant to
be adapted to your log vocabulary.

| Gate | Lit when an evidence line shows | Notes |
|---|---|---|
| ARRIVAL | booking request / reservation create from the channel | |
| INVENTORY | availability / sellable / inventory check | N/A for channels whose KB module lists it in `skip_gates` (push channels that deliver without an availability step in the booking path) |
| ACCEPTANCE | the channel manager's 200/202 acceptance | |
| CM_STORE | the channel-manager store (`hub-channel-store`, `cmBookingId`) | |
| PMS_RECEIVE | the PMS hub picked the booking up (`pms-hub`, `booking-events`) | |
| CONVERSION | a conversion **fault** (`faultCode`, `ERR_CONVERSION`, `"status":"Error"`) | Inverted: unlit is the healthy case. An `ERR_CONVERSION*` code marks the gate FAILED and is matched against KB `failure_signatures`; if the signature is terminal, later gates with no hits are N/A |
| DELIVERY | PMS-side success (`Success`, `Delivered`, `sent to pms`) | Marked FAILED (never lit as success) whenever a failure code binds to the booking |

The ordering models a pull-style channel (availability checked before the channel manager accepts)
and a PMS-side converter (conversion after the PMS hub receives). Estates where inventory is
decremented after storage, or where the CM converts before pushing, still classify correctly
because gates are independent; only the checklist order in `REPORT.md` differs. The "broken chain"
hint in `NEXT_ACTIONS.md` names the first unlit gate among ARRIVAL, ACCEPTANCE, CM_STORE,
PMS_RECEIVE. The reverse direction (PMS -> CM -> channel rates and availability) is the `ari` task,
with its own gates (SOURCE, INGEST, CM_PERSIST, CHANNEL_PUSH, OTA_ACK).

## Knowledge base: load and write-back

The KB is **not embedded** in the script. It lives in `~/pms_tools/kb/connector_kb.json`
(`PT_KB_DIR` / `PT_KB_FILE`).

**On startup**
1. Loads the live file. Write-back updates it in place, so it is always the newest version.
2. If the live file is missing, the newest `connector_kb*.json` in the folder (never a `.bak`) is promoted.
3. If the live file is not valid JSON, it is kept as `.corrupt.<ts>` and the newest *valid* `.bak` is restored.
4. If you deploy a versioned release (`connector_kb_v0.9.12.json`) that is newer than the live file, it is
   promoted: the old live file becomes `.bak.<ts>.promote`, the new structure wins, and learned data
   (history, apps the new file leaves empty, modules only the live KB knows) is carried over.

**On every live run (hits or zero hits)**
1. Takes an exclusive lock (`fcntl`, works on Linux and macOS). Concurrent detached runs do not lose updates.
2. Re-reads the live KB and writes `connector_kb.json.bak.<UTC-ts>.<pid>`.
3. Merges:
   - a `history` entry: case, ref, confirmed apps, winning patterns, signatures, key kinds;
   - newly discovered **channel-system** apps into `channels.<CODE>.log_apps`;
   - **PMS-side** apps into `pms.<CODE>.delivery_apps`;
   - stub modules for unknown channels;
   - stale-app repairs, but only against a verified catalog listing.
4. Writes to a temp file and renames it atomically.
5. Retention: `PT_KB_BAK_KEEP` (default 20) backups, and `PT_KB_HISTORY_KEEP` (default 50) history entries per module.

Write-back is skipped only in `PT_MODE=plan` (dry run) or when the KB file is not writable.
Structural KB changes (new sections or keys) are the only edits you should make by hand.

## Requirements

- bash >= 4 (macOS: `brew install bash`)
- python3 >= 3.8
- GNU coreutils (`date -d`, `timeout`). macOS: `brew install coreutils`. The g-prefixed tools are shimmed by the test.
- Access to your logs: a local folder, an SSH host, or an S3 bucket (see below).

## Log backend (`backends/`)

`pms_trace.sh` never reads logs directly. Every search goes through two small CLIs that only
**structure** the search: system + component + hour -> files -> matching lines. They don't index,
cache or modify anything. Working reference versions ship in `backends/`:

| Command | Purpose |
|---|---|
| `s3search.py ls <sys> <comp>` | List apps (`<sys>/<comp>`, globs allowed). This is the catalog used for discovery. |
| `s3search.py grep <sys> <comp> <blob> <literal>` | Raw lines containing a literal. `blob` = `YYYY-MM-DD-HH` or `YYYY-MM-DD-*`. |
| `s3search.py query <sys> <comp> <blob> '<expr>'` | JSON-field filter: `s.flowToken = "x" AND s.message LIKE "%y%"`. |
| `s3grep.py find <literal> <bucket-glob> <YYYY-MM-DD>` | Optional secondary / legacy archive. |

Exit code 0 = hits, 1 = no match, 2 = error. `.gz` files are decompressed transparently.

**Where the logs are** (`S3S_ROOT`; the legacy archive uses `S3G_ROOT` with the same schemes):

| `S3S_ROOT` | Transport | Needs |
|---|---|---|
| `/var/log/archive` or `file:///...` | local folder (or a mounted share) | nothing |
| `ssh://user@host/var/log/archive` | one `ssh -n` call per search; `grep` runs **on the remote host**, so only matching lines travel | key-based SSH (`~/.ssh/config` works); POSIX `sh`, `grep`, `gzip`, `awk` on the host |
| `s3://bucket/prefix` | AWS CLI v2; objects are streamed and filtered locally | `aws` configured (`AWS_PROFILE`, `AWS_REGION`) with `s3:ListBucket` and `s3:GetObject` permissions |

**How the files are laid out** (`S3S_LAYOUT`, which must start with `{sys}/{comp}/`):

```
default   {sys}/{comp}/{date}-{hour}*         cm-ota2/booking-gateway/2026-01-10-12.log.gz
dated     {sys}/{comp}/{yyyy}/{mm}/{dd}/{hour}/*
```

`S3S_SSH_OPTS` adds ssh options (default `-o BatchMode=yes -o ConnectTimeout=15`).
`S3S_CALL_LOG` writes an audit line for every call.

**How the tool finds the backend:** `PT_SEARCH_CMD` / `PT_GREP_CMD`, then `PATH`, then `backends/`
next to the script, then `~/pms_tools/backends/`. If you already have your own search scripts with the
same contract, put them on `PATH` (or point the two variables at them) and nothing else changes.

## Quick start

```bash
git clone https://github.com/<you>/pms-trace && cd pms-trace
mkdir -p ~/pms_tools/kb && cp connector_kb.json ~/pms_tools/kb/
export S3S_ROOT=ssh://ops@loghost/var/log/archive      # or a local path, or s3://bucket/prefix
PT_CASE=case-001 PT_REF=OTA2-1000000001 PT_CHANNEL=OTA2 PT_PMS=CLOUDPMS \
PT_AT=2026-01-10T12:00:00 PT_DETACH=0 bash pms_trace.sh
# results: ~/pms_tools/runs/case-001/{REPORT.md,verdict.json,NEXT_ACTIONS.md,join_keys.tsv,ledger.tsv,raw/}
```

By default the run detaches, so it survives SSH drops. Re-running resumes from finished stages.
You can also run the whole tool *on* the log host (copy the repo there and use a local `S3S_ROOT`).

## Test

```bash
bash ./run_mock_test.sh                              # Linux
cd ~/pms-trace && /opt/homebrew/bin/bash ./run_mock_test.sh   # macOS (Intel: /usr/local/bin/bash)
```

No network access is needed. It builds a synthetic fixture log archive and runs the **real** backends
and the real tool against it, one scenario per behaviour, and asserts on the files each mode writes.

**trace** (booking delivery path)
1. Delivered booking: all five gates lit, keys harvested, other reservations at the property excluded, KB write-back checked.
2. Re-run from the written-back KB: same gates, KB converged (no new apps learned).
3. **Discovery:** the channel module is empty, the system is found from the catalog, and the KB learns the apps.
4. **Re-learn:** starts from the learned KB and needs fewer backend calls (76 vs 100 in the fixture).
5. Conversion failure: the terminal signature is recognised and DELIVERY is not reported as success.
6. Backend contract: `ls`/`grep`/`query`, `.gz` files, globs, no-match and error exit codes, input validation.
7. Full trace over `ssh://` and over `s3://` (through local `ssh`/`aws` stand-ins), with results checked against the local run.

**Other modes**

| Scenario | Mode / goal | What is asserted |
|---|---|---|
| fidelity-files | `fidelity` | Offline OTA + PMS documents: all seven field rows PRESERVED, no verdicts. |
| fidelity-nodocs | `fidelity` | No documents and nothing to fetch: says "docs unavailable", writes no report. |
| trace-fidelity | `trace fidelity` | echoToken / hubBookingUuid harvested by trace, both documents auto-fetched, verdicts CONFIG_IGNORED, TAX_BREAKDOWN_DROPPED, FIELD_DROPPED. |
| ari-manual | `ari` via `PT_GOAL=rate-mismatch` | Backward sweep stops at the target hour, SET line = TARGET_EVENT, actor email resolved through gui-gateway, gates SOURCE/CM_PERSIST/CHANNEL_PUSH/OTA_ACK, INGEST N/A, GOAL_VERDICT = VALUE_ATTRIBUTED, push app written back to the KB. |
| ari-pms | `ari` | PMS-push origin: SET found in region rb after ra is empty, bridge ingest leg auto-selected, CHANNEL_PUSH reported missing (no channel given). |
| goal-contract | `PT_GOAL=overbooking` | Missing anchors: refuses with exit 2, lists them, makes zero backend calls. |
| guiattrib | `guiattrib` | link-api burst -> flowToken -> actor email + client IP, mutation, session read, entity-api audit write in GUIACTORS.md. |
| kb-test | `kb-test` | Every KB module reported; fixture apps LIVE, absent apps STALE?; failure signatures counted. |
| bridgeflush | `bridgeflush` via `PT_GOAL=bridge-flush` | Flush trigger hour found, failing sub-leg isolated, culprit room rate derived, token chain, all 8 gates, fail-fast single-blocker, actor + origin. |
| unknown-task | `PT_TASKS="not-a-real-task kb-test"` | The invalid name is reported with the valid list, the valid task still runs, exit 0. |

Exit 0 = PASS (96 checks across 18 scenarios), exit 1 = a check failed. A pass proves the logic
against these fixtures; it is not evidence about your own logs until you run the tool on them.

Run it with no options in a terminal and it asks three y/N questions. Flags (or env vars) skip the
questions; anything unset defaults to off when there is no terminal (CI, pipes):

| Flag | Env | Effect |
|---|---|---|
| `-s`, `--save [FILE]` | `SAVE=1`, `SAVE_PATH=FILE` | Save the full console output. Default `./test-reports/FULL_TEST_REPORT_<UTC>.txt` in the folder you run from. Local paths are redacted (`<sandbox>`, `<repo>`, `~`), so the file is safe to share. |
| `-v`, `--verbose` | `VERBOSE=1` | Per scenario: every backend call plus the files the mode wrote (system resolution, harvested keys, `verdict.json`, `REPORT.md`, `NEXT_ACTIONS.md`, `fidelity_report.txt`, `ari_evidence.tsv`, `GUIACTORS.md`, `GOAL_VERDICT.md`, bridge-flush analysis). |
| `-k`, `--keep` | `KEEP=1` | Keep the throw-away sandbox (raw run folders) for inspection. |
| `-y`, `--no-prompt` | | Never ask. |

```bash
bash ./run_mock_test.sh --save --verbose      # full evidence report in ./test-reports/
```

A complete verbose run is included as [docs/sample-test-report.txt](docs/sample-test-report.txt).

## Adapting to your estate

All names in this repository (`cm-ota2`, `pms-hub`, `OTA1..4`, `CLOUDPMS`, ...) are **generic
placeholders**. Map them to your systems in `connector_kb.json`. Gate regexes live in `pt_verdict`.

## Privacy warning

A KB that has been used on live data accumulates real booking refs and case ids in its `history`
arrays. So does everything under `runs/`. **Never commit them.** `.gitignore` covers runs and backups.
Before contributing KB changes upstream, empty the `history` arrays and replace real identifiers.


## OpenSearch backend (optional)

The default transport is still the zero-dependency `backends/s3search.py` /
`s3grep.py` pair (local directory, `ssh://`, or `s3://`).

An **OpenSearch** transport is available as an optional extra. Core stays
installable with no third-party packages.

```bash
pip install 'pms-trace[opensearch]'   # pulls opensearch-py
# or: pip install opensearch-py>=2.4
```

### Selection

| `PT_BACKEND` | Behaviour |
|---|---|
| `auto` (default) | OpenSearch when `OS_URL` is set, otherwise file/S3/SSH |
| `opensearch` | Force OpenSearch (requires `OS_URL`) |
| `s3search` / `file` / `s3` / `ssh` | Force the classic backend |

### Configuration

```bash
export PT_BACKEND=opensearch          # or leave as auto and just set OS_URL
export OS_URL=https://opensearch.example:9200
export OS_INDEX_PATTERN='logs-{sys}-{comp}-*'   # placeholders: {sys} {comp} {date} {yyyy} {mm} {dd} {hour}
export OS_TIME_FIELD=@timestamp
export OS_MESSAGE_FIELD=message
export OS_SYS_FIELD=system
export OS_COMP_FIELD=component
# auth (pick one)
export OS_USER=admin
export OS_PASSWORD=secret
# or: export OS_API_KEY='...'
export OS_VERIFY_CERTS=1              # set 0 to disable TLS verify
```

`list` / `grep` / `query` keep the same safety refusals as the file backend
(empty pattern, weak numerics, pms-hub full-day). `legacy_find` is not
supported on OpenSearch and returns a clear error.

## Versioning

Package version has a **single source of truth**: `pms_trace.__version__` in
`src/pms_trace/__init__.py`. `pyproject.toml` reads it dynamically. Do not
re-declare the version string in `setup.cfg`, scripts, or other modules.

## License

MIT - see [LICENSE](LICENSE).
