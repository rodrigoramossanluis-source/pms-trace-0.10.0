import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pms_trace.backend import Backend, StageRunner
from pms_trace.tasks.ari import (
    pt_ari,
    pt_ari_booking_xml_lgcy,
    pt_ari_push_lgcy,
    pt_ari_verdict,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKENDS_DIR = REPO_ROOT / "backends"
KB_SRC = REPO_ROOT / "connector_kb.json"


def _write_archive(logs_dir: Path, records: list[dict]) -> None:
    grouped: dict[Path, list[str]] = {}
    for rec in records:
        ts = rec["@timestamp"]
        rel = Path(rec["_sys"]) / rec["_comp"] / f"{ts[:10]}-{ts[11:13]}.log"
        payload = {k: v for k, v in rec.items() if not k.startswith("_")}
        grouped.setdefault(logs_dir / rel, []).append(json.dumps(payload, separators=(",", ":")))
    for path, lines in grouped.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class AriEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kb = json.loads(KB_SRC.read_text(encoding="utf-8"))

    def _backend(self, logs_dir: Path, arch_dir: Path) -> Backend:
        env = dict(os.environ, S3S_ROOT=str(logs_dir), S3G_ROOT=str(arch_dir))
        return Backend(
            s3search_cmd=BACKENDS_DIR / "s3search.py",
            s3grep_cmd=BACKENDS_DIR / "s3grep.py",
            timeout=30.0,
            env=env,
        )

    def test_ari_manual_fixture_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs_dir = root / "logs"
            arch_dir = root / "archive"
            run_dir = root / "run"
            arch_dir.mkdir(parents=True)
            _write_archive(
                logs_dir,
                [
                    {
                        "_sys": "cm",
                        "_comp": "avail-api-ra",
                        "@timestamp": "2026-01-12T09:14:05.000Z",
                        "level": "INFO",
                        "message": "SET RATE propertyUuid=33333333-4444-4555-8666-777777777777 roomRateKey=RRK-DBL-BAR date=2026-02-14 value=129.00",
                        "originSystem": "platform",
                        "client": "webcm",
                        "flowToken": "3c3c3c3c-1111-4222-8333-944455550003",
                        "roomRateKey": "RRK-DBL-BAR",
                    },
                    {
                        "_sys": "portal-property",
                        "_comp": "gui-gateway",
                        "@timestamp": "2026-01-12T09:14:04.500Z",
                        "level": "INFO",
                        "msg": "graphql server operation was successful",
                        "flowToken": "3c3c3c3c-1111-4222-8333-944455550003",
                        "email": "jane.doe@demo-hotel.test",
                        "ipAddress": "198.51.100.10",
                    },
                    {
                        "_sys": "cm-ota2",
                        "_comp": "avail-gateway",
                        "@timestamp": "2026-01-12T09:15:20.000Z",
                        "level": "INFO",
                        "msg": "ARI push rate update listing LA-778899 date 2026-02-14 value 129.00",
                        "listingAcctId": "LA-778899",
                        "response": "Success",
                        "statusCode": 200,
                    },
                ],
            )
            kb_copy = root / "connector_kb.json"
            kb_copy.write_text(KB_SRC.read_text(encoding="utf-8"), encoding="utf-8")

            cfg = {
                "PT_CASE": "mock-ari-manual",
                "PT_GOAL": "rate-mismatch",
                "PT_CHANNEL": "OTA2",
                "PT_HOTEL_UUID": "33333333-4444-4555-8666-777777777777",
                "PT_MEMBERSHIP": "LA-778899",
                "PT_TARGET_VALUES": "129.00",
                "PT_STAY_DATES": "2026-02-14",
                "PT_AT": "2026-01-12T10:00:00",
                "PT_FROM": "2026-01-11",
                "PT_TO": "2026-01-12",
                "PT_REGION": "both",
                "PT_SOURCE_SYSTEM": "auto",
                "PT_NO_PREFLIGHT": "1",
            }
            buf = io.StringIO()
            with mock.patch("pms_trace.tasks.ari.write_back", return_value=True) as wb_mock:
                with contextlib.redirect_stdout(buf):
                    verdict = pt_ari(
                        run_dir,
                        cfg,
                        self.kb,
                        backend=self._backend(logs_dir, arch_dir),
                        runner=StageRunner(run_dir),
                        kb_path=kb_copy,
                    )

            out = buf.getvalue()
            self.assertIn("TARGET hour found at 2026-01-12-09", out)
            ev_txt = (run_dir / "ari_evidence.tsv").read_text(encoding="utf-8")
            self.assertIn("TARGET_EVENT", ev_txt)
            self.assertEqual(verdict["value_events"], 1)
            self.assertEqual((run_dir / "ari_actors.txt").read_text(encoding="utf-8").strip(), "jane.doe@demo-hotel.test")
            self.assertTrue({"SOURCE", "CM_PERSIST", "CHANNEL_PUSH", "OTA_ACK"} <= set(verdict["reached"]))
            self.assertEqual(verdict["na"], ["INGEST"])
            self.assertIn("VALUE_ATTRIBUTED", (run_dir / "GOAL_VERDICT.md").read_text(encoding="utf-8"))
            self.assertIn("Manual change confirmed", (run_dir / "NEXT_ACTIONS.md").read_text(encoding="utf-8"))
            wb_mock.assert_called_once()
            _, wb_kwargs = wb_mock.call_args
            self.assertEqual(wb_kwargs["chan"], "OTA2")
            self.assertIn("cm-ota2/avail-gateway", wb_kwargs["apps"])

    def test_ari_pms_fixture_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs_dir = root / "logs"
            arch_dir = root / "archive"
            run_dir = root / "run"
            arch_dir.mkdir(parents=True)
            _write_archive(
                logs_dir,
                [
                    {
                        "_sys": "cm",
                        "_comp": "avail-api-rb",
                        "@timestamp": "2026-01-12T17:40:00.000Z",
                        "level": "INFO",
                        "message": "SET AVAIL propertyUuid=44444444-5555-4666-8777-888888888888 date=2026-02-20 avail=3",
                        "originSystem": "CLOUDPMS",
                        "flowToken": "4d4d4d4d-1111-4222-8333-944455550004",
                    },
                    {
                        "_sys": "pms-cm-bridge",
                        "_comp": "avail-sync",
                        "@timestamp": "2026-01-12T17:39:30.000Z",
                        "level": "INFO",
                        "msg": "availability batch received",
                        "propertyKey": "PK-445566",
                        "originFlowTokens": ["4d4d4d4d-1111-4222-8333-944455550004"],
                    },
                ],
            )
            cfg = {
                "PT_CASE": "mock-ari-pms",
                "PT_HOTEL_UUID": "44444444-5555-4666-8777-888888888888",
                "PT_PROPERTY_ID": "PK-445566",
                "PT_TARGET_VALUES": "3",
                "PT_STAY_DATES": "2026-02-20",
                "PT_AT": "2026-01-12T18:00:00",
                "PT_FROM": "2026-01-12",
                "PT_TO": "2026-01-12",
                "PT_REGION": "both",
                "PT_SOURCE_SYSTEM": "auto",
                "PT_NO_PREFLIGHT": "1",
            }
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                verdict = pt_ari(
                    run_dir,
                    cfg,
                    self.kb,
                    backend=self._backend(logs_dir, arch_dir),
                    runner=StageRunner(run_dir),
                )

            out = buf.getvalue()
            ev_txt = (run_dir / "ari_evidence.tsv").read_text(encoding="utf-8")
            self.assertIn("avail-api-rb", ev_txt)
            self.assertNotIn("avail-api-ra", ev_txt)
            self.assertIn("CloudPMS ingest leg", out)
            self.assertTrue({"SOURCE", "INGEST", "CM_PERSIST"} <= set(verdict["reached"]))
            self.assertIn("CHANNEL_PUSH", verdict["missing"])
            self.assertIn("PMS push origin", (run_dir / "NEXT_ACTIONS.md").read_text(encoding="utf-8"))

    def test_billing_series_and_noise_scrubbing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs_dir = root / "logs"
            arch_dir = root / "archive"
            run_dir = root / "run"
            arch_dir.mkdir(parents=True)
            _write_archive(
                logs_dir,
                [
                    {
                        "_sys": "cm",
                        "_comp": "avail-api-ra",
                        "@timestamp": "2026-01-12T09:10:00.000Z",
                        "level": "INFO",
                        "message": "SET AVAIL propertyUuid=55555555-1111-4222-8333-444444444444 ownerId=129990 date=2026-02-14 avail=5",
                        "statusCode": 200,
                        "originSystem": "CLOUDPMS",
                    },
                    {
                        "_sys": "billing-service",
                        "_comp": "events",
                        "@timestamp": "2026-01-11T08:00:00.000Z",
                        "level": "INFO",
                        "msg": "accountStatus=Suspended propertyKey=PK-998877",
                    },
                    {
                        "_sys": "pms-cm-bridge",
                        "_comp": "avail-sync",
                        "@timestamp": "2026-01-12T07:00:00.000Z",
                        "level": "INFO",
                        "msg": "sync ok PK-998877",
                    },
                ],
            )
            cfg = {
                "PT_CASE": "mock-billing-series",
                "PT_HOTEL_UUID": "55555555-1111-4222-8333-444444444444",
                "PT_PROPERTY_ID": "PK-998877",
                "PT_TARGET_VALUES": "200",
                "PT_STAY_DATES": "2026-02-14",
                "PT_AT": "2026-01-12T10:00:00",
                "PT_FROM": "2026-01-12",
                "PT_TO": "2026-01-12",
                "PT_REGION": "ra",
                "PT_BILLING_FROM": "2026-01-11",
                "PT_BILLING_TO": "2026-01-11",
                "PT_SERIES_FROM": "2026-01-11",
                "PT_SERIES_TO": "2026-01-12",
                "PT_NO_PREFLIGHT": "1",
            }
            verdict = pt_ari(
                run_dir,
                cfg,
                self.kb,
                backend=self._backend(logs_dir, arch_dir),
                runner=StageRunner(run_dir),
            )
            self.assertEqual(verdict["value_events"], 0)
            self.assertTrue((run_dir / "raw" / "t_bil_billing-service_events_2026-01-11.out").is_file())
            report = (run_dir / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("### ARI presence timeline (pms-cm-bridge, by day)", report)
            self.assertIn("- 2026-01-11 inv=0 core=0", report)
            self.assertIn("- 2026-01-12 inv=1 (2026-01-12T07:00:00..2026-01-12T07:00:00) core=0", report)

    def test_plan_mode_and_lgcy_wrapper_with_backend_legacy_find(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "plan_run"
            cfg = {
                "PT_HOTEL_UUID": "33333333-4444-4555-8666-777777777777",
                "PT_CHANNEL": "OTA1",
                "PT_MEMBERSHIP": "LA-100",
                "PT_BOOKING_REF": "OTA1-9001",
                "PT_AT": "2026-01-12T10:00:00",
                "PT_FROM": "2026-01-12",
                "PT_TO": "2026-01-12",
            }
            backend = mock.MagicMock(spec=Backend)
            pt_ari(run_dir, cfg, self.kb, backend=backend, runner=StageRunner(run_dir, plan=True))
            backend.grep.assert_not_called()
            backend.legacy_find.assert_not_called()
            self.assertIn("PLAN [", (run_dir / "REPORT.md").read_text(encoding="utf-8"))

            lgcy_dir = root / "lgcy_run"
            backend.legacy_find.return_value = mock.Mock(stdout="hit\n", stderr="", rc=0, refused=False)
            outs = pt_ari_push_lgcy(lgcy_dir, cfg, self.kb, backend=backend, runner=StageRunner(lgcy_dir))
            self.assertEqual(len(outs), 1)
            backend.legacy_find.assert_called_once()
            xml_outs = pt_ari_booking_xml_lgcy(lgcy_dir, cfg, self.kb, backend=backend, runner=StageRunner(lgcy_dir))
            self.assertEqual(len(xml_outs), 1)

    def test_ari_verdict_offline_backward_compat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "raw").mkdir()
            verdict = pt_ari_verdict(run_dir, kb={})
            self.assertEqual(verdict["mode"], "ari")
            self.assertEqual(verdict["hits"], 0)
