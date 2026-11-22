"""OpenSearch transport for pms-trace.

Implements the same surface as :class:`pms_trace.backend.Backend`
(``list``, ``grep``, ``query``, ``legacy_find``) against an OpenSearch
cluster. Core package stays zero-dependency: ``opensearch-py`` is only
imported when this backend is selected.

Configuration (environment):
  OS_URL              required, e.g. https://opensearch.example:9200
  OS_INDEX_PATTERN    default ``logs-{sys}-{comp}-*``
                      placeholders: {sys} {comp} {date} {yyyy} {mm} {dd} {hour}
  OS_TIME_FIELD       default ``@timestamp``
  OS_MESSAGE_FIELD    default ``message``
  OS_SYS_FIELD        default ``system``   (field that holds the system name)
  OS_COMP_FIELD       default ``component``
  OS_USER / OS_PASSWORD   basic auth (optional)
  OS_API_KEY          alternative to user/password
  OS_VERIFY_CERTS     default ``1`` (set ``0`` to disable TLS verify)
  OS_TIMEOUT          request timeout seconds (default 30)
  OS_MAX_HITS         default 200000 (capped by Backend.max_lines)

Selection:
  PT_BACKEND=opensearch   or
  PT_BACKEND=auto and OS_URL is set
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .backend import (
    BackendResult,
    _HOUR_BOUNDED_BLOB_RE,
    _LOW_SELECTIVITY_NUMERIC_RE,
    _WEAK_NUMERIC_RE,
    split_app,
)

_BLOB_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2}|\*)$")


class OpenSearchBackend:
    """Log-search backend backed by an OpenSearch cluster."""

    def __init__(
        self,
        url: Optional[str] = None,
        timeout: float = 120.0,
        max_lines: int = 200000,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.env = dict(env) if env is not None else dict(os.environ)
        self.url = (url or self.env.get("OS_URL") or "").rstrip("/")
        self.timeout = float(self.env.get("OS_TIMEOUT", timeout))
        self.max_lines = int(self.env.get("OS_MAX_HITS", max_lines))
        self.index_pattern = self.env.get("OS_INDEX_PATTERN", "logs-{sys}-{comp}-*")
        self.time_field = self.env.get("OS_TIME_FIELD", "@timestamp")
        self.message_field = self.env.get("OS_MESSAGE_FIELD", "message")
        self.sys_field = self.env.get("OS_SYS_FIELD", "system")
        self.comp_field = self.env.get("OS_COMP_FIELD", "component")
        self.verify_certs = self.env.get("OS_VERIFY_CERTS", "1") != "0"
        self._client = None

        if not self.url:
            raise ValueError(
                "OpenSearch backend requires OS_URL "
                "(e.g. export OS_URL=https://opensearch.example:9200)"
            )

    # ------------------------------------------------------------------ client

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from opensearchpy import OpenSearch  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "OpenSearch backend requires the optional dependency.\n"
                "  pip install 'pms-trace[opensearch]'\n"
                "  # or: pip install opensearch-py>=2.4"
            ) from exc

        kwargs: Dict[str, Any] = {
            "hosts": [self.url],
            "verify_certs": self.verify_certs,
            "ssl_show_warn": False,
            "timeout": self.timeout,
        }
        api_key = self.env.get("OS_API_KEY")
        user = self.env.get("OS_USER")
        password = self.env.get("OS_PASSWORD")
        if api_key:
            kwargs["http_auth"] = None
            # opensearch-py accepts api_key as (id, api_key) or a header via headers=
            kwargs["headers"] = {"Authorization": f"ApiKey {api_key}"}
        elif user:
            kwargs["http_auth"] = (user, password or "")

        self._client = OpenSearch(**kwargs)
        return self._client

    # ------------------------------------------------------------------ helpers

    def _resolve_index(self, sys_name: str, comp_name: str, blob: str = "") -> str:
        yyyy = mm = dd = hour = "*"
        m = _BLOB_RE.match(blob) if blob else None
        if m:
            yyyy, mm, dd, hour = m.groups()
        date = f"{yyyy}-{mm}-{dd}" if yyyy != "*" else "*"
        return (
            self.index_pattern.replace("{sys}", sys_name)
            .replace("{comp}", comp_name)
            .replace("{date}", date)
            .replace("{yyyy}", yyyy)
            .replace("{mm}", mm)
            .replace("{dd}", dd)
            .replace("{hour}", hour)
        )

    def _time_range(self, blob: str) -> Optional[Dict[str, Any]]:
        m = _BLOB_RE.match(blob or "")
        if not m:
            return None
        yyyy, mm, dd, hour = m.groups()
        if hour == "*":
            start = datetime(int(yyyy), int(mm), int(dd), tzinfo=timezone.utc)
            end = start + timedelta(days=1)
        else:
            start = datetime(int(yyyy), int(mm), int(dd), int(hour), tzinfo=timezone.utc)
            end = start + timedelta(hours=1)
        return {
            "range": {
                self.time_field: {
                    "gte": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "lt": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            }
        }

    def _sys_comp_filters(self, sys_name: str, comp_name: str) -> List[Dict[str, Any]]:
        filters: List[Dict[str, Any]] = []
        if sys_name and sys_name != "*":
            if "*" in sys_name or "?" in sys_name:
                filters.append({"wildcard": {self.sys_field: sys_name}})
            else:
                filters.append({"term": {self.sys_field: sys_name}})
        if comp_name and comp_name != "*":
            if "*" in comp_name or "?" in comp_name:
                filters.append({"wildcard": {self.comp_field: comp_name}})
            else:
                filters.append({"term": {self.comp_field: comp_name}})
        return filters

    def _search(self, index: str, body: Dict[str, Any]) -> BackendResult:
        try:
            client = self._get_client()
            body = dict(body)
            body.setdefault("size", min(self.max_lines, 10000))
            resp = client.search(index=index, body=body)
        except ImportError as exc:
            return BackendResult(stdout="", stderr=f"[opensearch] {exc}\n", rc=2)
        except Exception as exc:  # network, auth, mapping errors
            return BackendResult(
                stdout="",
                stderr=f"[opensearch] search failed: {exc}\n",
                rc=2,
            )

        hits = resp.get("hits", {}).get("hits", [])
        lines: List[str] = []
        truncated = False
        for h in hits:
            if len(lines) >= self.max_lines:
                truncated = True
                break
            src = h.get("_source", {})
            msg = src.get(self.message_field)
            if msg is None:
                msg = json.dumps(src, ensure_ascii=False, default=str)
            elif not isinstance(msg, str):
                msg = json.dumps(msg, ensure_ascii=False, default=str)
            lines.append(msg if msg.endswith("\n") else msg + "\n")

        stdout = "".join(lines)
        total = resp.get("hits", {}).get("total", 0)
        if isinstance(total, dict):
            total = total.get("value", 0)
        rc = 0 if lines else 1
        warning = None
        if truncated or (isinstance(total, int) and total > len(lines)):
            warning = f"[opensearch] truncated to {len(lines)} lines (total≈{total})"
        return BackendResult(stdout=stdout, stderr="", rc=rc, truncated=truncated, warning=warning)

    # ------------------------------------------------------------------ API

    def list(self, sys_glob: str, comp_glob: str = "*") -> BackendResult:
        """List distinct system/component pairs via a terms aggregation."""
        index = self._resolve_index(sys_glob.replace("*", "*"), comp_glob.replace("*", "*"))
        # Broad index when globs are present
        if "*" in sys_glob or "*" in comp_glob:
            index = self.index_pattern.replace("{sys}", "*").replace("{comp}", "*")
            for token in ("{date}", "{yyyy}", "{mm}", "{dd}", "{hour}"):
                index = index.replace(token, "*")

        body = {
            "size": 0,
            "aggs": {
                "systems": {
                    "terms": {"field": self.sys_field, "size": 500, "include": _glob_to_regex(sys_glob)},
                    "aggs": {
                        "components": {
                            "terms": {
                                "field": self.comp_field,
                                "size": 500,
                                "include": _glob_to_regex(comp_glob),
                            }
                        }
                    },
                }
            },
        }
        # Simpler path: if include regex is too strict for the cluster, drop it
        try:
            client = self._get_client()
            resp = client.search(index=index, body=body)
        except Exception:
            body["aggs"]["systems"]["terms"].pop("include", None)
            body["aggs"]["systems"]["aggs"]["components"]["terms"].pop("include", None)
            try:
                client = self._get_client()
                resp = client.search(index=index, body=body)
            except Exception as exc:
                return BackendResult(stdout="", stderr=f"[opensearch] list failed: {exc}\n", rc=2)

        lines: List[str] = []
        for bucket in resp.get("aggregations", {}).get("systems", {}).get("buckets", []):
            sys_name = bucket.get("key", "")
            if not _fnmatch_simple(sys_name, sys_glob):
                continue
            for cb in bucket.get("components", {}).get("buckets", []):
                comp_name = cb.get("key", "")
                if not _fnmatch_simple(comp_name, comp_glob):
                    continue
                lines.append(f"{sys_name}/{comp_name}\n")
        lines.sort()
        return BackendResult(stdout="".join(lines), stderr="", rc=0 if lines else 1)

    def grep(self, app: str, blob: str, pattern: str) -> BackendResult:
        """Literal / query_string search over the message field."""
        sys_name, comp_name = split_app(app)

        if not pattern:
            return BackendResult(
                stdout="",
                stderr=f"[fetch] REFUSED: empty pattern ({app} {blob}) - a required input is blank\n",
                rc=2,
                refused=True,
            )
        if _WEAK_NUMERIC_RE.match(pattern):
            return BackendResult(
                stdout="",
                stderr=(
                    f"[fetch] REFUSED: weak numeric pattern '{pattern}' (<=5 digits) "
                    f"at {app} {blob} - matches timestamps/ids everywhere; use a field-anchored pattern\n"
                ),
                rc=2,
                refused=True,
            )
        if sys_name == "pms-hub" and comp_name == "*" and not _HOUR_BOUNDED_BLOB_RE.match(blob or ""):
            return BackendResult(
                stdout="",
                stderr=f"[fetch] REFUSED: pms-hub '*' full-day ({app} {blob}) - hour-bounded only\n",
                rc=2,
                refused=True,
            )

        warning: Optional[str] = None
        if _LOW_SELECTIVITY_NUMERIC_RE.match(pattern):
            warning = (
                f"[fetch] LOW-SELECTIVITY warn: bare numeric '{pattern}' at {app} {blob} "
                "- if output floods, re-run field-anchored"
            )

        index = self._resolve_index(sys_name, comp_name, blob)
        must: List[Dict[str, Any]] = list(self._sys_comp_filters(sys_name, comp_name))
        tr = self._time_range(blob)
        if tr:
            must.append(tr)
        must.append(
            {
                "query_string": {
                    "query": _escape_query_string(pattern),
                    "default_field": self.message_field,
                    "default_operator": "AND",
                    "analyze_wildcard": True,
                }
            }
        )
        body = {
            "query": {"bool": {"filter": must}},
            "sort": [{self.time_field: "asc"}],
            "_source": [self.message_field],
        }
        result = self._search(index, body)
        if warning and not result.warning:
            result.warning = warning
        return result

    def query(self, app: str, blob: str, expr: str) -> BackendResult:
        """Structured expression → OpenSearch query_string on the whole document.

        The existing s3search expression language is treated as a query_string
        so simple ``field = "value"`` / ``field LIKE "%x%"`` forms continue to
        work when users adapt them to Lucene syntax, and raw Lucene is accepted.
        """
        sys_name, comp_name = split_app(app)
        if not expr:
            return BackendResult(
                stdout="",
                stderr=f"[fetch] REFUSED: empty query ({app} {blob})\n",
                rc=2,
                refused=True,
            )

        index = self._resolve_index(sys_name, comp_name, blob)
        must: List[Dict[str, Any]] = list(self._sys_comp_filters(sys_name, comp_name))
        tr = self._time_range(blob)
        if tr:
            must.append(tr)
        # Translate a few common s3search-style forms into Lucene
        lucene = _expr_to_lucene(expr)
        must.append({"query_string": {"query": lucene, "default_operator": "AND"}})
        body = {
            "query": {"bool": {"filter": must}},
            "sort": [{self.time_field: "asc"}],
            "_source": [self.message_field],
        }
        return self._search(index, body)

    def legacy_find(self, pattern: str, bucket_glob: str, day: str) -> BackendResult:
        """Legacy archive search is not applicable to OpenSearch."""
        return BackendResult(
            stdout="",
            stderr="[lgcy] OpenSearch backend does not support legacy_find; use grep/query\n",
            rc=2,
        )


# ------------------------------------------------------------------ utilities

def _escape_query_string(literal: str) -> str:
    """Escape Lucene special characters for a literal match."""
    specials = r'+-=&|><!(){}[]^"~*?:\/'
    out = []
    for ch in literal:
        if ch in specials or ch == "\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _expr_to_lucene(expr: str) -> str:
    """Best-effort translation of simple field expressions to Lucene.

    Supports:
      field = "value"
      field LIKE "%value%"
      AND / OR between clauses
    Anything else is passed through as-is (raw Lucene / query_string).
    """
    s = expr.strip()
    # field = "value"  or  field = value
    s = re.sub(
        r'''(\w+(?:\.\w+)*)\s*=\s*"([^"]*)"''',
        lambda m: f'{m.group(1)}:"{_escape_query_string(m.group(2))}"',
        s,
    )
    s = re.sub(
        r'''(\w+(?:\.\w+)*)\s*=\s*'([^']*)\'''',
        lambda m: f'{m.group(1)}:"{_escape_query_string(m.group(2))}"',
        s,
    )
    # field LIKE "%x%" → field:*x*
    def _like(m: re.Match) -> str:
        field, val = m.group(1), m.group(2)
        val = val.replace("%", "*")
        return f"{field}:{val}"

    s = re.sub(r'''(\w+(?:\.\w+)*)\s+LIKE\s+"([^"]*)"''', _like, s, flags=re.IGNORECASE)
    s = re.sub(r'''(\w+(?:\.\w+)*)\s+LIKE\s+'([^']*)\'''', _like, s, flags=re.IGNORECASE)
    return s


def _glob_to_regex(glob: str) -> Optional[str]:
    if not glob or glob == "*":
        return None
    # Very small glob → regex for terms include
    esc = re.escape(glob).replace(r"\*", ".*").replace(r"\?", ".")
    return f"{esc}"


def _fnmatch_simple(name: str, pattern: str) -> bool:
    if not pattern or pattern == "*":
        return True
    rx = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(rx, name) is not None
