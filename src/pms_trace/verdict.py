from __future__ import annotations

import datetime
import json
import pathlib
import re
from typing import Any

GATES: list[tuple[str, re.Pattern[str]]] = [
    ("ARRIVAL", re.compile(r"make_reservation|booked_at|booking.?request|reservation.?create", re.I)),
    ("INVENTORY", re.compile(r"sellable|availab|inventory", re.I)),
    ("ACCEPTANCE", re.compile(r'acceptance|status:?\s*"?20[02]|RESPONSE.*\b200\b|"statusCode":\s*20[02]', re.I)),
    ("CM_STORE", re.compile(r"hub-channel-store|Fetching reservation|cmBookingId", re.I)),
    ("PMS_RECEIVE", re.compile(r"pms-hub|pmsBookings|booking-events", re.I)),
    ("CONVERSION", re.compile(r'faultCode|ERR_CONVERSION|"status":\s*"Error"', re.I)),
    (
        "DELIVERY",
        re.compile(
            r'"status":\s*"(Success|Delivered|Completed|Processed)"|delivery.?success|sent to pms|Delivered reservation version',
            re.I,
        ),
    ),
]

_DEFAULT_LOW_SELECTIVITY = ["pmsType", "hubPmsPropertyUuid", "roomCode", "ratePlanCode"]


def _load_inputs_env(run_dir: pathlib.Path) -> dict[str, str]:
    loaded: dict[str, str] = {}
    inputs_path = run_dir / "inputs.env"
    if not inputs_path.exists():
        return loaded
    try:
        for line in inputs_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, val = line.split("=", 1)
                loaded[k] = val
    except OSError:
        pass
    return loaded


def synthesize_verdict(run_dir: pathlib.Path, kb: dict[str, Any], env: dict[str, str]) -> None:
    run_dir = pathlib.Path(run_dir)
    merged_env = _load_inputs_env(run_dir)
    if env:
        merged_env.update(env)

    ref = merged_env.get("PT_REF", "")
    sigs: dict[str, Any] = kb.get("failure_signatures", {})
    low: set[str] = set(kb.get("low_selectivity_kinds", _DEFAULT_LOW_SELECTIVITY))

    kind_of: dict[str, str] = {}
    tokens: set[str] = set()
    jk_path = run_dir / "join_keys.tsv"
    if jk_path.exists():
        for line in jk_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                kind_of[parts[1][:13]] = parts[0]
                if parts[0] not in low and len(parts[1]) >= 6:
                    tokens.add(parts[1])

    refpats: set[str] = set()
    if ref:
        refpats.add(ref)
        refpats.add(ref.upper())
        if "-" in ref:
            bare = ref.split("-", 1)[1]
            if bare:
                refpats.add(bare)
                refpats.add(bare.upper())

    raw_dir = run_dir / "raw"

    def files_of(pat: str) -> list[pathlib.Path]:
        if not raw_dir.exists():
            return []
        return sorted(raw_dir.glob(pat))

    def is_context(fname: pathlib.Path) -> bool:
        b = fname.name
        if not b.startswith("f_"):
            return False
        key13 = b[:-4].rsplit("_", 1)[-1]
        return kind_of.get(key13) in low

    all_f = files_of("f_*.out")
    evidence_files = files_of("t_*.out") + [f for f in all_f if not is_context(f)]
    context_files = [f for f in all_f if is_context(f)]

    # v0.7.7: full-day property sweeps are evidence ONLY where a line carries the ref
    # or a harvested high-selectivity token; every other line is a sibling reservation
    hits: list[str] = []
    fd_files = 0
    fd_admitted = 0
    for f in evidence_files:
        is_fd = "_fd_" in f.name or f.name.startswith("t_lad_")
        if is_fd:
            fd_files += 1
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip() or line.startswith("[fetch]"):
                continue
            if is_fd:
                if not (any(p in line for p in refpats) or any(t in line for t in tokens)):
                    continue
                fd_admitted += 1
            hits.append(line)

    # v0.9.1: canonical dedupe - the same event appears in t_*, f_* and t_fallback_icase files
    seen_ev: set[tuple[Any, ...]] = set()
    deduped: list[str] = []
    for line in hits:
        s = line[len("[fallback-icase] ") :] if line.startswith("[fallback-icase] ") else line
        k: tuple[Any, ...] | None = None
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                k = (obj.get("@timestamp") or obj.get("time") or obj.get("date"), obj.get("msg"))
        except Exception:
            pass
        k = k or ("raw", s)
        if k in seen_ev:
            continue
        seen_ev.add(k)
        deduped.append(line)
    hits = deduped

    preflight_ok: dict[str, bool] = {}
    for f in files_of("preflight_*.stdout"):
        sysname = f.name[len("preflight_") :].rsplit(".stdout", 1)[0].replace("_", "-")
        preflight_ok[sysname] = f.stat().st_size > 0

    skip_gates: dict[str, str] = {}
    chan_sys = merged_env.get("PT_CHAN_SYS", "")
    sc = kb.get("channelrelay", {})
    if chan_sys and chan_sys == sc.get("system"):
        for g in sc.get("skip_gates", []):
            skip_gates[g] = f"no {g} stage for ChannelRelay push channels (KB channelrelay.skip_gates)"

    gate_hits: dict[str, list[str]] = {g: [] for g, _ in GATES}
    seen_gate_lines: set[tuple[str, str]] = set()
    failure_codes: dict[str, int] = {}
    email_silence = False
    times: dict[str, Any] = {}

    for line in hits:
        for g, rx in GATES:
            if rx.search(line) and (g, line) not in seen_gate_lines:
                gate_hits[g].append(line)
                seen_gate_lines.add((g, line))
        m = re.search(r'faultCode"?\s*[:=]\s*"?([A-Z_]+)', line)
        if m:
            code = m.group(1)
            failure_codes[code] = failure_codes.get(code, 0) + 1
        if re.search(r"notification email", line, re.I):
            email_silence = True
        try:
            obj = json.loads(line)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            t = obj.get("time") or obj.get("@timestamp") or obj.get("date")
            if t:
                for g, rx in GATES:
                    if rx.search(line) and g not in times:
                        times[g] = t

    terminal: str | None = None
    for code in failure_codes:
        if sigs.get(code, {}).get("terminal"):
            terminal = code
            break

    failed_gates: dict[str, list[str]] = {}
    if failure_codes:
        conv = [c for c in failure_codes if c.startswith("ERR_CONVERSION")]
        others = [c for c in failure_codes if not c.startswith("ERR_CONVERSION")]
        if conv:
            failed_gates["CONVERSION"] = conv
        if others or conv:
            failed_gates["DELIVERY"] = others or conv

    order = [g for g, _ in GATES]
    na_gates: set[str] = set()
    if terminal and "CONVERSION" in order:
        for g in order[order.index("CONVERSION") + 1 :]:
            if not gate_hits[g]:
                na_gates.add(g)

    reached = [g for g in order if gate_hits[g]]
    missing = [
        g
        for g in order
        if not gate_hits[g] and g not in na_gates and g not in skip_gates and g not in failed_gates
    ]

    rep: list[str] = []
    rep.append("")
    ts_now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rep.append(f"## VERDICT (synthesized {ts_now})")
    rep.append("")
    for g in order:
        if g in failed_gates:
            rep.append(
                f"- [!] **{g} - FAILED** - failure event(s) bound to this booking: {', '.join(failed_gates[g])} ({len(gate_hits[g])} hit(s) also seen)"
            )
        elif gate_hits[g]:
            first_at = f", first at {times[g]}" if g in times else ""
            rep.append(f"- [x] **{g}** - {len(gate_hits[g])} hit(s){first_at}")
        elif g in na_gates:
            rep.append(f"- [~] {g} - N/A: terminal failure `{terminal}` at CONVERSION (downstream never attempted)")
        elif g in skip_gates:
            rep.append(f"- [~] {g} - N/A: {skip_gates[g]}")
        else:
            rep.append(f"- [ ] {g} - no hits in evidence")
    rep.append("")
    if context_files:
        rep.append(
            f"_context sweeps excluded from evidence: {len(context_files)} file(s) from low-selectivity keys ({', '.join(sorted(low))}) - co-tenant traffic, never gate evidence_"
        )
    if fd_files:
        rep.append(
            f"_property/full-day sweeps: {fd_files} property-keyed file(s), {fd_admitted} line(s) admitted by ref/token match; sibling-reservation lines excluded (v0.7.7/v0.7.9 co-tenant guard)_"
        )
    rep.append("")
    if failure_codes:
        rep.append("### Failure signatures")
        for code, n in sorted(failure_codes.items()):
            sig = sigs.get(code)
            if sig:
                rep.append(f"- `{code}` (x{n}): {sig.get('meaning', '')}")
                if sig.get("fix_path"):
                    rep.append(f"  - fix: {sig['fix_path']}")
                if sig.get("silence_caveat"):
                    rep.append(f"  - caveat: {sig['silence_caveat']}")
            else:
                rep.append(f"- `{code}` (x{n}): NOT in KB failure_signatures - add it after this case")
        rep.append("")
    if email_silence:
        rep.append("### Silence caveat")
        rep.append(
            "- The hotel has **no notification email configured** - this failure was SILENT for the property (pms-hub error-handler tried to notify and failed)."
        )
        rep.append("")

    ledger_path = run_dir / "ledger.tsv"
    try:
        ledger = [l.split("|") for l in ledger_path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except OSError:
        ledger = []
    n_scans = len(ledger)
    n_empty = sum(1 for r in ledger if len(r) > 3 and r[3] == "lines=0")
    n_hit = sum(1 for r in ledger if len(r) > 3 and r[3].startswith("lines=") and r[3] != "lines=0")
    pf_txt = " ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in sorted(preflight_ok.items()))
    gate_why = {
        "ARRIVAL": "channel inbound swept per-app across the trusted pool (+catalog-only on empty), patterns=uppercase ref + bare code (CM IDs stored UPPERCASE), hour fan-out + full-day fallback + icase sweep",
        "INVENTORY": "inventory state only visible via listing_id follow scans; none of the harvested keys produced an inventory hit",
        "ACCEPTANCE": "acceptance events swept with the arrival patterns; booking-processor joined by flowToken",
        "CM_STORE": "hub-channel-store scanned directly and via every harvested flowToken/UUID",
        "PMS_RECEIVE": "pms-hub booking-api + pms-booking-events scanned with all pattern variants (hour-bounded) and all harvested keys",
        "CONVERSION": "no faultCode/status=Error observed in any evidence hit",
        "DELIVERY": f"no Success/Delivered/Completed status observed in any of the {len(hits)} evidence hit(s)",
    }
    rep.append("### Elimination ledger (why an empty stage is believed)")
    rep.append(
        f"- coverage: {n_scans} stage(s) run, {n_empty} empty, {n_hit} with hits; preflight: {pf_txt or 'n/a'}"
    )
    for g in missing:
        rep.append(f"- **{g} empty**: {gate_why.get(g, 'swept, no evidence')}")
    if not missing and not na_gates:
        rep.append("- no empty stages; chain fully lit")
    rep.append("")

    report_text = "\n".join(rep) + "\n"
    with (run_dir / "REPORT.md").open("a", encoding="utf-8") as fh:
        fh.write(report_text)
    print("\n".join(rep))

    verdict_data = {
        "reached": reached,
        "missing": missing,
        "na": sorted(na_gates),
        "skipped": sorted(skip_gates),
        "fd_files": fd_files,
        "fd_admitted": fd_admitted,
        "failure_codes": failure_codes,
        "email_silence": email_silence,
        "terminal": terminal,
        "hits": len(hits),
        "context_files": len(context_files),
    }
    (run_dir / "verdict.json").write_text(json.dumps(verdict_data, indent=1), encoding="utf-8")
