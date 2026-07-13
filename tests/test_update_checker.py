import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from update_checker import choose_setup_asset, is_newer_version, normalize_tag, parse_version


def test_parse_version_ignores_prefixes_and_suffixes():
    assert parse_version("v0.1.4") == (0, 1, 4)
    assert parse_version("release-1.2.3-win-x64") == (1, 2, 3)


def test_is_newer_version_compares_padded_parts():
    assert is_newer_version("0.1.4", "0.1.3")
    assert is_newer_version("0.2", "0.1.9")
    assert not is_newer_version("0.1.3", "0.1.3")
    assert not is_newer_version("0.1.3", "0.1.4")


def test_normalize_tag_removes_v_prefix():
    assert normalize_tag("v0.1.4") == "0.1.4"
    assert normalize_tag("V1.0.0") == "1.0.0"


def test_choose_setup_asset_prefers_windows_x64_setup():
    asset = choose_setup_asset(
        [
            {"name": "reagent-approval-bot-0.1.4-win-x64-portable.zip", "browser_download_url": "zip"},
            {"name": "reagent-approval-bot-0.1.4-win-x64-setup.exe", "browser_download_url": "exe", "size": 123},
        ]
    )
    assert asset is not None
    assert asset.name.endswith("-win-x64-setup.exe")
    assert asset.url == "exe"
    assert asset.size == 123
