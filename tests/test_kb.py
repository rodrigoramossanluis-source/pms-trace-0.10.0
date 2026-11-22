"""Tests for pms_trace.kb knowledge base management."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pms_trace.kb import (
    is_valid_kb,
    load_kb,
    lookup_signature,
    query_kb,
    query_list,
    write_back,
)


def _sample_kb(version: str = "0.9.7") -> dict:
    return {
        "schema_version": version,
        "channels": {
            "OTA2": {
                "name": "Channel Two",
                "code": "OTA2",
                "log_apps": {
                    "reservation_inbound": "cm-ota2/booking-gateway",
                    "ari_outbound": None,
                    "other_apps": ["cm-ota2/old-app"],
                },
                "example_anchors": {"reservation": "OTA2-1", "verified_at": "2026-01-01"},
                "history": [],
            }
        },
        "pms": {
            "CloudPMS": {
                "delivery_apps": ["pms-cloud/delivery"],
                "example_anchors": {"verified_at": "2026-01-01"},
                "history": [],
            }
        },
        "failure_signatures": {
            "ERR_TEST": {"meaning": "test error", "terminal": True},
        },
        "low_selectivity_kinds": ["roomCode", "ratePlanCode"],
    }


class TestKB(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.kb_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_json(self, path: Path, data: dict, mtime: float | None = None) -> Path:
        path.write_text(json.dumps(data), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_is_valid_kb(self) -> None:
        valid = self._write_json(self.kb_dir / "ok.json", {"a": 1})
        bad = self.kb_dir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertTrue(is_valid_kb(valid))
        self.assertFalse(is_valid_kb(bad))
        self.assertFalse(is_valid_kb(self.kb_dir / "missing.json"))

    def test_load_kb_direct(self) -> None:
        self._write_json(self.kb_dir / "connector_kb.json", _sample_kb())
        kb = load_kb(self.kb_dir, "connector_kb.json")
        self.assertEqual(kb["schema_version"], "0.9.7")

    def test_load_kb_missing_promotes_newest_candidate(self) -> None:
        now = time.time()
        self._write_json(self.kb_dir / "connector_kb_old.json", _sample_kb("0.9.5"), now - 20)
        self._write_json(self.kb_dir / "connector_kb_new.json", _sample_kb("0.9.6"), now - 10)
        kb = load_kb(self.kb_dir, "connector_kb.json")
        self.assertEqual(kb["schema_version"], "0.9.6")
        self.assertTrue((self.kb_dir / "connector_kb.json").exists())

    def test_load_kb_corrupt_restores_newest_valid_bak(self) -> None:
        now = time.time()
        live = self.kb_dir / "connector_kb.json"
        live.write_text("{corrupt", encoding="utf-8")
        self._write_json(self.kb_dir / "connector_kb.json.bak.1", _sample_kb("0.9.1"), now - 20)
        self._write_json(self.kb_dir / "connector_kb.json.bak.2", _sample_kb("0.9.2"), now - 10)
        bad_bak = self.kb_dir / "connector_kb.json.bak.3"
        bad_bak.write_text("{bad bak", encoding="utf-8")
        os.utime(bad_bak, (now - 5, now - 5))

        kb = load_kb(self.kb_dir, "connector_kb.json")
        self.assertEqual(kb["schema_version"], "0.9.2")
        self.assertTrue(any(self.kb_dir.glob("connector_kb.json.corrupt.*")))

    def test_load_kb_missing_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_kb(self.kb_dir, "connector_kb.json")

    def test_load_kb_promotes_newer_versioned_with_carryover(self) -> None:
        now = time.time()
        live_data = _sample_kb("0.9.7")
        live_data["channels"]["OTA2"]["history"] = [{"case": "C1", "ts": "2026-02-01"}]
        live_data["channels"]["OTA2"]["log_apps"]["ari_outbound"] = "cm-ota2/avail-gw"
        live_data["channels"]["OTA2"]["example_anchors"]["verified_at"] = "2026-02-01"
        live_data["channels"]["STUB"] = {"code": "STUB", "history": [{"case": "C0"}]}
        self._write_json(self.kb_dir / "connector_kb.json", live_data, now - 20)

        rel_data = _sample_kb("0.9.8")
        self._write_json(self.kb_dir / "connector_kb_v0.9.8.json", rel_data, now - 5)

        kb = load_kb(self.kb_dir, "connector_kb.json")
        self.assertEqual(kb["schema_version"], "0.9.8")
        self.assertIn("STUB", kb["channels"])
        self.assertEqual(len(kb["channels"]["OTA2"]["history"]), 1)
        self.assertEqual(kb["channels"]["OTA2"]["log_apps"]["ari_outbound"], "cm-ota2/avail-gw")
        self.assertEqual(kb["channels"]["OTA2"]["example_anchors"]["verified_at"], "2026-02-01")
        self.assertTrue(any(self.kb_dir.glob("connector_kb.json.bak.*.promote")))

    def test_query_helpers(self) -> None:
        kb = _sample_kb()
        self.assertEqual(query_kb(kb, "channels.OTA2.log_apps.reservation_inbound"), "cm-ota2/booking-gateway")
        self.assertEqual(query_kb(kb, "channels.OTA2.log_apps.ari_outbound", "fallback"), "fallback")
        self.assertIsNone(query_kb(kb, "channels.MISSING.code"))
        self.assertEqual(query_list(kb, "channels"), ["OTA2"])
        self.assertEqual(query_list(kb, "low_selectivity_kinds"), ["roomCode", "ratePlanCode"])
        self.assertEqual(lookup_signature(kb, "ERR_TEST"), {"meaning": "test error", "terminal": True})
        self.assertIsNone(lookup_signature(kb, "NO_SUCH"))

    def test_write_back_plan_mode_skips(self) -> None:
        kb_path = self._write_json(self.kb_dir / "connector_kb.json", _sample_kb())
        self.assertFalse(write_back(kb_path, case="C1", chan="OTA2", plan_mode=True))
        self.assertEqual(list(self.kb_dir.glob("*.bak.*")), [])

    def test_write_back_stub_apps_history_rotation_and_stale_repair(self) -> None:
        kb_path = self._write_json(self.kb_dir / "connector_kb.json", _sample_kb())

        # Create a new channel stub and repair stale app on OTA2 + CloudPMS
        ok = write_back(
            kb_path,
            case="CASE-1",
            ref="OTA9-123",
            create="OTA9",
            chan_sys="cm-ota9",
            pms="CloudPMS",
            today="2026-03-01",
            apps=["cm-ota9/booking-api", "cm-ota9/avail-api", "pms-cloud/new-app", "cm-ota9/*"],
            patterns_hit=["OTA9-123"],
            window_mode=["hour"],
            signatures=["ERR_TEST"],
            key_kinds=["traceToken"],
            bak_keep=2,
            hist_keep=2,
        )
        self.assertTrue(ok)
        kb = json.loads(kb_path.read_text(encoding="utf-8"))
        self.assertIn("OTA9", kb["channels"])
        self.assertEqual(kb["channels"]["OTA9"]["log_apps"]["reservation_inbound"], "cm-ota9/booking-api")
        self.assertIn("cm-ota9/avail-api", kb["channels"]["OTA9"]["log_apps"]["other_apps"])
        self.assertIn("pms-cloud/new-app", kb["pms"]["CloudPMS"]["delivery_apps"])

        # Stale repair + history truncation + backup rotation
        for i in range(3):
            write_back(
                kb_path,
                case=f"CASE-{i+2}",
                chan="OTA2",
                today="2026-03-02",
                stale_repaired=["cm-ota2/old-app"] if i == 0 else None,
                bak_keep=2,
                hist_keep=2,
            )

        kb = json.loads(kb_path.read_text(encoding="utf-8"))
        self.assertEqual(len(kb["channels"]["OTA2"]["history"]), 2)
        self.assertNotIn("cm-ota2/old-app", kb["channels"]["OTA2"]["log_apps"]["other_apps"])
        self.assertEqual(kb["channels"]["OTA2"]["_stale_repaired"][0]["app"], "cm-ota2/old-app")
        baks = list(self.kb_dir.glob("connector_kb.json.bak.*"))
        self.assertLessEqual(len(baks), 2)


if __name__ == "__main__":
    unittest.main()
