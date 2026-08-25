from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from write_failure_diagnostics import build_write_failure_debug_payload  # noqa: E402


class WriteFailureDiagnosticsTest(unittest.TestCase):
    def test_build_payload_includes_dropdown_state(self) -> None:
        payload = build_write_failure_debug_payload(
            sequence="36",
            attempt=1,
            reason="could not select 未知类",
            category="未知类",
            failure_stage="bound_scroll_scan",
            dropdown_state_loader=lambda: {"seenOptions": ["普通类", "未知类"]},
        )

        self.assertEqual(payload["sequence"], "36")
        self.assertEqual(payload["target_category"], "未知类")
        self.assertEqual(payload["failure_stage"], "bound_scroll_scan")
        self.assertEqual(payload["dropdown"]["seenOptions"], ["普通类", "未知类"])

    def test_dropdown_diagnostic_error_does_not_raise(self) -> None:
        def broken_loader() -> dict[str, object]:
            raise RuntimeError("page closed")

        payload = build_write_failure_debug_payload(
            sequence="1",
            attempt=2,
            reason="failed",
            category="易燃类",
            dropdown_state_loader=broken_loader,
        )

        self.assertEqual(payload["dropdown"]["diagnostic_error"], "page closed")


if __name__ == "__main__":
    unittest.main()
