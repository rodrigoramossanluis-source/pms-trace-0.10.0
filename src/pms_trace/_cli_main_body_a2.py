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
    """Resolve PT_GOAL / mode into tasks and enforce input contracts."""
    goal = (args.goal or "").strip()
    if not goal or goal == "custom":
        tasks = [t for t in (args.tasks or "").split() if t]
        return tasks

    if goal not in GOAL_SPECS:
        known = ", ".join(sorted(GOAL_SPECS)) or "(none)"
        print(f"[goal] unknown mode/goal '{goal}'; known: {known}", file=sys.stderr)
        raise SystemExit(2)

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
