# AGENTS.md

## Project

This repository contains a Python + Playwright automation bot for reagent approval workflows.

## Development Notes

- Keep the legacy browser entrypoints in `src/browser_bot.py`, but put new ERP automation behavior in focused modules:
  - `src/erp_session.py` for login, page opening, and baseline waits.
  - `src/reagent_page.py` for reagent approval pages, list/detail reading, pagination, and sorting.
  - `src/approval_flow.py` for semi-automatic workflow orchestration.
  - `src/approval_writer.py` for future page writes, saves, and reagent-library generation.
  - `src/excel_exports.py` for safe Excel output.
  - `src/review_queue.py` for manual review queue reads and writes.
- Keep Excel rule parsing and decision logic in `src/rule_engine.py`.
- Keep approved structured rules in `config/rules_structured.xlsx`; write uncertain rule candidates to `config/rule_candidates.xlsx` for human review before promotion.
- Keep rule candidate maintenance helpers in `src/rule_maintainer.py`.
- Keep chemical information lookup helpers in `src/chemical_searcher.py`.
- Keep LLM-based text extraction isolated in `src/llm_extractor.py`.
- Keep audit output centralized in `src/audit_logger.py`.
- Configuration belongs in `config/settings.yaml`.
- Secrets belong in `.env`; do not commit real credentials.

## Safety

- Run automation in headed mode during development.
- Log every approval decision with enough context for later review.
- Prefer dry-run mode until selectors and approval rules are verified.
