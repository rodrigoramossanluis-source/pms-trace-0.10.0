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


def _anchor_presence(args: argparse.Namespace) -> Dict[str, bool]:
    """Map common PT_* anchors to whether the CLI/env provided a value."""
    mapping = {
        "PT_REF": args.ref,
        "PT_CHANNEL": args.channel,
        "PT_PMS": args.pms,
        "PT_AT": args.at,
        "PT_FROM": args.from_date,
        "PT_TO": args.to_date,
        "PT_PROPERTY_ID": getattr(args, "property_id", "") or "",
        "PT_HOTEL_UUID": getattr(args, "hotel_uuid", "") or "",
        "PT_STAY_DATES": getattr(args, "stay_dates", "") or "",
        "PT_TARGET_VALUES": getattr(args, "target_values", "") or "",
        "PT_BILLING_FROM": getattr(args, "billing_from", "") or "",
        "PT_BILLING_TO": getattr(args, "billing_to", "") or "",
        "PT_CLOUDPMS_PROPERTY_ID": getattr(args, "cloudpms_property_id", "") or "",
        "PT_REGION": getattr(args, "region", "") or "",
        "PT_BOOKING_REF": getattr(args, "booking_ref", "") or "",
    }
    return {k: bool(v) for k, v in mapping.items()}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    tasks = resolve_goal_and_tasks(args)
    case, run_dir = prepare_run_dir(args)

    goal = (args.goal or "").strip()
    mode_spec = None
    if goal and goal != "custom":
        mode_spec = DEFAULT_REGISTRY.get(goal)
    elif len(tasks) == 1:
        mode_spec = DEFAULT_REGISTRY.get(tasks[0])

    if mode_spec is not None:
        from .modes.runner import append_mode_section_to_report, record_mode_start, write_mode_plan

        write_mode_plan(
            run_dir,
            mode_spec,
            anchors_present=_anchor_presence(args),
        )
        record_mode_start(run_dir, mode_spec)
        append_mode_section_to_report(run_dir, mode_spec)

    print(
        f"== pms-trace v{__version__} - case={case} tasks='{' '.join(tasks)}' "
        f"mode={args.mode} goal='{args.goal or 'custom'}'"
        + (f" mode_id={mode_spec.mode_id}" if mode_spec else "")
        + " =="
    )
    rc = 0
    for task in tasks:
        handler = TASK_HANDLERS.get(task, _run_task)
        task_rc = handler(task, case, run_dir, args)
        if task_rc != 0:
            rc = task_rc

    if mode_spec is not None and mode_spec.mode_id in ("ari-discrepancy", "rate-mismatch"):
        from .modes.discrepancy import run_discrepancy_observation

        assessment = run_discrepancy_observation(
            run_dir,
            mode_spec,
            anchors_present=_anchor_presence(args),
        )
        n_ev = sum(1 for s in assessment.get("stages", []) if s.get("status") == "evidence")
        print(
            f"[mode] discrepancy observation: {len(assessment.get('stages', []))} stages, "
            f"{n_ev} with evidence artifacts"
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
