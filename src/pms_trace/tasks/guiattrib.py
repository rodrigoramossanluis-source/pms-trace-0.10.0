"""GUI attribution task: link-api burst, token correlation, and GUIACTORS.md."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional


def _run_stage(runner: Optional[Any], name: str, label: str, fn: Any) -> Any:
    if runner is not None and hasattr(runner, "run"):
        return runner.run(name, label, fn)
    return fn()


def _jl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("{"):
            try:
                out.append(json.loads(s))
            except Exception:
                continue
    return out


def _backend_grep(backend: Any, runner: Any, sn: str, label: str, app: str, blob: str, pat: str, out_path: Path) -> None:
    if backend is None:
        return
    if hasattr(backend, "fetch"):
        def _fn() -> Path:
            data = backend.fetch(app, blob, pat)
            out_path.write_text(data or "", encoding="utf-8")
            return out_path
        _run_stage(runner, sn, label, _fn)
    elif hasattr(backend, "grep"):
        if runner is not None and hasattr(runner, "run_stage"):
            runner.run_stage(sn, label, lambda: backend.grep(app, blob, pat), plan_cmd=f"s3search grep {app} {blob} '{pat}'")
        else:
            res = backend.grep(app, blob, pat)
            out_path.write_text(res.stdout or "", encoding="utf-8")


def _backend_query(backend: Any, runner: Any, sn: str, label: str, app: str, blob: str, q: str, out_path: Path) -> None:
    if backend is None:
        return
    if hasattr(backend, "qfetch"):
        def _fn() -> Path:
            data = backend.qfetch(app, blob, q)
            out_path.write_text(data or "", encoding="utf-8")
            return out_path
        _run_stage(runner, sn, label, _fn)
    elif hasattr(backend, "query"):
        if runner is not None and hasattr(runner, "run_stage"):
            runner.run_stage(sn, label, lambda: backend.query(app, blob, q), plan_cmd=f"s3search query {app} {blob} '{q}'")
        else:
            res = backend.query(app, blob, q)
            out_path.write_text(res.stdout or "", encoding="utf-8")


def pt_guiattrib(
    run_dir: Path,
    cfg: Dict[str, Any],
    kb: Optional[Dict[str, Any]] = None,
    backend: Optional[Any] = None,
    runner: Optional[Any] = None,
) -> Optional[Path]:
    """Run GUI attribution and write GUIACTORS.md."""
    _ = kb
    gui_date = str(cfg.get("PT_GUI_DATE") or "").strip()
    gui_hour = str(cfg.get("PT_GUI_HOUR") or "").strip()
    if not gui_date or not gui_hour:
        return None

    dh = f"{gui_date}-{gui_hour}"
    region = str(cfg.get("PT_REGION") or "ra")
    if region == "both":
        region = "ra"

    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    keys = [
        "PT_GUI_DATE",
        "PT_GUI_HOUR",
        "PT_GUI_USER",
        "PT_PROPERTY_ID",
        "PT_CHANNEL_HOTEL_UUID",
        "PT_MEMBERSHIP",
    ]
    (run_dir / "guiattrib_inputs.env").write_text(
        "\n".join(f"{k}={cfg.get(k, '')}" for k in keys) + "\n",
        encoding="utf-8",
    )

    ch_uuid = str(cfg.get("PT_CHANNEL_HOTEL_UUID") or "").strip()
    burst_path = raw_dir / f"gui_channelapi_{dh}.out"
    tokens: List[str] = []

    if ch_uuid:
        _backend_grep(
            backend,
            runner,
            "gui_burst",
            f"link-api {dh} by channelPropertyUuid",
            "hub-channel-link/link-api",
            dh,
            ch_uuid,
            burst_path,
        )
        # runner.run_stage writes raw/<stage>.out; mirror to gui_channelapi_<dh>.out for compatibility
        stage_burst = raw_dir / "gui_burst.out"
        if not burst_path.exists() and stage_burst.exists():
            burst_path.write_bytes(stage_burst.read_bytes())

        counts: Counter[str] = Counter()
        for obj in _jl(burst_path):
            tok = obj.get("flowToken")
            if tok:
                counts[str(tok)] += 1
        ranked = counts.most_common()
        (run_dir / "gui_tokens.txt").write_text(
            "\n".join(f"{cnt} {tok}" for tok, cnt in ranked) + ("\n" if ranked else ""),
            encoding="utf-8",
        )
        tokens = [tok for tok, _ in ranked[:8]]

    for idx, tok in enumerate(tokens, start=1):
        sn = f"gui_tok_{idx}"
        tok_out = raw_dir / f"{sn}.out"
        pp_out = raw_dir / f"{sn}_pp.out"
        _backend_query(backend, runner, sn, f"link-api token {tok[:8]}", "hub-channel-link/link-api", dh, f's.flowToken = "{tok}"', tok_out)
        _backend_query(backend, runner, f"{sn}_pp", f"portal-property token {tok[:8]}", "portal-property/*", dh, f's.flowToken = "{tok}"', pp_out)

    gui_user = str(cfg.get("PT_GUI_USER") or "").strip()
    if gui_user:
        _backend_query(
            backend,
            runner,
            "gui_sweep",
            f"gui-gateway ops by {gui_user}",
            "portal-property/gui-gateway",
            dh,
            f's.email = "{gui_user}"',
            raw_dir / "gui_sweep.out",
        )

    prop_id = str(cfg.get("PT_PROPERTY_ID") or "").strip()
    if prop_id:
        _backend_query(
            backend,
            runner,
            "gui_session",
            f"platform session {dh}",
            "platform-session/api",
            dh,
            f's.propertyId = "{prop_id}"',
            raw_dir / "gui_session.out",
        )

    membership = str(cfg.get("PT_MEMBERSHIP") or "").strip()
    if membership:
        _backend_query(
            backend,
            runner,
            "gui_core",
            f"entity-api audit write ({region})",
            f"cm/entity-api-{region}",
            dh,
            f's.message LIKE "%{membership}%"',
            raw_dir / "gui_core.out",
        )

    rows = []
    for pp_file in sorted(raw_dir.glob("gui_tok_*_pp.out")):
        tok = email = ip = comp = mut = ts = None
        for obj in _jl(pp_file):
            tok = tok or obj.get("flowToken")
            comp = (obj.get("kubernetes") or {}).get("labels", {}).get("component") or comp
            msg = str(obj.get("msg") or "")
            if obj.get("email"):
                email = str(obj["email"])
            if obj.get("ipAddress"):
                ip = str(obj["ipAddress"])
            if msg.startswith("Mutation"):
                mut = msg
            if "operation was successful" in msg:
                ts = str(obj.get("@timestamp") or obj.get("ts") or "")
        if tok or email:
            rows.append((tok, comp, email, ip, mut, ts))

    out_lines = [
        "# GUI attribution - per-token actor table",
        "",
        "| flowToken | component | actor email | client IP | mutation | success ts (UTC) |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        out_lines.append("| %s | %s | %s | %s | %s | %s |" % tuple(x or "-" for x in r))

    sweep = _jl(raw_dir / "gui_sweep.out")
    muts = sorted({str(o.get("msg") or "") for o in sweep if str(o.get("msg") or "").startswith("Mutation")})
    out_lines += ["", "## mutations by GUI user in the hour (email sweep)"]
    out_lines += ["- " + m for m in muts] or ["- (none / sweep skipped)"]

    core = _jl(raw_dir / "gui_core.out")
    hits = [o for o in core if re.search(r"rate.?modification", json.dumps(o), re.I)]
    out_lines += ["", "## entity-api audit writes matching rate modification"]
    out_lines += ["- %s %s" % (o.get("@timestamp"), str(o.get("message") or o.get("msg") or "")[:300]) for o in hits] or ["- (none found)"]

    out_md = run_dir / "GUIACTORS.md"
    out_md.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"[gui] GUIACTORS.md: {len(rows)} tokens, {len(muts)} user mutations, {len(hits)} core audit hits")
    return out_md
