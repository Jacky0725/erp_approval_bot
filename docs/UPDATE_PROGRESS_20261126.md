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


---

## 4. WorkBuddy 无头模式阶段 1 进展（2026-11-26 下午）

- **方案交叉审查与修订**：吸收 Codex 审查意见，更新 [`docs/WORKBUDDY_HEADLESS_INTEGRATION_PLAN_20261126.md`](file:///d:/codex/reagent-approval-bot/docs/WORKBUDDY_HEADLESS_INTEGRATION_PLAN_20261126.md) 为 v2 修订版（明确现有桌面功能零影响、批次级 `dispatch_mode`/`headless_pid` 崩溃恢复、最小数据范围 `--add-dir` + `--strict-mcp-config`、复用摘要冻结原子发布、首轮不开放自动通过）。
- **CLI 专项技术验证（固定版本 `@tencent-ai/codebuddy-code@2.187.0`）**：
  - UTF-8 stdin 管道 62KB 中文长输入：`exit=0`，耗时 **6.5s**，完整准确返回。
  - `--tools Read,WebSearch,WebFetch,StructuredOutput` + `--json-schema` + `--strict-mcp-config`：`exit=0`，耗时 **7.1s**，原生返回 `structured_output`。
- **阶段 1 隔离实现**：新增 [`src/workbuddy_headless.py`](file:///d:/codex/reagent-approval-bot/src/workbuddy_headless.py) 与 [`tests/test_workbuddy_headless.py`](file:///d:/codex/reagent-approval-bot/tests/test_workbuddy_headless.py)；未修改任何现有业务流程、桌面适配器或默认配置。

- **阶段 2 双模流程接入**：已将 `workbuddy_headless` 双模分流接入 `src/approval_flow.py`、`src/scheduler.py`、`src/web_app.py`、`src/web_runner.py`、`src/templates/partials/run.html` 和 `packaging/windows/build_runtime_defaults.py`；默认保持 `dispatch_mode="desktop"`、`headless.enabled=False`、`headless.auto_pass_enabled=False`，`workbuddy_desktop.py` 与 `workbuddy_recovery.py` 零修改。
