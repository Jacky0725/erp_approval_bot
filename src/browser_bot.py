from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from approval_flow import ApprovalFlowMixin
from erp_session import ErpSessionMixin
from excel_exports import ExcelExportsMixin
from reagent_page import ReagentPageMixin
from review_queue import ReviewQueueMixin


@dataclass
class BrowserBot(
    ApprovalFlowMixin,
    ReagentPageMixin,
    ReviewQueueMixin,
    ErpSessionMixin,
    ExcelExportsMixin,
):
    settings: dict[str, Any]
    root_dir: Path
    save_results: list[dict[str, Any]] | None = None
    auto_match_succeeded: bool = False
    pagination_check_succeeded: bool = False
