import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import update_checker
from update_checker import choose_setup_asset, check_for_update, is_newer_version, normalize_tag, parse_version


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


def test_check_for_update_uses_public_release_before_api(monkeypatch):
    calls = []

    def public_release(timeout_seconds=20):
        calls.append("public")
        return {
            "tag_name": "v0.1.5",
            "html_url": "https://github.example/release",
            "assets": [
                {
                    "name": "reagent-approval-bot-0.1.5-win-x64-setup.exe",
                    "browser_download_url": "https://github.example/setup.exe",
                    "size": 100,
                }
            ],
        }

    def api_release(timeout_seconds=20):
        calls.append("api")
        raise AssertionError("GitHub API should not be called when public release check succeeds")

    monkeypatch.setattr(update_checker, "fetch_latest_release_public", public_release)
    monkeypatch.setattr(update_checker, "fetch_latest_release", api_release)

    result = check_for_update(current_version="0.1.4")

    assert result.ok
    assert result.latest_version == "0.1.5"
    assert result.update_available
    assert calls == ["public"]
