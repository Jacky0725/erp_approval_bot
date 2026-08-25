from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from approval_batch_state import MultiPageWriteState, normalize_write_result  # noqa: E402


def key(item: dict[str, object]) -> str:
    return str(item["id"])


class ApprovalBatchStateTest(unittest.TestCase):
    def test_normalize_write_result_defaults_legacy_none_to_all_handled(self) -> None:
        suggestions = [{"id": "1"}, {"id": "2"}]

        result = normalize_write_result(None, suggestions, key)

        self.assertEqual(result["attempted"], set())
        self.assertEqual(result["handled"], {"1", "2"})
        self.assertEqual(result["failed"], set())
        self.assertEqual(result["deferred"], set())

    def test_pending_candidates_survive_deferred_batches(self) -> None:
        state = MultiPageWriteState(max_attempts=2)
        suggestions = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        state.register_writable_suggestions(suggestions, key)

        delta = state.apply_write_result(
            {
                "attempted": {"1"},
                "handled": {"1"},
                "failed": set(),
                "deferred": {"2", "3"},
            }
        )

        self.assertEqual(delta["saved_or_terminal"], {"1"})
        self.assertEqual(delta["deferred"], {"2", "3"})
        self.assertNotIn("1", state.pending_write_suggestions)
        self.assertEqual(set(state.pending_write_suggestions), {"2", "3"})

    def test_failed_key_becomes_terminal_after_retry_limit(self) -> None:
        state = MultiPageWriteState(max_attempts=2)
        state.register_writable_suggestions([{"id": "9"}], key)

        first = state.apply_write_result({"attempted": {"9"}, "handled": {"9"}, "failed": {"9"}})
        second = state.apply_write_result({"attempted": {"9"}, "handled": {"9"}, "failed": {"9"}})

        self.assertEqual(first["retry_limited"], set())
        self.assertEqual(second["retry_limited"], {"9"})
        self.assertIn("9", state.handled_keys)
        self.assertNotIn("9", state.pending_write_suggestions)


if __name__ == "__main__":
    unittest.main()
