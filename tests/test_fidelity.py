import tempfile
import unittest
from pathlib import Path

from pms_trace.backend import BackendResult, StageRunner
from pms_trace.fidelity import run_fidelity, run_fidelity_with_fetch


class FidelityTests(unittest.TestCase):
    def test_preserved_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            ota = Path(tmp) / "ota.xml"
            pms = Path(tmp) / "pms.xml"
            ota.write_text(
                '<OTA_HotelResNotifRQ EchoToken="ECHO-0000">'
                '<RoomType RoomTypeCode="DBL"/><RatePlan RatePlanCode="BAR"/>'
                '<Total AmountAfterTax="220.00" CurrencyCode="EUR">'
                '<Taxes><Tax Amount="20.00" Code="VAT" Type="Inclusive"/></Taxes>'
                '</Total><TimeSpan Start="2026-02-14" End="2026-02-16"/>'
                "<GivenName>Alex</GivenName></OTA_HotelResNotifRQ>",
                encoding="utf-8",
            )
            pms.write_text(
                "<Reservation><valueNum>220.00</valueNum>"
                '<Total CurrencyCode="EUR"><Taxes>'
                '<Tax Amount="20.00" Code="VAT" Type="Inclusive"/>'
                '</Taxes></Total><RoomType RoomTypeCode="DBL"/>'
                '<RatePlan RatePlanCode="BAR"/>'
                '<TimeSpan Start="2026-02-14" End="2026-02-16"/>'
                "<GivenName>Alex</GivenName></Reservation>",
                encoding="utf-8",
            )

            ok = run_fidelity(run_dir, str(ota), str(pms))
            self.assertTrue(ok)
            report = (run_dir / "fidelity_report.txt").read_text(encoding="utf-8")
            self.assertTrue(report.startswith("FIDELITY REPORT"))
            self.assertEqual(report.count("PRESERVED"), 7)
            self.assertTrue(report.rstrip().endswith("VERDICTS:"))

    def test_unavailable_docs_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            ok = run_fidelity(run_dir, "", "")
            self.assertFalse(ok)
            self.assertFalse((run_dir / "fidelity_report.txt").exists())

    def test_verdicts_config_ignored_tax_and_field_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            raw_dir = run_dir / "raw"
            raw_dir.mkdir(parents=True)
            (raw_dir / "ota_doc.xml").write_text(
                '<OTA_HotelResNotifRQ><Total AmountBeforeTax="200.00" '
                'AmountAfterTax="220.00" CurrencyCode="EUR"><Taxes>'
                '<Tax Amount="20.00" Code="VAT" Type="Inclusive"/>'
                "</Taxes></Total></OTA_HotelResNotifRQ>",
                encoding="utf-8",
            )
            (raw_dir / "pms_doc.xml").write_text(
                '<Reservation><valueNum>220.00</valueNum>'
                '<Total CurrencyCode="EUR"/></Reservation>',
                encoding="utf-8",
            )

            ok = run_fidelity(run_dir, "", "")
            self.assertTrue(ok)
            report = (run_dir / "fidelity_report.txt").read_text(encoding="utf-8")
            self.assertIn("CONFIG_IGNORED", report)
            self.assertIn("TAX_BREAKDOWN_DROPPED: 1 tax lines", report)
            self.assertIn("FIELD_DROPPED: rate.amount_before_tax", report)

    def test_run_fidelity_with_fetch_autofetches_from_join_keys(self) -> None:
        class FakeBackend:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str]] = []

            def grep(self, app: str, blob: str, pattern: str) -> BackendResult:
                self.calls.append((app, blob, pattern))
                if app == "site-connect/notify-gateway":
                    return BackendResult(
                        stdout=(
                            '<OTA_HotelResNotifRQ EchoToken="ECHO-123">'
                            '<Total AmountAfterTax="150.00" CurrencyCode="USD"/>'
                            '<RoomType RoomTypeCode="STD"/><RatePlan RatePlanCode="BAR"/>'
                            '<TimeSpan Start="2026-03-01" End="2026-03-03"/>'
                            "<GivenName>Sam</GivenName></OTA_HotelResNotifRQ>\n"
                        ),
                        stderr="",
                        rc=0,
                    )
                if app == "pms-hub/booking-api":
                    return BackendResult(
                        stdout=(
                            "<Reservation><valueNum>150.00</valueNum>"
                            '<Total CurrencyCode="USD"/>'
                            '<RoomType RoomTypeCode="STD"/><RatePlan RatePlanCode="BAR"/>'
                            '<TimeSpan Start="2026-03-01" End="2026-03-03"/>'
                            "<GivenName>Sam</GivenName></Reservation>\n"
                        ),
                        stderr="",
                        rc=0,
                    )
                return BackendResult(stdout="", stderr="", rc=1)

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            (run_dir / "join_keys.tsv").write_text(
                "echoToken\tECHO-123\nhubBookingUuid\tUUID-456\n",
                encoding="utf-8",
            )
            backend = FakeBackend()
            runner = StageRunner(run_dir)
            env = {
                "PT_CHAN_SYS": "site-connect",
                "PT_FROM": "2026-03-01",
            }

            ok = run_fidelity_with_fetch(run_dir, {}, env, backend, runner)
            self.assertTrue(ok)
            self.assertEqual(
                backend.calls,
                [
                    ("site-connect/notify-gateway", "2026-03-01-*", "ECHO-123"),
                    ("pms-hub/booking-api", "2026-03-01-*", "UUID-456"),
                ],
            )
            self.assertTrue((run_dir / "raw" / "ota_doc.xml").is_file())
            self.assertTrue((run_dir / "raw" / "pms_doc.xml").is_file())
            report = (run_dir / "fidelity_report.txt").read_text(encoding="utf-8")
            self.assertIn("PRESERVED", report)


if __name__ == "__main__":
    unittest.main()
