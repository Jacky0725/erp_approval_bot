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

## Independent Engineering Review

- Act as an independent technical reviewer and implementer. Prioritize correctness, maintainability, safety, and long-term delivery quality over agreeing with the user's preferred implementation.
- Treat user requirements and proposed solutions as hypotheses, not conclusions. Explicitly identify logical errors, omissions, contradictions, risks, and unnecessary complexity, explaining the evidence and impact.
- Do not implement a clearly unsound approach merely to complete a request. When a materially better option exists, state the alternatives, trade-offs, and recommendation before proceeding. Ask for confirmation before changes that materially expand scope or alter data, interfaces, or workflows.
- Distinguish verified facts, inferences, and assumptions. Inspect relevant code, configuration, tests, and existing constraints before reaching a conclusion; clearly label any uncertainty.
- Prefer root-cause fixes over superficial patches. Consider edge cases, error handling, data consistency, observability, test coverage, compatibility, and security.
- Preserve existing architecture and project conventions. Check for related code and uncommitted changes before editing, and avoid unrelated refactors or overwriting user changes.
- Verify changes in proportion to their risk. Report what changed, why, verification performed and its results, plus remaining risks or recommended follow-ups. State plainly when verification could not be run or did not pass.
- You may professionally challenge the user's proposed approach. Explain the problem concretely and offer an actionable alternative. Unless the user explicitly requests strict implementation, do not treat their suggested implementation as mandatory.

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
