from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from audit_logger import AuditLogger


def close_loggers(loggers):
    for logger in {item.logger for item in loggers}:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_different_roots_do_not_share_audit_output(tmp_path):
    first = AuditLogger.from_settings({}, tmp_path / "first")
    second = AuditLogger.from_settings({}, tmp_path / "second")
    try:
        first.record_decision("first-only", {"category": "review"}, True)
        second.record_decision("second-only", {"category": "review"}, True)
        first_text = (first.log_dir / "bot.log").read_text(encoding="utf-8")
        second_text = (second.log_dir / "bot.log").read_text(encoding="utf-8")
        assert "first-only" in first_text and "second-only" not in first_text
        assert "second-only" in second_text and "first-only" not in second_text
    finally:
        close_loggers([first, second])


def test_concurrent_initialization_does_not_duplicate_decisions(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        loggers = list(pool.map(lambda _: AuditLogger.from_settings({}, tmp_path), range(16)))
    try:
        for item in loggers:
            item.info("one-decision")
        assert (loggers[0].log_dir / "bot.log").read_text(encoding="utf-8").count("one-decision") == 16
    finally:
        close_loggers(loggers)


def test_decision_and_execution_records_are_structured_and_distinct(tmp_path):
    audit = AuditLogger.from_settings({}, tmp_path)
    try:
        audit.record_decision("item", {"rule_version": "sha256:test", "final_category": "普通类"}, True)
        audit.record_execution("reagent_save_1", False, "outcome unknown", True)
        text = (audit.log_dir / "bot.log").read_text(encoding="utf-8")
        assert '"event": "approval_decision"' in text
        assert '"rule_version": "sha256:test"' in text
        assert '"event": "execution_result"' in text
        assert '"name": "reagent_save_1"' in text
    finally:
        close_loggers([audit])
