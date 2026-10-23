# Target computer acceptance checklist

This is an execution checklist, not evidence that a target computer has passed. Use a test account and a test list. Do not enable AUTO_PASS or click ERP's final approval button during validation.

## Before installation

- [ ] Record Windows edition, architecture, current user privilege, antivirus state, and existing version. Do not record credentials.
- [ ] Confirm the release tag matches `VERSION`; run `python packaging/windows/verify_release_assets.py --release-dir dist/releases --version <version>` on the build machine.
- [ ] Copy the previous user-data directory to a separate backup. Record file hashes and sizes of `.env`, SQLite memory, review queue, rules, and logs without printing contents.
- [ ] Confirm port 8000 availability or set the supported alternate port.

## New installation and use

- [ ] Install as a non-administrator in a Chinese or space-containing path; record installer progress and exit result.
- [ ] Run `python scripts/check_target_install.py` or provide `--install-dir` and `--runtime-dir`; all checks must pass. This is a layout check, not an ERP acceptance test.
- [ ] Start the Web UI; confirm the process, localhost binding, `/api/status`, log page, review page, and absence of browser-console errors.
- [ ] Log in to the test ERP; read a test list in dry-run mode. Reconcile list number, row count, sequence, name, and property with ERP.
- [ ] Generate suggestions and inspect the reason and source for representative normal, high-risk, mixed, unknown, and missing-CAS rows.
- [ ] Confirm a test review item manually, restart, and confirm that the approved memory is reused without duplicate pending review.
- [ ] On an explicitly authorized test row only, save one property; reconcile stable record ID, API readback, refreshed page value, write summary, and audit log. Do not click final approval.

## Upgrade, failure, and removal

- [ ] Restart the app twice; confirm only one worker owns the port and the same user data is loaded.
- [ ] Simulate disconnected ERP or external chemical sources; confirm bounded wait, visible failure/review state, and no speculative write.
- [ ] Install an older supported version and use the Web UI updater to detect and install a newer version. Confirm signature/source, manifest hashes, version pointer, launch, and rollback path.
- [ ] Interrupt a test update before version switch; verify the previous version still starts. Do not interrupt a production update for this test.
- [ ] Compare configuration, SQLite, review queue, and log hashes before/after upgrade. Existing user files must remain intact.
- [ ] Uninstall with the keep-data option, then with a disposable test profile check the remove-data option. Confirm program files and user data follow the selected option.

Record each item as **passed / failed / not tested**, with time, version, operator, screenshot or log path, and observed result. A code-level or simulated pass must not be reported as target-computer evidence.
