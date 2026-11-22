"""Trace orchestration module ported from pms_trace.sh."""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import __version__, kb
from .backend import Backend, BackendResult, StageRunner
from .verdict import synthesize_verdict

KIND_RES: List[Tuple[str, re.Pattern[str]]] = [
    ("flowToken", re.compile(r'flowToken"?\s*[:=]\s*"?(?P<v>[0-9a-f]{8}-[0-9a-f-]{27,36})')),
    ("hubBookingUuid", re.compile(r'hubBookingUuid"?\s*[:=]\s*"?(?P<v>[0-9a-f-]{36})')),
    ("hubPmsPropertyUuid", re.compile(r'hubPmsPropertyUuid"?\s*[:=]\s*"?(?P<v>[0-9a-f-]{36})')),
    ("pmsBookingId", re.compile(r'pmsBookingId"?\s*[:=]\s*"?(?P<v>[A-Za-z0-9-]{4,40})')),
    ("echoToken", re.compile(r'[Ee]choToken\\?"?\s*[:=]?\s*\\?"?(?P<v>[A-Za-z0-9-]{4,40})')),
    ("listing_id", re.compile(r'listing_id"?\s*[:=]\s*"?(?P<v>\d{4,20})')),
    ("pmsType", re.compile(r'pmsType"?\s*[:=]\s*"?(?P<v>[A-Z0-9_]{2,20})')),
    ("sourceAgentCode", re.compile(r'sourceAgentCode"?\s*[:=]\s*"?(?P<v>[A-Z0-9]{2,5})')),
    ("hotelCode", re.compile(r'HotelCode\\?"?=\\?"?(?P<v>\d{3,12})')),
    ("roomCode", re.compile(r'(?:roomCode|roomTypeCode)"?\s*[:=]\s*"?(?P<v>[A-Za-z0-9_.-]{1,30})')),
    ("ratePlanCode", re.compile(r'(?:ratePlanCode|rateCode)"?\s*[:=]\s*"?(?P<v>[A-Za-z0-9_.-]{1,30})')),
]

DEFAULT_LOW_SELECTIVITY_KINDS = (
    "pmsType",
    "hubPmsPropertyUuid",
    "roomCode",
    "ratePlanCode",
    "sourceAgentCode",
    "hotelCode",
)


@dataclass
class TraceContext:
    """Holds configuration, state, and collaborators for a trace run."""

    ref: str
    run_dir: Path
    kb_path: Path
    backend: Backend
    runner: StageRunner
    case: str = ""
    pms: str = ""
    channel: str = ""
    at: str = ""
    at_uncertain: bool = False
    tz_offset: str = "+00:00"
    from_date: str = ""
    to_date: str = ""
    fanout_hours: int = 3
    no_preflight: bool = False
    sys_sweep: str = "auto"  # "auto", "0", "1"
    fullday_fallback: bool = True
    lad_fd_zero: bool = False
    follow_enabled: bool = True
    max_follow_keys: int = 12
    grep_fallback: bool = True
    control_token: str = "booking"
    control_token_cm: str = "propertyUuid"
    preflight_timeout: int = 30
    hotel_uuid: str = ""
    hotelier_id: str = ""
    propkey: str = ""
    membership: str = ""
    listing_id: str = ""
    guest: str = ""
    kb_data: Dict[str, Any] = field(default_factory=dict)

    # Runtime state
    prefix: str = ""
    chan: str = ""
    chan_sys: str = ""
    chan_apps: List[str] = field(default_factory=list)
    pool: List[str] = field(default_factory=list)
    stale_apps: List[str] = field(default_factory=list)
    catalog_only: List[str] = field(default_factory=list)
    at_date: str = ""
    at_hour: str = ""
    anchor: str = ""
    pms_effective: str = ""
    logs: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.run_dir = Path(self.run_dir)
        self.kb_path = Path(self.kb_path)
        (self.run_dir / "raw").mkdir(parents=True, exist_ok=True)
        if not self.kb_data and self.kb_path.exists():
            try:
                self.kb_data = json.loads(self.kb_path.read_text(encoding="utf-8"))
            except Exception:
                self.kb_data = {}
        if not self.prefix and "-" in self.ref:
            self.prefix = self.ref.split("-", 1)[0]
        if self.at and not self.at_date:
            parts = re.split(r"[T ]", self.at, maxsplit=1)
            self.at_date = parts[0]
            if len(parts) > 1:
                self.at_hour = parts[1].split(":", 1)[0].zfill(2)
            if not self.from_date:
                self.from_date = self.at_date
            if not self.to_date:
                self.to_date = self.at_date
        if not self.from_date:
            self.from_date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        if not self.to_date:
            self.to_date = self.from_date

    def to_env(self) -> Dict[str, str]:
        return {
            "PT_CASE": self.case,
            "PT_REF": self.ref,
            "PT_PMS": self.pms_effective or self.pms,
            "PT_CHANNEL": self.channel,
            "PT_CHAN": self.chan,
            "PT_CHAN_SYS": self.chan_sys,
            "PT_AT": self.at,
            "PT_FROM": self.from_date,
            "PT_TO": self.to_date,
            "PT_HOTEL_UUID": self.hotel_uuid,
            "PT_HOTELIER_ID": self.hotelier_id,
            "PT_PROPKEY": self.propkey,
            "PT_MEMBERSHIP": self.membership,
            "PT_LISTING_ID": self.listing_id,
            "PT_GUEST": self.guest,
        }

    def log(self, msg: str) -> None:
        ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")

    def note(self, line: str) -> None:
        with open(self.run_dir / "REPORT.md", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def pt_sanitize(value: str) -> str:
    translated = value.replace("/", "_").replace(" ", "_").replace("*", "_")
    return re.sub(r"[^A-Za-z0-9_.-]", "", translated)


def pt_variants(ref: str) -> List[str]:
    if not ref:
        return []
    up = ref.upper()
    out = [up]
    if "-" in up:
        bare = up.split("-", 1)[1]
        if bare and bare not in out:
            out.append(bare)
    return out


def pt_ctlapp(sys_name: str) -> str:
    if sys_name == "pms-hub":
        return "pms-hub/booking-api"
    if sys_name == "cm":
        return "cm/avail-api-ra"
    return f"{sys_name}/*"


def _kb_get(path: str, default: Any = None, kb_data: Optional[Dict[str, Any]] = None) -> Any:
    if kb_data is not None:
        return kb.query_kb(kb_data, path, default)
    if hasattr(kb, "get"):
        return kb.get(path, default)
    if hasattr(kb, "kb"):
        return kb.kb(path, default)
    return default


def _kb_list(path: str, kb_data: Optional[Dict[str, Any]] = None) -> List[str]:
    if kb_data is not None:
        val = kb.query_kb(kb_data, path, [])
        if isinstance(val, dict):
            return list(val.keys())
        if isinstance(val, list):
            return [str(x) for x in val]
        return []
    if hasattr(kb, "kb_list"):
        res = kb.kb_list(path)
        return list(res) if res else []
    val = _kb_get(path, [])
    if isinstance(val, dict):
        return list(val.keys())
    if isinstance(val, list):
        return [str(x) for x in val]
    return []


def _parse_apps(val: Any) -> List[str]:
    if not val or val == "null":
        return []
    if isinstance(val, list):
        return [str(x) for x in val if x]
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("["):
            try:
                loaded = json.loads(s)
                if isinstance(loaded, list):
                    return [str(x) for x in loaded if x]
            except Exception:
                pass
        return [x for x in s.split() if x and x not in ("None", "null")]
    return []


def _catalog_apps(ctx: TraceContext) -> List[str]:
    if hasattr(ctx.backend, "catalog"):
        return list(ctx.backend.catalog())
    res = ctx.backend.list("*", "*")
    if res.rc != 0:
        return []
    return [line.strip() for line in res.stdout.splitlines() if "/" in line]


def _pool_apps(ctx: TraceContext, sys_name: str) -> List[str]:
    if hasattr(ctx.backend, "pool"):
        return list(ctx.backend.pool(sys_name))
    res = ctx.backend.list(sys_name, "*")
    if res.rc != 0:
        return []
    return sorted({line.strip() for line in res.stdout.splitlines() if line.strip().startswith(f"{sys_name}/")})


def _run_stage_grep(ctx: TraceContext, sn: str, label: str, app: str, blob: str, pat: str) -> Path:
    out_file = ctx.run_dir / "raw" / f"{sn}.out"
    if hasattr(ctx.runner, "run_stage"):
        ctx.runner.run_stage(
            sn,
            label,
            lambda a=app, b=blob, p=pat: ctx.backend.grep(a, b, p),
            plan_cmd=f"s3search grep {app} {blob} '{pat}'",
        )
    else:
        ctx.runner.run(sn, label, lambda a=app, b=blob, p=pat, o=out_file: ctx.backend.fetch(a, b, p, o))
    return out_file


def _run_stage_fn(ctx: TraceContext, sn: str, label: str, fn: Any) -> Any:
    if hasattr(ctx.runner, "run"):
        return ctx.runner.run(sn, label, fn)
    def _wrap() -> BackendResult:
        fn()
        return BackendResult(stdout="", stderr="", rc=0)
    return ctx.runner.run_stage(sn, label, _wrap)


def _sys_candidates(ctx: TraceContext, token: str) -> List[str]:
    if not token:
        return []
    tok = token.lower()
    seen: List[str] = []
    for app in _catalog_apps(ctx):
        if "/" in app:
            sys_part = app.split("/", 1)[0]
            if tok in app.lower() and sys_part not in seen:
                seen.append(sys_part)
    return seen


def _channel_from_codes(ctx: TraceContext) -> None:
    if ctx.channel:
        ctx.chan = ctx.channel.upper()
        return
    if not ctx.prefix:
        return
    pref_up = ctx.prefix.upper()
    codes = _kb_get("channel_codes.codes", {}, ctx.kb_data) or {}
    if isinstance(codes, dict) and pref_up in codes:
        entry = codes[pref_up]
        if isinstance(entry, dict) and entry.get("module"):
            ctx.chan = str(entry["module"])
            return
    channels = _kb_list("channels", ctx.kb_data)
    if pref_up in channels:
        ctx.chan = pref_up


def pt_resolve(ctx: TraceContext) -> None:
    """Resolve channel system and app pool, writing resolve.tsv."""
    _channel_from_codes(ctx)
    resolve_path = ctx.run_dir / "resolve.tsv"
    resolve_lines: List[str] = []
    ctx.stale_apps = []
    ctx.pool = []
    ctx.catalog_only = []

    if ctx.chan:
        firstapp = ""
        for key in ("reservation_inbound", "ari_outbound", "other_apps"):
            apps = _parse_apps(_kb_get(f"channels.{ctx.chan}.log_apps.{key}", None, ctx.kb_data))
            slash_apps = [a for a in apps if "/" in a]
            if slash_apps:
                firstapp = slash_apps[0]
                break

        if firstapp:
            ctx.chan_sys = firstapp.split("/", 1)[0]
            resolve_lines.append(f"sys_from_kb_apps|{ctx.chan}|{ctx.chan_sys}")
            ctx.log(f"[resolve] sys={ctx.chan_sys} from KB app paths (authoritative; catalog name-match skipped)")
        else:
            conv = f"cm-{ctx.prefix.lower()}" if ctx.prefix else ""
            conv_pool = _pool_apps(ctx, conv) if conv else []
            if conv and conv_pool:
                ctx.chan_sys = conv
                resolve_lines.append(f"sys_from_convention|{ctx.chan}|{ctx.chan_sys}")
                ctx.log(f"[resolve] no KB app paths - sys={ctx.chan_sys} from cm-<prefix> convention (pool-verified)")
            else:
                am = _kb_get(f"channels.{ctx.chan}.app_match", None, ctx.kb_data) or ctx.prefix
                cands = _sys_candidates(ctx, str(am)) if am else []
                ctx.chan_sys = cands[0] if cands else ""
                resolve_lines.append(f"sys_from_namematch|{ctx.chan}|{ctx.chan_sys or 'none'}")
                ctx.log(f"[resolve] no KB app paths and convention failed - sys={ctx.chan_sys} from catalog name-match")

        kb_apps: List[str] = []
        for key in ("reservation_inbound", "ari_outbound", "other_apps"):
            kb_apps.extend(_parse_apps(_kb_get(f"channels.{ctx.chan}.log_apps.{key}", None, ctx.kb_data)))
        ctx.chan_apps = kb_apps
        resolve_lines.append(f"kb_module|{ctx.chan}|sys={ctx.chan_sys or '?'}|apps={' '.join(ctx.chan_apps)}")
        ctx.log(f"[resolve] KB module {ctx.chan}: sys={ctx.chan_sys or '?'} apps={' '.join(ctx.chan_apps)}")

        if ctx.chan_sys:
            ctx.pool = _pool_apps(ctx, ctx.chan_sys)
            if ctx.pool:
                ctx.log(f"[resolve] pool for {ctx.chan_sys} ({len(ctx.pool)} apps, verified listing)")
                resolve_lines.append(f"pool|{ctx.chan_sys}|{','.join(ctx.pool)}")
            else:
                ctx.log(f"[resolve] WARN: no verified pool for {ctx.chan_sys} - stale-check SKIPPED, KB apps trusted")
                resolve_lines.append(f"stale_check|{ctx.chan}|SKIPPED - pool unverified, KB trusted")

        ok_apps: List[str] = []
        for app in ctx.chan_apps:
            if "/" not in app:
                ok_apps.append(app)
                ctx.log(f"[resolve] KB entry '{app}' is system-level (thin module) - kept as system sweep intent")
                continue
            if not ctx.pool or app in ctx.pool:
                ok_apps.append(app)
            else:
                ctx.stale_apps.append(app)
                comp = app.split("/", 1)[1][:6].lower()
                hints = [p for p in ctx.pool if comp in p.lower()][:3]
                hint_str = " ".join(hints) if hints else "none"
                resolve_lines.append(f"kb_stale|{ctx.chan}|{app}|not in verified pool|closest:{hint_str}")
                ctx.log(f"[resolve] STALE KB app: {app} (not in verified pool; closest:{hint_str})")
        ctx.chan_apps = ok_apps

        if ctx.pool:
            for p in ctx.pool:
                if p not in ctx.chan_apps:
                    ctx.catalog_only.append(p)
                    resolve_lines.append(f"catalog_only|{ctx.chan}|{p}|in live pool, not in KB")
            if not ctx.chan_apps:
                ctx.chan_apps = list(ctx.pool)
                ctx.catalog_only = []
                ctx.log("[resolve] every KB app stale - sweeping the full verified pool")

    if not ctx.chan_sys:
        for cand in (ctx.prefix, f"cm-{ctx.prefix}"):
            if not cand:
                continue
            cands = _sys_candidates(ctx, cand)
            if cands:
                ctx.chan_sys = cands[0]
                resolve_lines.append(f"catalog_alias|{cand}|sys={ctx.chan_sys}")
                break
        if not ctx.chan_sys:
            for c in _kb_list("channels", ctx.kb_data):
                am2 = _kb_get(f"channels.{c}.app_match", None, ctx.kb_data)
                if not am2 or am2 == "null":
                    continue
                cands = _sys_candidates(ctx, str(am2))
                if cands:
                    resolve_lines.append(f"kb_app_match_available|{c}|{cands[0]} (not matched to prefix {ctx.prefix})")

    resolve_path.write_text("\n".join(resolve_lines) + ("\n" if resolve_lines else ""), encoding="utf-8")
    if ctx.chan_sys:
        ctx.log(f"[resolve] channel system: {ctx.chan_sys}")
    else:
        ctx.log("[resolve] WARN: no channel system resolved - trace will lean on pms-hub + harvest")


def pt_hours_plan(ctx: TraceContext, fanout: int = 0) -> List[str]:
    if ctx.anchor:
        base_dt = dt.datetime.strptime(ctx.anchor, "%Y-%m-%d-%H").replace(tzinfo=dt.timezone.utc)
        return [(base_dt + dt.timedelta(hours=delta)).strftime("%Y-%m-%d-%H") for delta in (-1, 0, 1)]
    if not ctx.at_date:
        return []
    hour = int(ctx.at_hour or "0")
    base_dt = dt.datetime.strptime(f"{ctx.at_date}-{hour:02d}", "%Y-%m-%d-%H").replace(tzinfo=dt.timezone.utc)
    return [(base_dt + dt.timedelta(hours=delta)).strftime("%Y-%m-%d-%H") for delta in range(-fanout, fanout + 1)]


def pt_preflight(ctx: TraceContext, systems: Sequence[str]) -> int:
    rc = 0
    for sys_name in systems:
        if not sys_name:
            continue
        ctl_out = ctx.run_dir / "raw" / f"preflight_{pt_sanitize(sys_name)}.stdout"
        tok = ctx.control_token_cm if sys_name == "cm" else ctx.control_token
        if ctx.at_date:
            hours = pt_hours_plan(ctx, 0)
            blob = hours[0] if hours else f"{ctx.at_date}-00"
        else:
            blob = f"{ctx.from_date}-*"
        if hasattr(ctx.backend, "fetch"):
            ctx.backend.fetch(
                pt_ctlapp(sys_name),
                blob,
                tok,
                ctl_out,
                max_lines=200,
                timeout=ctx.preflight_timeout,
            )
        else:
            res = ctx.backend.grep(pt_ctlapp(sys_name), blob, tok)
            if res.stdout:
                ctl_out.write_text("\n".join(res.stdout.splitlines()[:200]) + "\n", encoding="utf-8")
        lines = 0
        if ctl_out.exists():
            lines = sum(1 for line in ctl_out.read_text(encoding="utf-8", errors="replace").splitlines() if line)
        if lines == 0:
            ctx.log(f"[preflight] FAIL {sys_name}: control '{tok}' returned 0 lines - empty results for this system are UNVERIFIED")
            ctx.note(f"- **PREFLIGHT FAIL {sys_name}** - control returned 0 lines; empty sweeps for this system are UNVERIFIED (pipe, retention or wrong system).")
            rc = 1
        else:
            ctx.log(f"[preflight] OK {sys_name} ({lines} lines) - empty sweeps for this system are trustworthy")
            ctx.note(f"- preflight control {sys_name}: OK ({lines} lines)")
    return rc


def pt_hours_refresh(ctx: TraceContext) -> Dict[str, Set[str]]:
    pats = pt_variants(ctx.ref)
    tags = ("anchorfd_fd", "fallback", "anchorfd", "disc", "lad", "pms", "kb", "cat", "sys")
    hours: Dict[str, Set[str]] = {}
    for fpath in sorted(glob.glob(str(ctx.run_dir / "raw" / "t_*.out"))):
        base = os.path.basename(fpath)[:-4]
        app = None
        for tag in tags:
            if base.startswith("t_" + tag + "_"):
                app = base[len(tag) + 3 :]
                break
        if not app:
            continue
        m = re.search(r"_20\d\d-\d\d-\d\d", app)
        if m:
            app = app[: m.start()]
        try:
            lines = Path(fpath).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip() or line.startswith("[fetch]"):
                continue
            if not any(p in line for p in pats):
                continue
            hm = re.search(r"(20\d\d-\d\d-\d\d)[T ](\d\d)", line)
            if hm:
                hours.setdefault(app, set()).add(f"{hm.group(1)}-{hm.group(2)}")
    out = ctx.run_dir / "hours.tsv"
    with open(out, "w", encoding="utf-8") as fh:
        for app in sorted(hours):
            for h in sorted(hours[app]):
                fh.write(f"{app}\t{h}\n")
    return hours


def pt_app_hours(ctx: TraceContext, app: str, fanout: int = 0) -> List[str]:
    san = pt_sanitize(app)
    hours_path = ctx.run_dir / "hours.tsv"
    hs: List[str] = []
    if hours_path.exists() and hours_path.stat().st_size > 0:
        for line in hours_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0] == san:
                hs.append(parts[1])
    if hs:
        out: Set[str] = set()
        for h in sorted(set(hs)):
            base_dt = dt.datetime.strptime(h, "%Y-%m-%d-%H").replace(tzinfo=dt.timezone.utc)
            out.add(base_dt.strftime("%Y-%m-%d-%H"))
            out.add((base_dt + dt.timedelta(hours=1)).strftime("%Y-%m-%d-%H"))
        return sorted(out)
    return pt_hours_plan(ctx, fanout)


def _normalize_apps(apps: Sequence[str] | str) -> List[str]:
    if isinstance(apps, str):
        return [a for a in apps.split() if a]
    return [a for a in apps if a]


def pt_sweep(ctx: TraceContext, tag: str, apps: Sequence[str] | str, fanout: int = 0) -> int:
    app_list = _normalize_apps(apps)
    pats = pt_variants(ctx.ref)
    pt_hours_refresh(ctx)
    hit_any = 0
    for app in app_list:
        for pat in pats:
            if ctx.at_date:
                for bp in pt_app_hours(ctx, app, fanout):
                    sn = f"t_{tag}_{pt_sanitize(app)}_{pt_sanitize(bp)}_{pt_sanitize(pat)}"
                    out_file = _run_stage_grep(ctx, sn, f"{app} {bp} '{pat}'", app, bp, pat)
                    if out_file.exists() and out_file.stat().st_size > 0:
                        hit_any = 1
            else:
                day = dt.date.fromisoformat(ctx.from_date)
                end_day = dt.date.fromisoformat(ctx.to_date)
                while day <= end_day:
                    day_s = day.isoformat()
                    sn = f"t_{tag}_{pt_sanitize(app)}_{pt_sanitize(day_s)}_{pt_sanitize(pat)}"
                    out_file = _run_stage_grep(ctx, sn, f"{app} {day_s}-* '{pat}'", app, f"{day_s}-*", pat)
                    if out_file.exists() and out_file.stat().st_size > 0:
                        hit_any = 1
                    day += dt.timedelta(days=1)
    return hit_any


def pt_sweep_fd(ctx: TraceContext, tag: str, apps: Sequence[str] | str, day: str) -> int:
    if not day:
        return 0
    app_list = _normalize_apps(apps)
    pats = pt_variants(ctx.ref)
    ctx.log(f"[sweep_fd:{tag}] full-day wildcard {day}-* (hour plan earned the fallback)")
    hit_any = 0
    for app in app_list:
        for pat in pats:
            sn = f"t_{tag}_fd_{pt_sanitize(app)}_{pt_sanitize(day)}_{pt_sanitize(pat)}"
            out_file = _run_stage_grep(ctx, sn, f"{app} {day}-* '{pat}'", app, f"{day}-*", pat)
            if out_file.exists() and out_file.stat().st_size > 0:
                hit_any = 1
    return hit_any


def pt_any_hit(ctx: TraceContext, tag: str) -> bool:
    for fpath in glob.glob(str(ctx.run_dir / "raw" / f"t_{tag}_*.out")):
        if os.path.getsize(fpath) > 0:
            return True
    return False


def _anchor_scan(ctx: TraceContext, pats: Sequence[str]) -> str:
    best: Optional[str] = None
    for fpath in sorted(glob.glob(str(ctx.run_dir / "raw" / "t_*.out"))):
        try:
            lines = Path(fpath).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip() or line.startswith("[fetch]"):
                continue
            if not any(p in line for p in pats):
                continue
            m = re.search(r"(20\d\d-\d\d-\d\d)[T ](\d\d)", line)
            if m:
                h = f"{m.group(1)}-{m.group(2)}"
                if best is None or h < best:
                    best = h
                break
    return best or ""


def pt_anchor(ctx: TraceContext) -> int:
    if not ctx.at_date or ctx.anchor:
        return 0
    pats = pt_variants(ctx.ref)
    ctx.anchor = _anchor_scan(ctx, pats)

    if not ctx.anchor and ctx.fullday_fallback:
        arr_apps = list(ctx.chan_apps)
        if not arr_apps and ctx.chan_sys:
            arr_apps = [f"{ctx.chan_sys}/*"]
        if arr_apps:
            pt_sweep_fd(ctx, "anchorfd", arr_apps, ctx.at_date)
            ctx.anchor = _anchor_scan(ctx, pats)
        if not ctx.anchor and arr_apps:
            hh = int(ctx.at_hour or "0")
            if hh >= 20:
                days = [1]
            elif hh <= 4:
                days = [-1]
            else:
                days = [-1, 1]
            base_day = dt.date.fromisoformat(ctx.at_date)
            for delta in days:
                nd = (base_day + dt.timedelta(days=delta)).isoformat()
                ctx.log(f"[anchor] day drift -> {nd}-* (typed hour {ctx.at_hour} near day boundary)")
                pt_sweep_fd(ctx, "anchorfd", arr_apps, nd)
                ctx.anchor = _anchor_scan(ctx, pats)
                if ctx.anchor:
                    break

    if ctx.anchor:
        anchor_dt = dt.datetime.strptime(ctx.anchor, "%Y-%m-%d-%H").replace(tzinfo=dt.timezone.utc)
        at_dt = dt.datetime.strptime(f"{ctx.at_date}-{int(ctx.at_hour or '0'):02d}", "%Y-%m-%d-%H").replace(tzinfo=dt.timezone.utc)
        drift_h = int((anchor_dt - at_dt).total_seconds() // 3600)
        ctx.log(f"[anchor] LOCKED live arrival {ctx.anchor} (typed {ctx.at_date} h{ctx.at_hour}, drift {drift_h}h) - downstream stages scan anchor +/-1h")
        ctx.note(f"- time anchor: **{ctx.anchor}** locked from live data (typed AT={ctx.at}, drift {drift_h}h; tz_offset={ctx.tz_offset} informational) - downstream scans = anchor +/-1h")
    else:
        ctx.log("[anchor] no arrival evidence in hour plan, full-day, or day drift")
        ctx.note("- time anchor: NOT locked (hour plan, full-day and +/-1 day drift all empty) - downstream uses the typed hour plan")
    return 0


def pt_harvest(ctx: TraceContext) -> Dict[str, Dict[str, str]]:
    pats = [ctx.ref]
    if "-" in ctx.ref:
        bare = ctx.ref.split("-", 1)[1]
        if bare and bare != ctx.ref:
            pats.append(bare)

    def hour_of(line: str, fname: str) -> str:
        m = re.search(r"(20\d\d-\d\d-\d\d)[T ](\d\d)", line)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
        m = re.search(r"(20\d\d-\d\d-\d\d)-(\d\d)", fname)
        return f"{m.group(1)}-{m.group(2)}" if m else ""

    keys: Dict[str, Dict[str, str]] = {}
    fd_files = 0
    fd_admitted = 0
    raw_dir = ctx.run_dir / "raw"
    files = sorted(glob.glob(str(raw_dir / "t_*.out"))) + sorted(glob.glob(str(raw_dir / "f_*.out")))
    for fpath in files:
        base = os.path.basename(fpath)
        is_fd = "_fd_" in base or base.startswith("t_lad_")
        if is_fd:
            fd_files += 1
        try:
            lines = Path(fpath).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            if is_fd:
                if not any(p in line for p in pats):
                    continue
                fd_admitted += 1
            for kind, rx in KIND_RES:
                for m in rx.finditer(line):
                    v = m.group("v")
                    if v not in keys:
                        keys[v] = {"kind": kind, "src": base, "hour": hour_of(line, base)}

    out = ctx.run_dir / "join_keys.tsv"
    with open(out, "w", encoding="utf-8") as fh:
        for v, meta in sorted(keys.items(), key=lambda kv: kv[1]["hour"]):
            fh.write(f"{meta['kind']}\t{v}\t{meta['hour']}\t{meta['src']}\n")
    ctx.log(f"[harvest] {len(keys)} join keys -> {out}")
    if fd_files:
        ctx.log(f"[harvest] property/full-day guard: {fd_files} property-keyed file(s), {fd_admitted} ref-matching line(s) admitted; co-tenant lines ignored")
    return keys


def pt_follow(ctx: TraceContext) -> int:
    if not ctx.follow_enabled:
        return 0
    jk_path = ctx.run_dir / "join_keys.tsv"
    if not jk_path.exists() or jk_path.stat().st_size == 0:
        ctx.log("[follow] no join keys harvested")
        return 0

    lowk = set(_kb_list("low_selectivity_kinds", ctx.kb_data) or DEFAULT_LOW_SELECTIVITY_KINDS)
    chan_sys = ctx.chan_sys
    n = 0
    for line in jk_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        kind, key, hour, _src = parts[0], parts[1], parts[2], parts[3]
        if kind == "pmsBookingId":
            continue
        if kind in lowk:
            ctx.note(f"[follow] {kind}={key} - context-only (low selectivity, co-tenant risk); not swept")
            continue
        n += 1
        if n > ctx.max_follow_keys:
            break
        hb = hour or f"{ctx.at_date or ctx.from_date}-00"
        dayp = hb.rsplit("-", 1)[0]
        m = re.search(r"-(\d{2})$", hb)
        h10 = int(m.group(1)) if m else 0
        hlo = max(0, h10 - 1)
        hhi = min(23, h10 + 1)
        hh_list = [f"{dayp}-{hh:02d}" for hh in range(hlo, hhi + 1)]

        if kind == "listing_id":
            targets = [f"{chan_sys}/avail-api", f"{chan_sys}/notify-gateway"] if chan_sys else []
        elif kind == "echoToken":
            targets = ([f"{chan_sys}/notify-gateway"] if chan_sys else []) + ["pms-hub/booking-api"]
        elif kind in ("hubBookingUuid", "hubPmsPropertyUuid"):
            targets = ["pms-hub/booking-api", "pms-hub/pms-booking-events", "hub-channel-store/booking-api"]
        else:
            targets = ([f"{chan_sys}/booking-processor"] if chan_sys else []) + [
                "hub-channel-store/booking-api",
                "pms-hub/booking-api",
                "pms-hub/pms-booking-events",
            ]
        if not targets:
            continue
        for app in targets:
            for hh in hh_list:
                sn = f"f_{pt_sanitize(app)}_{pt_sanitize(hh)}_{pt_sanitize(key[:13])}"
                _run_stage_grep(ctx, sn, f"FOLLOW {app} {hh} '{key}'", app, hh, key)
    pt_harvest(ctx)
    return 0


def pt_crossref(ctx: TraceContext) -> List[Tuple[str, List[str], Dict[str, Any]]]:
    def env_of(d: Path) -> Dict[str, str]:
        env: Dict[str, str] = {}
        p = d / "inputs.env"
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k] = v
        return env

    def keys_of(d: Path) -> Dict[str, Set[str]]:
        keys: Dict[str, Set[str]] = {}
        p = d / "join_keys.tsv"
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    keys.setdefault(parts[0], set()).add(parts[1])
        return keys

    cur_env = env_of(ctx.run_dir)
    cur_keys = keys_of(ctx.run_dir)
    cur_ids: Dict[str, Set[str]] = {}
    for k, label in (("PT_MEMBERSHIP", "membership"), ("PT_PROPERTY_ID", "property_id"), ("PT_HOTEL_UUID", "hotel_uuid"), ("PT_PMS_CONF", "pms_conf")):
        if cur_env.get(k):
            cur_ids.setdefault(label, set()).add(cur_env[k])
    for kind in ("hubPmsPropertyUuid", "hotelCode"):
        if kind in cur_keys:
            cur_ids.setdefault(kind, set()).update(cur_keys[kind])
    cur_ids = {k: v for k, v in cur_ids.items() if v}
    if not cur_ids:
        return []

    runs_root = ctx.run_dir.parent
    matches: List[Tuple[str, List[str], Dict[str, Any]]] = []
    for d in sorted(runs_root.iterdir()):
        if d.resolve() == ctx.run_dir.resolve() or not d.is_dir():
            continue
        o_env = env_of(d)
        o_keys = keys_of(d)
        shared: List[str] = []
        for label, vals in cur_ids.items():
            for v in vals:
                if len(v) < 4:
                    continue
                if v in o_env.values() or any(v in s for s in o_keys.values()):
                    shared.append(f"{label}={v}")
        if shared:
            codes: Dict[str, Any] = {}
            vp = d / "verdict.json"
            if vp.exists():
                try:
                    codes = json.loads(vp.read_text(encoding="utf-8")).get("failure_codes", {})
                except Exception:
                    pass
            matches.append((d.name, sorted(set(shared)), codes))

    if not matches:
        return []

    lines = ["", "### Cross-run property correlation"]
    for case, shared, codes in matches:
        lines.append(f"- **SAME PROPERTY as run `{case}`** (shared: {', '.join(shared)})")
        if codes:
            code_str = ", ".join(f"`{c}`" for c in sorted(codes))
            lines.append(f"  - that run failed with: {code_str} - same root-cause class; one mapping fix can cover all channels of the property")
    txt = "\n".join(lines) + "\n"
    with open(ctx.run_dir / "REPORT.md", "a", encoding="utf-8") as fh:
        fh.write(txt)
    return matches


def pt_fallback_icase(ctx: TraceContext) -> int:
    if not ctx.grep_fallback:
        ctx.log("[fallback] disabled (PT_GREP_FALLBACK=0)")
        return 0
    out = ctx.run_dir / "raw" / "t_fallback_icase.out"
    up = ctx.ref.upper()
    core = up.rsplit("-", 1)[-1] if "-" in up else ""
    needles = [n.lower() for n in (ctx.ref, up, core) if n]
    seen: Set[str] = set()
    for fpath in sorted(glob.glob(str(ctx.run_dir / "raw" / "*.out"))):
        if Path(fpath) == out:
            continue
        try:
            lines = Path(fpath).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith("[fallback-icase]"):
                continue
            low = line.lower()
            if any(n in low for n in needles):
                seen.add(line)
    if seen:
        with open(out, "w", encoding="utf-8") as fh:
            for line in sorted(seen):
                fh.write(f"[fallback-icase] {line}\n")
        ctx.log(f"[fallback] icase sweep: {len(seen)} line(s) mention the ref (case-insensitive) -> {out}")
        return len(seen)
    if out.exists():
        out.unlink()
    ctx.log("[fallback] icase sweep: ref absent from ALL fetched data (any case) - strengthens elimination")
    return 0


def pt_next_actions(ctx: TraceContext) -> str:
    vp = ctx.run_dir / "verdict.json"
    if not vp.exists() or vp.stat().st_size == 0:
        ctx.log("[next] no verdict.json - run trace first")
        return ""
    v = json.loads(vp.read_text(encoding="utf-8"))
    sigs = _kb_get("failure_signatures", {}, ctx.kb_data) or {}
    keys: Dict[str, str] = {}
    jk = ctx.run_dir / "join_keys.tsv"
    if jk.exists():
        for line in jk.read_text(encoding="utf-8", errors="replace").splitlines():
            p = line.split("\t")
            if len(p) >= 2 and p[0] not in keys:
                keys[p[0]] = p[1]
    env: Dict[str, str] = {}
    ie = ctx.run_dir / "inputs.env"
    if ie.exists():
        for line in ie.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, val = line.split("=", 1)
                env[k] = val

    out = ["# NEXT ACTIONS (chained from the verdict - the tool's own reading of what to do now)", ""]
    n = 0
    codes = sorted(v.get("failure_codes", {}))
    for code in codes:
        sig = sigs.get(code, {})
        n += 1
        cnt = v["failure_codes"][code]
        out.append(f"{n}. **Failure `{code}`** ({cnt} event{'s' if cnt > 1 else ''})")
        if sig.get("meaning"):
            out.append(f"   - meaning: {sig['meaning']}")
        if sig.get("fix_path"):
            out.append(f"   - fix path: {sig['fix_path']}")
        if not sig:
            out.append("   - not in KB failure_signatures - verify it belongs to THIS booking before acting, then add it")
        if code == "ERR_CONVERSION_UNMAPPED_ROOM_RATE":
            h = keys.get("hubPmsPropertyUuid", "(see join_keys.tsv)")
            obs_r, obs_rt = keys.get("roomCode", "?"), keys.get("ratePlanCode", "?")
            exp_r = env.get("PT_PMS_ROOM_CODE") or "?"
            exp_rt = env.get("PT_PMS_RATE_CODE") or "?"
            out.append(f"   - mapping check: confirm room/rate mapping Channel Manager -> PMS for hubPmsPropertyUuid `{h}` (PMS-HUB Admin UI / CM mapping screen); after mapping, resend the reservation from the CM reservation page")
            out.append(f"   - codes: payload observed roomCode=`{obs_r}` ratePlanCode=`{obs_rt}` | PMS expects room=`{exp_r}` rate=`{exp_rt}` (fill PT_PMS_ROOM_CODE/PT_PMS_RATE_CODE from the PMS to compare)")
            out.append("   - property-wide: this mapping covers ALL channels of the property - expect every other channel's bookings to fail with the same signature until mapped; one fix clears them all (confirmed 2026-10-30: OTA4 + OTA5, same property)")
    if v.get("email_silence"):
        n += 1
        out.append(f"{n}. **Silence**: the hotel has no notification email configured - recommend enabling reservation-failure notifications so this class of failure is never silent again")
    if "DELIVERY" in v.get("reached", []) and "CONVERSION" not in v.get("reached", []):
        n += 1
        out.append(f'{n}. **Delivered**: chain into fidelity - re-paste with PT_TASKS="trace fidelity" (docs auto-fetch from the harvested tokens) to verify OTA->PMS data transformation')
    if not v.get("hits"):
        n += 1
        out.append(f"{n}. **No evidence anywhere**: before calling it absent - widen PT_FROM/PT_TO, set PT_AT_UNCERTAIN=1 (or full-day), and add any GUI identifiers you can get (PT_PMS_CONF / PT_CM_RES_UUID / PT_HOTEL_UUID / PT_PROPERTY_ID / PT_MEMBERSHIP / PT_LISTING_ID / PT_GUEST)")
    missing = [g for g in ("ARRIVAL", "ACCEPTANCE", "CM_STORE", "PMS_RECEIVE") if g in v.get("missing", [])]
    if v.get("hits") and missing and not codes:
        n += 1
        out.append(f"{n}. **Broken chain**: stage(s) {', '.join(missing)} never lit although the ref is present - inspect the elimination ledger; the break point is the first unlit stage")
    if v.get("context_files"):
        out.append("")
        out.append(f"_{v['context_files']} context file(s) from low-selectivity keys were excluded from evidence (co-tenant traffic); see REPORT.md_")
    if n == 0:
        out.append("- verdict chain is complete and clean; no chained action required")
    txt = "\n".join(out) + "\n"
    (ctx.run_dir / "NEXT_ACTIONS.md").write_text(txt, encoding="utf-8")
    return txt


def _kb_writeback(ctx: TraceContext) -> None:
    raw_dir = ctx.run_dir / "raw"
    ledger_path = ctx.run_dir / "ledger.tsv"
    ledger_map: Dict[str, str] = {}
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("|", 2)
            if len(parts) >= 2:
                ledger_map[parts[0]] = parts[1]

    lowk = set(_kb_list("low_selectivity_kinds", ctx.kb_data) or DEFAULT_LOW_SELECTIVITY_KINDS)
    jk_map: Dict[str, str] = {}
    jk_path = ctx.run_dir / "join_keys.tsv"
    if jk_path.exists():
        for line in jk_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                jk_map[parts[1]] = parts[0]

    hitapps: Set[str] = set()
    raw_files = sorted(glob.glob(str(raw_dir / "t_*.out"))) + sorted(glob.glob(str(raw_dir / "f_*.out")))
    for fpath in raw_files:
        p = Path(fpath)
        if not p.exists() or p.stat().st_size == 0:
            continue
        b = p.stem
        ann = ""
        if "_fd_" in b or b == "t_fallback_icase":
            ann = " [full-day: ref/token-filtered at verdict]"
        elif b.startswith("f_"):
            k13 = b.rsplit("_", 1)[-1]
            kkind = next((kind for val, kind in jk_map.items() if val.startswith(k13)), "")
            if kkind and kkind in lowk:
                ann = f" [context: low-selectivity {kkind} - excluded from evidence]"
        nlines = sum(1 for _ in p.read_text(encoding="utf-8", errors="replace").splitlines())
        ctx.note(f"- HIT: {b} ({nlines} lines){ann}")
        lbl = ledger_map.get(b, "")
        if lbl.startswith("FOLLOW "):
            lbl = lbl[len("FOLLOW ") :]
        app = lbl.split()[0] if lbl.split() else ""
        if "/" in app:
            hitapps.add(app)

    if hasattr(kb, "kb_write"):
        kb.kb_write("-", sorted(hitapps))
    elif ctx.kb_path.exists():
        kb.write_back(
            ctx.kb_path,
            case=ctx.case,
            ref=ctx.ref,
            chan=ctx.chan,
            pms=ctx.pms_effective or ctx.pms,
            prefix=ctx.prefix,
            chan_sys=ctx.chan_sys,
            apps=sorted(hitapps),
            plan_mode=getattr(ctx.runner, "plan", False),
        )


def pt_trace(ctx: TraceContext) -> int:
    if not ctx.ref:
        ctx.log("[trace] FATAL: set PT_REF")
        return 2

    ctx.log(f"[trace] ref={ctx.ref} window={ctx.from_date}..{ctx.to_date} at={ctx.at or 'none'} uncertain={int(ctx.at_uncertain)}")
    ctx.note(f"# PMS TOOL PASTE v{__version__} - case {ctx.case}")
    ctx.note(f"- ref={ctx.ref} pms={ctx.pms or 'auto'} window={ctx.from_date}..{ctx.to_date} at={ctx.at or 'full-day'} uncertain={int(ctx.at_uncertain)} tz={ctx.tz_offset}")

    # 1. resolve
    pt_resolve(ctx)
    inputs_env = ctx.run_dir / "inputs.env"
    if ctx.chan_sys:
        existing = inputs_env.read_text(encoding="utf-8", errors="replace") if inputs_env.exists() else ""
        if "PT_CHAN_SYS=" not in existing:
            with open(inputs_env, "a", encoding="utf-8") as fh:
                fh.write(f"PT_CHAN_SYS={ctx.chan_sys}\n")

    if not ctx.chan_sys:
        disc_systems = sorted({a.split("/")[0] for a in _catalog_apps(ctx) if "/" in a and a.split("/")[0].startswith("cm-")})[:40]
        ctx.log(f"[resolve] discovery sweep over systems: {' '.join(disc_systems)}")
        ctx.note(f"- resolve: no KB module / alias; discovery sweep over: {' '.join(disc_systems)}")
        for ds in disc_systems:
            pt_sweep(ctx, "disc", [f"{ds}/*"], ctx.fanout_hours)
        disc_hits = sorted(glob.glob(str(ctx.run_dir / "raw" / "t_disc_*.out")))
        if disc_hits:
            first_hit = os.path.basename(disc_hits[0])
            ctx.chan_sys = first_hit[len("t_disc_") :].split("_", 1)[0].replace("_", "-")
            ctx.log(f"[resolve] discovery hit -> channel system {ctx.chan_sys}")
            if hasattr(kb, "kb_write"):
                kb.kb_write(f"create:{ctx.prefix.upper()}", [f"{ctx.chan_sys}/discovered"])

    pats = pt_variants(ctx.ref)
    ctx.note(f"- patterns: {' '.join(pats)} (uppercase rule: CM booking IDs are stored UPPERCASE; lowercase variants dropped - verified 2026-10-28)")
    ctx.log(f"[trace] patterns: {' '.join(pats)}")

    # 2. preflight
    if not ctx.no_preflight:
        syslist = [s for s in (ctx.chan_sys, "pms-hub") if s]
        if pt_preflight(ctx, syslist) != 0:
            ctx.note("- (continuing despite preflight failure; verdict marks UNVERIFIED stages)")

    # 3. channel-side sweeps
    fan = ctx.fanout_hours if ctx.at_uncertain else 0
    chan_apps = list(ctx.chan_apps)
    if not chan_apps and ctx.chan:
        chan_apps = _parse_apps(_kb_get(f"channels.{ctx.chan}.log_apps.reservation_inbound", None, ctx.kb_data)) + _parse_apps(
            _kb_get(f"channels.{ctx.chan}.log_apps.other_apps", None, ctx.kb_data)
        )
    if chan_apps:
        pt_sweep(ctx, "kb", chan_apps, fan)
    if not pt_any_hit(ctx, "kb") and ctx.catalog_only:
        ctx.log(f"[trace] trusted pool empty - widening to catalog-only apps: {' '.join(ctx.catalog_only)}")
        pt_sweep(ctx, "cat", ctx.catalog_only, fan)

    if ctx.sys_sweep != "0" and ctx.chan_sys:
        resolve_txt = (ctx.run_dir / "resolve.tsv").read_text(encoding="utf-8", errors="replace") if (ctx.run_dir / "resolve.tsv").exists() else ""
        if not re.search(r"sys_from_(kb_apps|convention)", resolve_txt):
            ctx.log("[trace] system from name-match only - system-wide sweep SUPPRESSED (unverified system)")
        elif ctx.sys_sweep == "1" or (not pt_any_hit(ctx, "kb") and not pt_any_hit(ctx, "cat")):
            ctx.log("[trace] LAST RESORT system-wide sweep")
            pt_sweep(ctx, "sys", [f"{ctx.chan_sys}/*"], fan)
        else:
            ctx.log("[trace] per-app hits landed - system-wide sweep skipped (PT_SYS_SWEEP=auto)")

    # 3a. arrival anchor
    pt_anchor(ctx)

    # 3b. GUI input ladder
    extra_keys = [k for k in (ctx.hotel_uuid, ctx.hotelier_id, ctx.propkey, ctx.membership, ctx.listing_id, ctx.guest) if k]
    if extra_keys:
        ctx.log(f"[ladder] extra GUI keys: {' '.join(extra_keys)}")
        ctx.note(f"- GUI ladder keys: {' '.join(extra_keys)}")
        fd_day = ctx.anchor[:10] if ctx.anchor else ctx.at_date
        for k in extra_keys:
            ref_save = ctx.ref
            ctx.ref = k
            chan_rc = pt_sweep(ctx, "lad", chan_apps, fan) if chan_apps else 0
            core_apps = ["pms-hub/booking-api", "pms-hub/pms-booking-events", "hub-channel-store/booking-api"]
            core_rc = pt_sweep(ctx, "lad", core_apps, fan)
            if ctx.fullday_fallback:
                if chan_rc == 1 or core_rc == 1 or ctx.lad_fd_zero:
                    if chan_rc == 0 and chan_apps:
                        pt_sweep_fd(ctx, "lad", chan_apps, fd_day)
                    if core_rc == 0:
                        pt_sweep_fd(ctx, "lad", core_apps, fd_day)
                else:
                    ctx.log(f"[ladder] key {k}: zero echo in all apps/hours - full-day skipped")
                    ctx.note(f"- ladder key `{k}`: zero echo in all apps/hours - full-day skipped (absent near the booking; PT_LAD_FD_ZERO=1 forces)")
            ctx.ref = ref_save

    # 4. pms-hub direct
    pms_apps = ["pms-hub/booking-api", "pms-hub/pms-booking-events"]
    if ctx.pms:
        pms_apps.extend(_parse_apps(_kb_get(f"pms.{ctx.pms}.delivery_apps", None, ctx.kb_data)))
    pt_sweep(ctx, "pms", pms_apps, fan)

    # 5. harvest -> follow -> crossref -> pmsType reaction
    _run_stage_fn(ctx, "harvest", "join-key harvest", lambda: pt_harvest(ctx))
    jk_path = ctx.run_dir / "join_keys.tsv"
    if jk_path.exists():
        bagt = sorted({p.split("\t")[1] for p in jk_path.read_text(encoding="utf-8", errors="replace").splitlines() if p.startswith("sourceAgentCode\t")})
        if bagt:
            resolved = ctx.chan or ctx.prefix.upper()
            if resolved in bagt:
                ctx.note(f"- channel cross-check: sourceAgentCode={' '.join(bagt)} matches resolved channel {resolved} - diagnosis confirmed")
            else:
                ctx.note(f"- channel cross-check WARN: sourceAgentCode={' '.join(bagt)} != resolved channel {resolved} - re-check channel resolution")

    _run_stage_fn(ctx, "follow", "follow harvested keys", lambda: pt_follow(ctx))
    _run_stage_fn(ctx, "crossref", "cross-run property correlation", lambda: pt_crossref(ctx))

    pms_code = ""
    if jk_path.exists():
        for line in jk_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("pmsType\t"):
                pms_code = line.split("\t")[1]
                break
    if pms_code and not ctx.pms:
        pd_apps = _parse_apps(_kb_get(f"pms.{pms_code}.delivery_apps", None, ctx.kb_data))
        if pd_apps:
            pd_new = [pa for pa in pd_apps if pa not in pms_apps]
            if pd_new:
                ctx.log(f"[pmsType] harvested {pms_code} -> sweeping extra delivery apps: {' '.join(pd_new)}")
                ctx.note(f"- pmsType reaction: harvested {pms_code}; swept extra delivery apps: {' '.join(pd_new)} (rest already covered)")
                pt_sweep(ctx, "pmsd", pd_new, fan)
            else:
                ctx.log(f"[pmsType] harvested {pms_code} -> delivery apps already swept in the pms pass - skipping duplicate")
                ctx.note(f"- pmsType reaction: harvested {pms_code}; delivery apps already covered by the pms pass")
        else:
            ctx.log(f"[pmsType] harvested {pms_code} but no KB module - recorded for KB completion")
            ctx.note(f"- pmsType reaction: {pms_code} harvested; NO KB module yet (add pms.{pms_code})")
        ctx.pms_effective = pms_code
    else:
        ctx.pms_effective = ctx.pms or pms_code

    # 6. fallback_icase -> verdict -> next_actions
    _run_stage_fn(ctx, "fallback_icase", "case-insensitive ref sweep (fallback)", lambda: pt_fallback_icase(ctx))
    _run_stage_fn(ctx, "verdict", "stage classification + verdict", lambda: synthesize_verdict(ctx.run_dir, ctx.kb_data, ctx.to_env()))
    _run_stage_fn(ctx, "next_actions", "conditional next actions", lambda: pt_next_actions(ctx))

    # 7. KB writeback
    _kb_writeback(ctx)
    ctx.note(f"- ledger: {ctx.run_dir / 'ledger.tsv'}  keys: {ctx.run_dir / 'join_keys.tsv'}  raw: {ctx.run_dir / 'raw'}/")
    return 0
