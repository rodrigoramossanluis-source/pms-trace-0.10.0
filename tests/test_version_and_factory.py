"""Version single-source and backend factory tests."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from pms_trace import __version__
from pms_trace.backend import Backend, create_backend


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKENDS_DIR = REPO_ROOT / "backends"


class VersionConsistencyTests(unittest.TestCase):
    def test_version_is_semver_string(self) -> None:
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+")

    def test_cli_imports_same_version(self) -> None:
        from pms_trace.cli import __version__ as cli_version

        self.assertEqual(cli_version, __version__)

    def test_trace_uses_package_version(self) -> None:
        import pms_trace.trace as trace_mod

        self.assertFalse(hasattr(trace_mod, "PT_VERSION"), "PT_VERSION must not be re-declared")
        self.assertEqual(trace_mod.__version__, __version__)

    def test_ari_imports_same_version(self) -> None:
        from pms_trace.tasks.ari import __version__ as ari_version

        self.assertEqual(ari_version, __version__)


class BackendFactoryTests(unittest.TestCase):
    def test_default_is_s3search_backend(self) -> None:
        env = {"PT_BACKEND": "auto"}
        # Ensure OS_URL is not set
        env.pop("OS_URL", None)
        with mock.patch.dict(os.environ, env, clear=False):
            # Remove OS_URL if present in real env
            cleaned = {k: v for k, v in os.environ.items() if k != "OS_URL"}
            cleaned["PT_BACKEND"] = "auto"
            with mock.patch.dict(os.environ, cleaned, clear=True):
                b = create_backend(backends_dir=BACKENDS_DIR, env=cleaned)
        self.assertIsInstance(b, Backend)

    def test_explicit_s3search(self) -> None:
        env = {"PT_BACKEND": "s3search"}
        b = create_backend(backends_dir=BACKENDS_DIR, env=env)
        self.assertIsInstance(b, Backend)

    def test_opensearch_requires_url(self) -> None:
        env = {"PT_BACKEND": "opensearch"}
        with self.assertRaises(ValueError):
            create_backend(backends_dir=BACKENDS_DIR, env=env)

    def test_auto_selects_opensearch_when_url_set(self) -> None:
        env = {
            "PT_BACKEND": "auto",
            "OS_URL": "https://opensearch.example:9200",
        }
        # Import happens inside; if opensearch-py missing, constructor still succeeds
        # until first search (lazy client). ValueError only for missing URL.
        from pms_trace.opensearch_backend import OpenSearchBackend

        b = create_backend(backends_dir=BACKENDS_DIR, env=env)
        self.assertIsInstance(b, OpenSearchBackend)
        self.assertEqual(b.url, "https://opensearch.example:9200")

    def test_opensearch_safety_refusals(self) -> None:
        from pms_trace.opensearch_backend import OpenSearchBackend

        b = OpenSearchBackend(url="https://opensearch.example:9200", env={"OS_URL": "https://opensearch.example:9200"})
        r = b.grep("cm/booking", "2026-01-10-12", "")
        self.assertTrue(r.refused)
        self.assertEqual(r.rc, 2)

        r2 = b.grep("cm/booking", "2026-01-10-12", "12345")  # weak numeric
        self.assertTrue(r2.refused)

        r3 = b.query("cm/booking", "2026-01-10-12", "")
        self.assertTrue(r3.refused)


if __name__ == "__main__":
    unittest.main()
