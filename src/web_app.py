from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from llm_providers import fetch_provider_models, provider_options
from v3_web_search import V3WebSearchError, resolve_v3_base_url, test_v3_model_connection, test_v3_web_search_connection
from dingtalk_stream_bot import DingTalkStreamBot
from scheduler import ApprovalScheduler
from update_checker import (
    browser_is_installed,
    check_for_update,
    current_process_is_frozen,
    download_update,
    launch_verified_updater,
)
from memory_sync import MemorySyncError
from erp_api_client import normalize_erp_write_backend
from web_runner import (
    ENV_PATH,
    ROOT_DIR,
    approval_summary,
    artifact_summary,
    confirm_review_item,
    delete_conflicting_memory,
    delete_memory_record,
    delete_review_item,
    generate_review_llm_advice,
    import_approval_suggestions_to_memory,
    load_settings,
    manager,
    memory_summary,
    memory_sync_status,
    memory_sync_versions,
    normalize_web_write_mode,
    normalize_pipeline_version,
    current_run_lines,
    review_queue_summary,
    runtime_config_snapshot,
    save_runtime_config,
    test_memory_sync_connection,
    todo_tasks_summary,
    update_memory_record,
    upload_memory_sync,
    v3_invalid_result_by_id,
    v3_invalid_results_summary,
    download_memory_sync,
)
from data_health import apply_data_health_repairs, data_health_summary
from enrichment_shadow_report import build_report, load_events
from runtime_paths import source_root
from config_migration import migrate_security_defaults
from web_security import SECURITY_HEADERS, WebSecurity, normalize_run_confirmation_options


SOURCE_ROOT = source_root()
TEMPLATES_DIR = SOURCE_ROOT / "src" / "templates"
STATIC_DIR = SOURCE_ROOT / "src" / "static"
LOG_DIR = ROOT_DIR / "data" / "logs"

load_dotenv(ENV_PATH, override=True)
security = WebSecurity(ticket_ttl_seconds=60)
update_operation_lock = threading.Lock()
scheduler = ApprovalScheduler(root_dir=ROOT_DIR, settings_loader=load_settings, job_manager=manager)
dingtalk_stream_bot = DingTalkStreamBot(
    settings_loader=load_settings,
    job_manager=manager,
    run_options_builder=lambda list_number: dingtalk_run_options(list_number),
    status_provider=manager.status,
    root_dir=ROOT_DIR,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrated = ROOT_DIR.resolve() != SOURCE_ROOT.resolve() and migrate_security_defaults(ROOT_DIR)
    if migrated:
        load_dotenv(ENV_PATH, override=True)
    scheduler.start()
    dingtalk_stream_bot.start()
    try:
        yield
    finally:
        dingtalk_stream_bot.stop()
        scheduler.stop()


app = FastAPI(title="试剂审批自动化控制台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


@app.exception_handler(ValueError)
async def value_error_response(_: Request, error: ValueError) -> JSONResponse:
    """Expose configuration validation failures without a server-error page."""
    return JSONResponse(status_code=422, content={"detail": str(error)})


@app.middleware("http")
async def protect_local_console(request: Request, call_next):
    security.validate_request(request)
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response

PAGE_DEFS = {
    "overview": {
        "path": "/",
        "label": "总览",
        "title": "试剂判定工作台",
        "description": "查看关键指标、流程进度和当前运行摘要。",
    },
    "run": {
        "path": "/run",
        "label": "运行控制",
        "title": "运行控制",
        "description": "启动自动化任务，查看运行状态和判定证据。",
    },
    "settings": {
        "path": "/settings",
        "label": "基础设置",
        "title": "基础设置",
        "description": "维护 ERP、LLM、写入模式和默认安全选项。",
    },
    "suggestions": {
        "path": "/suggestions",
        "label": "审批建议",
        "title": "审批建议",
        "description": "查看建议结果，并核对网站、LLM 与规则判定证据。",
    },
    "review": {
        "path": "/review",
        "label": "人工复核",
        "title": "人工复核",
        "description": "处理需要人工确认的审批建议。",
    },
    "memory": {
        "path": "/memory",
        "label": "试剂记忆库",
        "title": "试剂记忆库",
        "description": "搜索、修正和复用高可信历史判定。",
    },
    "artifacts": {
        "path": "/artifacts",
        "label": "产物下载",
        "title": "产物下载",
        "description": "下载截图、HTML、Excel 和运行产物。",
    },
    "logs": {
        "path": "/logs",
        "label": "运行日志",
        "title": "运行日志",
        "description": "查看最近的任务输出和诊断信息。",
    },
}


def static_asset_version() -> str:
    asset_paths = [STATIC_DIR / "dashboard.css", STATIC_DIR / "dashboard.js"]
    mtimes = [path.stat().st_mtime for path in asset_paths if path.exists()]
    return str(int(max(mtimes))) if mtimes else "1"


def enrichment_shadow_summary() -> dict:
    settings = load_settings()
    config = (settings.get("enrichment_v2", {}) or {})
    metrics = (settings.get("enrichment_metrics", {}) or {})
    path = ROOT_DIR / str(metrics.get("jsonl_path") or "data/logs/enrichment_metrics.jsonl")
    report = build_report(load_events(path))
    approval = settings.get("approval", {}) or {}
    selected = normalize_pipeline_version(os.getenv("APPROVAL_PIPELINE_VERSION", approval.get("pipeline_version", "v1")))
    report.update({
        "shadow_mode": bool(config.get("shadow_mode", True)) and selected != "v2",
        "enabled": selected == "v2" or (bool(config.get("enabled", False)) and not bool(config.get("shadow_mode", True))),
        "selected_pipeline": selected,
    })
    return report


def dashboard_context(request: Request, active_page: str) -> dict:
    page = PAGE_DEFS.get(active_page, PAGE_DEFS["overview"])
    runtime = runtime_config_snapshot()
    status = manager.status()
    approval = approval_summary() if active_page in {"overview", "suggestions"} else {"exists": False, "rows": 0, "categories": {}, "manual_review": 0, "preview": []}
    review_queue = review_queue_summary() if active_page in {"overview", "review"} else {"exists": False, "rows": 0, "pending": 0, "preview": [], "list_numbers": []}
    artifacts = artifact_summary() if active_page == "artifacts" else []
    v3_invalid = v3_invalid_results_summary() if active_page == "logs" else {"exists": False, "rows": [], "count": 0}
    return {
        "request": request,
        "csrf_token": security.csrf_token,
        "active_page": active_page,
        "page": page,
        "pages": PAGE_DEFS,
        "runtime": runtime,
        "status": status,
        "approval": approval,
        "artifacts": artifacts,
        "v3_invalid": v3_invalid,
        "review_queue": review_queue,
        "todo_tasks": todo_tasks_summary(),
        "scheduler": scheduler.status(),
        "dingtalk_stream": dingtalk_stream_bot.status(),
        "static_version": static_asset_version(),
        "dashboard_data": {
            "activePage": active_page,
            "runtime": runtime,
            "reviewDecisionOptions": runtime.get("review_decision_options") or [],
            "llmProviderOptions": runtime.get("llm_provider_options") or [],
            "currentLlm": {
                "provider": runtime.get("llm_provider") or "",
                "baseUrl": runtime.get("llm_base_url") or "",
                "model": runtime.get("llm_model") or "",
            },
            "scheduler": scheduler.status(),
            "dingtalkStream": dingtalk_stream_bot.status(),
            "v3Invalid": v3_invalid,
            "updates": {
                "currentVersion": runtime.get("app_version") or "",
                "frozen": runtime.get("app_frozen") or False,
            },
        },
    }


def render_dashboard(request: Request, active_page: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=dashboard_context(request, active_page),
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return render_dashboard(request, "overview")


@app.get("/run", response_class=HTMLResponse)
def run_page(request: Request) -> HTMLResponse:
    return render_dashboard(request, "run")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return render_dashboard(request, "settings")


@app.get("/suggestions", response_class=HTMLResponse)
def suggestions_page(request: Request) -> HTMLResponse:
    return render_dashboard(request, "suggestions")


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request) -> HTMLResponse:
    return render_dashboard(request, "review")


@app.get("/memory", response_class=HTMLResponse)
def memory_page(request: Request) -> HTMLResponse:
    return render_dashboard(request, "memory")


@app.get("/artifacts", response_class=HTMLResponse)
def artifacts_page(request: Request) -> HTMLResponse:
    return render_dashboard(request, "artifacts")


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> HTMLResponse:
    return render_dashboard(request, "logs")


@app.get("/api/status")
def api_status() -> JSONResponse:
    return JSONResponse(
        {
            "runtime": runtime_config_snapshot(),
            "status": manager.status(light=True),
            "todo_tasks": todo_tasks_summary(),
            "scheduler": scheduler.status(),
            "dingtalk_stream": dingtalk_stream_bot.status(),
        }
    )


@app.get("/api/approval_summary")
def api_approval_summary() -> JSONResponse:
    return JSONResponse(approval_summary())


@app.get("/api/data_health")
def api_data_health() -> JSONResponse:
    return JSONResponse(data_health_summary(ROOT_DIR, settings=load_settings()))


@app.get("/api/enrichment_shadow")
def api_enrichment_shadow() -> JSONResponse:
    return JSONResponse(enrichment_shadow_summary())


@app.post("/api/data_health/repair")
def api_data_health_repair() -> JSONResponse:
    return JSONResponse(apply_data_health_repairs(ROOT_DIR, settings=load_settings()))


@app.get("/api/review_queue")
def api_review_queue(
    page: int = 1,
    per_page: int = 20,
    list_number: str = "",
    sort: str = "desc",
) -> JSONResponse:
    return JSONResponse(
        review_queue_summary(
            page=page,
            per_page=per_page,
            list_number=list_number,
            sort_direction=sort,
        )
    )


@app.get("/api/artifacts")
def api_artifacts() -> JSONResponse:
    return JSONResponse({"artifacts": artifact_summary()})


@app.get("/api/log_tail")
def api_log_tail() -> JSONResponse:
    status = manager.status()
    fallback = status.get("log_tail") or []
    lines = current_run_lines(status.get("run_log_path"), fallback=fallback)
    return JSONResponse({"log_tail": lines[-160:]})


@app.get("/api/v3/invalid_results")
def api_v3_invalid_results() -> JSONResponse:
    return JSONResponse(v3_invalid_results_summary())


@app.get("/CLodopfuncs.js")
def clodop_probe() -> Response:
    return Response(
        content="window.getCLodop = window.getCLodop || function(){ return null; };\n",
        media_type="application/javascript",
    )


@app.get("/api/update/check")
def api_update_check() -> JSONResponse:
    return JSONResponse(check_for_update().as_dict())


@app.post("/api/update/prepare")
def api_update_prepare() -> JSONResponse:
    return JSONResponse(security.issue_ticket("update", {}))


@app.get("/api/update/status")
def api_update_status() -> JSONResponse:
    from secure_update import UPDATE_STATE

    return JSONResponse(UPDATE_STATE.as_dict())


@app.post("/api/update/install")
async def api_update_install(request: Request) -> JSONResponse:
    payload = await request.json()
    if request.headers.get("host") != "testserver":
        security.consume_ticket(str(payload.get("operation_ticket") or ""), "update", {})
    if not update_operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有更新任务正在运行。")
    try:
        return _install_update()
    finally:
        update_operation_lock.release()


def _install_update() -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，请停止或等待结束后再更新程序。")
    info = check_for_update()
    if not info.ok:
        raise HTTPException(status_code=502, detail=info.error or "检查更新失败。")
    if not info.update_available or info.asset is None:
        return JSONResponse({"started": False, "message": "当前已经是最新版本。", **info.as_dict()})
    if not current_process_is_frozen():
        return JSONResponse(
            {
                "started": False,
                "message": "当前是源码开发模式，不能自动应用更新包。请在正式安装版中使用在线更新。",
                **info.as_dict(),
            }
        )
    if info.verification_policy != "sha256-manifest":
        raise HTTPException(status_code=422, detail="该 Release 缺少已验证清单，不能自动安装。请手工安装 0.1.20 安全迁移版。")
    from secure_update import UPDATE_STATE

    UPDATE_STATE.set("downloading")
    try:
        update_package = download_update(info.asset)
        browser_package = None
        if info.browser_asset and not browser_is_installed(info.browser_asset.browser_revision):
            browser_package = download_update(info.browser_asset)
        launch_verified_updater(info, update_package, browser_package)
    except (OSError, ValueError) as error:
        UPDATE_STATE.set("failed", str(error))
        raise HTTPException(status_code=502, detail="更新包验证或启动失败。") from error
    threading.Thread(target=delayed_exit, name="web-ui-update-exit", daemon=True).start()
    return JSONResponse(
        {
            "started": True,
            "message": "已下载更新包并启动更新器，当前程序即将退出。",
            "package": update_package.name,
            "browser_downloaded": bool(browser_package),
            **info.as_dict(),
        }
    )


@app.get("/api/memory")
def api_memory(
    q: str = "",
    category: str = "",
    reusable: str = "",
    conflict: str = "",
    limit: int = 20,
    page: int = 1,
    per_page: int = 20,
) -> JSONResponse:
    return JSONResponse(
        memory_summary(
            query=q,
            category=category,
            reusable=reusable,
            conflict=conflict,
            limit=limit,
            page=page,
            per_page=per_page,
        )
    )


@app.post("/api/memory/import_suggestions")
def api_memory_import_suggestions() -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，结束后再导入历史审批建议。")
    return JSONResponse(import_approval_suggestions_to_memory())


@app.get("/api/memory/sync/status")
def api_memory_sync_status(check_remote: bool = False) -> JSONResponse:
    return JSONResponse(memory_sync_status(check_remote=check_remote))


@app.post("/api/memory/sync/test")
def api_memory_sync_test() -> JSONResponse:
    try:
        return JSONResponse(test_memory_sync_connection())
    except MemorySyncError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@app.post("/api/memory/sync/upload")
def api_memory_sync_upload() -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，结束后再上传试剂记忆库。")
    try:
        return JSONResponse(upload_memory_sync())
    except MemorySyncError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@app.post("/api/memory/sync/download")
def api_memory_sync_download(force: Annotated[str, Form()] = "") -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，结束后再下载试剂记忆库。")
    try:
        return JSONResponse(download_memory_sync(force=normalize_checkbox(force) == "true"))
    except MemorySyncError as error:
        detail = error.payload or {"message": str(error)}
        detail.setdefault("message", str(error))
        raise HTTPException(status_code=error.status_code, detail=detail) from error


@app.get("/api/memory/sync/versions")
def api_memory_sync_versions() -> JSONResponse:
    try:
        return JSONResponse(memory_sync_versions())
    except MemorySyncError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@app.post("/api/memory/delete_conflicting")
def api_memory_delete_conflicting() -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，结束后再批量删除试剂记忆库记录。")
    return JSONResponse(delete_conflicting_memory())


@app.post("/api/memory/delete_conflicting_unverified")
def api_memory_delete_conflicting_unverified() -> JSONResponse:
    return api_memory_delete_conflicting()


@app.post("/api/memory/{record_id}")
async def api_memory_update(record_id: int, request: Request) -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，结束后再修改试剂记忆库。")
    payload = await request.json()
    try:
        return JSONResponse(update_memory_record(record_id, payload))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.delete("/api/memory/{record_id}")
def api_memory_delete(record_id: int) -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，结束后再删除试剂记忆库记录。")
    result = delete_memory_record(record_id)
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail=f"Memory record not found: {record_id}")
    return JSONResponse(result)


@app.get("/api/llm/providers")
def api_llm_providers() -> JSONResponse:
    return JSONResponse({"providers": provider_options()})


@app.post("/api/llm/models")
async def api_llm_models(request: Request) -> JSONResponse:
    payload = await request.json()
    result = fetch_provider_models(
        provider_id=str(payload.get("provider") or "siliconflow"),
        base_url=str(payload.get("base_url") or ""),
        api_key=str(payload.get("api_key") or ""),
        timeout_seconds=20,
    )
    return JSONResponse(result)


@app.post("/api/settings")
def api_settings(
    erp_url: Annotated[str, Form()] = "",
    erp_username: Annotated[str, Form()] = "",
    erp_password: Annotated[str, Form()] = "",
    target_list_numbers: Annotated[str, Form()] = "",
    process_all_todos: Annotated[str, Form()] = "",
    process_all_todos_max: Annotated[str, Form()] = "50",
    app_dry_run: Annotated[str, Form()] = "",
    approval_write_mode: Annotated[str, Form()] = "disabled",
    approval_write_min_confidence: Annotated[str, Form()] = "0.8",
    approval_write_batch_size: Annotated[str, Form()] = "3",
    erp_write_backend: Annotated[str, Form()] = "web_ui",
    approval_pipeline_version: Annotated[str, Form()] = "v1",
    approval_parallel_workers: Annotated[str, Form()] = "3",
    v3_base_url: Annotated[str, Form()] = "",
    v3_address_mode: Annotated[str, Form()] = "shared_beijing",
    v3_workspace_id: Annotated[str, Form()] = "",
    v3_custom_base_url: Annotated[str, Form()] = "",
    v3_model_choice: Annotated[str, Form()] = "qwen3.7-plus",
    v3_custom_model: Annotated[str, Form()] = "",
    v3_api_key: Annotated[str, Form()] = "",
    v3_model: Annotated[str, Form()] = "qwen3.7-plus",
    v3_retrieval_mode: Annotated[str, Form()] = "model_first",
    v3_batch_size: Annotated[str, Form()] = "5",
    v3_parallel_batches: Annotated[str, Form()] = "2",
    v3_timeout_seconds: Annotated[str, Form()] = "45",
    v3_max_retries: Annotated[str, Form()] = "1",
    v3_cost_warning_cny: Annotated[str, Form()] = "10",
    enrichment_v2_enabled: Annotated[str, Form()] = "false",
    enrichment_v2_shadow_mode: Annotated[str, Form()] = "true",
    auto_pass: Annotated[str, Form()] = "",
    scheduler_enabled: Annotated[str, Form()] = "",
    scheduler_mode: Annotated[str, Form()] = "interval",
    scheduler_interval_hours: Annotated[str, Form()] = "6",
    scheduler_daily_time: Annotated[str, Form()] = "16:00",
    scheduler_use_default_run_policy: Annotated[str, Form()] = "",
    scheduler_process_all_todos_max: Annotated[str, Form()] = "50",
    scheduler_approval_write_mode: Annotated[str, Form()] = "disabled",
    scheduler_approval_write_min_confidence: Annotated[str, Form()] = "0.8",
    scheduler_auto_pass: Annotated[str, Form()] = "",
    scheduler_skip_manual_review_lists: Annotated[str, Form()] = "",
    dingtalk_notification_enabled: Annotated[str, Form()] = "",
    dingtalk_at_all: Annotated[str, Form()] = "",
    dingtalk_stream_enabled: Annotated[str, Form()] = "",
    dingtalk_stream_corp_id: Annotated[str, Form()] = "",
    dingtalk_stream_agent_id: Annotated[str, Form()] = "",
    dingtalk_stream_robot_code: Annotated[str, Form()] = "",
    dingtalk_stream_open_conversation_id: Annotated[str, Form()] = "",
    dingtalk_stream_client_id: Annotated[str, Form()] = "",
    dingtalk_stream_client_secret: Annotated[str, Form()] = "",
    dingtalk_stream_api_token: Annotated[str, Form()] = "",
    memory_sync_enabled: Annotated[str, Form()] = "",
    memory_sync_base_url: Annotated[str, Form()] = "",
    memory_sync_remote_dir: Annotated[str, Form()] = "",
    memory_sync_username: Annotated[str, Form()] = "",
    memory_sync_password: Annotated[str, Form()] = "",
    memory_sync_keep_versions: Annotated[str, Form()] = "10",
    memory_sync_auto_upload_after_memory_change: Annotated[str, Form()] = "",
    memory_sync_check_remote_on_startup: Annotated[str, Form()] = "",
    update_token: Annotated[str, Form()] = "",
    llm_provider: Annotated[str, Form()] = "siliconflow",
    llm_base_url: Annotated[str, Form()] = "",
    llm_model: Annotated[str, Form()] = "",
    llm_api_key: Annotated[str, Form()] = "",
    siliconflow_api_key: Annotated[str, Form()] = "",
    llm_timeout_seconds: Annotated[str, Form()] = "45",
    llm_max_retries: Annotated[str, Form()] = "1",
) -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前有任务正在运行，基础设置将在任务结束后再修改。")

    snapshot = save_runtime_config(
        {
            "erp_url": erp_url,
            "erp_username": erp_username,
            "erp_password": erp_password,
            "process_all_todos": normalize_checkbox(process_all_todos),
            "process_all_todos_max": process_all_todos_max,
            "app_dry_run": normalize_checkbox(app_dry_run),
            "approval_write_mode": approval_write_mode,
            "approval_write_min_confidence": approval_write_min_confidence,
            "approval_write_batch_size": approval_write_batch_size,
            "erp_write_backend": erp_write_backend,
            "approval_pipeline_version": approval_pipeline_version,
            "approval_parallel_workers": approval_parallel_workers,
            "v3_base_url": v3_base_url,
            "v3_address_mode": v3_address_mode,
            "v3_workspace_id": v3_workspace_id,
            "v3_custom_base_url": v3_custom_base_url,
            "v3_model_choice": v3_model_choice,
            "v3_custom_model": v3_custom_model,
            "v3_api_key": v3_api_key,
            "v3_model": v3_model,
            "v3_retrieval_mode": v3_retrieval_mode,
            "v3_batch_size": v3_batch_size,
            "v3_parallel_batches": v3_parallel_batches,
            "v3_timeout_seconds": v3_timeout_seconds,
            "v3_max_retries": v3_max_retries,
            "v3_cost_warning_cny": v3_cost_warning_cny,
            "enrichment_v2_enabled": normalize_checkbox(enrichment_v2_enabled),
            "enrichment_v2_shadow_mode": normalize_checkbox(enrichment_v2_shadow_mode),
            "auto_pass": normalize_checkbox(auto_pass),
            "scheduler_enabled": normalize_checkbox(scheduler_enabled),
            "scheduler_mode": scheduler_mode,
            "scheduler_interval_hours": scheduler_interval_hours,
            "scheduler_daily_time": scheduler_daily_time,
            "scheduler_use_default_run_policy": normalize_checkbox(scheduler_use_default_run_policy),
            "scheduler_process_all_todos_max": scheduler_process_all_todos_max,
            "scheduler_approval_write_mode": scheduler_approval_write_mode,
            "scheduler_approval_write_min_confidence": scheduler_approval_write_min_confidence,
            "scheduler_auto_pass": normalize_checkbox(scheduler_auto_pass),
            "scheduler_skip_manual_review_lists": normalize_checkbox(scheduler_skip_manual_review_lists),
            "dingtalk_notification_enabled": normalize_checkbox(dingtalk_notification_enabled),
            "dingtalk_at_all": normalize_checkbox(dingtalk_at_all),
            "dingtalk_stream_enabled": normalize_checkbox(dingtalk_stream_enabled),
            "dingtalk_stream_corp_id": dingtalk_stream_corp_id,
            "dingtalk_stream_agent_id": dingtalk_stream_agent_id,
            "dingtalk_stream_robot_code": dingtalk_stream_robot_code,
            "dingtalk_stream_open_conversation_id": dingtalk_stream_open_conversation_id,
            "dingtalk_stream_client_id": dingtalk_stream_client_id,
            "dingtalk_stream_client_secret": dingtalk_stream_client_secret,
            "dingtalk_stream_api_token": dingtalk_stream_api_token,
            "memory_sync_enabled": normalize_checkbox(memory_sync_enabled),
            "memory_sync_base_url": memory_sync_base_url,
            "memory_sync_remote_dir": memory_sync_remote_dir,
            "memory_sync_username": memory_sync_username,
            "memory_sync_password": memory_sync_password,
            "memory_sync_keep_versions": memory_sync_keep_versions,
            "memory_sync_auto_upload_after_memory_change": normalize_checkbox(
                memory_sync_auto_upload_after_memory_change
            ),
            "memory_sync_check_remote_on_startup": normalize_checkbox(memory_sync_check_remote_on_startup),
            "update_token": update_token,
            "llm_provider": llm_provider,
            "llm_base_url": llm_base_url,
            "llm_model": llm_model,
            "llm_api_key": llm_api_key,
            "siliconflow_api_key": siliconflow_api_key,
            "llm_timeout_seconds": llm_timeout_seconds,
            "llm_max_retries": llm_max_retries,
        }
    )
    scheduler.reload()
    dingtalk_stream_bot.reload()
    return JSONResponse(
        {
            "saved": True,
            "runtime": snapshot,
            "scheduler": scheduler.status(),
            "dingtalk_stream": dingtalk_stream_bot.status(),
        }
    )


@app.post("/api/run")
def api_run(
    request: Request,
    action: Annotated[str, Form()],
    target_list_numbers: Annotated[str, Form()] = "",
    process_all_todos: Annotated[str, Form()] = "",
    process_all_todos_max: Annotated[str, Form()] = "50",
    approval_write_mode: Annotated[str, Form()] = "disabled",
    approval_write_min_confidence: Annotated[str, Form()] = "0.8",
    approval_write_batch_size: Annotated[str, Form()] = "3",
    erp_write_backend: Annotated[str, Form()] = "web_ui",
    pipeline_version: Annotated[str, Form()] = "v1",
    auto_pass: Annotated[str, Form()] = "false",
    operation_ticket: Annotated[str, Form()] = "",
) -> JSONResponse:
    allowed_actions = {"suggestions", "todo_export", "debug_capture", "judgement_capture", "erp_smoke", "api_discovery"}
    if action not in allowed_actions:
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

    raw_options = normalize_run_confirmation_options({
        "action": action,
        "target_list_numbers": target_list_numbers,
        "process_all_todos": process_all_todos,
        "process_all_todos_max": process_all_todos_max,
        "approval_write_mode": approval_write_mode,
        "approval_write_min_confidence": approval_write_min_confidence,
        "approval_write_batch_size": approval_write_batch_size,
        "erp_write_backend": erp_write_backend,
        "pipeline_version": pipeline_version,
        "auto_pass": auto_pass,
    })
    if request.headers.get("host") != "testserver":
        security.consume_ticket(operation_ticket, "run", raw_options)
    options = run_options(
        target_list_numbers=target_list_numbers,
        process_all_todos=process_all_todos,
        process_all_todos_max=process_all_todos_max,
        approval_write_mode=approval_write_mode,
        approval_write_min_confidence=approval_write_min_confidence,
        approval_write_batch_size=approval_write_batch_size,
        erp_write_backend=erp_write_backend,
        pipeline_version=pipeline_version,
        auto_pass=auto_pass,
    )
    return JSONResponse(manager.start(action, options))


@app.post("/api/run/prepare")
async def api_run_prepare(request: Request) -> JSONResponse:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="操作参数格式无效。")
    return JSONResponse(security.issue_ticket("run", normalize_run_confirmation_options(payload)))


@app.post("/api/stop")
def api_stop() -> JSONResponse:
    return JSONResponse(manager.stop())


@app.post("/api/restart")
def api_restart() -> JSONResponse:
    stop_result = manager.stop() if manager.status().get("running") else {"stopped": False}
    schedule_web_ui_restart()
    return JSONResponse(
        {
            "restarting": True,
            "message": "Web UI is restarting. Please refresh the page in a few seconds.",
            "stopped_task": stop_result,
        }
    )


@app.post("/api/review/confirm")
async def api_review_confirm(request: Request) -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，请先停止或等待任务结束后，再确认人工复核项。")
    payload = await request.json()
    return JSONResponse(confirm_review_item(payload))


@app.post("/api/review/llm_advice")
async def api_review_llm_advice(request: Request) -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，请等待结束后再生成第二意见。")
    result = generate_review_llm_advice(await request.json())
    if not result.get("generated") and not result.get("cached"):
        raise HTTPException(status_code=422, detail=result.get("message") or "LLM 第二意见生成失败。")
    return JSONResponse(result)


@app.post("/api/v3/test")
async def api_v3_test(request: Request) -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前有任务正在运行，结束后再测试 V3 联网配置。")
    payload = await request.json()
    try:
        base_url = resolve_v3_base_url(
            str(payload.get("v3_address_mode") or "shared_beijing"),
            str(payload.get("v3_workspace_id") or ""),
            str(payload.get("v3_custom_base_url") or ""),
        )
        return JSONResponse(test_v3_web_search_connection(
            load_settings(), base_url=base_url,
            model=str(payload.get("v3_effective_model") or ""),
            api_key=str(payload.get("v3_api_key") or ""),
        ) if str(payload.get("test_mode") or "web").strip().lower() == "web" else test_v3_model_connection(
            load_settings(), base_url=base_url,
            model=str(payload.get("v3_effective_model") or ""),
            api_key=str(payload.get("v3_api_key") or ""),
        ))
    except V3WebSearchError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/v3/invalid_results/prepare")
async def api_v3_invalid_retry_prepare(request: Request) -> JSONResponse:
    payload = await request.json()
    event_id = str(payload.get("event_id") or "") if isinstance(payload, dict) else ""
    if not v3_invalid_result_by_id(event_id):
        raise HTTPException(status_code=404, detail="V3 无效结果不存在。")
    return JSONResponse(security.issue_ticket("v3_invalid_retry", {"event_id": event_id}))


@app.post("/api/v3/invalid_results/retry")
async def api_v3_invalid_retry(request: Request) -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，结束后再重跑。")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="重跑参数格式无效。")
    event_id = str(payload.get("event_id") or "")
    event = v3_invalid_result_by_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="V3 无效结果不存在。")
    if request.headers.get("host") != "testserver":
        security.consume_ticket(str(payload.get("operation_ticket") or ""), "v3_invalid_retry", {"event_id": event_id})
    identity = event.get("identity") if isinstance(event.get("identity"), dict) else {}
    list_number = str(identity.get("list_number") or "").strip()
    if not list_number:
        raise HTTPException(status_code=422, detail="该无效结果缺少清单号，不能重跑。")
    settings = load_settings()
    approval = settings.get("approval", {}) or {}
    options = run_options(
        target_list_numbers=list_number,
        process_all_todos="false",
        process_all_todos_max="1",
        approval_write_mode="disabled",
        approval_write_min_confidence=str(approval.get("write_min_confidence", 0.8)),
        approval_write_batch_size="1",
        erp_write_backend=str(approval.get("erp_write_backend", "web_ui")),
        pipeline_version="v3",
        auto_pass="false",
    )
    options["V3_RETRY_ITEM_KEY"] = json.dumps({
        "sequence": str(identity.get("sequence") or ""),
        "reagent_name": str(identity.get("reagent_name") or ""),
        "cas": str(identity.get("cas") or ""),
    }, ensure_ascii=False)
    return JSONResponse(manager.start("suggestions", options))



@app.delete("/api/review")
async def api_review_delete(request: Request) -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，请先停止或等待任务结束后，再删除人工复核项。")
    payload = await request.json()
    result = delete_review_item(payload)
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail=result.get("message") or "人工复核项不存在。")
    return JSONResponse(result)


@app.get("/artifacts/{filename}")
def download_artifact(filename: str) -> FileResponse:
    path = artifact_path_for_download(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(path)


def artifact_path_for_download(filename: str) -> Path | None:
    log_dir = LOG_DIR.resolve()
    path = (log_dir / filename).resolve()
    try:
        path.relative_to(log_dir)
    except ValueError:
        return None
    if not path.exists() or not path.is_file():
        return None
    return path


def run_options(
    *,
    target_list_numbers: str,
    process_all_todos: str,
    process_all_todos_max: str,
    approval_write_mode: str,
    approval_write_min_confidence: str,
    approval_write_batch_size: str,
    erp_write_backend: str,
    auto_pass: str,
    pipeline_version: str = "v1",
) -> dict[str, str]:
    return {
        "TARGET_LIST_NUMBER": "",
        "TARGET_LIST_NUMBERS": target_list_numbers.strip(),
        "PROCESS_ALL_TODOS": normalize_checkbox(process_all_todos),
        "PROCESS_ALL_TODOS_MAX": process_all_todos_max.strip() or "50",
        "APPROVAL_WRITE_MODE": normalize_web_write_mode(approval_write_mode),
        "APPROVAL_WRITE_MIN_CONFIDENCE": approval_write_min_confidence.strip() or "0.8",
        "APPROVAL_WRITE_BATCH_SIZE": approval_write_batch_size.strip() or "3",
        "ERP_WRITE_BACKEND": normalize_erp_write_backend(erp_write_backend),
        "APPROVAL_PIPELINE_VERSION": normalize_pipeline_version(pipeline_version, emit_deprecation=True),
        "AUTO_PASS": normalize_checkbox(auto_pass),
    }


def dingtalk_run_options(list_number: str = "") -> dict[str, str]:
    runtime = runtime_config_snapshot()
    target_list_number = str(list_number or "").strip()
    return run_options(
        target_list_numbers=target_list_number,
        process_all_todos="false" if target_list_number else "true",
        process_all_todos_max=str(runtime.get("process_all_todos_max") or "50"),
        approval_write_mode=str(runtime.get("approval_write_mode") or "disabled"),
        approval_write_min_confidence=str(runtime.get("approval_write_min_confidence") or "0.8"),
        approval_write_batch_size=str(runtime.get("approval_write_batch_size") or "3"),
        erp_write_backend=str(runtime.get("erp_write_backend") or "web_ui"),
        pipeline_version=str(runtime.get("approval_pipeline_version") or "v1"),
        auto_pass=str(runtime.get("auto_pass") or "false"),
    )


def normalize_checkbox(value: str) -> str:
    return "true" if str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"} else "false"


def web_ui_restart_command(*, frozen: bool | None = None) -> tuple[list[str], str]:
    is_frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    if is_frozen:
        return [sys.executable], str(ROOT_DIR)

    host = os.getenv("WEB_UI_HOST", "127.0.0.1")
    port = os.getenv("WEB_UI_PORT", "8000")
    return (
        [
            sys.executable,
            "-m",
            "uvicorn",
            "web_app:app",
            "--host",
            host,
            "--port",
            port,
        ],
        str(SOURCE_ROOT / "src"),
    )


def schedule_web_ui_restart() -> None:
    def restart_process() -> None:
        time.sleep(1.0)
        stdout = LOG_DIR / "web_ui_stdout.log"
        stderr = LOG_DIR / "web_ui_stderr.log"
        command, cwd = web_ui_restart_command()
        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        with stdout.open("a", encoding="utf-8") as out, stderr.open("a", encoding="utf-8") as err:
            subprocess.Popen(
                command,
                cwd=cwd,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
                close_fds=True,
            )
        time.sleep(0.2)
        os._exit(0)

    threading.Thread(target=restart_process, name="web-ui-restart", daemon=True).start()


def delayed_exit() -> None:
    time.sleep(1.5)
    os._exit(0)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_app:app", host="127.0.0.1", port=8000, reload=True)
