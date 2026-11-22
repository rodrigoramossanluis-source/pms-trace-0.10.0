"""Unit tests for pms_trace.trace."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pms_trace.backend import BackendResult, StageRunner  # noqa: E402
from pms_trace.trace import (  # noqa: E402
    TraceContext,
    pt_anchor,
    pt_crossref,
    pt_fallback_icase,
    pt_follow,
    pt_harvest,
    pt_next_actions,
    pt_preflight,
    pt_resolve,
    pt_sweep,
    pt_trace,
)


class FakeBackend:
    def __init__(self, responses: Dict[str, str] | None = None, cat: List[str] | None = None) -> None:
        self.responses = responses or {}
        self._cat = cat or []
        self.calls: List[tuple] = []

    def list(self, sys_name: str, comp: str = "*") -> BackendResult:
        if sys_name == "*":
            lines = list(self._cat)
        else:
            lines = [a for a in self._cat if a.startswith(f"{sys_name}/")]
        return BackendResult(stdout="\n".join(lines) + ("\n" if lines else ""), stderr="", rc=0 if lines else 1)

    def grep(self, app: str, blob: str, pattern: str) -> BackendResult:
        self.calls.append((app, blob, pattern))
        for key, content in self.responses.items():
            if key in app or key in pattern or key == "*":
                return BackendResult(stdout=content, stderr="", rc=0)
        return BackendResult(stdout="", stderr="", rc=1)


class TestTrace(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "run1"
        self.run_dir.mkdir(parents=True)
        self.kb_data = {
            "channel_codes": {"codes": {"OTA1": {"module": "OTA1"}}},
            "channels": {
                "OTA1": {
                    "log_apps": {
                        "reservation_inbound": ["cm-ota1/booking-api", "cm-ota1/stale-app"],
                        "ari_outbound": [],
                        "other_apps": [],
                    }
                }
            },
            "low_selectivity_kinds": ["pmsType", "hubPmsPropertyUuid", "roomCode", "ratePlanCode", "sourceAgentCode", "hotelCode"],
            "failure_signatures": {
                "ERR_CONVERSION_UNMAPPED_ROOM_RATE": {
                    "meaning": "Room or rate not mapped",
                    "fix_path": "Map in PMS-HUB",
                }
            },
        }
        self.kb_path = self.root / "kb.json"
        self.kb_path.write_text(json.dumps(self.kb_data), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ctx(self, backend: FakeBackend | None = None, **kwargs: Any) -> TraceContext:
        b = backend or FakeBackend()
        r = StageRunner(self.run_dir)
        return TraceContext(
            ref=kwargs.pop("ref", "OTA1-ABC123"),
            run_dir=self.run_dir,
            kb_path=self.kb_path,
            kb_data=self.kb_data,
            backend=b,  # type: ignore[arg-type]
            runner=r,
            at=kwargs.pop("at", "2026-11-01T10:15:00"),
            **kwargs,
        )

    def test_resolve_authoritative_and_stale(self) -> None:
        b = FakeBackend(cat=["cm-ota1/booking-api", "cm-ota1/extra-app"])
        ctx = self._ctx(backend=b)
        pt_resolve(ctx)
        self.assertEqual(ctx.chan_sys, "cm-ota1")
        self.assertEqual(ctx.chan_apps, ["cm-ota1/booking-api"])
        self.assertEqual(ctx.stale_apps, ["cm-ota1/stale-app"])
        self.assertEqual(ctx.catalog_only, ["cm-ota1/extra-app"])
        tsv = (self.run_dir / "resolve.tsv").read_text(encoding="utf-8")
        self.assertIn("sys_from_kb_apps|OTA1|cm-ota1", tsv)
        self.assertIn("kb_stale|OTA1|cm-ota1/stale-app", tsv)

    def test_preflight_ok_and_fail(self) -> None:
        b = FakeBackend(responses={"cm-ota1": "2026-11-01T10:00:00 booking ok\n"})
        ctx = self._ctx(backend=b)
        rc = pt_preflight(ctx, ["cm-ota1", "pms-hub"])
        self.assertEqual(rc, 1)
        report = (self.run_dir / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("preflight control cm-ota1: OK", report)
        self.assertIn("PREFLIGHT FAIL pms-hub", report)

    def test_sweep_and_hours_refresh(self) -> None:
        b = FakeBackend(responses={"cm-ota1/booking-api": "2026-11-01T10:05:00 OTA1-ABC123 received\n"})
        ctx = self._ctx(backend=b)
        rc = pt_sweep(ctx, "kb", ["cm-ota1/booking-api"], fanout=0)
        self.assertEqual(rc, 1)
        out = self.run_dir / "raw" / "t_kb_cm-ota1_booking-api_2026-11-01-10_OTA1-ABC123.out"
        self.assertTrue(out.exists())
        pt_anchor(ctx)
        self.assertEqual(ctx.anchor, "2026-11-01-10")

    def test_harvest_and_follow(self) -> None:
        b = FakeBackend()
        ctx = self._ctx(backend=b)
        ctx.chan_sys = "cm-ota1"
        raw = self.run_dir / "raw"
        (raw / "t_kb_cm-ota1_booking-api_2026-11-01-10_OTA1-ABC123.out").write_text(
            '2026-11-01T10:05:00 OTA1-ABC123 flowToken="11111111-2222-3333-4444-555555555555" pmsType="MEWS"\n',
            encoding="utf-8",
        )
        # Co-tenant guard: _fd_ file without ref must be ignored
        (raw / "t_lad_fd_app_2026-11-01_KEY.out").write_text(
            '2026-11-01T10:05:00 OTHER-REF flowToken="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"\n',
            encoding="utf-8",
        )
        keys = pt_harvest(ctx)
        self.assertIn("11111111-2222-3333-4444-555555555555", keys)
        self.assertNotIn("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", keys)

        pt_follow(ctx)
        # flowToken should be followed, pmsType should not
        followed_calls = [c for c in b.calls if "11111111-2222-3333-4444-555555555555" in c[2]]
        self.assertTrue(len(followed_calls) > 0)
        pms_calls = [c for c in b.calls if "MEWS" in c[2]]
        self.assertEqual(len(pms_calls), 0)

    def test_crossref(self) -> None:
        (self.run_dir / "inputs.env").write_text("PT_PROPERTY_ID=PROP9999\n", encoding="utf-8")
        other = self.root / "run2"
        other.mkdir()
        (other / "inputs.env").write_text("PT_PROPERTY_ID=PROP9999\n", encoding="utf-8")
        (other / "verdict.json").write_text(json.dumps({"failure_codes": {"ERR_FOO": 1}}), encoding="utf-8")

        ctx = self._ctx()
        matches = pt_crossref(ctx)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0], "run2")
        report = (self.run_dir / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("SAME PROPERTY as run `run2`", report)

    def test_fallback_icase(self) -> None:
        ctx = self._ctx()
        (self.run_dir / "raw" / "t_kb_app_2026-11-01-10_OTA1-ABC123.out").write_text(
            "lowercase ota1-abc123 hit\n", encoding="utf-8"
        )
        n = pt_fallback_icase(ctx)
        self.assertEqual(n, 1)
        out = self.run_dir / "raw" / "t_fallback_icase.out"
        self.assertTrue(out.exists())
        self.assertIn("[fallback-icase] lowercase ota1-abc123 hit", out.read_text(encoding="utf-8"))

    def test_next_actions(self) -> None:
        (self.run_dir / "verdict.json").write_text(
            json.dumps({"failure_codes": {"ERR_CONVERSION_UNMAPPED_ROOM_RATE": 2}, "hits": ["x"], "reached": [], "missing": []}),
            encoding="utf-8",
        )
        ctx = self._ctx()
        txt = pt_next_actions(ctx)
        self.assertIn("ERR_CONVERSION_UNMAPPED_ROOM_RATE", txt)
        self.assertTrue((self.run_dir / "NEXT_ACTIONS.md").exists())

    def test_pt_trace_end_to_end(self) -> None:
        b = FakeBackend(
            cat=["cm-ota1/booking-api"],
            responses={"cm-ota1/booking-api": '2026-11-01T10:05:00 OTA1-ABC123 sourceAgentCode="OTA1"\n'},
        )
        ctx = self._ctx(backend=b, case="CASE-1")
        rc = pt_trace(ctx)
        self.assertEqual(rc, 0)
        self.assertTrue((self.run_dir / "REPORT.md").exists())
        self.assertTrue((self.run_dir / "NEXT_ACTIONS.md").exists())
        self.assertTrue((self.run_dir / "resolve.tsv").exists())
        self.assertTrue((self.run_dir / "hours.tsv").exists())
        self.assertTrue((self.run_dir / "join_keys.tsv").exists())
        updated_kb = json.loads(self.kb_path.read_text(encoding="utf-8"))
        self.assertTrue(len(updated_kb["channels"]["OTA1"]["history"]) >= 1)


if __name__ == "__main__":
    unittest.main()
