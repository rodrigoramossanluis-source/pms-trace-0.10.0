from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from pms_trace.verdict import synthesize_verdict


class TestSynthesizeVerdict(unittest.TestCase):
    def test_empty_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                synthesize_verdict(run_dir, kb={}, env={"PT_REF": "BOOK-12345"})

            verdict = json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual(verdict["reached"], [])
            self.assertEqual(
                verdict["missing"],
                ["ARRIVAL", "INVENTORY", "ACCEPTANCE", "CM_STORE", "PMS_RECEIVE", "CONVERSION", "DELIVERY"],
            )
            self.assertEqual(verdict["hits"], 0)
            self.assertTrue((run_dir / "REPORT.md").exists())

    def test_dedupe_cotenant_and_failure_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            raw_dir = run_dir / "raw"
            raw_dir.mkdir()

            (run_dir / "join_keys.tsv").write_text(
                "flowToken\tflowtok12345678\nroomCode\tROOM123\n",
                encoding="utf-8",
            )
            (run_dir / "inputs.env").write_text("PT_REF=CHAN-99999\nPT_CHAN_SYS=cm-relay\n", encoding="utf-8")
            (run_dir / "ledger.tsv").write_text("1|s1|ok|lines=2\n2|s2|ok|lines=0\n", encoding="utf-8")

            ev1 = json.dumps({"@timestamp": "2026-11-19T10:00:00Z", "msg": "make_reservation 99999"})
            ev2 = json.dumps({"@timestamp": "2026-11-19T10:01:00Z", "msg": "pms-hub faultCode=ERR_CONVERSION_MAP"})
            (raw_dir / "t_kb_app.out").write_text(f"{ev1}\n{ev2}\n", encoding="utf-8")
            (raw_dir / "t_fallback_icase.out").write_text(f"[fallback-icase] {ev2}\n", encoding="utf-8")
            (raw_dir / "f_app_10_ROOM123.out").write_text("sellable inventory sibling\n", encoding="utf-8")
            (raw_dir / "t_lad_app.out").write_text(
                "sibling line without ref\n"
                "admitted line with 99999 Delivered reservation version\n",
                encoding="utf-8",
            )

            kb = {
                "channelrelay": {"system": "cm-relay", "skip_gates": ["INVENTORY"]},
                "failure_signatures": {
                    "ERR_CONVERSION_MAP": {"meaning": "Mapping error", "terminal": True},
                },
            }

            with contextlib.redirect_stdout(io.StringIO()):
                synthesize_verdict(run_dir, kb=kb, env={})

            verdict = json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual(verdict["hits"], 3)
            self.assertEqual(verdict["context_files"], 1)
            self.assertEqual(verdict["fd_files"], 1)
            self.assertEqual(verdict["fd_admitted"], 1)
            self.assertEqual(verdict["failure_codes"], {"ERR_CONVERSION_MAP": 1})
            self.assertEqual(verdict["terminal"], "ERR_CONVERSION_MAP")
            self.assertEqual(verdict["skipped"], ["INVENTORY"])
            self.assertIn("ARRIVAL", verdict["reached"])
            self.assertIn("CONVERSION", verdict["reached"])
            self.assertIn("DELIVERY", verdict["reached"])

            report = (run_dir / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("**CONVERSION - FAILED**", report)
            self.assertIn("**DELIVERY - FAILED**", report)
            self.assertIn("INVENTORY - N/A:", report)


if __name__ == "__main__":
    unittest.main()
