from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from erp_session import ErpSessionMixin  # noqa: E402


class ErpSessionRuntimeTest(unittest.TestCase):
    def test_packaged_headless_only_forces_headless_browser(self) -> None:
        with patch.dict(os.environ, {"REAGENT_APPROVAL_HEADLESS_ONLY": "true"}, clear=False):
            self.assertTrue(ErpSessionMixin.effective_browser_headless({"headless": False}))

    def test_source_runtime_respects_configured_headed_browser(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ErpSessionMixin.effective_browser_headless({"headless": False}))

    def test_configured_headless_stays_headless(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(ErpSessionMixin.effective_browser_headless({"headless": True}))


if __name__ == "__main__":
    unittest.main()
