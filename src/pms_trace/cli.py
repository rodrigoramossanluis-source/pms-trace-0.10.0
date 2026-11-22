"""Command-line interface for pms-trace."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__

GOAL_SPECS: Dict[str, Tuple[List[str], List[str]]] = {
    "overbooking": (
        ["ari"],
        ["PT_CHANNEL", "PT_AT", "PT_FROM", "PT_TO", "PT_PROPERTY_ID", "PT_HOTEL_UUID", "PT_STAY_DATES"],
    ),
    "missing-reservation": (
        ["trace"],
        ["PT_REF", "PT_AT", "PT_FROM", "PT_TO", "PT_CHANNEL"],
    ),
    "rate-mismatch": (
        ["ari"],
        ["PT_CHANNEL", "PT_AT", "PT_FROM", "PT_TO", "PT_TARGET_VALUES", "PT_STAY_DATES", "PT_HOTEL_UUID"],
    ),
    "delivery-failure": (
        ["trace", "fidelity"],
        ["PT_REF", "PT_AT", "PT_FROM", "PT_TO"],
    ),
    "billing-trace": (
        ["ari"],
        ["PT_PROPERTY_ID", "PT_BILLING_FROM", "PT_BILLING_TO"],
    ),
    "bridge-flush": (
        ["bridgeflush"],
        ["PT_PROPERTY_ID", "PT_CLOUDPMS_PROPERTY_ID", "PT_AT", "PT_FROM", "PT_TO", "PT_REGION"],
    ),
}

VALID_TASKS = ("trace", "fidelity", "kb-test", "ari", "guiattrib", "bridgeflush")


def find_backends_dir() -> Optional[Path]:
    """Locate the backends directory (repo root or installed share data)."""
    candidates = [
        Path(__file__).resolve().parents[2] / "backends",
        Path(sys.prefix) / "share" / "pms_trace" / "backends",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pms-trace",
        description="Solver-driven PMS<->channel reservation investigator",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    parser.add_argument("--case", default=_env("PT_CASE", "CASE-0001-sample-trace"), help="Case ID / run directory name (env: PT_CASE)")
    parser.add_argument("--ref", default=_env("PT_REF"), help="Reservation reference (env: PT_REF)")
    parser.add_argument("--pms", default=_env("PT_PMS"), help="PMS type (env: PT_PMS)")
    parser.add_argument("--channel", default=_env("PT_CHANNEL"), help="Channel/OTA code (env: PT_CHANNEL)")
    parser.add_argument("--at", default=_env("PT_AT"), help="Booking timestamp YYYY-MM-DDTHH:MM:SS (env: PT_AT)")
    parser.add_argument("--from-date", dest="from_date", default=_env("PT_FROM"), help="Start date YYYY-MM-DD (env: PT_FROM)")
    parser.add_argument("--to-date", dest="to_date", default=_env("PT_TO"), help="End date YYYY-MM-DD (env: PT_TO)")

    parser.add_argument(
        "--goal",
        default=_env("PT_GOAL"),
        choices=[*GOAL_SPECS.keys(), "custom", ""],
        help="Goal playbook (env: PT_GOAL)",
    )
    parser.add_argument(
        "--tasks",
        default=_env("PT_TASKS", "trace"),
        help="Space-separated tasks when goal is blank/custom (env: PT_TASKS)",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        default=_env("PT_ALLOW_PARTIAL", "0") == "1",
        help="Run despite goal input-contract gaps (env: PT_ALLOW_PARTIAL=1)",
    )
    parser.add_argument(
        "--mode",
        default=_env("PT_MODE", "run"),
        choices=["run", "plan"],
        help="Execution mode (env: PT_MODE)",
    )
    parser.add_argument(
        "--runs-dir",
        default=_env("PT_RUNS_DIR", str(Path.home() / "pms_tools" / "runs")),
        help="Base directory for run outputs (env: PT_RUNS_DIR)",
    )
    parser.add_argument(
        "--on-exists",
        default=_env("PT_ON_EXISTS", "ask"),
        choices=["ask", "resume", "wipe", "suffix"],
        help="Action when PARTIAL run dir exists (env: PT_ON_EXISTS)",
    )
    parser.add_argument(
        "--on-finished",
        default=_env("PT_ON_FINISHED", "ask"),
        choices=["ask", "wipe", "suffix"],
        help="Action when FINISHED run dir exists (env: PT_ON_FINISHED)",
    )

    # Additional PT_* anchors used by goal contracts
    parser.add_argument("--property-id", default=_env("PT_PROPERTY_ID"), help="Property / hotelier ID (env: PT_PROPERTY_ID)")
    parser.add_argument("--hotel-uuid", default=_env("PT_HOTEL_UUID"), help="CM hotel UUID (env: PT_HOTEL_UUID)")
    parser.add_argument("--stay-dates", default=_env("PT_STAY_DATES"), help="Stay dates (env: PT_STAY_DATES)")
    parser.add_argument("--target-values", default=_env("PT_TARGET_VALUES"), help="Target values for ari (env: PT_TARGET_VALUES)")
    parser.add_argument("--billing-from", default=_env("PT_BILLING_FROM"), help="Billing start date (env: PT_BILLING_FROM)")
    parser.add_argument("--billing-to", default=_env("PT_BILLING_TO"), help="Billing end date (env: PT_BILLING_TO)")
    parser.add_argument("--cloudpms-property-id", default=_env("PT_CLOUDPMS_PROPERTY_ID"), help="CloudPMS numeric property ID (env: PT_CLOUDPMS_PROPERTY_ID)")
    parser.add_argument("--region", default=_env("PT_REGION", "both"), help="Regional selector ra|rb|both (env: PT_REGION)")
    parser.add_argument("--booking-ref", default=_env("PT_BOOKING_REF"), help="Related booking ref (env: PT_BOOKING_REF)")

    return parser


def resolve_goal_and_tasks(args: argparse.Namespace) -> List[str]:
    """Resolve PT_GOAL into tasks and enforce input contracts."""
    goal = (args.goal or "").strip()
    if not goal or goal == "custom":
        tasks = [t for t in (args.tasks or "").split() if t]
        return tasks

    tasks, required_vars = GOAL_SPECS[goal]
    env_map = {
        "PT_CHANNEL": args.channel,
        "PT_AT": args.at,
        "PT_FROM": args.from_date,
        "PT_TO": args.to_date,
        "PT_PROPERTY_ID": args.property_id,
        "PT_HOTEL_UUID": args.hotel_uuid,
        "PT_STAY_DATES": args.stay_dates,
        "PT_REF": args.ref,
        "PT_TARGET_VALUES": args.target_values,
        "PT_BILLING_FROM": args.billing_from,
        "PT_BILLING_TO": args.billing_to,
        "PT_CLOUDPMS_PROPERTY_ID": args.cloudpms_property_id,
        "PT_REGION": args.region,
    }
    missing = [v for v in required_vars if not env_map.get(v)]
    if goal == "overbooking" and not args.ref and not args.booking_ref:
        missing.append("PT_REF|PT_BOOKING_REF")

    if missing:
        print(f"[goal] PREFLIGHT INPUT CONTRACT - MISSING required anchors: {' '.join(missing)}", file=sys.stderr)
        if not args.allow_partial:
            print(
                "[goal] REFUSING to run with gaps. Fill the blanks or pass --allow-partial (PT_ALLOW_PARTIAL=1).",
                file=sys.stderr,
            )
            raise SystemExit(2)
    else:
        print(f"[goal] {goal}: all required anchors present")
    return list(tasks)


def prepare_run_dir(args: argparse.Namespace) -> Tuple[str, Path]:
    """Handle existing run directory lifecycle (resume / wipe / suffix)."""
    base = Path(args.runs_dir)
    case = args.case
    run_dir = base / case

    if run_dir.exists():
        finished = (run_dir / "verdict.json").is_file()
        state_dir = run_dir / "state"
        partial = state_dir.is_dir() and any(state_dir.glob("*.done"))
        if finished or partial:
            mode = args.on_finished if finished else args.on_exists
            default = "suffix" if finished else "resume"
            if mode == "ask":
                mode = default

            if mode == "wipe":
                print(f"[run] wiping {run_dir}")
                shutil.rmtree(run_dir)
            elif mode == "suffix":
                i = 2
                while (base / f"{case}-v{i}").exists():
                    i += 1
                case = f"{case}-v{i}"
                run_dir = base / case
                print(f"[run] isolating results -> {run_dir}")
            elif mode == "resume":
                print(f"[run] resuming {run_dir}")

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state").mkdir(exist_ok=True)
    (run_dir / "raw").mkdir(exist_ok=True)
    return case, run_dir


def _build_runtime(case: str, run_dir: Path, args: argparse.Namespace):
    from .backend import StageRunner, create_backend
    from .kb import load_kb

    backends_dir = find_backends_dir()
    backend = create_backend(backends_dir=backends_dir)
    skip_legs = [s for s in os.environ.get("PT_SKIP_LEGS", "").split() if s]
    runner = StageRunner(
        run_dir,
        resume=os.environ.get("PT_RESUME", "1") == "1",
        plan=(args.mode == "plan"),
        skip_legs=skip_legs,
    )
    kb_dir = Path(os.environ.get("PT_KB_DIR", str(Path.home() / "pms_tools" / "kb")))
    kb_file = os.environ.get("PT_KB_FILE", "connector_kb.json")
    kb_path = kb_dir / kb_file
    kb_data = load_kb(kb_dir, kb_file)
    return backend, runner, kb_path, kb_data



def _run_task(task: str, case: str, run_dir: Path, args: argparse.Namespace) -> int:
    if task not in VALID_TASKS:
        print(f"[tasks] unknown task '{task}' ({'|'.join(VALID_TASKS)})", file=sys.stderr)
        return 0

    backend, runner, kb_path, kb_data = _build_runtime(case, run_dir, args)
    env = dict(os.environ)

    if task == "trace":
        from .trace import TraceContext, pt_trace

        ctx = TraceContext(
            ref=args.ref,
            run_dir=run_dir,
            kb_path=kb_path,
            kb_data=kb_data,
            backend=backend,
            runner=runner,
            case=case,
            pms=args.pms,
            channel=args.channel,
            at=args.at,
            at_uncertain=os.environ.get("PT_AT_UNCERTAIN", "1") == "1",
            tz_offset=os.environ.get("PT_TZ_OFFSET", "-6"),
            from_date=args.from_date,
            to_date=args.to_date,
            fanout_hours=int(os.environ.get("PT_FANOUT_HOURS", "3")),
            no_preflight=os.environ.get("PT_NO_PREFLIGHT", "0") == "1",
            sys_sweep=os.environ.get("PT_SYS_SWEEP", "auto"),
            fullday_fallback=os.environ.get("PT_FULLDAY_FALLBACK", "1") == "1",
            lad_fd_zero=os.environ.get("PT_LAD_FD_ZERO", "0") == "1",
            follow_enabled=os.environ.get("PT_FOLLOW", "1") == "1",
            max_follow_keys=int(os.environ.get("PT_MAX_FOLLOW_KEYS", "12")),
            grep_fallback=os.environ.get("PT_GREP_FALLBACK", "1") == "1",
            hotel_uuid=os.environ.get("PT_HOTEL_UUID", ""),
            hotelier_id=os.environ.get("PT_HOTELIER_ID", ""),
            propkey=os.environ.get("PT_PROPKEY", ""),
            membership=os.environ.get("PT_MEMBERSHIP", ""),
            listing_id=os.environ.get("PT_LISTING_ID", ""),
            guest=os.environ.get("PT_GUEST", ""),
        )
        return pt_trace(ctx)

    if task == "fidelity":
        from .fidelity import run_fidelity_with_fetch

        run_fidelity_with_fetch(run_dir, kb_data, env, backend, runner)
        return 0

    if task == "ari":
        from .tasks.ari import pt_ari

        pt_ari(run_dir, env, kb_data, backend=backend, runner=runner, kb_path=kb_path)
        return 0

    if task == "guiattrib":
        from .tasks.guiattrib import pt_guiattrib

        pt_guiattrib(run_dir, env, kb_data, backend=backend, runner=runner)
        return 0

    if task == "kb-test":
        from .tasks.kb_test import pt_kb_test

        pt_kb_test(run_dir, kb_data, backend=backend, runner=runner)
        return 0

    if task == "bridgeflush":
        from .tasks.bridgeflush import pt_bridgeflush

        pt_bridgeflush(run_dir, env, kb_data, backend=backend, runner=runner)
        return 0

    return 0


TASK_HANDLERS: Dict[str, Callable[[str, str, Path, argparse.Namespace], int]] = {
    t: _run_task for t in VALID_TASKS
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    tasks = resolve_goal_and_tasks(args)
    case, run_dir = prepare_run_dir(args)

    print(f"== pms-trace v{__version__} - case={case} tasks='{' '.join(tasks)}' mode={args.mode} goal='{args.goal or 'custom'}' ==")
    rc = 0
    for task in tasks:
        handler = TASK_HANDLERS.get(task, _run_task)
        task_rc = handler(task, case, run_dir, args)
        if task_rc != 0:
            rc = task_rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
