"""Bridge-flush goal task: CloudPMS -> pms-cm-bridge -> entity-api failure tracer."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


def _sanitize(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", value.replace("/", "_").replace(" ", "_").replace("*", "_"))


def _run_stage(runner: Optional[Any], name: str, label: str, fn: Any) -> Any:
    if runner is not None and hasattr(runner, "run"):
        return runner.run(name, label, fn)
    return fn()


def _grep_stage(
    run_dir: Path,
    backend: Optional[Any],
    runner: Optional[Any],
    sn: str,
    label: str,
    app: str,
    blob: str,
    pat: str,
) -> Path:
    out_path = run_dir / "raw" / f"{_sanitize(sn)}.out"
    if backend is None:
        return out_path
    if runner is not None and hasattr(runner, "run_stage"):
        runner.run_stage(
            sn,
            label,
            lambda: backend.grep(app, blob, pat),
            plan_cmd=f"s3search grep {app} {blob} '{pat}'",
        )
    elif hasattr(backend, "grep"):
        res = backend.grep(app, blob, pat)
        out_path.write_text(res.stdout or "", encoding="utf-8")
    return out_path


def _parse_at(cfg: Dict[str, Any]) -> tuple[str, str, str]:
    at = str(cfg.get("PT_AT") or "").strip()
    from_date = str(cfg.get("PT_FROM") or "").strip()
    if at:
        parts = re.split(r"[T ]", at, maxsplit=1)
        at_date = parts[0]
        at_hour = parts[1].split(":", 1)[0].zfill(2) if len(parts) > 1 else "23"
    else:
        at_date = str(cfg.get("PT_AT_DATE") or from_date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"))
        at_hour = str(cfg.get("PT_AT_HOUR") or "23").zfill(2)
    if not from_date:
        from_date = at_date
    return at_date, at_hour, from_date


def _bridgeflush_hours_scan(
    run_dir: Path,
    backend: Optional[Any],
    runner: Optional[Any],
    app: str,
    pat: str,
    pfx: str,
    at_date: str,
    at_hour: str,
    from_date: str,
) -> List[str]:
    hours_file = run_dir / "state" / f"{pfx}_hours.txt"
    hours_found: List[str] = []
    day = dt.date.fromisoformat(at_date)
    end_day = dt.date.fromisoformat(from_date)
    start_h = int(at_hour)

    while True:
        day_str = day.isoformat()
        hlist = [f"{h:02d}" for h in range(start_h if day_str == at_date else 23, -1, -1)]
        day_hit = False
        for hh in hlist:
            h = f"{day_str}-{hh}"
            sn = f"{pfx}_{_sanitize(f'{app}_{h}')}"
            out_path = _grep_stage(run_dir, backend, runner, sn, f"{app} {h} '{pat}'", app, h, pat)
            if out_path.is_file() and out_path.stat().st_size > 0:
                hours_found.append(h)
                day_hit = True
        if day_hit or day <= end_day:
            break
        day -= dt.timedelta(days=1)

    hours_file.write_text("\n".join(hours_found) + ("\n" if hours_found else ""), encoding="utf-8")
    return hours_found


def _bridgeflush_classify(run_dir: Path, propkey: str) -> Dict[str, Any]:
    raw_dir = run_dir / "raw"
    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    user_re = re.compile(r'(?:user|email|username|actor)["\s:=]+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)', re.I)
    status_re = re.compile(r"\s([1-5]\d\d)\s")

    flushes: Dict[str, Dict[str, Any]] = {}
    for f in sorted(raw_dir.glob("t_bfl_trig_*.out")):
        hour = f.stem.rsplit("_", 1)[-1]
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "flush" not in line:
                continue
            for tok in uuid_re.findall(line):
                d = flushes.setdefault(tok, {"token": tok, "hours": set(), "user": None, "overall": set(), "sublegs": {}})
                d["hours"].add(hour)
                m = status_re.search(line)
                if m:
                    d["overall"].add(m.group(1))
                u = user_re.search(line)
                if u and not d["user"]:
                    d["user"] = u.group(1)

    for f in sorted(raw_dir.glob("t_bfl_req_*.out")):
        parts = f.name.split("_")
        tok = parts[3] if len(parts) > 3 else ""
        if not tok:
            continue
        d = flushes.setdefault(tok, {"token": tok, "hours": set(), "user": None, "overall": set(), "sublegs": {}})
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = status_re.search(line)
            st = m.group(1) if m else None
            if not st:
                continue
            u = user_re.search(line)
            if u and not d["user"]:
                d["user"] = u.group(1)
            low = line.lower()
            if "type=inventory" in line or "type%3dinventory" in low:
                d["sublegs"].setdefault("inventory", set()).add(st)
            elif "type=core" in line or "type%3dcore" in low:
                d["sublegs"].setdefault("core", set()).add(st)
            elif "hub-pms-sync" in line:
                d["sublegs"].setdefault("hub-pms-sync", set()).add(st)
            if "resync_pms_link" in line:
                d["overall"].add(st)

    sync: Dict[str, Any] = {"lines": 0, "update_failed_400": 0, "flush_failed": 0, "tokens": set(), "other_msgs": []}
    for f in sorted(raw_dir.glob("t_bfl_sync_*.out")):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            sync["lines"] += 1
            if "setRoomRate failed" in line and "400" in line:
                sync["update_failed_400"] += 1
            if "Flushing property data failed" in line:
                sync["flush_failed"] += 1
            for tok in uuid_re.findall(line):
                sync["tokens"].add(tok)
            if "setRoomRate failed" not in line and "Flushing property data failed" not in line and len(sync["other_msgs"]) < 5:
                m = re.search(r'"msg"\s*:\s*"([^"]{1,120})"', line)
                if m:
                    sync["other_msgs"].append(m.group(1))

    rate_re = re.compile(
        r"(?:PUT|POST|PATCH)\s+(?:https?://\S+)?/properties/"
        + re.escape(propkey)
        + r'/room-types/([A-Za-z0-9_-]+)/room-rates/([A-Za-z0-9_-]+)\S*"?\s+(\d{3})\b'
    )
    type_re = re.compile(
        r"(?:PUT|POST|PATCH)\s+(?:https?://\S+)?/properties/"
        + re.escape(propkey)
        + r'/room-types/([A-Za-z0-9_-]+)\S*"?\s+(\d{3})\b'
    )
    rate_puts: Dict[str, Dict[str, int]] = {}
    type_puts: Dict[str, Dict[str, int]] = {}
    for f in sorted(raw_dir.glob("t_bfl_core_*.out")):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = rate_re.search(line)
            if m:
                key = f"{m.group(1)}/{m.group(2)}"
                rate_puts.setdefault(key, {})
                rate_puts[key][m.group(3)] = rate_puts[key].get(m.group(3), 0) + 1
                continue
            m = type_re.search(line)
            if m:
                type_puts.setdefault(m.group(1), {})
                type_puts[m.group(1)][m.group(2)] = type_puts[m.group(1)].get(m.group(2), 0) + 1

    culprit = sorted(k for k, v in rate_puts.items() if any(s.startswith(("4", "5")) for s in v))
    ok_rates = sorted(k for k, v in rate_puts.items() if not any(s.startswith(("4", "5")) for s in v))

    legfail: Dict[str, int] = {}
    for d in flushes.values():
        for leg, sts in d["sublegs"].items():
            if any(s.startswith("5") for s in sts):
                legfail[leg] = legfail.get(leg, 0) + 1
        if not d["overall"] and d["sublegs"]:
            sts_all = [s for v in d["sublegs"].values() for s in v]
            if sts_all:
                d["overall"] = {"500"} if any(s.startswith("5") for s in sts_all) else set(sts_all)

    failing_subleg: Optional[str] = None
    if legfail:
        top = sorted(legfail.items(), key=lambda kv: -kv[1])
        failing_subleg = top[0][0] if len(top) == 1 or top[0][1] > top[1][1] else "AMBIGUOUS:" + ",".join(legfail)

    out = {
        "flushes": [
            {
                **d,
                "hours": sorted(d["hours"]),
                "overall": sorted(d["overall"]),
                "sublegs": {k: sorted(v) for k, v in d["sublegs"].items()},
            }
            for d in flushes.values()
        ],
        "sync": {**sync, "tokens": sorted(sync["tokens"])},
        "rate_puts": rate_puts,
        "type_puts": type_puts,
        "culprit_ids": culprit,
        "ok_rate_count": len(ok_rates),
        "failing_subleg": failing_subleg,
        "token_chain": sorted(set(sync["tokens"]) & set(flushes.keys())),
    }
    (run_dir / "state" / "bfl_analysis.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"[bridgeflush] classify: flushes={len(out['flushes'])} failing_subleg={failing_subleg} culprits={culprit} ok_rates={len(ok_rates)}")
    return out


def pt_bridgeflush_verdict(
    run_dir: Path,
    propkey: str,
    kb: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute the 8 bridge-flush gates and write REPORT.md + verdict.json."""
    analysis_path = run_dir / "state" / "bfl_analysis.json"
    if analysis_path.exists():
        an = json.loads(analysis_path.read_text(encoding="utf-8"))
    else:
        an = {
            "flushes": [],
            "sync": {"update_failed_400": 0, "flush_failed": 0, "tokens": []},
            "culprit_ids": [],
            "ok_rate_count": 0,
            "failing_subleg": None,
            "token_chain": [],
            "type_puts": {},
        }

    raw_dir = run_dir / "raw"
    origin_hits = [
        f for f in sorted(raw_dir.glob("t_bfl_origin_*.out"))
        if f.is_file() and f.stat().st_size > 0
    ]

    flushes = an.get("flushes") or []
    sync = an.get("sync") or {}
    culprits = an.get("culprit_ids") or []
    okn = int(an.get("ok_rate_count") or 0)
    subleg = an.get("failing_subleg")
    token_chain = an.get("token_chain") or []

    gates = {
        "FLUSH_TRIGGERED": len(flushes) > 0,
        "SUBLEG_ISOLATED": subleg is not None and not str(subleg).startswith("AMBIGUOUS"),
        "CM_PMS_SYNC_400": int(sync.get("update_failed_400") or 0) > 0,
        "CORE_BLOCKER_DERIVED": len(culprits) >= 1,
        "FAIL_FAST_SINGLE": len(culprits) == 1 and okn >= 2,
        "TOKEN_CHAIN": len(token_chain) > 0,
        "ACTOR_CAPTURED": any(d.get("user") for d in flushes),
        "ORIGIN_FOUND": len(origin_hits) > 0,
    }

    sigs = (kb or {}).get("bridge_flush", {}).get("validation_signatures", [])
    pitfalls = (kb or {}).get("bridge_flush", {}).get("pitfalls", [])

    lines: List[str] = [
        "",
        "## BRIDGE-FLUSH VERDICT (derived from evidence; inputs were propkey + window only)",
        "",
        "### Gates",
    ]
    for k, v in gates.items():
        mark = "x" if v else " "
        suffix = "" if v else " - no evidence"
        lines.append(f"- [{mark}] {k}{suffix}")

    lines.append("")
    lines.append(f"### Flush runs ({len(flushes)})")
    for d in flushes:
        legs = " ".join(f"{k}={'/'.join(v)}" for k, v in sorted((d.get("sublegs") or {}).items()))
        overall = "/".join(d.get("overall") or []) or "?"
        user = d.get("user") or "?"
        lines.append(f"- {d.get('token')} overall={overall} user={user} | {legs or 'no sub-leg lines'}")

    lines.append("")
    if not flushes:
        lines.append(
            "**No flush triggers in window** - elimination fact within the swept hours only. "
            "Widen PT_FROM/PT_TO or confirm a flush was actually clicked."
        )
    elif subleg != "hub-pms-sync":
        lines.append(f"### PLAYBOOK BRANCH: failing sub-leg is **{subleg}**, NOT hub-pms-sync")
        lines.append(
            f"- The hub-pms-sync 400 playbook does NOT apply. Evidence points at the '{subleg}' leg; "
            "investigate that hop (raw/t_bfl_req_* carries the failing lines)."
        )
    else:
        lines.append("### Failing sub-leg: hub-pms-sync (inventory/core 204)")
        lines.append(
            f"- hub-sync: setRoomRate-failed-400 lines={sync.get('update_failed_400', 0)}, "
            f"flush wrappers={sync.get('flush_failed', 0)}, "
            f"tokens chained to cloudpms/main={', '.join(token_chain) or 'NONE'}"
        )
        if culprits:
            lines.append(f"- **Blocking room rate (DERIVED): {', '.join(culprits)}**")
            for c in culprits:
                rt, rr = c.split("/", 1)
                lines.append(
                    f"  - GET http://cloudpms-entity-api/properties/{propkey}/room-types/{rt}/room-rates/{rr}  "
                    "(400 body is logged nowhere - inspect values directly)"
                )
            if gates["FAIL_FAST_SINGLE"]:
                lines.append(
                    f"- Fail-fast pattern CONFIRMED: {okn} distinct room rates updated 200, exactly 1 rejected - "
                    "one invalid rate blocks the whole property flush."
                )
            elif len(culprits) > 1:
                lines.append(
                    f"- MULTI-BLOCKER: {len(culprits)} distinct room rates rejected - "
                    "not the classic single-blocker; fix all listed."
                )
            type_puts = an.get("type_puts") or {}
            t200 = [k for k, v in type_puts.items() if "200" in v]
            if t200:
                lines.append(
                    f"- Room-TYPE updates 200 while room-RATE 400s (type endpoints healthy: {', '.join(t200[:5])}) - "
                    "rate-level validation, not a type problem."
                )
            if sigs:
                lines.append("")
                lines.append("### Validation signature checklist (KB bridge_flush - compare the GET against these)")
                for s in sigs:
                    lines.append(f"- {'/'.join(s.get('fields', []))}: {s.get('rule', '?')} [{s.get('reference', '')}]")
        else:
            lines.append(
                "- hub-sync 400s seen but NO failing PUT isolated in cloudpms/entity-api at the chained hours - "
                "partial evidence; check raw/t_bfl_core_* and widen hours."
            )

        if origin_hits:
            lines.append(
                f"- ORIGIN: {len(origin_hits)} platform-side hit(s) for the culprit id - see raw/t_bfl_origin_* "
                "(who/when set the invalid values)."
            )
        elif culprits:
            lines.append(
                "- ORIGIN: no platform-side lines for the culprit id at flush hours (bounded scan; "
                "set PT_FLUSH_ORIGIN_DAY=1 for the full flush day, or trace earlier days manually)."
            )

    if pitfalls:
        lines.append("")
        lines.append("### KB pitfalls")
        for p in pitfalls[:6]:
            lines.append(f"- {p}")

    report_path = run_dir / "REPORT.md"
    existing = report_path.read_text(encoding="utf-8", errors="replace") if report_path.exists() else ""
    report_path.write_text(existing + "\n".join(lines) + "\n", encoding="utf-8")

    verdict = {
        "gates": gates,
        "reached": [k for k, v in gates.items() if v],
        "missing": [k for k, v in gates.items() if not v],
        "failing_subleg": subleg,
        "culprit_ids": culprits,
    }
    (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=1), encoding="utf-8")
    return verdict


def pt_bridgeflush(
    run_dir: Path,
    cfg: Dict[str, Any],
    kb: Optional[Dict[str, Any]] = None,
    backend: Optional[Any] = None,
    runner: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run bridge-flush scanning, classification, origin attribution, and gate synthesis."""
    propkey = str(cfg.get("PT_PROPERTY_ID") or "").strip()
    if not propkey:
        raise ValueError("PT_PROPERTY_ID is required for bridgeflush mode")

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state").mkdir(parents=True, exist_ok=True)
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)

    # Pre-seeded state/bfl_analysis.json without backend -> preserve existing unit test behavior
    if backend is None and (run_dir / "state" / "bfl_analysis.json").exists():
        return _run_stage(
            runner,
            "bfl_verdict",
            f"bridge-flush verdict for {propkey}",
            lambda: pt_bridgeflush_verdict(run_dir, propkey, kb=kb),
        )

    region = str(cfg.get("PT_REGION") or "ra").strip()
    if region == "both":
        region = "ra"
    cp_id = str(cfg.get("PT_CLOUDPMS_PROPERTY_ID") or "").strip()
    at_date, at_hour, from_date = _parse_at(cfg)

    # 1. Trigger scan on cloudpms/main-<region>
    trig_pat = f"properties/{cp_id}/flush" if cp_id else propkey
    main_app = f"cloudpms/main-{region}"
    hours_found = _bridgeflush_hours_scan(run_dir, backend, runner, main_app, trig_pat, "t_bfl_trig", at_date, at_hour, from_date)

    # Request-id follow-ups
    uuid_re = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
    toks: Set[str] = set()
    for f in sorted((run_dir / "raw").glob("t_bfl_trig_*.out")):
        if f.stat().st_size > 0:
            for t in uuid_re.findall(f.read_text(encoding="utf-8", errors="ignore")):
                toks.add(t.lower())
    sorted_toks = sorted(toks)
    (run_dir / "state" / "bfl_tokens.txt").write_text("\n".join(sorted_toks) + ("\n" if sorted_toks else ""), encoding="utf-8")
    for tok in sorted_toks[:6]:
        for h in hours_found:
            _grep_stage(run_dir, backend, runner, f"t_bfl_req_{tok}_{h}", f"{main_app} {h} request {tok}", main_app, h, tok)

    # 2. Chained hub-sync-api and cloudpms/entity-api scans
    for h in hours_found:
        _grep_stage(run_dir, backend, runner, f"t_bfl_sync_{_sanitize(f'pms-cm-bridge/hub-sync-api_{h}')}", f"pms-cm-bridge/hub-sync-api {h} propkey", "pms-cm-bridge/hub-sync-api", h, propkey)
        _grep_stage(run_dir, backend, runner, f"t_bfl_core_{_sanitize(f'cloudpms/entity-api_{h}')}", f"cloudpms/entity-api {h} propkey", "cloudpms/entity-api", h, propkey)

    # 3. Classify
    an = _bridgeflush_classify(run_dir, propkey)

    # 4. Origin scan for derived culprits
    culprits = an.get("culprit_ids") or []
    if culprits:
        origin_hours = list(hours_found)
        if str(cfg.get("PT_FLUSH_ORIGIN_DAY") or "0") == "1":
            origin_hours = []
            for d in sorted({h[:10] for h in hours_found}):
                for hh in range(23, -1, -1):
                    origin_hours.append(f"{d}-{hh:02d}")
        for pair in culprits:
            rr = pair.split("/", 1)[1] if "/" in pair else pair
            for app in ("portal-property/gui-gateway", "portal-property/job-runner"):
                for h in origin_hours:
                    _grep_stage(run_dir, backend, runner, f"t_bfl_origin_{_sanitize(f'{app}_{h}_{rr}')}", f"{app} {h} culprit {rr}", app, h, rr)

    # 5. Verdict
    return pt_bridgeflush_verdict(run_dir, propkey, kb=kb)
