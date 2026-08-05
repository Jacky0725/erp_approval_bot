from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from non_reagent_classifier import NonReagentClassifier  # noqa: E402


class NonReagentClassifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = NonReagentClassifier(root_dir=ROOT_DIR)

    def test_lab_consumables_are_normal_class(self) -> None:
        for name in (
            "15ml离心管",
            "一次性巴氏吸管",
            "PCR管",
            "移液枪头",
            "培养皿",
            "96孔酶标板",
            "PVDF膜",
            "0.22um针式过滤器",
            "冻存管",
            "丁腈手套",
        ):
            with self.subTest(name=name):
                result = self.classifier.classify(name)

                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result["final_category"], "普通类")
                self.assertFalse(result["need_manual_review"])

    def test_risk_reagent_names_are_not_consumables(self) -> None:
        for name in ("盐酸标准溶液", "叠氮化钠", "高氯酸样品瓶"):
            with self.subTest(name=name):
                self.assertIsNone(self.classifier.classify(name))


if __name__ == "__main__":
    unittest.main()
