# Release and maintenance gates

## Legacy release migration

The secure split release requires a core ZIP, browser ZIP, manifest, and setup EXE. Older monolithic releases remain downloadable and should not be deleted automatically. Before retiring any old asset:

1. Inventory the installed versions on target computers and identify whether their updater understands `release-manifest.json`.
2. Validate an old-to-new migration on a disposable profile, including data preservation and a rollback to the previous executable.
3. Publish a dated migration notice and support period. Keep the last known working asset during that period.
4. Only after target-computer sign-off, mark old releases as unsupported; archive or remove assets under a separate approved release operation.

The current repository cannot prove target-computer compatibility from unit tests. No release asset is removed by this plan.

## Incremental module boundaries

The broader staged refactor remains in `docs/PROJECT_REFACTOR_PHASED_PLAN_20260910.md`. For the current ERP/API and chemical-search path, the next small changes should be:

| Order | Boundary | Move only after these tests exist | Acceptance |
| --- | --- | --- | --- |
| 1 | API discovery persistence versus protocol inference | Compact report, two verified saves across runs, redaction, old-report migration | No raw request/response or personal fields on disk; same candidate result |
| 2 | API write verification versus page sampling | Stable-ID readback, delayed consistency, stale page, row not located, canary rejection | No unconfirmed success and no uncertain double write |
| 3 | Search identity versus evidence enrichment | Valid/invalid/missing CAS, name mismatch, cache replay, timeout | Identical category/review outcomes before and after extraction |
| 4 | Run-summary events versus log-text compatibility | API and page saves, multi-list runs, duplicate events, partial logs | Summary reconciles with terminal write states |

Keep the public `browser_bot`, FastAPI routes, workbook columns, and existing settings schema compatible. Each extraction should be behavior-equivalent and independently revertible; do not combine it with chemistry rule changes or live ERP writes.

## Search-policy drift to resolve separately

The active configuration selects V3, while the V1 searcher still enables `chemical_search.cas_authoritative`. The removed `chemical_search.query_priority` list was not read by the searcher and falsely suggested that a name-first order was configured. Do not switch V1's authoritative-CAS branch by editing the YAML alone: that would change identity and approval behavior. A future policy change needs explicit name/CAS conflict cases, cache migration rules, and ERP dry-run comparison before activation.
