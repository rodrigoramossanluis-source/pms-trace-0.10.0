"""KB <-> live catalog cross-validation task."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


def _collect_apps(entry: Dict[str, Any]) -> List[str]:
    apps: List[str] = []
    la = entry.get("log_apps") or {}
    if isinstance(la, dict):
        for k, v in la.items():
            if v and not str(k).startswith("_"):
                if isinstance(v, list):
                    apps.extend(str(x) for x in v)
                else:
                    apps.append(str(v))
    for key in ("delivery_apps", "inventory_apps", "other_apps"):
        vals = entry.get(key) or []
        if isinstance(vals, list):
            apps.extend(str(x) for x in vals)
    return sorted(set(apps))


def _split_app(app: str) -> Tuple[str, str]:
    if "/" in app:
        sys_name, comp = app.split("/", 1)
        return sys_name, comp
    return app, ""


def pt_kb_test(
    run_dir: Path,
    kb: Dict[str, Any],
    backend: Optional[Any] = None,
    runner: Optional[Any] = None,
) -> Dict[str, Any]:
    """Cross-validate KB app entries against the live catalog."""
    print("[kb-test] module reachability + catalog cross-check + anchor probes")
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    cat_path = raw_dir / "catalog_root.stdout"

    catalog = ""
    if backend is not None:
        if hasattr(backend, "catalog"):
            catalog = backend.catalog() or ""
        elif hasattr(backend, "list"):
            res = backend.list("*", "*")
            catalog = res.stdout or ""
        cat_path.write_text(catalog, encoding="utf-8")
    elif cat_path.exists():
        catalog = cat_path.read_text(encoding="utf-8", errors="replace")

    cat_norm = catalog.replace("*", "")
    sections: List[Dict[str, Any]] = []
    all_apps: Set[str] = set()

    for sect in ("channels", "pms"):
        sect_data = kb.get(sect) or {}
        for code, m in sect_data.items():
            apps = _collect_apps(m)
            all_apps.update(apps)
            live: List[str] = []
            stale: List[str] = []
            for a in apps:
                sys_name, comp = _split_app(a)
                needle = f"/{sys_name}/{comp}" if comp else f"/{sys_name}"
                if needle in cat_norm or a.split("/")[-1] in catalog:
                    live.append(a)
                else:
                    stale.append(a)
            ea = m.get("example_anchors") or {}
            verified_at = ea.get("verified_at", "?")
            history_count = len(m.get("history") or [])
            print(
                f"[kb-test] {sect[:-1]} {code}: apps live={len(live)} stale={len(stale)} "
                f"verified={verified_at} history={history_count}"
            )
            for a in stale:
                print(f"[kb-test]   STALE? {a} (not in live catalog)")
            sections.append(
                {
                    "section": sect[:-1],
                    "code": code,
                    "live": live,
                    "stale": stale,
                    "verified_at": verified_at,
                    "history_count": history_count,
                }
            )

    sigs = kb.get("failure_signatures") or {}
    sig_names = ", ".join(sorted(sigs)) if sigs else "none"
    print(f"[kb-test] failure_signatures: {len(sigs)} loaded ({sig_names})")

    app_probes: Dict[str, str] = {}
    for app in sorted(all_apps):
        sys_name, comp = _split_app(app)
        comp_arg = comp or "*"
        status = "STALE?"
        if backend is not None:
            if hasattr(backend, "ls"):
                entries = backend.ls(sys_name, comp) or []
                if any(f"{sys_name}/" in str(e) for e in entries):
                    status = "LIVE"
            elif hasattr(backend, "list"):
                res = backend.list(sys_name, comp_arg)
                if any(f"{sys_name}/" in line for line in (res.stdout or "").splitlines()):
                    status = "LIVE"
        elif f"/{sys_name}/{comp}" in cat_norm:
            status = "LIVE"
        app_probes[app] = status
        print(f"[kb-test] {status}  {app}")

    return {
        "sections": sections,
        "failure_signatures": sorted(sigs.keys()),
        "app_probes": app_probes,
    }
