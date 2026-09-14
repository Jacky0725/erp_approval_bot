from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from typing import Iterator
from uuid import uuid4

import pandas as pd


@contextmanager
def atomic_excel_path(output_path: Path) -> Iterator[Path]:
    """Publish a completed workbook without truncating the previous version."""
    output_path = Path(output_path)
    fd, name = tempfile.mkstemp(prefix=f".{output_path.stem}_", suffix=output_path.suffix, dir=output_path.parent)
    os.close(fd)  # Excel writers must reopen the file on Windows.
    temporary_path = Path(name)
    try:
        yield temporary_path
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_excel_atomic(dataframe: pd.DataFrame, output_path: Path) -> None:
    with atomic_excel_path(output_path) as temporary_path:
        dataframe.to_excel(temporary_path, index=False)


class ExcelExportsMixin:

    def _log_dir(self) -> Path:
        paths = self.settings.get("paths", {})
        log_dir = self.root_dir / paths.get("audit_log_dir", "data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def write_excel_with_fallback(self, dataframe: pd.DataFrame, output_path: Path) -> Path:
        try:
            write_excel_atomic(dataframe, output_path)
            return output_path
        except PermissionError:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            fallback_path = output_path.with_name(f"{output_path.stem}_{timestamp}_{uuid4().hex}{output_path.suffix}")
            write_excel_atomic(dataframe, fallback_path)
            print(
                f"Could not write {output_path} because it is locked or open; "
                f"saved to {fallback_path} instead."
            )
            return fallback_path
