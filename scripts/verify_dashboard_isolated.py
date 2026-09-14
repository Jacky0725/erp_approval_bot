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
from urllib.parse import urlparse

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
            for width, height in [(1440, 900), (390, 844)]:
                for page_id, definition in pages.items():
                    errors, unexpected = [], []
                    state = {"fail": False}
                    context = browser.new_context(viewport={"width": width, "height": height}, service_workers="block")
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
                    data = {"activePage": page_id, "runtime": runtime, "reviewDecisionOptions": [],
                            "llmProviderOptions": [], "currentLlm": {}, "scheduler": {}, "updates": {}}
                    html = environment.get_template("dashboard.html").render(
                        active_page=page_id, page=definition, pages=pages, runtime=runtime, status=status,
                        approval={"exists": False, "rows": 0, "categories": {}, "preview": []},
                        review_queue=queue, todo_tasks={"tasks": []}, scheduler={}, dingtalk_stream={},
                        artifacts=[], static_version="isolated", dashboard_data=data)

                    def route_request(route):
                        request = route.request
                        path = urlparse(request.url).path
                        if request.method != "GET":
                            unexpected.append(request.method + " " + path)
                            route.abort()
                        elif path.startswith("/static/"):
                            filename = path.removeprefix("/static/")
                            if filename not in {"dashboard.js", "dashboard.css"}:
                                unexpected.append(path)
                                route.abort()
                            else:
                                route.fulfill(path=str(root / "src/static" / filename),
                                              content_type="text/javascript" if filename.endswith(".js") else "text/css")
                        elif path == "/api/status":
                            if state["fail"]:
                                route.fulfill(content_type="application/json", body="invalid JSON")
                            else:
                                route.fulfill(json={"status": status, "runtime": runtime, "todo_tasks": {}, "scheduler": {}})
                        elif path == "/api/review_queue":
                            route.fulfill(json=queue)
                        elif path == "/api/approval_summary":
                            route.fulfill(json={"exists": False, "preview": [], "categories": {}, "rows": 0})
                        elif path == "/api/artifacts":
                            route.fulfill(json={"items": []})
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
                    page.screenshot(path=str(args.output / f"{page_id}-{width}.png"), full_page=True)
                    if page_id == "overview":
                        page.locator("#dataHealthPanel").screenshot(path=str(args.output / f"health-{width}.png"))
                        page.locator("#enrichmentShadowPanel").screenshot(path=str(args.output / f"shadow-{width}.png"))
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
