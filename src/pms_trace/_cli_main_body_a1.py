"""Command-line interface for pms-trace."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .modes import DEFAULT_REGISTRY

# Goal → (tasks, required anchors) derived from the mode registry (single source).
GOAL_SPECS: Dict[str, Tuple[List[str], List[str]]] = DEFAULT_REGISTRY.goal_specs()

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


