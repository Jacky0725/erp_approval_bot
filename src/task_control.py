from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from dingtalk_state import DingTalkState


SAFE_STOP_EXIT_CODE = 20
STOPPABLE_ACTIONS = ("suggestions", "workbuddy_loop", "workbuddy_prepare", "workbuddy_import")


def selected_lists(options: dict[str, Any]) -> set[str]:
    if str(options.get("PROCESS_ALL_TODOS", "")).lower() in {"true", "1", "yes", "on"}:
        return set()
    value = options.get("TARGET_LIST_NUMBERS") or options.get("TARGET_LIST_NUMBER") or ""
    return {part for part in re.split(r"[,，;；\s]+", str(value)) if part}


class SafeStop(BaseException):
    """Not caught by ERP retry handlers: a checkpoint is not an ERP failure."""


def rules_digest(root: Path, settings: dict[str, Any]) -> str:
    path = Path((settings.get("paths", {}) or {}).get("structured_rules_excel", "config/rules_structured.xlsx"))
    path = path if path.is_absolute() else root / path
    if not path.is_file():
        return ""
    policy = {key: settings.get(key) for key in ("approval", "reagent", "app", "workbuddy")}
    policy["runtime"] = {key: os.getenv(key, "") for key in ("APP_DRY_RUN", "DRY_RUN", "ERP_URL")}
    return hashlib.sha256(path.read_bytes() + json.dumps(policy, sort_keys=True, default=str).encode()).hexdigest()


class TaskControl:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.storage = DingTalkState(root)

    def connect(self):
        return self.storage.connection()

    @staticmethod
    def schema(db) -> None:
        db.execute("CREATE TABLE IF NOT EXISTS task_control (id TEXT PRIMARY KEY, action TEXT NOT NULL, options TEXT NOT NULL, actor TEXT NOT NULL, state TEXT NOT NULL, phase TEXT NOT NULL, details TEXT NOT NULL, rule_hash TEXT NOT NULL, requested_by TEXT, created REAL NOT NULL)")

    def register(self, task_id: str, action: str, options: dict[str, Any], actor: dict[str, str], settings: dict[str, Any], *, resume: str = "") -> None:
        with self.connect() as db:
            self.schema(db)
            db.execute("BEGIN IMMEDIATE")
            details = {}
            if resume:
                row = db.execute("SELECT state, details, rule_hash FROM task_control WHERE id=?", (resume,)).fetchone()
                if not row or row[0] != "stopped":
                    raise ValueError("原任务并非可恢复的人工停止状态。")
                if row[2] != rules_digest(self.root, settings):
                    raise ValueError("规则、执行配置或 ERP 环境已变化，禁止直接恢复旧任务；请先人工核对。")
                details = json.loads(row[1])
                details["resume_ancestors"] = list(dict.fromkeys([*(details.get("resume_ancestors") or []), resume]))
                db.execute("UPDATE task_control SET state='resuming' WHERE id=?", (resume,))
            db.execute("INSERT INTO task_control VALUES (?, ?, ?, ?, 'running', '准备启动', ?, ?, NULL, ?)",
                       (task_id, action, json.dumps(options), json.dumps(actor), json.dumps(details), rules_digest(self.root, settings), time.time()))

    def ensure_allowed_start(self, action: str, options: dict[str, Any], resume: str = "") -> None:
        if action not in STOPPABLE_ACTIONS:
            return
        with self.connect() as db:
            self.schema(db)
            rows = db.execute("""SELECT id,options,details,state,requested_by FROM task_control
                                 WHERE action IN (?,?,?,?) AND state IN ('stopped','stop_requested','needs_review')""",
                              STOPPABLE_ACTIONS).fetchall()
        selected = selected_lists(options)
        for task_id, old_options, details, state, requested_by in rows:
            if task_id == resume:
                continue
            details, old_options = json.loads(details), json.loads(old_options)
            if state == "needs_review" and not (requested_by or details.get("resume_ancestors")):
                continue
            scope = set(details.get("scope") or []) or selected_lists(old_options)
            if not selected or not scope or selected & scope:
                raise ValueError("该范围存在人工停止或停止结果待核对的任务，禁止自动接续。请先在 Web UI 核对。")

    def get(self, task_id: str) -> dict[str, Any]:
        with self.connect() as db:
            self.schema(db)
            row = db.execute("SELECT id,action,options,actor,state,phase,details,requested_by FROM task_control WHERE id=?", (task_id,)).fetchone()
        if not row:
            return {}
        keys = ("task_id", "action", "options", "actor", "state", "phase", "details", "requested_by")
        data = dict(zip(keys, row))
        for key in ("options", "actor", "details", "requested_by"):
            data[key] = json.loads(data[key]) if data[key] else {}
        return data

    def stopped(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            self.schema(db)
            ids = db.execute("SELECT id FROM task_control WHERE state='stopped' ORDER BY created DESC LIMIT 50").fetchall()
        return [self.get(row[0]) for row in ids]

    def request(self, task_id: str, actor: dict[str, str]) -> bool:
        with self.connect() as db:
            self.schema(db)
            cursor = db.execute("UPDATE task_control SET state='stop_requested', requested_by=? WHERE id=? AND state='running'", (json.dumps(actor), task_id))
            if cursor.rowcount:
                return True
            row = db.execute("SELECT state FROM task_control WHERE id=?", (task_id,)).fetchone()
            return bool(row and row[0] == "stop_requested")

    def checkpoint(self, task_id: str, phase: str, **details: Any) -> None:
        with self.connect() as db:
            self.schema(db)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,details FROM task_control WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise RuntimeError("任务控制记录缺失，禁止继续审批。")
            merged = {**json.loads(row[1]), **details}
            db.execute("UPDATE task_control SET phase=?,details=? WHERE id=?", (phase, json.dumps(merged, ensure_ascii=False), task_id))
            stopping = row[0] == "stop_requested"
        if stopping:
            raise SafeStop(phase)

    def finish(self, task_id: str, state: str) -> None:
        with self.connect() as db:
            self.schema(db)
            db.execute("UPDATE task_control SET state=? WHERE id=?", (state, task_id))

    def record_details(self, task_id: str, **details: Any) -> None:
        with self.connect() as db:
            self.schema(db)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT details FROM task_control WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise RuntimeError("任务控制记录缺失。")
            db.execute("UPDATE task_control SET details=? WHERE id=?", (json.dumps({**json.loads(row[0]), **details}, ensure_ascii=False), task_id))

    def release_unstarted_resume(self, parent: str) -> None:
        with self.connect() as db:
            self.schema(db)
            db.execute("UPDATE task_control SET state='stopped' WHERE id=? AND state='resuming'", (parent,))


def checkpoint(phase: str, **details: Any) -> None:
    task_id = os.getenv("AUTOMATION_TASK_ID", "")
    if task_id:
        from runtime_paths import runtime_root
        TaskControl(runtime_root()).checkpoint(task_id, phase, **details)


def can_stop(task: dict[str, Any], actor: dict[str, str], settings: dict[str, Any]) -> bool:
    if actor.get("source") == "web":
        return True
    if actor.get("conversation_type") != "2" or not actor.get("user_id"):
        return False
    policy = settings.get("dingtalk_control", {}) or {}
    admin = f"{actor.get('corp_id', '')}/{actor['user_id']}" in (policy.get("admin_user_ids") or [])
    owner = task.get("actor") or {}
    same_group = bool(owner.get("conversation_id")) and actor.get("conversation_id") == owner.get("conversation_id")
    owner_match = owner.get("source") == "dingtalk" and owner.get("user_id") == actor["user_id"] and owner.get("corp_id", "") == actor.get("corp_id", "")
    return bool((same_group and (owner_match or admin)) or admin and actor.get("conversation_id") in (policy.get("management_group_ids") or []))
