from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from reagent_memory import ReagentMemory  # noqa: E402


class CleanupReagentMemoryConflictsScriptTest(unittest.TestCase):
    def test_script_deletes_conflicts_after_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings({}, root)
            memory.add_record(raw_name="conflict", final_category="普通类", confidence=0.9)
            conflict_row = memory.list_records(query="conflict")[0]
            memory.update_record(conflict_row["id"], {"conflict": True, "manual_verified": True})
            memory.add_record(raw_name="normal", final_category="普通类", confidence=0.9)

            script = ROOT_DIR / "scripts" / "cleanup_reagent_memory_conflicts.py"
            command = [sys.executable, str(script), "--yes", "--root", str(root)]
            result = subprocess.run(command, cwd=ROOT_DIR, text=True, capture_output=True, timeout=20)

            self.assertEqual(result.returncode, 0, result.stderr)
            cleaned = ReagentMemory.from_settings({}, root)
            self.assertEqual(cleaned.count_conflicting_records(), 0)
            self.assertIsNone(cleaned.find_any(raw_name="conflict"))
            self.assertIsNotNone(cleaned.find_any(raw_name="normal"))
            backups = list((root / "data" / "logs").glob("reagent_memory_backup_before_delete_conflicting_*.sqlite"))
            self.assertEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main()
