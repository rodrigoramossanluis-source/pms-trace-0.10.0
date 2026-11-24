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
