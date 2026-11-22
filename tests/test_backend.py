# SPDX-License-Identifier: MIT
"""Unit tests for pms_trace.backend."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pms_trace.backend import Backend, BackendResult, StageRunner, sanitize_stage_name  # noqa: E402


class BackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / "archive"
        app_dir = self.archive / "cm-ota2" / "booking-gateway"
        app_dir.mkdir(parents=True)
        (app_dir / "2026-01-10-12.log").write_text(
            '{"flowToken":"tok-1","message":"M-123456 ok"}\n'
            '{"flowToken":"tok-2","message":"other line"}\n'
            '{"flowToken":"tok-1","message":"line 3"}\n',
            encoding="utf-8",
        )

        self.lgcy = self.root / "lgcy"
        bkt = self.lgcy / "channelrelay-sync-partition-one-log-r1"
        bkt.mkdir(parents=True)
        (bkt / "2026-01-10.log").write_text(
            "listingAcctId=600123 OTA_HotelRateAmountNotifRQ\n",
            encoding="utf-8",
        )

        self.env = dict(os.environ)
        self.env["S3S_ROOT"] = str(self.archive)
        self.env["S3G_ROOT"] = str(self.lgcy)

        self.backend = Backend(
            s3search_cmd=REPO_ROOT / "backends" / "s3search.py",
            s3grep_cmd=REPO_ROOT / "backends" / "s3grep.py",
            timeout=10.0,
            max_lines=200000,
            env=self.env,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_list_grep_query_legacy_find(self) -> None:
        res_ls = self.backend.list("cm-*", "*")
        self.assertEqual(res_ls.rc, 0)
        self.assertIn("cm-ota2/booking-gateway", res_ls.stdout)

        res_grep = self.backend.grep("cm-ota2/booking-gateway", "2026-01-10-12", "tok-1")
        self.assertEqual(res_grep.rc, 0)
        self.assertEqual(len(res_grep.stdout.splitlines()), 2)

        res_query = self.backend.query("cm-ota2/booking-gateway", "2026-01-10-12", 's.flowToken = "tok-2"')
        self.assertEqual(res_query.rc, 0)
        self.assertIn("other line", res_query.stdout)

        res_lgcy = self.backend.legacy_find("listingAcctId=600123", "channelrelay*one*", "2026-01-10")
        self.assertEqual(res_lgcy.rc, 0)
        self.assertIn("OTA_HotelRateAmountNotifRQ", res_lgcy.stdout)

    def test_fetch_safety_constraints(self) -> None:
        empty_res = self.backend.grep("cm-ota2/booking-gateway", "2026-01-10-12", "")
        self.assertTrue(empty_res.refused)
        self.assertEqual(empty_res.rc, 2)
        self.assertIn("empty pattern", empty_res.stderr)

        weak_res = self.backend.grep("cm-ota2/booking-gateway", "2026-01-10-12", "12345")
        self.assertTrue(weak_res.refused)
        self.assertEqual(weak_res.rc, 2)
        self.assertIn("weak numeric", weak_res.stderr)

        warn_res = self.backend.grep("cm-ota2/booking-gateway", "2026-01-10-12", "123456")
        self.assertFalse(warn_res.refused)
        self.assertEqual(warn_res.rc, 0)
        self.assertIn("LOW-SELECTIVITY warn", warn_res.stderr)

        hub_fullday = self.backend.grep("pms-hub", "2026-01-10-*", "tok-1")
        self.assertTrue(hub_fullday.refused)
        self.assertEqual(hub_fullday.rc, 2)
        self.assertIn("pms-hub '*' full-day", hub_fullday.stderr)

        hub_hour = self.backend.grep("pms-hub", "2026-01-10-12", "tok-1")
        self.assertFalse(hub_hour.refused)
        self.assertEqual(hub_hour.rc, 1)  # no match in archive, not refused

    def test_max_lines_truncation_and_timeout(self) -> None:
        bounded = Backend(
            s3search_cmd=REPO_ROOT / "backends" / "s3search.py",
            max_lines=1,
            env=self.env,
        )
        res = bounded.grep("cm-ota2/booking-gateway", "2026-01-10-12", "tok-1")
        self.assertEqual(res.rc, 0)
        self.assertTrue(res.truncated)
        self.assertEqual(len(res.stdout.splitlines()), 1)
        self.assertIn("output capped at 1 lines", res.stderr)

        slow_script = self.root / "slow.py"
        slow_script.write_text("import time; time.sleep(2)\n", encoding="utf-8")
        slow_backend = Backend(s3search_cmd=slow_script, timeout=0.1)
        t_res = slow_backend.list("*", "*")
        self.assertEqual(t_res.rc, 124)
        self.assertIn("TIMEOUT", t_res.stderr)


class StageRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sanitize_stage_name(self) -> None:
        self.assertEqual(sanitize_stage_name("cm-ota2/booking *"), "cm-ota2_booking__")
        self.assertEqual(sanitize_stage_name("///"), "___")
        self.assertEqual(sanitize_stage_name(".."), "stage")

    def test_done_resume_refused_and_timeout(self) -> None:
        runner = StageRunner(self.run_dir)
        calls = {"ok": 0, "nomatch": 0, "refused": 0, "timeout": 0}

        r_ok = runner.run_stage("s/1", "ok", lambda: (calls.__setitem__("ok", calls["ok"] + 1) or BackendResult("a\nb\n", "", 0)))
        self.assertEqual(r_ok.status, "done")
        self.assertEqual(r_ok.lines, 2)
        self.assertTrue((self.run_dir / "state" / "s_1.done").exists())
        self.assertEqual((self.run_dir / "raw" / "s_1.rc").read_text().strip(), "0")

        r_nomatch = runner.run_stage("s_2", "nomatch", lambda: (calls.__setitem__("nomatch", calls["nomatch"] + 1) or BackendResult("", "", 1)))
        self.assertEqual(r_nomatch.status, "done")
        self.assertTrue((self.run_dir / "state" / "s_2.done").exists())

        r_refused = runner.run_stage("s_ref", "refused", lambda: (calls.__setitem__("refused", calls["refused"] + 1) or BackendResult("", "REFUSED", 2, refused=True)))
        self.assertEqual(r_refused.status, "refused")
        self.assertFalse((self.run_dir / "state" / "s_ref.done").exists())

        r_to = runner.run_stage("s_to", "timeout", lambda: (calls.__setitem__("timeout", calls["timeout"] + 1) or BackendResult("", "TIMEOUT", 124)))
        self.assertEqual(r_to.status, "timeout_retry")
        self.assertFalse((self.run_dir / "state" / "s_to.done").exists())

        resumed = StageRunner(self.run_dir, resume=True)
        r_ok_2 = resumed.run_stage("s/1", "ok", lambda: (calls.__setitem__("ok", calls["ok"] + 1) or BackendResult("a\n", "", 0)))
        self.assertEqual(r_ok_2.status, "skipped_done")
        self.assertEqual(calls["ok"], 1)

        r_to_2 = resumed.run_stage("s_to", "timeout", lambda: (calls.__setitem__("timeout", calls["timeout"] + 1) or BackendResult("ok\n", "", 0)))
        self.assertEqual(r_to_2.status, "done")
        self.assertEqual(calls["timeout"], 2)

    def test_skip_legs_and_plan_mode(self) -> None:
        runner = StageRunner(self.run_dir, plan=True, skip_legs=["ari"])
        called = False

        def _fn() -> BackendResult:
            nonlocal called
            called = True
            return BackendResult("x\n", "", 0)

        r_skip = runner.run_stage("t_ari_push", "ari", _fn)
        self.assertEqual(r_skip.status, "skipped_leg")
        self.assertFalse(called)
        self.assertIn("skipped-by-PT_SKIP_LEGS", (self.run_dir / "ledger.tsv").read_text())

        r_plan = runner.run_stage("t_res", "res", _fn, plan_cmd="grep cm-ota2")
        self.assertEqual(r_plan.status, "planned")
        self.assertFalse(called)
        self.assertIn("PLAN [t_res] grep cm-ota2", (self.run_dir / "REPORT.md").read_text())


if __name__ == "__main__":
    unittest.main()
