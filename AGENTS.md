# AGENTS.md

## Project

This repository contains a Python + Playwright automation bot for reagent approval workflows.

## Development Notes

- Keep browser automation in `src/browser_bot.py`.
- Keep Excel rule parsing and decision logic in `src/rule_engine.py`.
- Keep chemical information lookup helpers in `src/chemical_searcher.py`.
- Keep LLM-based text extraction isolated in `src/llm_extractor.py`.
- Keep audit output centralized in `src/audit_logger.py`.
- Configuration belongs in `config/settings.yaml`.
- Secrets belong in `.env`; do not commit real credentials.

## Safety

- Run automation in headed mode during development.
- Log every approval decision with enough context for later review.
- Prefer dry-run mode until selectors and approval rules are verified.

