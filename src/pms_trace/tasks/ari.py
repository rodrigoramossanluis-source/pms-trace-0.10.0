"""ARI mode task: value attribution, LGCY ChannelRelay legs, and verdict synthesis."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .. import __version__
from ..backend import Backend, StageRunner
from ..kb import write_back
from ._ari_scan import (
    MANUAL, PMS, AriScan, data_lines, evidence_rows, has_data, is_channelrelay, note, words,
)

_SEG_RE = re.compile(
    r'<StatusApplicationControl End="([^"]+)" InvTypeCode="([^"]+)" '
    r'RatePlanCode="([^"]+)" Start="([^"]+)"/>'
)
_AMT_RE = re.compile(r'AmountAfterTax="([0-9.]+)"')
_TS_RE = re.compile(r'TimeStamp="([^"]+)"')
_DEFAULT_KB_PATH = Path.home() / "pms_tools" / "kb" / "connector_kb.json"


def pt_ari_push_lgcy(
    run_dir: Path, cfg: Dict[str, Any], kb: Dict[str, Any],
    backend: Optional[Backend] = None, runner: Optional[StageRunner] = None,
) -> List[Path]:
    """Scan ChannelRelay legacy-stack sync partitions for listingAcctId=PT_MEMBERSHIP."""
    scan = AriScan(run_dir, cfg, kb, backend, runner)
    if not scan.channelrelay:
        return []
    membership = scan.get("PT_MEMBERSHIP")
    if not membership:
        scan.note("- LGCY_ARI: skipped (no membership id)")
        return []
    lookback = int(scan.get("PT_LGCY_LOOKBACK_DAYS") or 7)
    suffix = scan.get("PT_LGCY_REGION_SUFFIX") or "r1"
    scan.note(f"- LGCY_ARI leg: ChannelRelay {scan.channel} - key listingAcctId={membership}, lookback={lookback}d")
    outputs: List[Path] = []
    for part in scan.partitions():
        hit, day, count = False, scan.legacy_day, 0
        while day is not None:
            count += 1
            day_str = day.isoformat()
            out_path = scan.legacy(
                f"t_ari_push_lgcy_{part}_{day_str}", f"lgcy-sync/{part} {day_str} listingAcctId={membership}",
                f"listingAcctId={membership}", f"channelrelay*sync*partition*{part}*log*{suffix}*", day_str,
            )
            outputs.append(out_path)
            if has_data(out_path):
                hit = True
            if (hit and count >= 2) or count >= lookback or (scan.first_day and day <= scan.first_day):
                break
            day -= dt.timedelta(days=1)
        if hit:
            break
    return outputs


def pt_ari_lgcy_report(run_dir: Path, cfg: Dict[str, Any], kb: Dict[str, Any]) -> Dict[str, int]:
    """Summarize t_ari_push_lgcy_*.out into REPORT.md."""
    if not is_channelrelay(cfg, kb):
        return {"files": 0, "rq": 0, "rs_ok": 0, "rs_err": 0}
    dts = set(words(cfg.get("PT_STAY_DATES")))
    rate, room = str(cfg.get("PT_RATE_CODE") or ""), str(cfg.get("PT_ROOM_TYPE_CODE") or "")
    files = sorted((run_dir / "raw").glob("t_ari_push_lgcy_*.out"))
    rq = rs_ok = rs_err = 0
    hits: List[Tuple[str, str, str, str, str, str]] = []
    for path in files:
        for line in data_lines(path, encoding="latin-1"):
            if "OTA_HotelRateAmountNotifRQ" in line:
                rq += 1
                ts = _TS_RE.search(line)
                for end, inv, rp, start in _SEG_RE.findall(line):
                    if (dts and not any(start <= d <= end for d in dts)) or (room and inv != room) or (rate and rp != rate):
                        continue
                    amt = _AMT_RE.search(line[line.find(end):])
                    hits.append((ts.group(1) if ts else "?", start, end, inv, rp, amt.group(1) if amt else "?"))
            if "NotifRS" in line:
                if "<Success" in line:
                    rs_ok += 1
                if "Errors" in line or "Fault" in line:
                    rs_err += 1
    lines = ["", "### LGCY ARI pushes (ChannelRelay legacy-stack sync partitions)"]
    if not files:
        lines.append("- no LGCY scans ran (leg skipped or s3grep.py missing)")
    elif rq == 0:
        lines.append(f"- LGCY scans ran ({len(files)} day-files) but ZERO RateAmountNotifRQ for this membership")
    else:
        lines.append(f"- RateAmountNotifRQ={rq}  RS Success={rs_ok}  RS Errors={rs_err}")
        lines.extend(f"  - {t} {s}..{e} {i} / {p} = {a}" for t, s, e, i, p, a in hits[:40])
    note(run_dir, "\n".join(lines))
    return {"files": len(files), "rq": rq, "rs_ok": rs_ok, "rs_err": rs_err}


def pt_ari_booking_xml_lgcy(
    run_dir: Path, cfg: Dict[str, Any], kb: Dict[str, Any],
    backend: Optional[Backend] = None, runner: Optional[StageRunner] = None,
) -> List[Path]:
    """Fallback to LGCY partitions for booking XML when CUR produced no hits."""
    scan = AriScan(run_dir, cfg, kb, backend, runner)
    booking_ref = scan.get("PT_BOOKING_REF")
    if not scan.channelrelay or not booking_ref or scan.legacy_day is None:
        return []
    if any(has_data(p) for p in scan.raw.glob("t_ari_xml_*.out")):
        return []
    suffix = scan.get("PT_LGCY_REGION_SUFFIX") or "r1"
    day_str = scan.legacy_day.isoformat()
    outputs: List[Path] = []
    for part in scan.partitions():
        out_path = scan.legacy(
            f"t_ari_owxml_{part}_{day_str}", f"lgcy-part/{part} {day_str} '{booking_ref}'",
            booking_ref, f"channelrelay*partition*{part}*log*{suffix}*", day_str,
        )
        outputs.append(out_path)
        if has_data(out_path):
            break
    return outputs


def _goal_verdict(run_dir: Path, cfg: Dict[str, Any], gates: Dict[str, Optional[bool]]) -> Optional[str]:
    goal = str(cfg.get("PT_GOAL") or "").strip()
    if not goal or goal == "custom":
        return None
    on = lambda gate: bool(gates.get(gate))
    lines = [f"# GOAL VERDICT - {goal}", ""]
    if goal == "overbooking":
        if on("CM_PERSIST") and on("CHANNEL_PUSH") and on("OTA_ACK"):
            lines.append("**NOT_OVERBOOKING** - CM held the value, the booking consumed it, and CM pushed the result with an ack.")
        elif on("CM_PERSIST") and not on("CHANNEL_PUSH"):
            lines.append("**PUSH_FAILURE** - CM state changed but no OTA-bound push was found in window.")
        elif not on("CM_PERSIST") and on("SOURCE"):
            lines.append("**STALE_INVENTORY / PMS_SIDE** - source events exist but CM never persisted a matching SET in window.")
        else:
            lines.append("**UNVERIFIED** - evidence chain incomplete; see REPORT.md gates (never read empty as absent).")
    elif goal == "rate-mismatch":
        if on("SOURCE") and on("CM_PERSIST"):
            lines.append("**VALUE_ATTRIBUTED** - origin + CM persistence proven; answer who/what/when from Attribution.")
        elif on("SOURCE") and not on("CM_PERSIST"):
            lines.append("**NOT_PERSISTED_IN_CM** - source seen, CM SET not found in window (delta-check: unchanged values leave no line).")
        else:
            lines.append("**ORIGIN_NOT_FOUND** - no source events in window; widen PT_FROM or run guiattrib.")
    elif goal == "billing-trace":
        lines.append("See REPORT.md billing transitions + ARI presence timeline; correlate suspension dates with bridge-absence days.")
    else:
        lines.append("See REPORT.md / NEXT_ACTIONS.md - trace/fidelity verdict governs this goal.")
    (run_dir / "GOAL_VERDICT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    note(run_dir, f"- goal verdict ({goal}): GOAL_VERDICT.md")
    return lines[2]


def pt_ari_verdict(
    run_dir: Path, kb: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Synthesize ARI verdict from ari_evidence.tsv, ari_actors.txt, and raw outputs."""
    rows = evidence_rows(run_dir)
    actors = list(data_lines(run_dir / "ari_actors.txt"))
    raw_dir = run_dir / "raw"
    push_lines = [line for path in sorted(raw_dir.glob("t_ari_push_*.out")) for line in data_lines(path)]
    ing_files = [path for path in raw_dir.glob("t_ari_ing_*.out") if has_data(path)]
    src_systems = sorted({r["originSystem"] for r in rows if r["originSystem"]})
    src_clients = sorted({r["client"] for r in rows if r["client"]})
    targets = [r for r in rows if r["kind"] == "TARGET_EVENT"]
    first_set = rows[0]["ts"] if rows else ""
    manual = any(MANUAL.search(s) for s in src_systems)
    pms_push = any(PMS.search(s) for s in src_systems)
    gates: Dict[str, Optional[bool]] = {
        "SOURCE": bool(src_systems) or bool(actors),
        "INGEST": None if (manual and not pms_push) else bool(ing_files),
        "CM_PERSIST": bool(rows),
        "CHANNEL_PUSH": bool(push_lines),
        "OTA_ACK": any(re.search(r"\b(200|Success|success|OK)\b", line) for line in push_lines),
    }
    ledger = [line.split("|") for line in data_lines(run_dir / "ledger.tsv")]
    n_empty = sum(1 for row in ledger if len(row) > 3 and row[3] == "lines=0")
    now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rep = ["", f"## ARI VERDICT (synthesized {now_str})", "", "### Attribution (who/what set the value)"]
    if src_systems:
        rep.append(f"- source system(s) on SET lines: **{', '.join(src_systems)}** "
                   f"(client: {', '.join(src_clients) or '?'}){f', first at {first_set}' if first_set else ''}")
    if actors:
        rep.append(f"- acting user(s) (portal-property gui-gateway): **{', '.join(actors)}**")
    if manual and not pms_push:
        rep.append("- reading: value set **manually/bulk in Platform** by the user above - no PMS involvement in this chain")
    elif pms_push:
        rep.append("- reading: value arrived via **PMS/CloudPMS push** - the PMS provider's own send log (echoToken/flowToken) is the upstream evidence")
    if not rows:
        rep.append("- no SET lines found for this hotel in the window - see elimination ledger "
                   "(delta-check caveat: a value equal to the current one leaves NO log line)")
    order = ["SOURCE", "INGEST", "CM_PERSIST", "CHANNEL_PUSH", "OTA_ACK"]
    rep.extend(["", "### Gates"])
    for gate in order:
        val = gates[gate]
        rep.append(f"- [~] {gate} - N/A (manual origin: no PMS ingest leg exists)" if val is None
                   else f"- [x] **{gate}**" if val else f"- [ ] {gate} - no evidence")
    if targets:
        rep.extend(["", "### Value events (target value + suspect stay date on the same line)"])
        rep.extend(f"- `{r['ts']}` values={r['values']} dates={r['dates']} sys={r['originSystem'] or '?'} tok={r['flowToken'] or '-'}"
                   for r in targets[:20])
    rep.extend(["", "### Elimination ledger", f"- coverage: {len(ledger)} stage(s) run, {n_empty} empty, {len(ledger) - n_empty} with hits"])
    if not rows:
        rep.append("- CM_PERSIST empty: avail-api swept; delta-check means unchanged values leave no line")
    if not push_lines:
        rep.append("- CHANNEL_PUSH empty: no channel-side lines for the swept identifiers in window")
    rep_text = "\n".join(rep) + "\n"
    note(run_dir, rep_text.rstrip("\n"))
    if not _goal_verdict(run_dir, cfg or {}, gates):
        (run_dir / "GOAL_VERDICT.md").write_text(rep_text, encoding="utf-8")

    nxt = ["# NEXT ACTIONS (ari mode)", ""]
    if manual and actors:
        nxt.append(f"{len(nxt) - 1}. **Manual change confirmed**: {', '.join(actors)} set the value(s) in Platform{f' at {first_set}' if first_set else ''}.")
    elif manual:
        nxt.append(f"{len(nxt) - 1}. **Manual change indicated** but no user email joined: pull gui-gateway line for flowToken(s) in ari_evidence.tsv.")
    if pms_push:
        nxt.append(f"{len(nxt) - 1}. **PMS push origin**: ask the PMS for the echoToken/message id covering these dates; INGEST evidence is in raw/t_ari_ing_*.")
    if gates["CHANNEL_PUSH"] and targets:
        nxt.append(f"{len(nxt) - 1}. **Customer-facing answer**: the value CM holds and pushed matches the SOURCE event(s) above.")
    if not gates["CHANNEL_PUSH"]:
        nxt.append(f"{len(nxt) - 1}. **Push gap**: CM holds the value but no channel push was found - check channel mapping and multiplier config.")
    if not rows:
        nxt.append(f"{len(nxt) - 1}. **No SET evidence**: widen PT_FROM/PT_TO or provide PT_RATE_CODE/roomRateKey to narrow.")
    if len(nxt) == 2:
        nxt.append("- chain complete; no chained action required")
    (run_dir / "NEXT_ACTIONS.md").write_text("\n".join(nxt) + "\n", encoding="utf-8")

    verdict = {
        "mode": "ari", "reached": [g for g in order if gates[g]],
        "missing": [g for g in order if gates[g] is False], "na": [g for g in order if gates[g] is None],
        "attributions": {"systems": src_systems, "clients": src_clients, "actors": actors},
        "value_events": len(targets), "failure_codes": {}, "terminal": None,
        "hits": len(rows) + len(push_lines), "context_files": 0,
    }
    (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=1), encoding="utf-8")
    return verdict


def _write_inputs_env(run_dir: Path, cfg: Dict[str, Any], channel_sys: str) -> None:
    keys = (
        "PT_CASE", "PT_REF", "PT_BOOKING_REF", "PT_CHANNEL", "PT_HOTEL_UUID", "PT_PROPERTY_ID",
        "PT_MEMBERSHIP", "PT_HOTELIER_ID", "PT_CHANNEL_HOTEL_UUID", "PT_TARGET_VALUES", "PT_STAY_DATES",
        "PT_RATE_CODE", "PT_ROOM_TYPE_CODE", "PT_SOURCE_SYSTEM", "PT_PMS_HOTEL_CODE", "PT_AT",
        "PT_AT_UNCERTAIN", "PT_TZ_OFFSET", "PT_FROM", "PT_TO",
    )
    lines = [f"{key}={cfg.get(key, '')}" for key in keys]
    if chan_sys := (cfg.get("PT_CHAN_SYS") or channel_sys):
        lines.append(f"PT_CHAN_SYS={chan_sys}")
    (run_dir / "inputs.env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _kb_writeback(scan: AriScan, kb_path: Optional[Union[str, Path]]) -> None:
    if kb_path is None:
        if "PT_KB_DIR" in scan.cfg:
            target = Path(scan.cfg["PT_KB_DIR"]) / scan.get("PT_KB_FILE", "connector_kb.json")
        else:
            target = _DEFAULT_KB_PATH
    else:
        target = Path(kb_path)
    if scan.plan or not target.is_file() or not any(has_data(p) for p in scan.raw.glob("t_ari_push_*.out")):
        return
    write_back(target, case=scan.get("PT_CASE"), ref=scan.get("PT_REF"), chan=scan.channel,
               pms=scan.get("PT_PMS"), chan_sys=scan.channel_sys, apps=scan.channel_apps, plan_mode=scan.plan)


def pt_ari(
    run_dir: Path, cfg: Dict[str, Any], kb: Dict[str, Any],
    backend: Optional[Backend] = None, runner: Optional[StageRunner] = None,
    *, kb_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Run ARI mode end-to-end."""
    if not cfg.get("PT_HOTEL_UUID") and not cfg.get("PT_PROPERTY_ID"):
        raise ValueError("PT_HOTEL_UUID or PT_PROPERTY_ID is required for ARI mode")
    scan = AriScan(run_dir, cfg, kb, backend, runner)
    note(run_dir, f"# ARI MODE v{__version__} - case {scan.get('PT_CASE')}\n"
                  f"- hotel={scan.hotel} propkey={scan.propkey} membership={scan.get('PT_MEMBERSHIP')} "
                  f"channel={scan.get('PT_CHANNEL') or 'none'} booking={scan.get('PT_BOOKING_REF') or 'none'}")
    scan.resolve()
    _write_inputs_env(run_dir, cfg, scan.channel_sys)
    scan.preflight()
    scan.inventory()
    scan.stage("ari_filter", "classify inventory SET evidence", scan.classify)
    scan.stage("ari_attribute", "platform attribution (gui-gateway -> user email)", scan.attribute)
    scan.stage("ari_ingest", "PMS/CloudPMS ingest leg (if push origin)", scan.ingest)
    scan.stage("ari_push", "channel push leg (CM -> OTA)", scan.push)
    scan.stage("ari_push_lgcy", "LGCY ChannelRelay ARI push leg (legacy-stack partitions)",
               lambda: pt_ari_push_lgcy(run_dir, cfg, kb, backend=scan.backend, runner=scan.runner))
    scan.stage("ari_lgcy_report", "LGCY ARI push report", lambda: pt_ari_lgcy_report(run_dir, cfg, kb))
    scan.stage("ari_bridge_values", "CloudPMS bridge pushed values (propkey)", scan.bridge_values)
    scan.stage("ari_billing", "billing-service status trace (suspend/reactivate timeline)", scan.billing)
    scan.stage("ari_series", "ARI presence day-series (bridge, before+after booking)", scan.series)
    scan.stage("ari_booking_xml", "booking XML pull (channel source)", scan.booking_xml)
    scan.stage("ari_booking_xml_lgcy", "LGCY booking XML fallback",
               lambda: pt_ari_booking_xml_lgcy(run_dir, cfg, kb, backend=scan.backend, runner=scan.runner))
    verdict = scan.stage("ari_verdict", "ari gates + attribution + next actions", lambda: pt_ari_verdict(run_dir, kb, cfg))
    _kb_writeback(scan, kb_path)
    return verdict
