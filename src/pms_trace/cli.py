"""Command-line interface for pms-trace."""
from ._cli_main import *  # noqa: F401,F403
from ._cli_main import main

if __name__ == "__main__":
    raise SystemExit(main())
