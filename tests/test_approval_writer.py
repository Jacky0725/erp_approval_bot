from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from approval_writer import ApprovalWriter  # noqa: E402


class ApprovalWriterTest(unittest.TestCase):
    def test_strong_reaction_property_alias(self) -> None:
        writer = ApprovalWriter()

        self.assertEqual(writer.property_name_candidates("强反应性"), ["强反应性", "强反应"])

    def test_configured_property_aliases_are_used(self) -> None:
        writer = ApprovalWriter(
            settings={
                "reagent": {
                    "physicochemical_property_aliases": {
                        "易燃液体": ["易燃类"],
                    }
                }
            }
        )

        self.assertEqual(writer.property_name_candidates("易燃液体"), ["易燃液体", "易燃类"])


if __name__ == "__main__":
    unittest.main()
