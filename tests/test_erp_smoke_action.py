from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

import automation_worker  # noqa: E402


class ErpSmokeActionTest(unittest.TestCase):
    def test_erp_smoke_reuses_read_only_todo_export_path(self) -> None:
        bot = MagicMock()
        with patch("automation_worker.load_settings", return_value={}), patch(
            "automation_worker.BrowserBot",
            return_value=bot,
        ):
            automation_worker.run_action("erp_smoke")

        bot.run_todo_tasks_export.assert_called_once()
        bot.run_semi_auto_approval_suggestions.assert_not_called()


if __name__ == "__main__":
    unittest.main()
