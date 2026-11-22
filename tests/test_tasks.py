import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, List

from pms_trace.tasks import (
    pt_ari,
    pt_ari_lgcy_report,
    pt_ari_verdict,
    pt_bridgeflush,
    pt_bridgeflush_verdict,
    pt_guiattrib,
    pt_kb_test,
)


class _StubBackend:
    def __init__(self) -> None:
        self.calls: List[str] = []

    def catalog(self) -> str:
        self.calls.append("catalog")
        return "/cm/avail-api\n/hub-channel-link/link-api\n"

    def ls(self, sys_name: str, comp_name: str) -> List[str]:
        self.calls.append(f"ls:{sys_name}/{comp_name}")
        if sys_name == "cm":
            return ["cm/avail-api"]
        return []

    def fetch(self, app: str, dh: str, query: str) -> str:
        self.calls.append(f"fetch:{app}:{dh}:{query}")
        return json.dumps({"flowToken": "tok-12345678"}) + "\n"

    def qfetch(self, app: str, dh: str, where: str) -> str:
        self.calls.append(f"qfetch:{app}:{dh}:{where}")
        return json.dumps(
            {
                "flowToken": "tok-12345678",
                "email": "alice@example.com",
                "@timestamp": "2026-04-23T10:00:00Z",
                "kubernetes": {"labels": {"component": "gui-gateway"}},
            }
        ) + "\n"

    def lgcy_fetch(self, needle: str, bucket: str, day: str) -> str:
        self.calls.append(f"lgcy_fetch:{needle}:{bucket}:{day}")
        return ""


class _StubRunner:
    def __init__(self) -> None:
        self.stages: List[str] = []

    def run(self, name: str, label: str, fn: Any) -> Any:
        self.stages.append(name)
        return fn()


class TasksTests(unittest.TestCase):
    def test_kb_test_cross_validates_live_and_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            kb = {
                "channels": {
                    "BKG": {
                        "log_apps": {"push": "cm/avail-api"},
                        "delivery_apps": ["legacy/dead-app"],
                        "example_anchors": {"verified_at": "2026-01-01"},
                        "history": ["v1"],
                    }
                },
                "pms": {},
                "failure_signatures": {"ERR_TIMEOUT": {}},
            }
            res = pt_kb_test(run_dir, kb, backend=_StubBackend())
            self.assertEqual(res["failure_signatures"], ["ERR_TIMEOUT"])
            self.assertEqual(res["sections"][0]["live"], ["cm/avail-api"])
            self.assertEqual(res["sections"][0]["stale"], ["legacy/dead-app"])
            self.assertEqual(res["app_probes"]["cm/avail-api"], "LIVE")
            self.assertEqual(res["app_probes"]["legacy/dead-app"], "STALE?")

    def test_ari_verdict_manual_origin_writes_goal_verdict_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "raw").mkdir()
            (run_dir / "ari_evidence.tsv").write_text(
                "ts\tkind\toriginSystem\tclient\tflowToken\troomRateKey\tvalues\tdates\tapp\n"
                "2026-04-23T10:00:00Z\tTARGET_EVENT\tplatform\tweb\ttok-1\tDBL/BAR\t199.00\t2026-05-01\tavail-api\n",
                encoding="utf-8",
            )
            (run_dir / "ari_actors.txt").write_text("alice@example.com\n", encoding="utf-8")
            (run_dir / "raw" / "t_ari_push_1.out").write_text("200 Success\n", encoding="utf-8")

            verdict = pt_ari_verdict(run_dir, kb={})
            self.assertEqual(verdict["mode"], "ari")
            self.assertIn("SOURCE", verdict["reached"])
            self.assertIn("INGEST", verdict["na"])
            self.assertIn("OTA_ACK", verdict["reached"])
            self.assertTrue((run_dir / "GOAL_VERDICT.md").exists())
            self.assertTrue((run_dir / "NEXT_ACTIONS.md").exists())
            self.assertTrue((run_dir / "verdict.json").exists())

    def test_ari_lgcy_report_parses_ota_rate_amount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            raw = run_dir / "raw"
            raw.mkdir()
            (raw / "t_ari_push_lgcy_p1_2026-04-23.out").write_text(
                '<OTA_HotelRateAmountNotifRQ TimeStamp="2026-04-23T10:00:00Z">'
                '<StatusApplicationControl End="2026-05-02" InvTypeCode="DBL" RatePlanCode="BAR" Start="2026-05-01"/>'
                'AmountAfterTax="150.00"</OTA_HotelRateAmountNotifRQ>\n'
                "<OTA_HotelRateAmountNotifRS><Success/></OTA_HotelRateAmountNotifRS>\n",
                encoding="utf-8",
            )
            cfg = {
                "PT_CHANNEL": "channelrelay_bkg",
                "PT_STAY_DATES": "2026-05-01",
                "PT_RATE_CODE": "BAR",
                "PT_ROOM_TYPE_CODE": "DBL",
            }
            stats = pt_ari_lgcy_report(run_dir, cfg, kb={})
            self.assertEqual(stats, {"files": 1, "rq": 1, "rs_ok": 1, "rs_err": 0})
            self.assertIn("150.00", (run_dir / "REPORT.md").read_text(encoding="utf-8"))

    def test_ari_requires_hotel_or_property(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                pt_ari(Path(tmp), {}, kb={})

    def test_guiattrib_writes_guiactors_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            backend = _StubBackend()
            runner = _StubRunner()
            cfg = {
                "PT_GUI_DATE": "2026-04-23",
                "PT_GUI_HOUR": "10",
                "PT_CHANNEL_HOTEL_UUID": "ch-uuid-1",
                "PT_GUI_USER": "alice@example.com",
            }
            out = pt_guiattrib(run_dir, cfg, backend=backend, runner=runner)
            self.assertIsNotNone(out)
            md = (run_dir / "GUIACTORS.md").read_text(encoding="utf-8")
            self.assertIn("alice@example.com", md)
            self.assertIn("tok-12345678", md)
            self.assertIn("gui_burst", runner.stages)

    def test_bridgeflush_verdict_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "state").mkdir()
            (run_dir / "raw").mkdir()
            (run_dir / "state" / "bfl_analysis.json").write_text(
                json.dumps(
                    {
                        "flushes": [{"token": "tok-9", "overall": ["500"], "user": "bob@example.com", "sublegs": {}}],
                        "sync": {"update_failed_400": 1, "flush_failed": 1},
                        "culprit_ids": ["RT1/RR1"],
                        "ok_rate_count": 3,
                        "failing_subleg": "hub-pms-sync",
                        "token_chain": ["tok-9"],
                        "type_puts": [],
                    }
                ),
                encoding="utf-8",
            )
            runner = _StubRunner()
            verdict = pt_bridgeflush(run_dir, {"PT_PROPERTY_ID": "PROP1"}, runner=runner)
            self.assertTrue(verdict["gates"]["FLUSH_TRIGGERED"])
            self.assertTrue(verdict["gates"]["FAIL_FAST_SINGLE"])
            self.assertFalse(verdict["gates"]["ORIGIN_FOUND"])
            self.assertIn("bfl_verdict", runner.stages)
            self.assertIn("RT1/RR1", (run_dir / "REPORT.md").read_text(encoding="utf-8"))
