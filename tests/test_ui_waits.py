from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from ui_waits import wait_until_row_value, wait_until_table_stable  # noqa: E402


class FakePage:
    def __init__(self) -> None:
        self.pauses: list[int] = []

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.pauses.append(timeout_ms)

    def wait_for_selector(self, *args: object, **kwargs: object) -> None:
        return None


class UiWaitsTest(unittest.TestCase):
    def test_wait_until_row_value_returns_when_predicate_matches(self) -> None:
        page = FakePage()
        values = iter(["", "-", "普通类"])

        result = wait_until_row_value(
            page,
            lambda: next(values),
            lambda value: value == "普通类",
            timeout_ms=1000,
        )

        self.assertEqual(result, "普通类")
        self.assertGreaterEqual(len(page.pauses), 2)

    def test_wait_until_table_stable_requires_changed_stable_signature(self) -> None:
        page = FakePage()
        values = iter(["old", "new", "new"])

        result = wait_until_table_stable(
            page,
            lambda: next(values),
            previous_signature="old",
            timeout_ms=1000,
        )

        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
