"""Full CLI implementation for pms-trace (split for transport)."""
import pathlib
_d = pathlib.Path(__file__).parent
for _name in ("_cli_main_body_a.py", "_cli_main_body_b.py"):
    _p = _d / _name
    exec(compile(_p.read_text(encoding="utf-8"), str(_p), "exec"), globals())
