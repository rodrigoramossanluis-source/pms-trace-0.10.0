# pms-trace

**Python (>=3.9) pip-installable rewrite** of `pms_trace.sh` — solver-driven channel (OTA) → channel manager → PMS reservation and ARI investigator.

- **Zero external runtime dependencies** by default
- Optional **OpenSearch** backend: `pip install 'pms-trace[opensearch]'`
- **Mode foundation (0.11):** goals resolve through a registry; `ari-discrepancy` records observational stage history (SENT→…→DERIVATION) without changing verdict meanings
- External self-updating knowledge base (`connector_kb.json`)
- Install: `pip install .` → console script `pms-trace`

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install .
pms-trace --version   # pms-trace 0.11.0
```

See [RELEASE_NOTES_0.11.0.md](RELEASE_NOTES_0.11.0.md) and [CHANGELOG.md](CHANGELOG.md) for the full 0.11 delta.

---

*(Body of the README is unchanged from 0.10.0 — stage gates, KB, backends, OpenSearch, tests — and remains valid.)*

For the complete operator guide (gates, KB write-back, mock harness, OpenSearch config), use the 0.10 README sections in git history or pull the full local package README from the release tree.
