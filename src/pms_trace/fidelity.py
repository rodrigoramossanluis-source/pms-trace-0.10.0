"""Standalone OTA-vs-PMS payload fidelity checker."""

from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


def _resolve_doc(run_dir: Path, file_arg: str, default_raw_name: str) -> Optional[Path]:
    """Resolve an already-fetched document path without invoking external fetchers."""
    if not file_arg:
        candidate = run_dir / "raw" / default_raw_name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
        return None

    p = Path(file_arg)
    if p.is_file() and p.stat().st_size > 0:
        return p

    for base in (Path.cwd(), Path.home() / "pms_tools", Path.home(), run_dir):
        candidate = base / file_arg
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def extract_xml(raw: str, roots: Sequence[str]) -> Optional[str]:
    """Extract the first matching root element block from raw text."""
    for root in roots:
        start = raw.find("<" + root)
        if start >= 0:
            end = raw.rfind("</" + root + ">")
            if end > start:
                return raw[start : end + len(root) + 3]
    return None


def unesc(text: str) -> str:
    """Unescape backslash-escaped quotes and newlines from log-embedded XML."""
    return (
        text.replace("\\\\", "\x00")
        .replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\x00", "\\")
    )


def parse_fields(xml: str) -> Dict[str, Any]:
    """Extract comparable booking fields from OTA or PMS XML."""
    out: Dict[str, Any] = {}
    m = re.search(r'AmountAfterTax=\\?"([\d.]+)\\?"', xml)
    if m:
        out["after_tax"] = m.group(1)
    m = re.search(r'AmountBeforeTax=\\?"([\d.]+)\\?"', xml)
    if m:
        out["before_tax"] = m.group(1)
    taxes = re.findall(
        r'<Tax [^>]*Amount=\\?"([\d.]+)\\?"[^>]*Code=\\?"([\w]+)\\?"[^>]*Type=\\?"(\w+)\\?"',
        xml,
    )
    if taxes:
        out["taxes"] = taxes
    m = re.search(r"<valueNum>([\d.]+)</valueNum>", xml)
    if m:
        out["pms_amount"] = m.group(1)
    m = re.search(r'CurrencyCode=\\?"([A-Z]{3})\\?"', xml)
    if m:
        out["currency"] = m.group(1)
    m = re.search(r'RoomTypeCode=\\?"([\w-]+)\\?"', xml)
    if m:
        out["room_code"] = m.group(1)
    m = re.search(r'RatePlanCode=\\?"([\w-]+)\\?"', xml)
    if m:
        out["rate_code"] = m.group(1)
    guests = re.findall(r"<GivenName>([^<]+)</GivenName>", xml)
    if guests:
        out["guests"] = guests
    dates = re.findall(
        r'<TimeSpan Start=\\?"([\d-]+)\\?" End=\\?"([\d-]+)\\?"',
        xml,
    )
    if dates:
        out["dates"] = dates[0]
    return out


def _verdict(src: Any, dst: Any) -> Tuple[str, str]:
    if src and dst:
        return ("PRESERVED" if src == dst else "TRANSFORMED", f"{src} -> {dst}")
    if src and not dst:
        return ("DROPPED", f"{src} -> (absent)")
    if dst and not src:
        return ("ADDED", f"(absent) -> {dst}")
    return ("ABSENT_BOTH", "-")


def run_fidelity(run_dir: Path, ota_file: str, pms_file: str) -> bool:
    """Compare already-fetched OTA and PMS documents and write fidelity_report.txt.

    Returns True when both documents are available and the report is written,
    or False when either document is unavailable/empty.
    """
    run_path = Path(run_dir)
    ota_path = _resolve_doc(run_path, ota_file, "ota_doc.xml")
    pms_path = _resolve_doc(run_path, pms_file, "pms_doc.xml")

    if ota_path is None or pms_path is None:
        return False

    raw_ota = unesc(ota_path.read_text(encoding="latin-1"))
    raw_pms = unesc(pms_path.read_text(encoding="latin-1"))

    ota_xml = (
        extract_xml(
            raw_ota,
            ["OTA_HotelResNotifRQ", "OTA_HotelResNotifRS", "OTA_HotelResRQ"],
        )
        or raw_ota
    )
    pms_xml = extract_xml(raw_pms, ["Reservation", "OTA_HotelResNotifRQ"]) or raw_pms

    ota = parse_fields(ota_xml)
    pms = parse_fields(pms_xml)

    rows: List[Tuple[str, str, str]] = []
    for label, sk, pk in [
        ("rate.amount_after_tax -> pms.amount", "after_tax", "pms_amount"),
        ("rate.amount_before_tax", "before_tax", "before_tax"),
        ("rate.currency", "currency", "currency"),
        ("room.code", "room_code", "room_code"),
        ("rate.code", "rate_code", "rate_code"),
        ("guest.names", "guests", "guests"),
        ("stay.dates", "dates", "dates"),
    ]:
        v, d = _verdict(ota.get(sk), pms.get(pk))
        rows.append((label, v, d))

    ota_taxes = "x".join(t[0] for t in ota.get("taxes", [])) or None
    pms_taxes = "x".join(t[0] for t in pms.get("taxes", [])) or None
    v, d = _verdict(ota_taxes, pms_taxes)
    rows.append(("rate.taxes[]", v, d))

    verdicts: List[str] = []
    if (
        ota.get("before_tax")
        and pms.get("pms_amount")
        and pms["pms_amount"] == ota.get("after_tax")
    ):
        verdicts.append(
            "CONFIG_IGNORED: PMS doc carries the after-tax amount while a before-tax amount existed at source"
        )
    if ota.get("taxes") and not pms.get("taxes"):
        verdicts.append(
            f"TAX_BREAKDOWN_DROPPED: {len(ota['taxes'])} tax lines at source, none in the PMS doc"
        )
    for field_name, status, detail in rows:
        if status == "DROPPED":
            verdicts.append(f"FIELD_DROPPED: {field_name} ({detail})")

    lines = [
        "FIDELITY REPORT",
        f"ota_doc={ota_path}",
        f"pms_doc={pms_path}",
        "",
    ]
    lines.extend(f"{f:<42} {v:<12} {d}" for f, v, d in rows)
    lines.extend(["", "VERDICTS:"])
    lines.extend(f"  - {item}" for item in verdicts)

    run_path.mkdir(parents=True, exist_ok=True)
    report_path = run_path / "fidelity_report.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _lookup_join_key(run_dir: Path, keys: Sequence[str]) -> str:
    """Return the first matching value for ``keys`` from ``join_keys.tsv``."""
    jk_path = run_dir / "join_keys.tsv"
    if not jk_path.is_file():
        return ""
    wanted = set(keys)
    found: Dict[str, str] = {}
    for line in jk_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] in wanted and parts[0] not in found and parts[1]:
            found[parts[0]] = parts[1]
    for key in keys:
        if key in found:
            return found[key]
    return ""


def _fetch_doc(
    dest: Path,
    stage_name: str,
    app: str,
    blob: str,
    token: str,
    backend: Any,
    runner: Any,
) -> None:
    """Fetch a document via ``runner.run_stage`` (or ``backend.grep``) into ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if runner is not None:
        res = runner.run_stage(
            stage_name,
            f"{app} {blob} {token}",
            lambda: backend.grep(app, blob, token),
        )
        if res.out_path is not None and res.out_path.is_file() and res.out_path.stat().st_size > 0:
            dest.write_bytes(res.out_path.read_bytes())
    else:
        bres = backend.grep(app, blob, token)
        if bres.stdout:
            dest.write_text(bres.stdout, encoding="utf-8")


def run_fidelity_with_fetch(
    run_dir: Path,
    kb: Mapping[str, Any],
    env: Mapping[str, str],
    backend: Any,
    runner: Any,
) -> bool:
    """Auto-fetch missing OTA/PMS documents if tokens are available, then run fidelity."""
    _ = kb  # reserved for future KB-driven fidelity rules
    run_path = Path(run_dir)
    ota_file = env.get("PT_OTA_FILE", "")
    pms_file = env.get("PT_PMS_FILE", "")

    date_prefix = env.get("PT_AT_DATE") or env.get("PT_FROM") or ""
    if not date_prefix and env.get("PT_AT"):
        date_prefix = env["PT_AT"][:10]
    blob = f"{date_prefix}-*" if date_prefix else "*"

    chan_sys = env.get("PT_CHAN_SYS", "")
    if not chan_sys and (run_path / "resolve.tsv").is_file():
        for line in (run_path / "resolve.tsv").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(("sys_from_kb_apps|", "sys_from_convention|", "sys_from_namematch|")):
                parts = line.split("|")
                if len(parts) >= 3 and parts[2] and parts[2] != "none":
                    chan_sys = parts[2]
                    break

    if _resolve_doc(run_path, ota_file, "ota_doc.xml") is None:
        tok = env.get("PT_OTA_TOKEN") or _lookup_join_key(run_path, ("echoToken", "traceToken"))
        if tok and chan_sys:
            _fetch_doc(
                run_path / "raw" / "ota_doc.xml",
                "fidelity_ota_doc",
                f"{chan_sys}/notify-gateway",
                blob,
                tok,
                backend,
                runner,
            )

    if _resolve_doc(run_path, pms_file, "pms_doc.xml") is None:
        uuid = env.get("PT_PMS_TOKEN") or _lookup_join_key(
            run_path, ("hubBookingUuid", "pmsBookingId")
        )
        if uuid:
            _fetch_doc(
                run_path / "raw" / "pms_doc.xml",
                "fidelity_pms_doc",
                "pms-hub/booking-api",
                blob,
                uuid,
                backend,
                runner,
            )

    ok = run_fidelity(run_path, ota_file, pms_file)
    if not ok:
        print(f"[fidelity] docs unavailable (ota={ota_file or 'none'} pms={pms_file or 'none'}).")
    return ok
