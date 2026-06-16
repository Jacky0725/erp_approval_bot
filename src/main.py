from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

from browser_bot import BrowserBot


ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config" / "settings.yaml"


def load_settings() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def main() -> None:
    load_dotenv(ROOT_DIR / ".env")
    settings = load_settings()

    browser_bot = BrowserBot(settings=settings, root_dir=ROOT_DIR)
    browser_bot.run_semi_auto_approval_suggestions()


if __name__ == "__main__":
    main()
