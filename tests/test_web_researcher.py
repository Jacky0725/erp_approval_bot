from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from web_researcher import WebResearcher  # noqa: E402


class WebResearcherTest(unittest.TestCase):
    def test_pubchem_view_json_is_flattened_to_evidence_text(self) -> None:
        raw_json = """
        {
          "Record": {
            "Section": [
              {
                "TOCHeading": "Safety and Hazards",
                "Information": [
                  {"Value": {"StringWithMarkup": [{"String": "GHS Hazard Statements: H314"}]}}
                ]
              }
            ]
          }
        }
        """

        text = WebResearcher._pubchem_view_to_text(raw_json)

        self.assertIn("Safety and Hazards", text)
        self.assertIn("H314", text)

    def test_duckduckgo_redirect_url_is_decoded(self) -> None:
        html = """
        <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpubchem.ncbi.nlm.nih.gov%2Fcompound%2F14798"
           class="result__a">PubChem sodium hydroxide</a>
        """

        urls = WebResearcher._duckduckgo_result_urls(html, "https://duckduckgo.com/html/")

        self.assertEqual(urls, ["https://pubchem.ncbi.nlm.nih.gov/compound/14798"])


if __name__ == "__main__":
    unittest.main()
