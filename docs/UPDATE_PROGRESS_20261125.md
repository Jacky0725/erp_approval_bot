# 项目更新与维护进度记录（2026-11-25）

> 基准版本：`1.0.0`（分支：`main`，本地已提交至 `ed6f7ae`，尚未推送远端）

---

## 1. 本轮提交清单

| 顺序 | Commit | 类型 | 摘要 |
| :---: | :--- | :--- | :--- |
| 1 | `b82d59b` | 功能收口 | 提交既有未提交改动：WorkBuddy 整单自动通过模块（`src/workbuddy_autopass.py`）、钉钉 50 条批次配置与 Windows 自启安装器改动（18 个文件） |
| 2 | `a39988f` | 仓库卫生 | 补全 `.gitignore` 规则，移除孤立字节码 `src/v3_web_search.pyc` 与空目录 `src/chemical_sources/`，同步 `docs/RELEASE_AND_MAINTENANCE_GATES.md` 过期描述 |
| 3 | `8aab618` | 门禁拆分（Order 1） | 将 `ErpApiConfigurator` 持久化与原子配置提升逻辑从 `src/erp_api_discovery.py` 抽离至 `src/erp_api_discovery_store.py` |
| 4 | `b1ff819` | 门禁拆分（Order 2） | 将 API 写入后的页面抽样校验与灰度激活/回退逻辑从 `src/approval_flow.py` 抽离至 `src/erp_api_write_verifier.py` |
| 5 | `12ff099` | 门禁拆分（Order 3） | 将 978 行物性/危险性证据补全逻辑从 `src/chemical_searcher.py` 抽离至 `src/chemical_evidence_enrichment.py`（`ChemicalEvidenceEnrichmentMixin`） |
| 6 | `c5bafd5` | 门禁拆分（Order 4） | 将 640+ 行运行日志解析、写入计数与流程阶段追踪从 `src/web_runner.py` 抽离至 `src/run_summary_parser.py` |
| 7 | `ed6f7ae` | Bug 修复 | 修复 `WorkBuddyAutoPass` 点击右上角「通过」后未点击 ERP「审批结果」二级确认弹窗中「确 定」按钮的问题，并补充对应单元测试 |

---

## 2. 按既有门禁拆分模块进度

参照 `docs/RELEASE_AND_MAINTENANCE_GATES.md` 的 4 道增量拆分门禁，本轮已全部完成等价抽离（保持 `browser_bot`、FastAPI 路由、工作簿列和 `settings.yaml` 架构 100% 向后兼容，且每步独立可回滚）：

- **Order 1（API 探测持久化 vs 协议推断）**：已完成 → `src/erp_api_discovery_store.py`
- **Order 2（API 写入回读校验 vs 页面抽样）**：已完成 → `src/erp_api_write_verifier.py`
- **Order 3（检索身份确认 vs 物性证据补全）**：已完成 → `src/chemical_evidence_enrichment.py`
- **Order 4（运行摘要事件 vs 日志文本兼容）**：已完成 → `src/run_summary_parser.py`

---

## 3. WorkBuddy `auto_pass` 状态核查与问题修复

### 3.1 各入口生效状态
- **钉钉审批命令**：**已开启**（非试运行模式下，`src/web_app.py` 的 `dingtalk_run_options()` 自动注入 `WORKBUDDY_AUTO_PASS=true` 与 `AUTO_PASS=true`）。
- **Web UI 控制台**：**未开启**（安全设计保护，不注入 `WORKBUDDY_AUTO_PASS=true`）。
- **定时任务调度**：**未开启**（`src/scheduler.py` 默认策略强制 `auto_pass=False`）。

### 3.2 2026-11-24 钉钉任务未自动通过的根因与修复
- **现象**：`20261124_112904_workbuddy_loop.log` 中 `SJ202611240005`、`SJ202611240004` 因存在待人工复核/不允许自动提交类别被正常拦截；`SJ202611240003` 全部校验通过并点击了「通过」，但 1 秒后仍停留在待办列表并报 `整单提交结果待核对，禁止自动重发`。
- **根因**：现场截图 `data/logs/browser_failure_attempt_1.png` 与源码 `browser_failure_attempt_1.html` 证实，点击右上角「通过」后 ERP 弹出了 Ant Design「审批结果」二级确认弹窗（确认按钮文案为带空格的 `确 定`），原代码未点击该弹窗确认按钮。
- **修复**：在 `src/workbuddy_autopass.py` 中新增 `_confirm_approval_modal()`，等待「审批结果」弹窗出现并点击 `.ant-modal-footer button.ant-btn-primary`（或去空格文案为 `确定`/`确认`/`通过` 的按钮），等待弹窗关闭后再做两次 ERP 回读确认；弹窗无确认按钮时保持 fail-closed。

---

## 4. 验证记录

| 检查项 | 执行命令 | 结果 |
| :--- | :--- | :--- |
| 字节码编译 | `python -m compileall -q src packaging\windows` | **已通过** |
| 全量单元测试（4 道门禁拆分后） | `python -m pytest -q` | **已通过**（`1106 passed, 1 deselected`） |
| WorkBuddy 自动通过专项回归（弹窗修复后） | `python -m pytest -q tests/test_workbuddy_autopass.py tests/test_workbuddy_flow.py` | **已通过**（`89 passed`） |
| 真实 ERP 整单自动通过实测 | — | **未执行**（待重启本地服务后，于下次钉钉审批任务验收） |

---

## 5. 待办与后续跟进

- [ ] 重启本地 Web UI / 钉钉机器人后台进程，使 `ed6f7ae` 弹窗确认修复生效。
- [ ] 核对 `SJ202611240003` 当前 ERP 状态：如仍在待办且需由系统自动通过，人工确认后清理 `data/dingtalk_state.sqlite3` 中 `workbuddy_submissions` 表对应的 `needs_review` 记录。
- [ ] 在真实 ERP 环境下通过钉钉触发一次正式 WorkBuddy 审批，验收「审批结果」二级弹窗自动确认与整单通过闭环。
- [ ] 确认无误后推送本地 `main` 分支至远端，如准备发新版本再按 `README.md` 流程更新 `VERSION` 并打标签。
