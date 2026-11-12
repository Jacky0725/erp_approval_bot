import importlib.util
from pathlib import Path

import playwright


HELPER = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "playwright_browser_revision.py"


def test_browser_revision_is_read_from_installed_playwright():
    spec = importlib.util.spec_from_file_location("playwright_browser_revision_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    revisions = module.browser_revisions()
    assert revisions["chromium"].isdigit()
    assert revisions["headless"].isdigit()
    driver = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    assert driver.is_file()
