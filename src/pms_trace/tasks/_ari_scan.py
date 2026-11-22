"""Bounded log searches and evidence classification for ARI mode."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from ..backend import Backend, BackendResult, StageRunner, sanitize_stage_name
from ..kb import query_kb


EVIDENCE_COLUMNS = (
    "ts", "kind", "originSystem", "client", "flowToken", "roomRateKey", "values", "dates", "app",
)
MANUAL = re.compile(r"platform|webcm|extranet", re.I)
PMS = re.compile(r"pms|cloud", re.I)
_TS = re.compile(r"(20\d\d-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)")
_SET = re.compile(r'SET\s+(?:RATE|AVAIL|RESTRICTION|STOP|INVENTORY)|"message"\s*:\s*"SET', re.I)
_NON_RATE_SET = re.compile(r"\bSET\s+(?:AVAIL|RESTRICTION|STOP|INVENTORY)", re.I)
_RATE = re.compile(r"SET\s+RATE|rate|price|amount|value", re.I)
_NOISE = re.compile(
    r'\b(?:status(?:Code)?|[A-Za-z_]*[Ii][Dd]|roomRateRef)"?\s*[:=]\s*"?\d+\b'
    r'|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    r'|\b20\d\d-\d\d-\d\d(?:[T ]\d\d:\d\d:\d\d(?:\.\d+)?)?',
    re.I,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def words(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return str(value or "").split()


def data_lines(path: Path, encoding: str = "utf-8") -> Iterator[str]:
    if path.is_file():
        with path.open(encoding=encoding, errors="replace") as fh:
            for line in fh:
                if line.strip() and not line.startswith("[fetch]"):
                    yield line.rstrip("\n")


def has_data(path: Path) -> bool:
    return next(data_lines(path), None) is not None


def evidence_rows(run_dir: Path) -> List[Dict[str, str]]:
    lines = data_lines(run_dir / "ari_evidence.tsv")
    next(lines, None)  # header
    return [dict(zip(EVIDENCE_COLUMNS, fields)) for line in lines
            if len(fields := line.split("\t")) >= len(EVIDENCE_COLUMNS)]


def note(run_dir: Path, line: str) -> None:
    with (run_dir / "REPORT.md").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _date(value: Any) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def days(start: Optional[dt.date], end: Optional[dt.date]) -> Iterator[dt.date]:
    if start is not None and end is not None:
        for offset in range((end - start).days + 1):
            yield start + dt.timedelta(days=offset)


def hour_prefixes(anchor: dt.datetime) -> Iterator[str]:
    for delta in (-1, 0, 1):
        yield (anchor + dt.timedelta(hours=delta)).strftime("%Y-%m-%d-%H")


def channel_code(cfg: Dict[str, Any], kb: Dict[str, Any]) -> str:
    codes = query_kb(kb, "channel_codes.codes", {})
    wanted = str(cfg.get("PT_CHAN") or cfg.get("PT_CHANNEL") or "").strip().upper()
    if wanted:
        for code, entry in codes.items():
            name = str(entry.get("name", "")).upper()
            if wanted in (code, name, name.split("(")[0].strip()):
                return code
        return wanted
    prefix = str(cfg.get("PT_REF") or "").upper().split("-")[0].split(":")[0]
    return prefix if prefix in codes or prefix in (kb.get("channels") or {}) else ""


def is_channelrelay(cfg: Dict[str, Any], kb: Dict[str, Any]) -> bool:
    code = channel_code(cfg, kb)
    flag = query_kb(kb, f"channel_codes.codes.{code}.channelrelay")
    if flag is not None:
        return str(flag).lower() == "true"
    stack = query_kb(kb, f"channels.{code}.stack", "")
    return "channelrelay" in f"{code} {stack} {cfg.get('PT_CHAN_SYS', '')}".lower()


def _field(line: str, name: str) -> str:
    match = re.search(r'"' + name + r'"\s*:\s*"([^"\t]*)"', line)
    if not match:
        match = re.search(r'\b' + name + r'=\s*"?([^",\s}]+)', line)
    return match.group(1) if match else ""


class AriScan:
    def __init__(
        self, run_dir: Path, cfg: Dict[str, Any], kb: Dict[str, Any],
        backend: Optional[Backend], runner: Optional[StageRunner],
    ) -> None:
        self.run_dir = run_dir
        self.cfg = cfg
        self.kb = kb
        self.backend = backend
        self.runner = runner if runner is not None else StageRunner(
            run_dir, resume=self.get("PT_RESUME") == "1", plan=self.get("PT_MODE") == "plan",
            skip_legs=words(cfg.get("PT_SKIP_LEGS")),
        )
        self.raw = run_dir / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.state = run_dir / "state"
        self.state.mkdir(exist_ok=True)
        self.plan = bool(getattr(self.runner, "plan", False)) or self.get("PT_MODE") == "plan"
        self.errors: List[int] = []
        self.channel = channel_code(cfg, kb)
        self.channelrelay = is_channelrelay(cfg, kb)
        self.channel_sys = self.get("PT_CHAN_SYS")
        self.channel_apps: List[str] = []
        self._pools: Dict[str, List[str]] = {}
        at = re.match(r"^(\d{4}-\d\d-\d\d)(?:[T ](\d{1,2})(?=:|$)|$)", self.get("PT_AT"))
        # PT_AT is the log timestamp, already on the archive clock. Don't apply PT_TZ_OFFSET.
        at_date = _date(self.get("PT_AT_DATE") or (at.group(1) if at else None))
        at_hour = self.get("PT_AT_HOUR") or ((at.group(2) or "00") if at else "00")
        self.anchor = (dt.datetime.combine(at_date, dt.time(int(at_hour)))
                       if at_date and at_hour.isdecimal() and 0 <= int(at_hour) <= 23 else None)
        self.first_day = _date(self.get("PT_FROM") or at_date or self.get("PT_TO"))
        self.last_day = _date(self.get("PT_TO") or at_date or self.get("PT_FROM"))
        self.legacy_day = at_date or self.first_day
        self.propkey = self.get("PT_PROPERTY_ID")
        self.hotel = self.get("PT_HOTEL_UUID")
        self.target_values = words(cfg.get("PT_TARGET_VALUES"))
        self.stay_dates = words(cfg.get("PT_STAY_DATES"))
        self.push_hours: List[str] = []

    def get(self, key: str, default: str = "") -> str:
        return str(self.cfg.get(key) or default).strip()

    def log(self, msg: str) -> None:
        print(f"[ari] {msg}")

    def note(self, msg: str) -> None:
        note(self.run_dir, msg)

    def stage(self, name: str, label: str, fn: Callable[[], Any]) -> Any:
        """A stage that synthesizes artifacts or groups several individual fetches."""
        result = None

        def run() -> BackendResult:
            nonlocal result
            start = len(self.errors)
            result = fn()
            # If a child failed, don't mark the parent done (resume must retry it).
            return BackendResult("", "", max(self.errors[start:], default=0))

        if hasattr(self.runner, "run_stage"):
            self.runner.run_stage(name, label, run)
        else:
            result = self.runner.run(name, label, fn)
        return result

    def _search(self, name: str, label: str, command: str, fn: Callable[[], BackendResult]) -> Path:
        path = self.raw / f"{sanitize_stage_name(name)}.out"
        if self.backend is None and not self.plan:
            return path  # offline synthesis from existing artifacts
        if hasattr(self.runner, "run_stage"):
            result = self.runner.run_stage(name, label, fn, plan_cmd=command)
            if result.rc > 1:
                self.errors.append(result.rc)
            if result.status == "refused":
                self.note(f"- FETCH skipped: {label} (UNVERIFIED; see raw/{path.stem}.stderr)")
        else:
            def fetch() -> None:
                result = fn()
                path.write_text(result.stdout, encoding="utf-8")
            self.runner.run(name, label, fetch)
        return path

    def grep(self, name: str, app: str, blob: str, pattern: str, *,
             label: str = "", field: str = "") -> Path:
        command = f"s3search grep {app} {blob} {pattern!r}"
        # Query for exact flowToken when possible; retain literal grep for older
        # backends or non-JSON logs where the token isn't a top-level field.
        expr = f's.{field} = "{pattern}"' if field and re.fullmatch(r"[\w.:-]+", pattern) else ""
        if expr:
            command = f"s3search query {app} {blob} {expr!r} (literal grep fallback)"

        def fetch() -> BackendResult:
            if expr and hasattr(self.backend, "query"):
                result = self.backend.query(app, blob, expr)
                if result.stdout.strip() or result.rc not in (0, 1, 2):
                    return result
            return self.backend.grep(app, blob, pattern)

        return self._search(name, label or f"{app} {blob} {pattern!r}", command, fetch)

    def legacy(self, name: str, label: str, pattern: str, bucket: str, day: str) -> Path:
        def fetch() -> BackendResult:
            if hasattr(self.backend, "legacy_find"):
                return self.backend.legacy_find(pattern, bucket, day)
            data = self.backend.lgcy_fetch(pattern, bucket, day) or ""
            return BackendResult(data, "", 0 if data else 1)

        return self._search(name, label, f"s3grep find {pattern!r} {bucket!r} {day}", fetch)

    def partitions(self) -> List[str]:
        explicit = words(self.cfg.get("PT_LGCY_PARTITIONS"))
        if explicit:
            return explicit
        default = words(query_kb(self.kb, "channelrelay_lgcy.default_partition_order")) or ["three", "one", "two"]
        known = query_kb(self.kb, f"channelrelay_lgcy.partitions.{self.channel}.partition")
        return list(dict.fromkeys(([str(known)] if known else []) + default))

    def pool(self, system: str) -> List[str]:
        if system not in self._pools:
            apps = []
            if not self.plan and hasattr(self.backend, "list"):
                result = self.backend.list(system, "*")
                if result.rc == 0:
                    for line in result.stdout.splitlines():
                        match = re.search(r"(?:^|\s|/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s*$", line)
                        if match and (system == "*" or match.group(1).startswith(system + "/")):
                            apps.append(match.group(1))
            self._pools[system] = sorted(set(apps))
        return self._pools[system]

    def resolve(self) -> None:
        if not self.channel:
            return
        path = self.run_dir / "resolve.tsv"
        module = query_kb(self.kb, f"channels.{self.channel}", {})
        log_apps = module.get("log_apps") or {}
        kb_apps = [app for key in ("reservation_inbound", "ari_outbound", "other_apps")
                   for app in words(log_apps.get(key)) if "/" in app]
        dedicated = query_kb(self.kb, f"dedicated_systems.{self.channel}.system")
        if self.channel_sys:
            source = "sys_from_config"
        elif kb_apps:
            self.channel_sys = kb_apps[0].split("/", 1)[0]
            source = "sys_from_kb_apps"
        elif dedicated:
            self.channel_sys = str(dedicated)
            source = "sys_from_dedicated_systems"
        elif self.channelrelay:
            self.channel_sys = query_kb(self.kb, "channelrelay.system", "cm-channelrelay")
            source = "sys_from_channelrelay"
        else:
            conventional = f"cm-{self.channel.lower()}"
            if self.pool(conventional):
                self.channel_sys = conventional
                source = "sys_from_convention"
            else:
                token = str(module.get("app_match") or self.channel).lower()
                candidates = [app.split("/", 1)[0] for app in self.pool("*") if token in app.lower()]
                self.channel_sys = candidates[0] if candidates else ""
                source = "sys_from_namematch"
        path.write_text(f"{source}|{self.channel}|{self.channel_sys or 'none'}\n"
                        f"kb_module|{self.channel}|sys={self.channel_sys or '?'}|apps={' '.join(kb_apps)}\n",
                        encoding="utf-8")

    def preflight(self) -> None:
        if self.get("PT_NO_PREFLIGHT") == "1" or not self.anchor:
            return
        app, blob = "cm/avail-api-ra", self.anchor.strftime("%Y-%m-%d-%H")
        token = self.get("PT_CONTROL_TOKEN_CM", "propertyUuid")

        def fetch() -> BackendResult:
            backend = self.backend
            if type(backend) is Backend:
                backend = Backend(backend.s3search_cmd, timeout=float(self.get("PT_PREFLIGHT_TIMEOUT", "30")),
                                  max_lines=200, env=backend.env)
            return backend.grep(app, blob, token)

        path = self._search("preflight_cm", f"CM positive control {app} {blob}",
                            f"s3search grep {app} {blob} {token!r}", fetch)
        if self.plan:
            return
        if has_data(path):
            self.note("- preflight control cm: OK")
        else:
            self.note("- **PREFLIGHT FAIL cm** - positive control returned no lines; empty inventory sweeps are UNVERIFIED.")

    def _target(self, line: str, values: List[str], dates: List[str]) -> bool:
        return bool(values and dates and _RATE.search(line) and not _NON_RATE_SET.search(line))

    def _matches(self, line: str) -> tuple[List[str], List[str]]:
        scrub = _NOISE.sub(" ", line)
        values = [v for v in self.target_values if re.search(r"(?<![\d.])" + re.escape(v) + r"(?![\d.])", scrub)]
        dates = [d for d in self.stay_dates if d in line]
        return values, dates

    def inventory(self) -> None:
        if not self.anchor or not self.first_day:
            self.note("- inventory sweep: skipped (no valid anchor/PT_FROM; UNVERIFIED)")
            return
        anchor_day = self.anchor.date()
        mode = self.get("PT_ANCHOR_MODE", "booking")
        region = self.get("PT_REGION", "both")
        regions = (region,) if region in ("ra", "rb") else ("ra", "rb")
        stop = self.get("PT_BREAK_ON", "auto")
        if stop == "auto":
            stop = "target" if self.get("PT_GOAL") == "rate-mismatch" else "any"
        self.note(f"- inventory sweep: anchor_mode={mode} at {self.anchor:%Y-%m-%d-%H}, from {self.first_day}, "
                  f"region(s)={' '.join(regions)}, key={self.hotel}; hour-bounded, no auto-expansion.")
        if mode == "day":
            span: Iterable[dt.date] = (anchor_day + dt.timedelta(days=n) for n in (0, -1, 1))
            stop = "any"
        else:
            span = (anchor_day - dt.timedelta(days=n) for n in range((anchor_day - self.first_day).days + 1))
        hits: set[str] = set()
        stopped = False
        if self.hotel:
            for day in span:
                if day < self.first_day or (mode == "day" and self.last_day and day > self.last_day):
                    continue
                if mode == "day":
                    hours = [(self.anchor.hour - n) % 24 for n in range(24)]
                else:
                    hours = range(self.anchor.hour if day == anchor_day else 23, -1, -1)
                for reg in regions:
                    app = f"cm/avail-api-{reg}"
                    for hour in hours:
                        blob = f"{day}-{hour:02d}"
                        path = self.grep(f"t_ari_inv_{sanitize_stage_name(app)}_{blob}", app, blob, self.hotel)
                        if not self.plan and has_data(path):
                            hits.add(blob)
                            matched = stop != "target" or any(
                                _SET.search(line) and self._target(line, *self._matches(line))
                                for line in data_lines(path)
                            )
                            if matched:
                                if stop == "target":
                                    self.log(f"TARGET hour found at {blob} - stopping sweep")
                                stopped = True
                                break
                    if stopped:
                        break
                if stopped:
                    break
        else:
            self.note("- inventory SET search skipped (no CM hotel UUID; UNVERIFIED)")
        channel_hotel = self.get("PT_CHANNEL_HOTEL_UUID")
        if channel_hotel and channel_hotel != self.hotel:
            for reg in regions:
                app = f"cm/avail-api-{reg}"
                blob = self.anchor.strftime("%Y-%m-%d-%H")
                self.grep(f"t_ari_probe_{sanitize_stage_name(app)}_{blob}", app, blob, channel_hotel)
        self.push_hours = sorted({h for hit in hits for h in hour_prefixes(dt.datetime.strptime(hit, "%Y-%m-%d-%H"))})
        if not hits and not self.plan:
            self.log("no inventory lines in the bounded window - claim UNCONFIRMED")

    def classify(self) -> None:
        rows = []
        for path in sorted(self.raw.glob("t_ari_inv_*.out")):
            for line in data_lines(path):
                if not _SET.search(line):
                    continue
                ts = _TS.search(line)
                values, dates = self._matches(line)
                kind = "TARGET_EVENT" if self._target(line, values, dates) else "partial" if values or dates else "context"
                rows.append(("T".join(ts.groups()) if ts else "", kind,
                             _field(line, "originSystem") or _field(line, "client_system"),
                             _field(line, "client"), _field(line, "flowToken"), _field(line, "roomRateKey"),
                             ",".join(values), ",".join(dates), path.stem))
        rows.sort(key=lambda row: row[0])
        with (self.run_dir / "ari_evidence.tsv").open("w", encoding="utf-8") as fh:
            fh.write("\t".join(EVIDENCE_COLUMNS) + "\n")
            for row in rows:
                fh.write("\t".join(row) + "\n")
        self.log(f"{len(rows)} SET lines kept ({sum(row[1] == 'TARGET_EVENT' for row in rows)} TARGET_EVENT)")

    def attribute(self) -> None:
        pairs = sorted({(row["ts"], row["flowToken"]) for row in evidence_rows(self.run_dir)
                        if row["ts"] and row["flowToken"] and MANUAL.search(row["originSystem"])})[:8]
        seen = set()
        for ts, token in pairs:
            blob = ts[:13].replace("T", "-")
            name = f"f_ari_gui_{sanitize_stage_name(token)}"
            if name in seen:
                name += f"_{blob}"
            seen.add(name)
            self.grep(name, "portal-property/gui-gateway", blob, token, field="flowToken")
        # Preserve pre-existing offline evidence, but don't attribute from a token
        # carried over after the classifier found no matching manual SET lines.
        if not pairs:
            return
        emails = sorted({email for path in self.raw.glob("f_ari_gui_*.out") for line in data_lines(path)
                         for email in _EMAIL.findall(line) if not email.endswith(("example.com", "example.internal"))})
        if emails:
            (self.run_dir / "ari_actors.txt").write_text("\n".join(emails) + "\n", encoding="utf-8")
            self.log(f"user(s) behind platform updates: {', '.join(emails)}")

    def ingest(self) -> None:
        src = self.get("PT_SOURCE_SYSTEM", "auto").lower()
        if src == "auto":
            origins = [r["originSystem"] for r in evidence_rows(self.run_dir)]
            if not any(PMS.search(origin) for origin in origins):
                self.note("- INGEST leg: N/A - origin is platform/manual (or no SET evidence); no PMS push in the chain")
                return
            src = "bridge" if any("cloud" in origin.lower() for origin in origins) else "xengine"
        if src == "bridge" or src.startswith("cloud"):
            app, key, kind = "pms-cm-bridge/avail-sync", self.propkey, "propkey"
            if key:
                self.log("CloudPMS ingest leg: pms-cm-bridge/avail-sync by propkey (batch-renamed flowTokens)")
                self.note(f"- INGEST leg: {app} swept by {kind}={key} (batch tokens: free-text search)")
        elif src == "xengine":
            app, key, kind = "hub-xbridge/ari-feed", self.get("PT_PMS_HOTEL_CODE"), "hotelCode"
            if key:
                self.note(f"- INGEST leg: {app} swept by {kind}={key}; ari-relay skipped (blind hop)")
        else:
            self.log(f"unknown PT_SOURCE_SYSTEM={src!r} - ingest skipped")
            return
        if not key:
            self.note(f"- INGEST leg: skipped ({kind} missing; UNVERIFIED)")
            return
        for day in days(self.first_day, self.last_day):
            self.grep(f"t_ari_ing_{sanitize_stage_name(app)}_{day}", app, f"{day}-*", key)

    def push_apps(self) -> List[str]:
        explicit = words(self.cfg.get("PT_CHANNEL_APP"))
        if explicit or not self.channel_sys:
            return explicit
        pool = self.pool(self.channel_sys)
        apps = [a for a in pool if re.search(r"inventory|avail|ari|rate|connection", a.split("/", 1)[1], re.I)][:3]
        if not apps:
            apps = [f"{self.channel_sys}/avail-gateway"]
            self.note(f"- CHANNEL_PUSH apps: FALLBACK {apps[0]} (pool had no inventory/connection comps; pool={' '.join(pool)})")
        return apps

    def push(self) -> None:
        if self.channelrelay:
            self.note("- CHANNEL_PUSH(CUR): skipped - ChannelRelay ARI is on LGCY sync partitions (key listingAcctId)")
            return
        self.channel_apps = self.push_apps()
        if not self.channel_apps:
            self.note("- CHANNEL_PUSH: skipped (no channel resolved)")
            return
        patterns = list(dict.fromkeys(filter(None, (self.get(key) for key in (
            "PT_MEMBERSHIP", "PT_HOTELIER_ID2", "PT_CHANNEL_HOTEL_UUID", "PT_RATE_CODE",
            "PT_ROOM_TYPE_CODE", "PT_CM_HOTELIER_ID", "PT_ENTITY_ID",
        )))))
        (self.state / "push_pats.txt").write_text("".join(p + "\n" for p in patterns), encoding="utf-8")
        if not patterns:
            self.note("- CHANNEL_PUSH: skipped (no push-side identifiers)")
            return
        until = self.anchor.date() if self.anchor else self.last_day
        blobs = self.push_hours or [f"{day}-*" for day in days(self.first_day, until)]
        self.note(f"- CHANNEL_PUSH leg: apps={' '.join(self.channel_apps)} patterns={', '.join(patterns)} "
                  "(chained/bounded; OTA ack behavior is channel-specific)")
        for app in self.channel_apps:
            for pat in patterns:
                for blob in blobs:
                    self.grep(f"t_ari_push_{sanitize_stage_name(app)}_{blob}_{sanitize_stage_name(pat)}", app, blob, pat)

    def bridge_values(self) -> None:
        if not self.propkey or not self.push_hours:
            self.note("- BRIDGE_VALUES: skipped (no propkey or no inventory hit hours to chain from)")
            return
        for tag, app in (("bv", "avail-sync"), ("bridge", "state-sync-api")):
            self.note(f"- BRIDGE_{'VALUES' if tag == 'bv' else 'STATE'} leg: pms-cm-bridge/{app} "
                      f"by propkey={self.propkey} at chained hours: {' '.join(self.push_hours)} - raw/t_ari_{tag}_*")
            for blob in self.push_hours:
                self.grep(f"t_ari_{tag}_pms-cm-bridge_{app}_{blob}", f"pms-cm-bridge/{app}", blob, self.propkey)

    def billing(self) -> None:
        first, last = (_date(self.get(f"PT_BILLING_{bound}")) for bound in ("FROM", "TO"))
        if first is None or last is None or first > last or not self.propkey:
            self.note("- BILLING leg: skipped (no valid billing window or propkey)")
            return
        self.note(f"- BILLING leg: billing-service/events {first}..{last} by propkey={self.propkey} "
                  "(suspend/reactivate timeline; empty results are not proof of absence)")
        for day in days(first, last):
            self.grep(f"t_bil_billing-service_events_{day}", "billing-service/events", f"{day}-*", self.propkey)
        for day in words(self.cfg.get("PT_BILLING_KEY_DAYS")):
            if not _date(day):
                continue
            for account in words(self.cfg.get("PT_BILLING_ACCOUNTS")):
                self.grep(f"t_bil_payments_{day}_{sanitize_stage_name(account)}", "payments/payment-gateway", f"{day}-*", account)

    def series(self) -> None:
        first = _date(self.get("PT_SERIES_FROM")) if self.get("PT_SERIES_FROM") else self.first_day
        last = _date(self.get("PT_SERIES_TO")) if self.get("PT_SERIES_TO") else self.last_day
        if not self.propkey or first is None or last is None or first > last:
            self.note("- ARI_SERIES leg: skipped (no valid window or propkey)")
            return
        self.note(f"- ARI_SERIES leg: pms-cm-bridge/avail-sync + state-sync-api {first}..{last} by propkey={self.propkey} "
                  "(CM avail-api day blobs refused by design)")
        timeline = ["", "### ARI presence timeline (pms-cm-bridge, by day)"]
        for day in days(first, last):
            cells = [str(day)]
            for comp, short in (("avail-sync", "inv"), ("state-sync-api", "core")):
                path = self.grep(f"t_ser_bridge-{comp}_{day}", f"pms-cm-bridge/{comp}", f"{day}-*", self.propkey)
                count = 0
                first_ts = last_ts = ""
                for count, line in enumerate(data_lines(path), 1):
                    if match := _TS.search(line):
                        last_ts = match.group(0)
                        first_ts = first_ts or last_ts
                cells.append(f"{short}={count}" + (f" ({first_ts}..{last_ts})" if first_ts else ""))
            timeline.append("- " + " ".join(cells))
        timeline.append("(0 = no bridge traffic evidenced; cross-check BILLING and retention before concluding suspension.)")
        self.note("\n".join(timeline))

    def booking_xml(self) -> None:
        ref = self.get("PT_BOOKING_REF")
        if not ref or not self.anchor:
            return
        apps = [a for a in words(query_kb(self.kb, f"channels.{self.channel}.log_apps.reservation_inbound")) if "/" in a]
        if not apps and self.channel_sys:
            apps = [a for a in self.pool(self.channel_sys) if re.search(r"booking-gateway|booking-processor", a, re.I)][:2]
            apps = apps or [f"{self.channel_sys}/booking-gateway"]
        if not apps:
            self.note("- BOOKING_XML leg: skipped (no channel system resolved)")
            return
        patterns = list(dict.fromkeys((ref, ref.rsplit("-", 1)[-1])))
        self.note(f"- BOOKING_XML leg: apps={' '.join(apps)} at booking hour +/-1h; patterns={patterns} - raw/t_ari_xml_*")
        for app in apps:
            for blob in hour_prefixes(self.anchor):
                for pattern in filter(None, patterns):
                    self.grep(f"t_ari_xml_{sanitize_stage_name(app)}_{blob}_{sanitize_stage_name(pattern)}", app, blob, pattern)
