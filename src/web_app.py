from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web_runner import ROOT_DIR, approval_summary, artifact_summary, manager, runtime_config_snapshot


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
        }
    )


@app.post("/api/run")
def api_run(
    action: Annotated[str, Form()],
    target_list_number: Annotated[str, Form()] = "",
    process_all_todos: Annotated[str, Form()] = "",
    process_all_todos_max: Annotated[str, Form()] = "50",
    approval_write_mode: Annotated[str, Form()] = "disabled",
    approval_write_min_confidence: Annotated[str, Form()] = "0.8",
    auto_pass: Annotated[str, Form()] = "false",
) -> JSONResponse:
    allowed_actions = {"suggestions", "todo_export", "debug_capture", "judgement_capture"}
    if action not in allowed_actions:
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

    options = {
        "TARGET_LIST_NUMBER": target_list_number.strip(),
        "PROCESS_ALL_TODOS": normalize_checkbox(process_all_todos),
        "PROCESS_ALL_TODOS_MAX": process_all_todos_max.strip() or "50",
        "APPROVAL_WRITE_MODE": approval_write_mode.strip() or "disabled",
        "APPROVAL_WRITE_MIN_CONFIDENCE": approval_write_min_confidence.strip() or "0.8",
        "AUTO_PASS": normalize_checkbox(auto_pass),
    }
    return JSONResponse(manager.start(action, options))


@app.get("/artifacts/{filename}")
def download_artifact(filename: str) -> FileResponse:
    path = (LOG_DIR / filename).resolve()
    log_dir = LOG_DIR.resolve()
    if not str(path).startswith(str(log_dir)) or not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(path)


def normalize_checkbox(value: str) -> str:
    return "true" if str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"} else "false"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_app:app", host="127.0.0.1", port=8000, reload=True)
