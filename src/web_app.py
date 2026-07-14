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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from llm_providers import fetch_provider_models, provider_options
from scheduler import ApprovalScheduler
from update_checker import check_for_update, current_process_is_frozen, download_update, launch_installer
from web_runner import (
    ENV_PATH,
    ROOT_DIR,
    approval_summary,
    artifact_summary,
    confirm_review_item,
    delete_conflicting_memory,
    delete_memory_record,
    delete_review_item,
    import_approval_suggestions_to_memory,
    load_settings,
    manager,
    memory_summary,
    normalize_web_write_mode,
    review_queue_summary,
    runtime_config_snapshot,
    save_runtime_config,
    todo_tasks_summary,
    update_memory_record,
)
from runtime_paths import source_root


SOURCE_ROOT = source_root()
TEMPLATES_DIR = SOURCE_ROOT / "src" / "templates"
STATIC_DIR = SOURCE_ROOT / "src" / "static"
LOG_DIR = ROOT_DIR / "data" / "logs"

load_dotenv(ENV_PATH, override=True)
scheduler = ApprovalScheduler(root_dir=ROOT_DIR, settings_loader=load_settings, job_manager=manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title="试剂审批自动化控制台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

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


def dashboard_context(request: Request, active_page: str) -> dict:
    page = PAGE_DEFS.get(active_page, PAGE_DEFS["overview"])
    runtime = runtime_config_snapshot()
    return {
        "request": request,
        "active_page": active_page,
        "page": page,
        "pages": PAGE_DEFS,
        "runtime": runtime,
        "status": manager.status(),
        "approval": approval_summary(),
        "artifacts": artifact_summary(),
        "review_queue": review_queue_summary(),
        "todo_tasks": todo_tasks_summary(),
        "scheduler": scheduler.status(),
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
            "status": manager.status(),
            "approval": approval_summary(),
            "artifacts": artifact_summary(),
            "review_queue": review_queue_summary(),
            "todo_tasks": todo_tasks_summary(),
            "scheduler": scheduler.status(),
        }
    )


@app.get("/api/update/check")
def api_update_check() -> JSONResponse:
    return JSONResponse(check_for_update().as_dict())


@app.post("/api/update/install")
def api_update_install() -> JSONResponse:
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
                "message": "当前是源码开发模式，不能自动安装 setup.exe。请在正式安装版中使用在线更新。",
                **info.as_dict(),
            }
        )
    installer = download_update(info.asset)
    launch_installer(installer)
    threading.Thread(target=delayed_exit, name="web-ui-update-exit", daemon=True).start()
    return JSONResponse(
        {
            "started": True,
            "message": "已下载更新包并启动安装器，当前程序即将退出。",
            "installer": str(installer),
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
    approval_write_mode: Annotated[str, Form()] = "multi_page",
    approval_write_min_confidence: Annotated[str, Form()] = "0.8",
    approval_parallel_workers: Annotated[str, Form()] = "3",
    auto_pass: Annotated[str, Form()] = "",
    scheduler_enabled: Annotated[str, Form()] = "",
    scheduler_mode: Annotated[str, Form()] = "interval",
    scheduler_interval_hours: Annotated[str, Form()] = "6",
    scheduler_daily_time: Annotated[str, Form()] = "16:00",
    scheduler_use_default_run_policy: Annotated[str, Form()] = "",
    scheduler_process_all_todos_max: Annotated[str, Form()] = "50",
    scheduler_approval_write_mode: Annotated[str, Form()] = "multi_page",
    scheduler_approval_write_min_confidence: Annotated[str, Form()] = "0.8",
    scheduler_auto_pass: Annotated[str, Form()] = "",
    scheduler_skip_manual_review_lists: Annotated[str, Form()] = "",
    dingtalk_notification_enabled: Annotated[str, Form()] = "",
    dingtalk_webhook: Annotated[str, Form()] = "",
    dingtalk_secret: Annotated[str, Form()] = "",
    dingtalk_at_all: Annotated[str, Form()] = "",
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
            "approval_write_mode": approval_write_mode,
            "approval_write_min_confidence": approval_write_min_confidence,
            "approval_parallel_workers": approval_parallel_workers,
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
            "dingtalk_webhook": dingtalk_webhook,
            "dingtalk_secret": dingtalk_secret,
            "dingtalk_at_all": normalize_checkbox(dingtalk_at_all),
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
    return JSONResponse({"saved": True, "runtime": snapshot, "scheduler": scheduler.status()})


@app.post("/api/run")
def api_run(
    action: Annotated[str, Form()],
    target_list_numbers: Annotated[str, Form()] = "",
    process_all_todos: Annotated[str, Form()] = "",
    process_all_todos_max: Annotated[str, Form()] = "50",
    approval_write_mode: Annotated[str, Form()] = "multi_page",
    approval_write_min_confidence: Annotated[str, Form()] = "0.8",
    auto_pass: Annotated[str, Form()] = "false",
) -> JSONResponse:
    allowed_actions = {"suggestions", "todo_export", "debug_capture", "judgement_capture"}
    if action not in allowed_actions:
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

    options = run_options(
        target_list_numbers=target_list_numbers,
        process_all_todos=process_all_todos,
        process_all_todos_max=process_all_todos_max,
        approval_write_mode=approval_write_mode,
        approval_write_min_confidence=approval_write_min_confidence,
        auto_pass=auto_pass,
    )
    return JSONResponse(manager.start(action, options))


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
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，结束后再确认人工复核项。")
    payload = await request.json()
    return JSONResponse(confirm_review_item(payload))


@app.delete("/api/review")
async def api_review_delete(request: Request) -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，结束后再删除人工复核项。")
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
    auto_pass: str,
) -> dict[str, str]:
    return {
        "TARGET_LIST_NUMBER": "",
        "TARGET_LIST_NUMBERS": target_list_numbers.strip(),
        "PROCESS_ALL_TODOS": normalize_checkbox(process_all_todos),
        "PROCESS_ALL_TODOS_MAX": process_all_todos_max.strip() or "50",
        "APPROVAL_WRITE_MODE": normalize_web_write_mode(approval_write_mode),
        "APPROVAL_WRITE_MIN_CONFIDENCE": approval_write_min_confidence.strip() or "0.8",
        "AUTO_PASS": normalize_checkbox(auto_pass),
    }


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
