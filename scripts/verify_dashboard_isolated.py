"""Render the real templates with synthetic data; never import/start the app.

Run: python scripts/verify_dashboard_isolated.py --output <private-output-dir>
Requires an installed Playwright Chromium. All HTTP requests are intercepted.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "src/web_app.py").read_text(encoding="utf-8-sig"))
    pages = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "PAGE_DEFS" for target in node.targets))
    templates = root / "src/templates"
    text = "\n".join(p.read_text(encoding="utf-8-sig") for p in templates.rglob("*.html"))
    runtime = {key: "" for key in re.findall(r"runtime\.(\w+)", text)}
    runtime.update(app_dry_run="true", auto_pass="false", approval_write_mode="disabled",
                   erp_write_backend="web_ui", erp_api_discovery_status="disabled",
                   workbuddy_accepted_batch_sizes=[20], workbuddy_desktop_enabled=False,
                   review_decision_options=[], llm_provider_options=[], app_version="synthetic-test")
    environment = Environment(loader=FileSystemLoader(templates), undefined=ChainableUndefined,
                              autoescape=select_autoescape(["html"]))
    status = {"running": False, "success": None, "summary": {}, "workflow": {}}
    queue = {"exists": False, "rows": 0, "pending": 0, "preview": [], "list_numbers": [],
             "page": 1, "pages": 1, "total": 0}
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            logo_page = browser.new_page(viewport={"width": 192, "height": 192}, device_scale_factor=2)
            logo_page.goto((root / "src/static/reagent-logo.svg").as_uri())
            logo_page.screenshot(path=str(args.output / "logo-preview.png"), omit_background=True)
            logo_page.close()
            for width, height in [(1440, 900), (1280, 800), (390, 844)]:
                for page_id, definition in pages.items():
                    errors, unexpected = [], []
                    state = {"fail": False, "confirmed": 0, "launched": 0, "memory_name": "Demo memory"}
                    review_rows = [
                        {"review_key": f"demo-{i}", "list_number": "DEMO001", "sequence": i,
                         "reagent_name": f"Test record {i:02}", "status": "pending"}
                        for i in range(1, 11)
                    ]
                    context = browser.new_context(viewport={"width": width, "height": height}, service_workers="block")
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    def capture_console(msg):
                        if msg.type != "error":
                            return
                        if page_id == "settings" and "422 (Unprocessable Entity)" in msg.text:
                            return  # Deliberate field-validation response below.
                        errors.append(msg.text)

                    page.on("console", capture_console)
                    data = {"activePage": page_id, "runtime": runtime, "reviewDecisionOptions": ["Demo category", "普通类别"],
                            "llmProviderOptions": [], "currentLlm": {}, "scheduler": {}, "updates": {}}
                    html = environment.get_template("dashboard.html").render(
                        active_page=page_id, page=definition, pages=pages, runtime=runtime, status=status,
                        approval={"exists": False, "rows": 0, "categories": {}, "preview": []},
                        review_queue=queue, todo_tasks={"tasks": []}, scheduler={}, dingtalk_stream={},
                        artifacts=[], static_version="isolated", dashboard_data=data)

                    def route_request(route):
                        request = route.request
                        path = urlparse(request.url).path
                        if request.method == "POST" and path == "/api/review/confirm" and page_id == "review":
                            assert request.post_data_json["review_key"] == review_rows[0]["review_key"]
                            assert request.post_data_json["final_category"] == "Demo category"
                            review_rows.pop(0)
                            state["confirmed"] += 1
                            route.fulfill(json={"confirmed": True, "message": "Confirmed"})
                        elif request.method == "POST" and path == "/api/run/prepare" and page_id == "run":
                            assert request.post_data_json["action"] == "suggestions"
                            assert request.post_data_json["target_list_numbers"] == "DEMO001"
                            assert request.post_data_json["approval_write_mode"] == "disabled"
                            route.fulfill(json={"ticket": "fake"})
                        elif request.method == "POST" and path == "/api/run" and page_id == "run":
                            state["launched"] += 1
                            route.fulfill(json={"started": True})
                        elif request.method == "POST" and path == "/api/memory/1" and page_id == "memory":
                            payload = request.post_data_json
                            state["memory_name"] = payload["standard_name"]
                            assert "final_category" not in payload
                            assert payload["confidence"] == "0.85"
                            assert not {"reusable", "manual_verified", "conflict"} & payload.keys()
                            route.fulfill(json={"updated": True})
                        elif request.method == "PATCH" and path == "/api/memory/1/flags" and page_id == "memory":
                            payload = request.post_data_json
                            if "final_category" in payload:
                                assert payload == {"reusable": False, "manual_verified": False, "conflict": True,
                                                   "final_category": "普通类别"}
                                state["memory_category"] = payload["final_category"]
                            else:
                                assert payload == {"reusable": False, "manual_verified": True, "conflict": True}
                                state["memory_manual"] = True
                            route.fulfill(json={"updated": True})
                        elif request.method == "POST" and path == "/api/memory/delete_non_reusable" and page_id == "memory":
                            state["memory_bulk_deleted"] = True
                            route.fulfill(json={"deleted": 1, "backup": "test.sqlite"})
                        elif request.method == "POST" and path == "/api/settings" and page_id == "settings":
                            route.fulfill(status=422, json={"detail": [
                                {"loc": ["body", "erp_url"], "msg": "Demo validation error"},
                            ]})
                        elif request.method != "GET":
                            unexpected.append(request.method + " " + path)
                            route.abort()
                        elif path.startswith("/static/"):
                            filename = path.removeprefix("/static/")
                            content_types = {"dashboard.js": "text/javascript", "dashboard.css": "text/css",
                                             "reagent-logo.svg": "image/svg+xml"}
                            if filename not in content_types:
                                unexpected.append(path)
                                route.abort()
                            else:
                                route.fulfill(path=str(root / "src/static" / filename),
                                              content_type=content_types[filename])
                        elif path == "/api/status":
                            if state["fail"]:
                                route.fulfill(content_type="application/json", body="invalid JSON")
                            else:
                                route.fulfill(json={"status": status, "runtime": runtime, "scheduler": {},
                                                    "todo_tasks": {"exists": True, "tasks": [{"list_number": "DEMO001"}]}})
                        elif path == "/api/stopped-tasks":
                            route.fulfill(json={"tasks": []})
                        elif path == "/api/review_queue":
                            route.fulfill(json={**queue, "exists": True, "rows": 10, "preview": review_rows,
                                                "pending": len(review_rows), "list_numbers": ["DEMO001"]})
                        elif path == "/api/approval_summary":
                            route.fulfill(json={"exists": True, "rows": 1, "categories": {}, "preview": [
                                {"试剂名称": "Demo suggestion", "需人工复核": False, "查询来源": "business_rule"},
                            ]})
                        elif path == "/api/artifacts":
                            route.fulfill(json={"artifacts": []})
                        elif path == "/api/memory":
                            params = parse_qs(urlparse(request.url).query)
                            query = params.get("q", [""])[0]
                            per_page = int(params.get("per_page", ["10"])[0])
                            records = [
                                {"id": 1, "standard_name": state["memory_name"], "final_category": state.get("memory_category", "Demo category"),
                                 "raw_name": "Demo raw name", "cas": "64-17-5",
                                 "confidence": 0.85, "reusable": False, "manual_verified": state.get("memory_manual", False), "conflict": True},
                                *({"id": index, "standard_name": f"Sample {index:02} long reagent name",
                                   "final_category": "普通类别", "cas": "67-56-1",
                                   "reusable": True, "manual_verified": True} for index in range(2, 13)),
                            ]
                            if query == "invalid json":
                                route.fulfill(content_type="application/json", body="invalid JSON")
                                return
                            if state.get("memory_bulk_deleted"):
                                records = [row for row in records if row["reusable"]]
                            records = [row for row in records if query.lower() in row["standard_name"].lower()]
                            for key in ("reusable", "conflict"):
                                value = params.get(key, [""])[0]
                                if value:
                                    records = [row for row in records if bool(row.get(key)) == (value == "1")]
                            category = params.get("category", [""])[0]
                            if category:
                                records = [row for row in records if row["final_category"] == category]
                            pages = max(1, (len(records) + per_page - 1) // per_page)
                            current = min(pages, int(params.get("page", ["1"])[0]))
                            start = (current - 1) * per_page
                            route.fulfill(json={"exists": True, "rows": len(records), "page": current, "pages": pages,
                                                "categories": ["Demo category", "普通类别"],
                                                "per_page": per_page, "preview": records[start:start + per_page]})
                        elif path in {"/api/data_health", "/api/enrichment_shadow", "/api/log_tail", "/api/memory", "/api/memory/sync/status", "/api/update/check"}:
                            route.fulfill(json={"rows": [], "items": [], "preview": [], "categories": [], "lines": [], "ok": True})
                        elif path == definition["path"]:
                            route.fulfill(body=html, content_type="text/html")
                        elif path == "/favicon.ico":
                            route.fulfill(status=204)
                        else:
                            unexpected.append(path)
                            route.abort()

                    context.route("**/*", route_request)
                    page.goto("http://127.0.0.1:8769" + definition["path"])
                    page.wait_for_function("document.querySelector('#runBadge').textContent === '空闲'")
                    page.wait_for_function("document.querySelector('.brand-mark').naturalWidth > 0")
                    assert page.locator(".sidebar a").count() == len(pages)
                    if page_id == "overview":
                        page.locator("#sidebarToggle").click()
                        assert page.locator(".brand-mark").is_visible()
                        page.locator("#sidebarToggle").click()
                    if page_id == "run":
                        restart = page.locator("#restartRunButton")
                        assert restart.is_visible()
                        assert page.locator("#exitProgramButton").is_visible()
                        assert restart.evaluate(
                            "(el) => el.closest('.run-action-group').getBoundingClientRect().width "
                            "=== document.querySelector('#safeStopRunButton').closest('.run-action-group').getBoundingClientRect().width"
                        )
                        page.locator("#approvalModeV1").click()
                        page.locator('input[name="todo_list_number"]').check()
                        assert "DEMO001" in page.locator("#executionScope").inner_text()
                        assert "不写入" in page.locator("#executionImpact").inner_text()
                        page.locator('[data-action="suggestions"]').click()
                        page.wait_for_function("document.querySelector('#runLaunchFeedback').textContent.includes('已启动')")
                        assert state["launched"] == 1
                    if page_id == "review":
                        page.locator(".review-select-record").first.wait_for()
                        assert page.locator(".review-table-wrap").evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
                        if width < 680:
                            page.locator(".review-select-record").first.press("Enter")
                            page.locator("#reviewBackToQueue").click()
                            assert page.locator(".review-select-record").first.evaluate("(el) => el === document.activeElement")
                        page.locator(".review-select-record").first.click()
                        page.screenshot(path=str(args.output / f"review-detail-{width}.png"), full_page=True)
                        for index in range(1, 11):
                            assert page.locator("#reviewInspectorTitle").inner_text() == f"Test record {index:02}"
                            page.locator("#reviewCategorySlot select").select_option("Demo category")
                            page.locator("#reviewActionSlot .review-confirm").click()
                            page.wait_for_function(f"document.querySelectorAll('.review-row').length === {10 - index}")
                        assert state["confirmed"] == 10
                    if page_id == "suggestions":
                        assert page.get_by_text("无需复核").is_visible()
                        page.locator(".evidence-trigger").press("Enter")
                        page.locator("#closeEvidence").click()
                        assert page.locator(".evidence-trigger").evaluate("(el) => el === document.activeElement")
                    if page_id == "memory":
                        page.locator(".memory-row").first.wait_for()
                        assert page.locator(".memory-table th").count() == 4
                        assert "置信度" not in page.locator(".memory-table thead").inner_text()
                        assert page.locator(".memory-row").first.locator("td").count() == 4
                        row = page.locator('.memory-row[data-id="1"]')
                        assert row.locator(".memory-row-flags label").all_inner_texts() == ["可复用", "人工确认", "有冲突"]
                        assert row.get_by_role("checkbox", name="有冲突").is_checked()
                        assert not row.get_by_role("checkbox", name="可复用").is_checked()
                        assert page.locator('.memory-row[data-id="2"] .memory-manual').is_disabled()
                        edit = page.locator('.memory-row[data-id="1"] > td:last-child .memory-row-actions button:last-child')
                        assert edit.inner_text() == "编辑"
                        page.locator("#memoryNextPage").click()
                        page.get_by_text("Sample 12 long reagent name", exact=True).wait_for()
                        page.locator("#memoryPageSize").select_option("20")
                        page.locator('.memory-row[data-id="1"]').wait_for()
                        page.locator("#memoryConflictFilter").select_option("1")
                        page.get_by_text("匹配 1 条", exact=True).wait_for()
                        assert page.locator(".memory-row").count() == 1
                        page.locator("#memoryReusableFilter").select_option("1")
                        page.get_by_text("没有匹配的记录", exact=True).wait_for()
                        page.locator("#clearMemoryFilters").click()
                        assert page.locator("#memoryConflictFilter").input_value() == ""
                        page.locator('.memory-row[data-id="12"]').wait_for()
                        search = page.locator("#memorySearch")
                        search.fill("invalid json")
                        search.press("Enter")
                        page.get_by_text("刷新失败，仍显示上一次加载的记录。请重试。", exact=True).wait_for()
                        assert page.locator(".memory-row").count() == 12
                        search.fill("missing")
                        search.press("Enter")
                        page.get_by_text("没有匹配的记录", exact=True).wait_for()
                        page.locator("#clearMemoryFilters").click()
                        page.locator('.memory-row[data-id="1"]').wait_for()
                        assert search.input_value() == ""
                        edit.click()
                        assert page.locator("#memoryRecordDialog").is_visible()
                        assert page.locator("#memoryRecordDialog input[type=checkbox]").count() == 0
                        assert page.locator('#memoryRecordDialog [data-memory-field="final_category"]').count() == 0
                        assert page.locator("#memoryRecordDialog").evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
                        assert page.locator('[data-memory-field="confidence"]').input_value() == "0.85"
                        page.screenshot(path=str(args.output / f"memory-editor-{width}.png"))
                        page.locator('[data-memory-field="standard_name"]').fill("Discarded draft")
                        page.keyboard.press("Escape")
                        assert not page.locator("#memoryRecordDialog").is_visible()
                        edit.click()
                        name_field = page.locator('[data-memory-field="standard_name"]')
                        assert name_field.input_value() == "Demo memory"
                        name_field.fill("Updated demo memory")
                        page.locator("#memoryDetailSave").click()
                        page.get_by_text("Updated demo memory", exact=True).wait_for()
                        assert edit.evaluate("(el) => document.activeElement === el")
                        category = row.get_by_role("combobox", name="Updated demo memory：物化特性", exact=True)
                        assert category.input_value() == "Demo category"
                        category.select_option("普通类别")
                        assert not state.get("memory_category")
                        row.get_by_role("button", name="保存更改").click()
                        page.get_by_text("已保存列表更改。", exact=True).wait_for()
                        assert state.get("memory_category") == "普通类别"
                        assert category.input_value() == "普通类别"
                        row.get_by_role("checkbox", name="人工确认").check()
                        assert not state.get("memory_manual")
                        row.get_by_role("button", name="保存更改").click()
                        page.get_by_text("已保存列表更改。", exact=True).wait_for()
                        assert state.get("memory_manual")
                        assert row.get_by_role("checkbox", name="人工确认").is_checked()
                        page.screenshot(path=str(args.output / f"memory-inline-{width}.png"))
                        page.locator(".memory-maintenance summary").click()
                        page.once("dialog", lambda dialog: dialog.dismiss())
                        page.locator("#deleteNonReusableMemoryButton").click()
                        assert not state.get("memory_bulk_deleted")
                        page.once("dialog", lambda dialog: dialog.accept())
                        page.locator("#deleteNonReusableMemoryButton").click()
                        page.get_by_text("已删除 1 条不可复用记录，备份已保存在运行日志目录。", exact=True).wait_for()
                        assert state.get("memory_bulk_deleted")
                        assert page.locator(".memory-row").count() == 11
                        page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
                    if width < 680 and page_id in {"memory", "suggestions"}:
                        assert page.locator(".table-wrap").evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
                        assert page.locator("tbody tr").first.evaluate(
                            "(row) => [...row.cells].filter(c => c.offsetWidth).every(c => c.offsetWidth >= 40)")
                    if page_id == "settings":
                        page.locator(".settings-jump a").last.click()
                        assert not page.locator("#settingsMaintenance").evaluate("(el) => el.classList.contains('settings-section-collapsed')")
                        page.locator('input[name="erp_url"]').fill("https://example.invalid")
                        assert "未保存" in page.locator("#settingsSaveState").inner_text()
                        page.locator('#settingsForm button[type="submit"]').click()
                        page.get_by_text("Demo validation error", exact=True).wait_for()
                        assert page.locator('input[name="erp_url"]').get_attribute("aria-invalid") == "true"
                    page.screenshot(path=str(args.output / f"{page_id}-{width}.png"), full_page=True)
                    if page_id == "overview":
                        page.locator("#dataHealthPanel > summary").click()
                        page.locator("#dataHealthPanel").screenshot(path=str(args.output / f"health-{width}.png"))
                    overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth")
                    state["fail"] = True
                    page.evaluate("refreshStatus()")
                    page.wait_for_function("document.querySelector('#runBadge').textContent.includes('过期')")
                    if page_id == "overview":
                        page.screenshot(path=str(args.output / f"offline-{width}.png"), full_page=True)
                    state["fail"] = False
                    page.evaluate("refreshStatus()")
                    page.wait_for_function("document.querySelector('#runBadge').textContent === '空闲'")
                    results.append({"page": page_id, "width": width, "horizontal_overflow": overflow,
                                    "errors": list(errors), "unexpected_requests": list(unexpected),
                                    "failure_and_recovery": "passed"})
                    context.close()
        finally:
            browser.close()
    (args.output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))
    if any(row["errors"] or row["unexpected_requests"] or row["horizontal_overflow"] for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
