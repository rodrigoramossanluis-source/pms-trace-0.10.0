"""Knowledge base management for pms-trace.

Translates the external KB load ladder, validation, versioned promotion,
dotted-path lookup, fcntl-locked write-back, backup rotation, history
truncation, and stale app repair from ``pms_trace.sh``.

The KB is always an external JSON file (never embedded).
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _version_key(path: Path) -> tuple:
    """Natural/version sort key for filenames like connector_kb_v0.9.12.json."""
    parts = re.split(r"(\d+)", path.name)
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def is_valid_kb(path: Union[str, Path]) -> bool:
    """Return True if *path* exists, is a regular file, and contains valid JSON."""
    p = Path(path)
    if not p.is_file():
        return False
    try:
        with p.open("r", encoding="utf-8") as f:
            json.load(f)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _promote_versioned(cand: Path, live_path: Path) -> None:
    """Promote a newer versioned KB *cand* onto *live_path*, preserving learned data."""
    stamp = _utc_stamp()
    lock_path = str(live_path) + ".lock"
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with cand.open("r", encoding="utf-8") as f:
                new = json.load(f)
            with live_path.open("r", encoding="utf-8") as f:
                live = json.load(f)

            for sect in ("channels", "pms"):
                for code, lm in (live.get(sect) or {}).items():
                    nm = new.setdefault(sect, {}).get(code)
                    if nm is None:
                        new[sect][code] = lm
                        continue
                    seen = {json.dumps(h, sort_keys=True) for h in nm.get("history", [])}
                    for h in lm.get("history", []):
                        if json.dumps(h, sort_keys=True) not in seen:
                            nm.setdefault("history", []).append(h)
                    if lm.get("_stale_repaired"):
                        existing = nm.setdefault("_stale_repaired", [])
                        existing.extend(x for x in lm["_stale_repaired"] if x not in existing)
                    la_n, la_l = nm.get("log_apps"), lm.get("log_apps")
                    if isinstance(la_n, dict) and isinstance(la_l, dict):
                        for k, v in la_l.items():
                            if v and not la_n.get(k):
                                la_n[k] = v
                    ea_l = lm.get("example_anchors") or {}
                    v_live = ea_l.get("verified_at") or ""
                    v_new = (nm.get("example_anchors") or {}).get("verified_at") or ""
                    if v_live > v_new:
                        nm.setdefault("example_anchors", {})["verified_at"] = v_live

            shutil.copy2(live_path, f"{live_path}.bak.{stamp}.promote")
            tmp_path = Path(f"{live_path}.tmp.{os.getpid()}")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(new, f, indent=1)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, live_path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_kb(
    kb_dir: Union[str, Path],
    kb_file: str = "connector_kb.json",
) -> Dict[str, Any]:
    """Load the external KB following the 4-step load ladder.

    1. Use ``kb_dir / kb_file`` if present and valid.
    2. If missing, promote the newest valid ``connector_kb*.json`` in *kb_dir*.
    3. If corrupt, quarantine as ``.corrupt.<ts>`` and restore newest valid ``.bak.*``.
    4. If a versioned ``connector_kb_v*.json`` in *kb_dir* is newer than the live
       file, promote it while carrying over learned history, stubs, and apps.
    """
    kb_dir_path = Path(kb_dir)
    kb_path = kb_dir_path / kb_file

    # Step 2: missing live file -> promote newest connector_kb*.json
    if not kb_path.exists():
        cands = sorted(
            kb_dir_path.glob("connector_kb*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for cand in cands:
            if is_valid_kb(cand):
                shutil.copy2(cand, kb_path)
                break

    # Step 3: corrupt live file -> quarantine & restore newest valid .bak.*
    if kb_path.exists() and not is_valid_kb(kb_path):
        stamp = _utc_stamp()
        corrupt_path = kb_path.with_name(f"{kb_path.name}.corrupt.{stamp}")
        os.replace(kb_path, corrupt_path)
        baks = sorted(
            kb_dir_path.glob(f"{kb_file}.bak.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for bak in baks:
            if is_valid_kb(bak):
                shutil.copy2(bak, kb_path)
                break

    if not kb_path.exists():
        raise FileNotFoundError(
            f"KB file not found at {kb_path}. Place {kb_file} in {kb_dir_path} first."
        )

    # Step 4: newer versioned KB -> promote with learned-data carryover
    versioned = sorted(kb_dir_path.glob("connector_kb_v*.json"), key=_version_key)
    if versioned:
        cand = versioned[-1]
        if (
            cand != kb_path
            and cand.stat().st_mtime > kb_path.stat().st_mtime
            and is_valid_kb(cand)
        ):
            _promote_versioned(cand, kb_path)

    with kb_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def query_kb(kb: Dict[str, Any], dotted_path: str, default: Any = None) -> Any:
    """Lookup a value in *kb* by dotted path (e.g. ``'channels.OTA2.system'``)."""
    cur: Any = kb
    for part in dotted_path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return default if cur is None else cur


def query_list(kb: Dict[str, Any], key: str) -> List[str]:
    """Return list of keys/items for a top-level KB section (e.g. ``'channels'``)."""
    val = kb.get(key)
    if isinstance(val, dict):
        return list(val.keys())
    if isinstance(val, list):
        return list(val)
    return []


def lookup_signature(kb: Dict[str, Any], code: str) -> Optional[Dict[str, Any]]:
    """Lookup a failure signature by *code* in ``kb['failure_signatures']``."""
    sigs = kb.get("failure_signatures")
    if isinstance(sigs, dict):
        return sigs.get(code)
    return None


def write_back(
    kb_path: Union[str, Path],
    *,
    case: str = "",
    ref: str = "",
    chan: str = "",
    pms: str = "",
    today: Optional[str] = None,
    prefix: str = "",
    chan_sys: str = "",
    create: str = "",
    apps: Optional[Sequence[str]] = None,
    patterns_hit: Optional[Sequence[str]] = None,
    window_mode: Optional[Sequence[str]] = None,
    signatures: Optional[Sequence[str]] = None,
    key_kinds: Optional[Sequence[str]] = None,
    stale_repaired: Optional[Sequence[str]] = None,
    bak_keep: int = 20,
    hist_keep: int = 50,
    plan_mode: bool = False,
) -> bool:
    """Record a run in *kb_path* under an exclusive ``fcntl`` lock.

    Creates a timestamped ``.bak``, merges history (truncated to *hist_keep*),
    repairs stale apps, caches newly confirmed apps, atomically replaces the
    KB file, and rotates backups keeping the newest *bak_keep* files.

    Returns ``True`` if the KB was updated, ``False`` if skipped (e.g. plan
    mode or non-writable target).
    """
    path = Path(kb_path)
    if plan_mode or not os.access(path, os.W_OK):
        return False

    run_date = today or _utc_date()
    stamp = _utc_stamp()
    app_list = [a for a in (apps or ()) if a and a != "-"]

    extra: Dict[str, List[str]] = {
        "patterns_hit": [x for x in (patterns_hit or ()) if x],
        "window_mode": [x for x in (window_mode or ()) if x],
        "signatures": [x for x in (signatures or ()) if x],
        "key_kinds": [x for x in (key_kinds or ()) if x],
        "stale_repaired": [x for x in (stale_repaired or ()) if x],
    }

    lock_path = str(path) + ".lock"
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with path.open("r", encoding="utf-8") as f:
                kb = json.load(f)

            bak = f"{path}.bak.{stamp}.{os.getpid()}"
            shutil.copy2(path, bak)

            target_chan = (chan or create or prefix or "").strip().upper()
            hist: Dict[str, Any] = {
                "ts": run_date,
                "case": case,
                "mode": "trace-run",
                "ref": ref or None,
                "apps_confirmed_live": app_list,
                "source": "trace run, case " + case,
            }
            for k, v in extra.items():
                if v:
                    hist[k] = v

            def sys_of(app: str) -> str:
                return app.split("/", 1)[0]

            def repair_stale(mod: Dict[str, Any]) -> None:
                if not extra["stale_repaired"]:
                    return
                fixed = mod.setdefault("_stale_repaired", [])
                la = mod.get("log_apps") or {}
                for s in extra["stale_repaired"]:
                    for k, v in list(la.items()):
                        if v == s:
                            la[k] = None
                        elif isinstance(v, list) and s in v:
                            la[k] = [x for x in v if x != s]
                    for lst_key in ("other_apps", "delivery_apps", "inventory_apps"):
                        if isinstance(mod.get(lst_key), list) and s in mod[lst_key]:
                            mod[lst_key] = [x for x in mod[lst_key] if x != s]
                    fixed.append(
                        {
                            "app": s,
                            "marked": run_date,
                            "by": "pms_trace auto-writeback, case " + case,
                        }
                    )

            def touch(mod: Dict[str, Any]) -> Dict[str, Any]:
                h = mod.setdefault("history", [])
                h.append(hist)
                if hist_keep > 0 and len(h) > hist_keep:
                    del h[: len(h) - hist_keep]
                ea = mod.setdefault("example_anchors", {})
                if ref and not ea.get("reservation"):
                    ea["reservation"] = ref
                ea["verified_at"] = run_date
                return mod

            added_c = 0
            added_p = 0
            if target_chan:
                channels = kb.setdefault("channels", {})
                if target_chan not in channels:
                    ref_prefix = (
                        (ref.split("-")[0] + "-")
                        if ref and "-" in ref
                        else (target_chan + "-")
                    )
                    channels[target_chan] = {
                        "name": "UNKNOWN - stub created by auto-writeback; complete manually",
                        "code": target_chan,
                        "integration": None,
                        "ref_prefixes": [ref_prefix],
                        "log_apps": {"reservation_inbound": None, "ari_outbound": None},
                        "behavior_rules": [],
                        "capabilities": {},
                        "example_anchors": {},
                        "_stub_created": run_date,
                        "_stub_by": "pms_trace auto-writeback, case " + case,
                    }
                m = touch(channels[target_chan])
                repair_stale(m)
                la = m.setdefault("log_apps", {})
                concrete = [a for a in app_list if not a.endswith("/*") and "/" in a]
                inbound = la.get("reservation_inbound")
                first = next(iter(inbound), "") if isinstance(inbound, list) else inbound
                csys = chan_sys or (sys_of(first) if first else "")
                own = [a for a in concrete if csys and sys_of(a) == csys]
                if not inbound and own:
                    la["reservation_inbound"] = own[0]
                    la["_confirmed_by"] = "live trace, case " + case
                known = set()
                for key in ("reservation_inbound", "ari_outbound"):
                    value = la.get(key)
                    if isinstance(value, list):
                        known.update(value)
                    elif value:
                        known.add(value)
                oa = la.get("other_apps") if isinstance(la.get("other_apps"), list) else []
                for a in own:
                    if a not in known and a not in oa:
                        oa.append(a)
                        added_c += 1
                if oa:
                    la["other_apps"] = oa

            if pms and pms in kb.get("pms", {}):
                m = touch(kb["pms"][pms])
                repair_stale(m)
                da = m.setdefault("delivery_apps", [])
                psys = {sys_of(a) for a in da}
                for a in app_list:
                    if a.endswith("/*") or "/" not in a:
                        continue
                    if (sys_of(a) in psys or sys_of(a).startswith("pms-")) and a not in da:
                        da.append(a)
                        added_p += 1

            tmp_path = Path(f"{path}.tmp.{os.getpid()}")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(kb, f, indent=1)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)

            baks = sorted(
                glob.glob(str(path) + ".bak.*"),
                key=os.path.getmtime,
                reverse=True,
            )
            for old in baks[bak_keep:]:
                try:
                    os.remove(old)
                except OSError:
                    pass
            print(
                f"[kb-write] updated (channels.{target_chan or '-'} +{added_c} apps, "
                f"pms.{pms or '-'} +{added_p} apps, history appended; {min(len(baks), bak_keep)} .bak kept)"
            )
            return True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
