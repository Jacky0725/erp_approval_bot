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
- Keep the local management UI in sync with workflow/module changes:
  - `src/web_app.py` exposes FastAPI routes and artifact downloads.
  - `src/web_runner.py` maps UI actions to automation modules and captures logs.
  - `src/templates/dashboard.html` and `src/static/dashboard.css` show status, suggestions, artifacts, and controls.
- Configuration belongs in `config/settings.yaml`.
- Secrets belong in `.env`; do not commit real credentials.

## Safety

- Run automation in headed mode during development.
- Log every approval decision with enough context for later review.
- Prefer dry-run mode until selectors and approval rules are verified.

## Web UI Design System

- Keep the management UI in `src/templates/dashboard.html` and `src/static/dashboard.css` unless a dedicated frontend build is intentionally introduced.
- Treat `src/static/dashboard.css` as the local design-token source for the FastAPI dashboard.
- Use CSS custom properties for color, spacing, radius, shadow, and typography decisions; avoid one-off hex colors or hardcoded visual values when an existing token fits.
- Keep the dashboard dense and operational: prioritize scanability, tables, forms, status badges, logs, and repeated workflow controls over marketing-style sections.
- Cards and panels should use an 8px radius or less, clear borders, restrained shadows, and consistent internal spacing.
- Preserve existing element IDs and form field names in `src/templates/dashboard.html`; the JavaScript and FastAPI routes depend on them.
- Buttons, inputs, selects, status badges, pagination controls, and table affordances should share consistent sizing, focus states, disabled states, and hover states.
- Wide data tables may scroll horizontally inside `.table-wrap`, but the page itself should not introduce accidental horizontal scrolling at desktop or mobile breakpoints.
- Validate dashboard changes at desktop and narrow mobile widths, and check browser console errors when possible.
