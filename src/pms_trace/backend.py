# SPDX-License-Identifier: MIT
"""Backend and stage-execution utilities for pms-trace.

Provides:
- :class:`Backend`: Safe, shell-free subprocess wrapper around ``s3search.py``
  and ``s3grep.py`` with timeouts, bounded output, and fetch safety guards.
- :class:`StageRunner`: Resumable stage executor with skip-leg filtering,
  plan mode, sanitized stage names, ``.out``/``.stderr``/``.rc`` artifacts,
  ``ledger.tsv`` tracking, and refusal-safe completion markers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Sequence, Union

_WEAK_NUMERIC_RE = re.compile(r"^[0-9]{1,5}$")
_LOW_SELECTIVITY_NUMERIC_RE = re.compile(r"^[0-9]{6,7}$")
_HOUR_BOUNDED_BLOB_RE = re.compile(r".*-[0-9]{2}$")
_SAFE_NAME_STRIP_RE = re.compile(r"[^A-Za-z0-9_.-]")

CommandSpec = Union[str, Path, Sequence[str]]


@dataclass
class BackendResult:
    """Result of a backend invocation or pre-flight safety refusal."""

    stdout: str
    stderr: str
    rc: int
    truncated: bool = False
    refused: bool = False
    warning: Optional[str] = None


@dataclass
class StageResult:
    """Outcome of a single :meth:`StageRunner.run_stage` call."""

    name: str
    label: str
    status: str  # "done", "skipped_leg", "skipped_done", "planned", "refused", "timeout_retry", "error"
    rc: int
    lines: int
    out_path: Optional[Path] = None
    stderr_path: Optional[Path] = None
    rc_path: Optional[Path] = None


def split_app(app: str) -> tuple[str, str]:
    """Split ``<sys>/<comp>`` into ``(sys, comp)``, defaulting ``comp`` to ``*``."""
    if "/" in app:
        sys_name, comp_name = app.split("/", 1)
        return sys_name, comp_name or "*"
    return app, "*"


def sanitize_stage_name(name: str) -> str:
    """Sanitize a stage name for safe use as a filename (mirrors ``pt_sanitize``)."""
    replaced = name.replace("/", "_").replace(" ", "_").replace("*", "_")
    cleaned = _SAFE_NAME_STRIP_RE.sub("", replaced)
    if not cleaned or cleaned in (".", ".."):
        return "stage"
    return cleaned


def _normalize_cmd(cmd: CommandSpec, python_executable: str) -> List[str]:
    if isinstance(cmd, (str, Path)):
        cmd_str = str(cmd)
        if cmd_str.endswith(".py"):
            return [python_executable, cmd_str]
        return [cmd_str]
    return [str(part) for part in cmd]


class Backend:
    """Subprocess-based log-search backend wrapper without shell invocation.

    Parameters
    ----------
    s3search_cmd:
        Path or argument list for ``s3search.py``.
    s3grep_cmd:
        Path or argument list for ``s3grep.py`` (optional; required for
        :meth:`legacy_find`).
    timeout:
        Subprocess timeout in seconds (default ``120.0``).
    max_lines:
        Maximum number of stdout lines retained per call (default ``200000``).
    python_executable:
        Python interpreter used when ``s3search_cmd`` or ``s3grep_cmd`` is a
        ``.py`` path (defaults to ``sys.executable``).
    env:
        Optional environment dictionary passed to subprocesses.
    """

    def __init__(
        self,
        s3search_cmd: Optional[CommandSpec] = None,
        s3grep_cmd: Optional[CommandSpec] = None,
        timeout: float = 120.0,
        max_lines: int = 200000,
        python_executable: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.python_executable = python_executable or sys.executable
        self.s3search_cmd = (
            _normalize_cmd(s3search_cmd, self.python_executable)
            if s3search_cmd is not None
            else None
        )
        self.s3grep_cmd = (
            _normalize_cmd(s3grep_cmd, self.python_executable)
            if s3grep_cmd is not None
            else None
        )
        self.timeout = float(timeout)
        self.max_lines = int(max_lines)
        self.env = dict(env) if env is not None else None

    def _run(self, args: Sequence[str], warning: Optional[str] = None) -> BackendResult:
        try:
            proc = subprocess.run(
                list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=self.timeout,
                shell=False,
                env=self.env,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            rc = proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout.decode("utf-8", "replace") if exc.stdout else "")
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr.decode("utf-8", "replace") if exc.stderr else "")
            if stderr and not stderr.endswith("\n"):
                stderr += "\n"
            stderr += f"[backend] TIMEOUT after {self.timeout}s\n"
            rc = 124
        except OSError as exc:
            return BackendResult(
                stdout="",
                stderr=f"[backend] execution failed: {exc}\n",
                rc=2,
                warning=warning,
            )

        if warning:
            stderr = f"{warning}\n{stderr}" if stderr else f"{warning}\n"

        truncated = False
        if self.max_lines >= 0:
            lines = stdout.splitlines(keepends=True)
            if len(lines) > self.max_lines:
                stdout = "".join(lines[: self.max_lines])
                truncated = True
                if stderr and not stderr.endswith("\n"):
                    stderr += "\n"
                stderr += f"[fetch] note: output capped at {self.max_lines} lines\n"

        return BackendResult(
            stdout=stdout,
            stderr=stderr,
            rc=rc,
            truncated=truncated,
            refused=False,
            warning=warning,
        )

    def list(self, sys_glob: str, comp_glob: str = "*") -> BackendResult:
        """List matching ``<sys>/<comp>`` targets via ``s3search.py ls``."""
        if not self.s3search_cmd:
            return BackendResult(stdout="", stderr="[fetch] FATAL: s3search_cmd not configured\n", rc=2)
        return self._run([*self.s3search_cmd, "ls", sys_glob, comp_glob])

    def grep(self, app: str, blob: str, pattern: str) -> BackendResult:
        """Run a literal grep via ``s3search.py grep`` with safety checks."""
        if not self.s3search_cmd:
            return BackendResult(stdout="", stderr="[fetch] FATAL: s3search_cmd not configured\n", rc=2)

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

        if sys_name == "pms-hub" and comp_name == "*" and not _HOUR_BOUNDED_BLOB_RE.match(blob):
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

        return self._run([*self.s3search_cmd, "grep", sys_name, comp_name, blob, pattern], warning=warning)

    def query(self, app: str, blob: str, expr: str) -> BackendResult:
        """Run a structured JSON-field query via ``s3search.py query``."""
        if not self.s3search_cmd:
            return BackendResult(stdout="", stderr="[fetch] FATAL: s3search_cmd not configured\n", rc=2)

        if not expr:
            return BackendResult(
                stdout="",
                stderr=f"[fetch] REFUSED: empty query ({app} {blob})\n",
                rc=2,
                refused=True,
            )

        sys_name, comp_name = split_app(app)
        return self._run([*self.s3search_cmd, "query", sys_name, comp_name, blob, expr])

    def legacy_find(self, pattern: str, bucket_glob: str, day: str) -> BackendResult:
        """Run a legacy-archive search via ``s3grep.py find``."""
        if not self.s3grep_cmd:
            return BackendResult(stdout="", stderr="[lgcy] FATAL: s3grep.py not configured\n", rc=2)

        if not pattern:
            return BackendResult(
                stdout="",
                stderr=f"[lgcy] REFUSED: empty pattern ({bucket_glob} {day})\n",
                rc=2,
                refused=True,
            )

        return self._run([*self.s3grep_cmd, "find", pattern, bucket_glob, day])


class StageRunner:
    """Resumable stage runner with skip-legs, plan mode, and ledger tracking."""

    def __init__(
        self,
        run_dir: Union[str, Path],
        resume: bool = False,
        plan: bool = False,
        skip_legs: Sequence[str] = (),
    ) -> None:
        self.run_dir = Path(run_dir)
        self.raw_dir = self.run_dir / "raw"
        self.state_dir = self.run_dir / "state"
        self.ledger_path = self.run_dir / "ledger.tsv"
        self.report_path = self.run_dir / "REPORT.md"
        self.resume = resume
        self.plan = plan
        self.skip_legs = [s for s in skip_legs if s]

        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _append_ledger(self, safe_name: str, label: str, rc: int, lines: int) -> None:
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{safe_name}|{label}|rc={rc}|lines={lines}\n")

    def run_stage(
        self,
        name: str,
        label: str,
        fn: Callable[[], BackendResult],
        plan_cmd: Optional[str] = None,
    ) -> StageResult:
        """Execute a stage, recording ``.out``, ``.stderr``, ``.rc``, and ledger."""
        safe_name = sanitize_stage_name(name)

        for skip in self.skip_legs:
            if skip in safe_name or skip in name:
                self._append_ledger(safe_name, "skipped-by-PT_SKIP_LEGS", 0, 0)
                return StageResult(
                    name=safe_name,
                    label=label,
                    status="skipped_leg",
                    rc=0,
                    lines=0,
                )

        done_marker = self.state_dir / f"{safe_name}.done"
        if self.resume and done_marker.exists():
            return StageResult(
                name=safe_name,
                label=label,
                status="skipped_done",
                rc=0,
                lines=0,
            )

        if self.plan:
            with self.report_path.open("a", encoding="utf-8") as fh:
                fh.write(f"  PLAN [{safe_name}] {plan_cmd or label}\n")
            return StageResult(
                name=safe_name,
                label=label,
                status="planned",
                rc=0,
                lines=0,
            )

        result = fn()

        out_path = self.raw_dir / f"{safe_name}.out"
        stderr_path = self.raw_dir / f"{safe_name}.stderr"
        rc_path = self.raw_dir / f"{safe_name}.rc"

        out_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        rc_path.write_text(f"{result.rc}\n", encoding="utf-8")

        lines = sum(1 for line in result.stdout.splitlines() if line)

        if result.refused:
            status = "refused"
        elif result.rc <= 1:
            done_marker.touch()
            status = "done"
        elif result.rc == 124:
            status = "timeout_retry"
        else:
            status = "error"

        self._append_ledger(safe_name, label, result.rc, lines)

        return StageResult(
            name=safe_name,
            label=label,
            status=status,
            rc=result.rc,
            lines=lines,
            out_path=out_path,
            stderr_path=stderr_path,
            rc_path=rc_path,
        )


# ------------------------------------------------------------------ factory

def create_backend(
    backends_dir: Optional[Path] = None,
    timeout: Optional[float] = None,
    max_lines: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
    python_executable: Optional[str] = None,
):
    """Build the appropriate backend from environment configuration.

    Selection order (``PT_BACKEND``):
      - ``opensearch`` — always OpenSearch (requires ``OS_URL``)
      - ``s3search`` / ``file`` / ``s3`` / ``ssh`` — file/S3/SSH via s3search.py
      - ``auto`` (default) — OpenSearch when ``OS_URL`` is set, else s3search

    Core remains zero-dependency; OpenSearch only imports ``opensearch-py``
    when that backend is actually selected.
    """
    import os
    from pathlib import Path as _Path

    e = dict(env) if env is not None else dict(os.environ)
    kind = (e.get("PT_BACKEND") or "auto").strip().lower()
    if kind == "auto":
        kind = "opensearch" if e.get("OS_URL") else "s3search"

    to = float(timeout if timeout is not None else e.get("PT_SEARCH_TIMEOUT", "1500"))
    ml = int(max_lines if max_lines is not None else e.get("PT_FETCH_MAX_LINES", "200000"))

    if kind in ("opensearch", "os", "elasticsearch", "es"):
        from .opensearch_backend import OpenSearchBackend

        return OpenSearchBackend(url=e.get("OS_URL"), timeout=to, max_lines=ml, env=e)

    # Default: file / ssh / s3 via the reference scripts
    if backends_dir is None:
        backends_dir = _Path(__file__).resolve().parents[2] / "backends"
        if not backends_dir.is_dir():
            backends_dir = _Path(sys.prefix) / "share" / "pms_trace" / "backends"

    s3search = e.get("PT_SEARCH_CMD") or str(backends_dir / "s3search.py")
    s3grep = e.get("PT_GREP_CMD") or str(backends_dir / "s3grep.py")
    return Backend(
        s3search_cmd=s3search,
        s3grep_cmd=s3grep,
        timeout=to,
        max_lines=ml,
        python_executable=python_executable,
        env=e,
    )
