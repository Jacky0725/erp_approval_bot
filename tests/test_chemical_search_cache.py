from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chemical_search_cache import ChemicalSearchCache  # noqa: E402


class ChemicalSearchCacheTest(unittest.TestCase):
    def make_cache(self) -> tuple[tempfile.TemporaryDirectory[str], ChemicalSearchCache]:
        tmp = tempfile.TemporaryDirectory()
        cache = ChemicalSearchCache(
            root_dir=Path(tmp.name),
            settings={
                "chemical_search": {
                    "cache": {
                        "enabled": True,
                        "success_ttl_days": 30,
                        "failure_ttl_days": 1,
                    }
                }
            },
        )
        return tmp, cache

    def test_cache_hit_returns_deep_copy(self) -> None:
        tmp, cache = self.make_cache()
        with tmp:
            key = {
                "source": "chemical_search",
                "normalized_name": "ethanol",
                "cas": "64-17-5",
                "search_mode": "web",
                "settings_version": "unit",
            }
            cache.put(key, {"source": "Chemsrc", "need_manual_review": False, "nested": {"ok": True}})
            first = cache.get(key)
            assert first is not None
            first["nested"]["ok"] = False

            second = cache.get(key)

        assert second is not None
        self.assertTrue(second["nested"]["ok"])

    def test_expired_cache_row_is_not_returned(self) -> None:
        tmp, cache = self.make_cache()
        with tmp:
            key = {
                "source": "chemical_search",
                "normalized_name": "ethanol",
                "cas": "64-17-5",
                "search_mode": "web",
                "settings_version": "unit",
            }
            cache.put(key, {"source": "Chemsrc", "need_manual_review": False})
            cache_hash = cache.cache_key_hash(key)
            expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
            with closing(cache._connect()) as conn:  # noqa: SLF001 - verifies expiry behavior.
                with conn:
                    conn.execute(
                        "UPDATE chemical_search_cache SET expires_at = ? WHERE cache_key = ?",
                        (expired, cache_hash),
                    )

            self.assertIsNone(cache.get(key))


if __name__ == "__main__":
    unittest.main()
