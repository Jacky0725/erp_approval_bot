from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd


class ExcelExportsMixin:

    def _log_dir(self) -> Path:
        paths = self.settings.get("paths", {})
        log_dir = self.root_dir / paths.get("audit_log_dir", "data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def write_excel_with_fallback(self, dataframe: pd.DataFrame, output_path: Path) -> Path:
        try:
            dataframe.to_excel(output_path, index=False)
            return output_path
        except PermissionError:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            fallback_path = output_path.with_name(f"{output_path.stem}_{timestamp}{output_path.suffix}")
            dataframe.to_excel(fallback_path, index=False)
            print(
                f"Could not write {output_path} because it is locked or open; "
                f"saved to {fallback_path} instead."
            )
            return fallback_path
