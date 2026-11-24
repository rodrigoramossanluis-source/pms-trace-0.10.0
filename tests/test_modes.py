"""Mode registry and goal compatibility tests (0.11)."""

from __future__ import annotations

import unittest

from pms_trace import __version__
from pms_trace.modes import DEFAULT_REGISTRY, ModeSpec, build_default_registry


class VersionTests(unittest.TestCase):
    def test_version_011(self) -> None:
        self.assertTrue(__version__.startswith("0.11"))


class RegistryTests(unittest.TestCase):
    def test_builtin_modes_present(self) -> None:
        for mid in (
            "trace",
            "ari",
            "ari-discrepancy",
            "overbooking",
            "missing-reservation",
            "rate-mismatch",
            "delivery-failure",
            "bridge-flush",
            "bridgeflush",
        ):
            self.assertIsNotNone(DEFAULT_REGISTRY.get(mid), mid)

    def test_goal_specs_compatible_with_010(self) -> None:
        specs = DEFAULT_REGISTRY.goal_specs()
        self.assertEqual(specs["overbooking"][0], ["ari"])
        self.assertIn("PT_CHANNEL", specs["overbooking"][1])
        self.assertEqual(specs["missing-reservation"][0], ["trace"])
        self.assertEqual(specs["delivery-failure"][0], ["trace", "fidelity"])

    def test_trace_stages_match_verdict_gates(self) -> None:
        m = DEFAULT_REGISTRY.require("trace")
        ids = [s.stage_id for s in m.stages]
        for g in (
            "ARRIVAL",
            "INVENTORY",
            "ACCEPTANCE",
            "CM_STORE",
            "PMS_RECEIVE",
            "CONVERSION",
            "DELIVERY",
        ):
            self.assertIn(g, ids)

    def test_ari_discrepancy_stages_generic(self) -> None:
        m = DEFAULT_REGISTRY.require("ari-discrepancy")
        ids = [s.stage_id for s in m.stages]
        self.assertEqual(
            ids,
            ["DISCOVERY", "SENT", "RECEIVED", "OTA_FETCH", "ACTOR", "DERIVATION"],
        )
        self.assertTrue(m.legacy_task_bridge)

    def test_register_duplicate_raises(self) -> None:
        reg = build_default_registry()
        with self.assertRaises(ValueError):
            reg.register(
                ModeSpec(mode_id="trace", task_ids=("trace",), description="dup")
            )


class ModeRunnerTests(unittest.TestCase):
    def test_write_mode_plan_and_history(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from pms_trace.modes import DEFAULT_REGISTRY
        from pms_trace.modes.runner import (
            append_mode_section_to_report,
            record_mode_start,
            write_mode_plan,
        )

        mode = DEFAULT_REGISTRY.require("ari-discrepancy")
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "REPORT.md").write_text("# head\n", encoding="utf-8")
            plan_path = write_mode_plan(
                run_dir, mode, anchors_present={"PT_CHANNEL": True, "PT_REF": False}
            )
            self.assertTrue(plan_path.is_file())
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["mode_id"], "ari-discrepancy")
            self.assertEqual(len(plan["stages"]), 6)
            self.assertEqual(plan["stages"][1]["stage_id"], "SENT")
            record_mode_start(run_dir, mode)
            hist = (run_dir / "state" / "stage_history.jsonl").read_text(encoding="utf-8")
            self.assertIn("SENT", hist)
            self.assertIn("bridged", hist)
            append_mode_section_to_report(run_dir, mode)
            report = (run_dir / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("MODE PLAN", report)
            self.assertIn("OTA_FETCH", report)


class DiscrepancyObservationTests(unittest.TestCase):
    def test_assess_without_raw_uses_anchors(self) -> None:
        import tempfile
        from pathlib import Path

        from pms_trace.modes.discrepancy import assess_discrepancy_stages

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            a = assess_discrepancy_stages(
                run_dir,
                anchors_present={
                    "PT_CHANNEL": True,
                    "PT_STAY_DATES": True,
                    "PT_AT": True,
                },
            )
        by_id = {s["stage_id"]: s for s in a["stages"]}
        self.assertEqual(by_id["DISCOVERY"]["status"], "anchored")
        self.assertEqual(by_id["SENT"]["status"], "anchored")
        self.assertEqual(by_id["ACTOR"]["status"], "anchored")
        self.assertEqual(by_id["RECEIVED"]["status"], "planned")

    def test_assess_with_raw_evidence(self) -> None:
        import tempfile
        from pathlib import Path

        from pms_trace.modes.discrepancy import (
            assess_discrepancy_stages,
            run_discrepancy_observation,
        )
        from pms_trace.modes import DEFAULT_REGISTRY

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            raw = run_dir / "raw"
            raw.mkdir()
            (raw / "t_ari_push_demo.out").write_text(
                "AmountAfterTax=\"100.00\" RatePlanCode=\"BAR\"\n", encoding="utf-8"
            )
            (raw / "t_ari_attribute_demo.out").write_text(
                "clientSystem=gui user=operator\n", encoding="utf-8"
            )
            (run_dir / "REPORT.md").write_text("# h\n", encoding="utf-8")
            a = assess_discrepancy_stages(run_dir, anchors_present={"PT_CHANNEL": True})
            by_id = {s["stage_id"]: s for s in a["stages"]}
            self.assertEqual(by_id["SENT"]["status"], "evidence")
            self.assertEqual(by_id["ACTOR"]["status"], "evidence")
            mode = DEFAULT_REGISTRY.require("ari-discrepancy")
            out = run_discrepancy_observation(
                run_dir, mode, anchors_present={"PT_CHANNEL": True}
            )
            self.assertTrue((run_dir / "state" / "discrepancy_stages.json").is_file())
            report = (run_dir / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("DISCREPANCY STAGES", report)
            self.assertIn("SENT", report)
            self.assertEqual(out["mode_id"], "ari-discrepancy")


if __name__ == "__main__":
    unittest.main()
