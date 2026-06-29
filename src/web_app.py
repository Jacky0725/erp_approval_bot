from __future__ import annotations

import os
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

from llm_providers import fetch_provider_models, provider_options
from web_runner import (
    ROOT_DIR,
    approval_summary,
    artifact_summary,
    confirm_review_item,
    delete_memory_record,
    import_approval_suggestions_to_memory,
    manager,
    memory_summary,
    review_queue_summary,
    runtime_config_snapshot,
    save_runtime_config,
    todo_tasks_summary,
    update_memory_record,
)


TEMPLATES_DIR = ROOT_DIR / "src" / "templates"
STATIC_DIR = ROOT_DIR / "src" / "static"
LOG_DIR = ROOT_DIR / "data" / "logs"

app = FastAPI(title="试剂审批自动化控制台")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "runtime": runtime_config_snapshot(),
            "status": manager.status(),
            "approval": approval_summary(),
            "artifacts": artifact_summary(),
            "review_queue": review_queue_summary(),
            "todo_tasks": todo_tasks_summary(),
        },
    )


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
        }
    )


@app.get("/api/memory")
def api_memory(
    q: str = "",
    category: str = "",
    reusable: str = "",
    conflict: str = "",
    limit: int = 200,
) -> JSONResponse:
    return JSONResponse(
        memory_summary(
            query=q,
            category=category,
            reusable=reusable,
            conflict=conflict,
            limit=limit,
        )
    )


@app.post("/api/memory/import_suggestions")
def api_memory_import_suggestions() -> JSONResponse:
    if manager.status().get("running"):
        raise HTTPException(status_code=409, detail="当前自动化任务正在运行，结束后再导入历史审批建议。")
    return JSONResponse(import_approval_suggestions_to_memory())


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
    approval_write_mode: Annotated[str, Form()] = "disabled",
    approval_write_min_confidence: Annotated[str, Form()] = "0.8",
    approval_parallel_workers: Annotated[str, Form()] = "3",
    auto_pass: Annotated[str, Form()] = "",
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
            "llm_provider": llm_provider,
            "llm_base_url": llm_base_url,
            "llm_model": llm_model,
            "llm_api_key": llm_api_key,
            "siliconflow_api_key": siliconflow_api_key,
            "llm_timeout_seconds": llm_timeout_seconds,
            "llm_max_retries": llm_max_retries,
        }
    )
    return JSONResponse({"saved": True, "runtime": snapshot})


@app.post("/api/run")
def api_run(
    action: Annotated[str, Form()],
    target_list_numbers: Annotated[str, Form()] = "",
    process_all_todos: Annotated[str, Form()] = "",
    process_all_todos_max: Annotated[str, Form()] = "50",
    approval_write_mode: Annotated[str, Form()] = "disabled",
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


@app.get("/artifacts/{filename}")
def download_artifact(filename: str) -> FileResponse:
    path = (LOG_DIR / filename).resolve()
    log_dir = LOG_DIR.resolve()
    if not str(path).startswith(str(log_dir)) or not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(path)


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
        "APPROVAL_WRITE_MODE": approval_write_mode.strip() or "disabled",
        "APPROVAL_WRITE_MIN_CONFIDENCE": approval_write_min_confidence.strip() or "0.8",
        "AUTO_PASS": normalize_checkbox(auto_pass),
    }


def normalize_checkbox(value: str) -> str:
    return "true" if str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"} else "false"


def schedule_web_ui_restart() -> None:
    def restart_process() -> None:
        time.sleep(1.0)
        stdout = LOG_DIR / "web_ui_stdout.log"
        stderr = LOG_DIR / "web_ui_stderr.log"
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        with stdout.open("a", encoding="utf-8") as out, stderr.open("a", encoding="utf-8") as err:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "web_app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                ],
                cwd=str(ROOT_DIR / "src"),
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )
        time.sleep(0.2)
        os._exit(0)

    threading.Thread(target=restart_process, name="web-ui-restart", daemon=True).start()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_app:app", host="127.0.0.1", port=8000, reload=True)
