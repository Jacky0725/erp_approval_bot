# 项目更新与维护进度记录（2026-11-26）

> 基准版本：`1.0.0`（分支：`main`，接续 `docs/UPDATE_PROGRESS_20261125.md`）

---

## 1. 本轮改动内容

- **Web UI 默认开启 WorkBuddy 正式写入 `auto_pass`**：[`src/web_app.py`](file:///d:/codex/reagent-approval-bot/src/web_app.py) 的 `api_run()` 在 `action == "workbuddy_loop"` 且处于正式写入模式（`not dry_run` 且 `APPROVAL_WRITE_MODE != "disabled"`）时，默认设置 `WORKBUDDY_AUTO_PASS=true` 与 `AUTO_PASS=true`；试运行或禁用写入模式仍强制为 `false`。
- **定时任务默认开启 WorkBuddy 正式写入 `auto_pass`**：[`src/scheduler.py`](file:///d:/codex/reagent-approval-bot/src/scheduler.py) 在 `workbuddy_preflight` 通过且处于正式写入模式时，默认设置 `WORKBUDDY_AUTO_PASS=true` 与 `AUTO_PASS=true`；当回退 V1 链路或试运行/禁用写入模式时，仍保持 `AUTO_PASS=false`。
- **控制台说明同步**：更新 [`src/templates/partials/run.html`](file:///d:/codex/reagent-approval-bot/src/templates/partials/run.html) 中的 WorkBuddy 执行与写入说明。
- **单元测试补充**：在 [`tests/test_workbuddy_flow.py`](file:///d:/codex/reagent-approval-bot/tests/test_workbuddy_flow.py) 和 [`tests/test_scheduler.py`](file:///d:/codex/reagent-approval-bot/tests/test_scheduler.py) 补充正式写入开启与试运行/禁用写入关闭的回归用例。

---

## 2. 验证记录

| 检查项 | 执行命令 | 结果 |
| :--- | :--- | :--- |
| 字节码编译 | `python -m compileall -q src` | **已通过** |
| 关联单元测试 | `python -m pytest -q tests/test_workbuddy_flow.py tests/test_scheduler.py tests/test_workbuddy_autopass.py tests/test_packaged_runtime_defaults.py` | **已通过**（`113 passed`） |
| 真实 ERP 实测 | — | **未执行**（待重启后台 Web UI 进程后验收） |

---

## 3. 待办与后续跟进

- [ ] 核对 `SJ202611240003` 当前 ERP 待办状态；若已手动通过则无需处理，若仍在待办可手工通过或备份后清理 `data/dingtalk_state.sqlite3` 中对应 `needs_review` 锁定记录。
- [ ] 重启本地 Web UI / 钉钉机器人后台进程，使 11-25 弹窗确认修复与 11-26 全入口默认 `auto_pass` 生效。
- [ ] 在真实 ERP 环境下实测验收一次 WorkBuddy 整单自动通过闭环。
- [ ] 确认无误后推送本地 `main` 分支至远端。
