# 项目更新与维护进度记录（2026-11-26）

> **基准版本**：`1.0.0`（分支：`main`，最新代码提交 `f558158`，接续 `docs/UPDATE_PROGRESS_20261125.md`）

---

## 1. 本日提交清单

| 顺序 | Commit | 类型 | 摘要 |
| :---: | :--- | :--- | :--- |
| 1 | `350d059` | 功能配置 | Web UI 与定时任务在 WorkBuddy 正式写入模式下默认开启 `auto_pass`（试运行及回退 V1 仍强制关闭） |
| 2 | `6f31113` | 阶段 1 | 新增只读无头 CodeBuddy CLI 适配器 `src/workbuddy_headless.py`、15 项单元测试与 v2 集成方案文档 |
| 3 | `0bb8167` | 阶段 2 | 接入 `approval_flow.py`、`scheduler.py`、`web_app.py`、`web_runner.py`、`run.html`、`build_runtime_defaults.py` 双模分流与崩溃恢复（默认保持 `desktop`，`workbuddy_desktop.py`/`workbuddy_recovery.py` 零修改） |
| 4 | `6e5e520` | 阶段 3 | 记录 20 条（105.8s）与 50 条（334.4s）脱敏全类别隔离验收结果 |
| 5 | `aa2fb5c` | 阶段 4 | 用户确认 50 条分类均符合业务预期后，将本机 `config/settings.yaml` 切换为 `dispatch_mode: headless`（`auto_pass_enabled: false`），并重启本地 Web UI 生效 |
| 6 | `f558158` | Web UI | 运行页 `/run` 新增当前投递模式徽标（`CodeBuddy CLI 无头模式` / `WorkBuddy 桌面客户端`），配置页 `/settings` 新增投递模式、无头模型、无头开关与无头自动通过开关并重启生效 |

---

## 2. WorkBuddy / CodeBuddy 无头模式验收与生效状态

- **CLI 专项实测（`@tencent-ai/codebuddy-code@2.187.0`）**：
  - UTF-8 stdin 管道 62KB 中文长输入：`exit=0`，耗时 **6.5s**。
  - `--tools Read,WebSearch,WebFetch,StructuredOutput` + `--json-schema` + `--strict-mcp-config`：`exit=0`，耗时 **7.1s**，原生返回 `structured_output`。
- **脱敏大批次隔离验收（零 ERP / 零记忆库写入）**：
  - **20 条批次**（`20261126_125424_a3021682`）：耗时 **105.8s**（预算 900s），`is_error=False`，`permission_denials=0`，`unmapped=0`，`WorkBuddyBridge.load_results()` 100% 通过。
  - **50 条批次**（`20261126_125641_ea75aea5`）：耗时 **334.4s**（约 5.6 分钟，预算 1,500s），`is_error=False`，`permission_denials=0`，`unmapped=0`，覆盖 13 个 ERP 类别，经用户抽检确认全部符合 `rules_structured.xlsx` 业务预期。
- **本机当前实时运行状态**：
  - `http://127.0.0.1:8000` 已重启并健康运行；
  - `workbuddy.dispatch_mode = headless`、`workbuddy.headless.enabled = true`、`workbuddy.headless.auto_pass_enabled = false`；
  - `headless_preflight`、`scheduler_preflight`、`dingtalk_run_options` 及 `/run`、`/settings` 实时页面均已验证通过。

---

## 3. 验证记录

| 检查项 | 执行命令 / 方式 | 结果 |
| :--- | :--- | :--- |
| 字节码编译 | `python -m compileall -q src packaging\windows` | **已通过** |
| 无头适配器与 UI 专项测试 | `python -m pytest -q tests/test_workbuddy_headless.py tests/test_dashboard_requests.py` | **已通过**（`23 passed`） |
| WorkBuddy / 自动通过 / 调度 / 打包回归 | `python -m pytest -q tests/test_workbuddy_flow.py tests/test_workbuddy_autopass.py tests/test_scheduler.py tests/test_packaged_runtime_defaults.py` | **已通过** |
| 本地 Web UI 实时页面校验 | 重启后校验 `GET /api/status`、`/run`、`/settings` | **已通过** |
| 真实 ERP 锁屏跑批与无头整单通过实测 | — | **未执行**（待实测验收） |

---

## 4. 待办与后续跟进

- [x] 完成 50 条无头批次业务抽检确认与本机切换 `dispatch_mode: headless`。
- [x] 重启本地 Web UI 进程，并在 `/run` 与 `/settings` 展示 CodeBuddy 无头模式状态与切换开关。
- [ ] 在真实 ERP 环境下（含锁屏状态）实测一次无头模式正式分类写入，验收端到端稳定性。
- [ ] 无头分类写入验收无误后，按需在 `/settings` 开启 `无头模式允许整单自动通过` 并实测一次「审批结果」二级弹窗自动确认闭环。
- [ ] 核对 `SJ202611240003` 当前 ERP 待办状态；若已手动通过则无需处理，若仍在待办可手工通过或备份后清理 `data/dingtalk_state.sqlite3` 中对应 `needs_review` 锁定记录。
- [ ] 确认无误后推送本地 `main` 分支至远端。
