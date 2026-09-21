# 整体技术审查与分阶段优化（2026-09-10）

完整的全项目阶段、依赖、验收门槛和当前状态见 `docs/PROJECT_REFACTOR_PHASED_PLAN_20260910.md`。本文记录已完成的第一批审查与修改。

## 范围与基线

基线为用户已提交的 `0bb865a`。开始时没有已跟踪文件的未提交修改，未跟踪的报告、ZIP 和构建依赖保持原样。本轮不读取 .env，不登录 ERP，不改变正式规则、候选晋升条件、API、请求字段或模板 ID/name。

基线全量测试：**478 passed, 1 deselected，220.89 秒**。真实 ERP 冒烟被现有 pytest 配置排除。源码/测试审查覆盖 ERP 会话、页面分页、规则分类、写入批次、导出、候选维护、复核队列、审计、Web 入口和任务管理。不是对所有路径的形式化证明。

## 问题、证据与处置

优先级 P1 表示可能影响处理结果或数据完整性，P2 表示可恢复的可靠性/可维护性问题。

| 优先级 | 位置（函数名便于重定位） | 已验证证据及影响 | 本轮处理 |
| --- | --- | --- | --- |
| P1 | `approval_batch_state.normalize_write_result` | `handled or 全部键` 将显式空集合变为全部已处理，deferred 项可能被提前移除 | 用户确认后修正；保留旧 None/缺省兼容，增加状态级回归 |
| P1 | `erp_session.run_after_login_capture` | after_login 的 RuntimeError/Playwright Error 进入三次会话重试，可能重跑已部分执行的业务 | 用户确认后禁止回调开始后的会话重放；回调前仍最多三次 |
| P1 | `excel_exports.write_excel_with_fallback`、`rule_maintainer`、`review_queue.migrate…` | 直接写目标文件，序列化中断会留下不完整目标；旧备用名同秒冲突 | 同目录临时文件完成后替换，异常清理；备用文件追加唯一标识；复用统一 helper |
| P1 | `rule_maintainer.promote_approved_candidates` | 规则保存后候选状态保存失败；重试时示例已存在，新增数为零，旧逻辑不保存状态 | 新增示例计数与状态变更分离，已批准候选可重试收尾，不重复规则 |
| P2 | `erp_session.run_after_login_capture` | context 初始化/未捕获回调异常不经过显式 close | 统一 finally 清理；错误诊断写盘失败不阻断原错误处理 |
| P2 | `audit_logger.AuditLogger.from_settings` | 全局 logger 仅首次添加文件处理器，不同 root 使用同一目标 | 以规范日志路径派生 logger，初始化加锁，防重复传播；隔离与并发测试 |
| P2 | `dashboard.js` refreshStatus 及相关读取 | 固定轮询直接发请求，缺少在途保护和统一 HTTP/超时处理；附属读取失败前已标记任务完成 | 共用读取 helper，10 秒超时，单次刷新在途、明确过期状态；成功完成后更新完成标记 |
| P2 | `dashboard.css` `.data-health-list` | 当前 390px 截图显示六列指标挤成逐字换行 | 820px 以下两列，复用边框 token；桌面保持六列 |
| P1 | `reagent_page.click_next_todo_page` | 页码读取前后都为空时，旧表达式 `after_page != before_page or not after_page` 返回真，即使表格没有变化也会报告翻页成功 | 与试剂分页一致，要求页码或待办表格签名发生变化；首次点击无证据时仅再尝试一次 DOM 点击 |
| P1 | `erp_session.open_login_page` / `wait_for_app_shell` | 三次导航全部失败后仍继续；登录后所有应用壳证据均缺失时也继续并将后续操作视为业务回调 | 导航耗尽后抛错；应用壳无法验证时在业务回调前抛错，使外层会话重试仍安全有效 |
| P1 | `reagent_page.wait_for_detail_ready` | 打开详情后旧逻辑可用目标清单号补齐读取缺失，无法证明实际详情属于选中待办 | 必须从页面读回一致清单号；不一致、缺失或超时均停止后续业务回调 |
| P1 | `approval_flow.apply_approval_write_mode` | 已验证保存但页面收尾失败时同时返回 handled/failed，批次状态会再次提交；保存后读回异常则直接退出且无未知终态 | 已保存项保持终态并记录页面失败以阻断自动通过；点击后确认异常转人工复核并停止本轮 |
| P1 | `approval_writer._click_row_peer_action` | 指定行没有操作按钮时，若全页仅有一个同名按钮会点击其它行的按钮 | 删除无身份依据的全局唯一按钮回退；页面级取消仅由 `cancel_any_edit` 使用 |
| P1 | `config/settings.yaml` | 默认 `dry_run: false`、`headless: true` 与开发安全约束相反 | 默认改为 dry-run 且 headed；授权运行仍可显式覆盖 |
| P2 | `rule_engine._enabled_rows` | 校验器接受 `enabled=on`，运行时静默忽略 | 统一布尔值集合并增加回归测试 |
| P1 | `audit_logger` / `approval_flow` | 审计类没有接入业务路径，决定和执行结果无法形成稳定结构化记录 | 建议生成记录输入、规则版本和决定；保存记录执行结果；文件流写后释放避免 Windows 长期锁定 |
| P2 | `web_runner.AutomationJobManager` | 自定义 root 的任务仍将日志/状态/缓存写到模块全局目录，实例间可能串线 | 所有运行期路径从实例 root 解析；默认实例路径不变 |

对前两项执行语义修正，用户已明确回复“同意两项修正”。其余是兼容性和错误恢复修复；未运行真实规则晋升、数据迁移或清理。

## 分阶段方案与完成情况

| 阶段 | 目标/涉及模块 | 兼容方式 | 验证与回退 |
| --- | --- | --- | --- |
| 0 基线 | Git、测试、模板/API 约束 | 以当前提交为准，不重做 V2 | 原基线通过；保留执行前提交 |
| 1 数据与执行可靠性 | batch_state、erp_session、excel_exports、review_queue、rule_maintainer | 旧返回结构和表格列保持；两项行为更正已获授权 | 注入序列化、锁定、部分晋升、回调失败；可独立撤销该组补丁 |
| 2 最小职责整理 | excel_exports、audit_logger | 提取两个共享写入 helper；不拆大文件凑行数，不引入依赖 | 相关模块回归；旧 browser_bot 入口与调用保持 |
| 3 管理界面 | dashboard.js/css | 共用 GET 读取，保留 DOM、表单、URL、POST/DELETE 语义 | JS 故障测试、八页双尺寸 headed 验证；JS/CSS 一起回退 |
| 4 交付 | 文档、隔离验证脚本、全量测试 | 不改配置或安装包方式 | 记录实际验证结果和边界 |

## 验证记录

- 修改前局部基线：17 项 ERP 会话、规则维护、队列和等待测试通过。
- 第一批修复：23 项针对性测试通过。
- 扩展回归：158 项通过（69.67 秒），包含审批流程、writer、Web runner、队列、写入状态及新可靠性测试。
- 候选晋升恢复：3 项通过，使用临时工作簿验证部分提交后重试，不操作正式规则。
- JavaScript：Node 语法检查与隔离断言通过；验证在途请求共享、HTTP 错误、无效 JSON、超时、失败后恢复、附属数据重试。
- UI：`scripts/verify_dashboard_isolated.py` 使用真实 Jinja 模板和合成空数据，headless=False，拦截全部网络请求，未导入 web_app 或启动 lifespan。八页 × 1440×900/390×844 共 16 个状态全部通过；无控制台错误、意外请求或页面横向溢出，失败/恢复通过。人工检查总览、窄屏复核以及修复后的健康/影子面板截图。
- UI 证据：`.codex-ui-audit/refactor-20260910-final/results.json` 及同目录 PNG；初次发现截图保留在 `.codex-ui-audit/refactor-20260910/`。这些本地产物被既有规则忽略。
- P3 阶段全量测试：**508 passed, 1 deselected，167.32 秒**，退出码 0；无新增失败。
- P4/P5 定向验证持续通过；最新规则/审批流/V2/复核定向 179 项通过，全量 622 passed、1 deselected。
- 最终全量测试：**511 passed, 1 deselected，202.59 秒**，退出码 0。
- 最终 UI 验证：8 页 × 1440×900/390×844 共 16 个状态通过；无控制台错误、意外网络请求或页面横向溢出，读取失败与恢复场景通过。证据位于 `.codex-ui-audit/refactor-20260911-final/`。
- 兼容性核验：`browser_bot.py` 旧入口、FastAPI endpoint、模板 ID/name 和 Excel 列保持；修改的 Python 文件可解析。
- P2 增量：ERP 会话、待办分页和 UI 等待相关 25 项测试通过；没有访问真实 ERP。

## 审查中的正面证据与未变更项

- browser_bot 已是多个职责 mixin 的兼容入口；templates 已拆成 layout/partials；无证据支持再引入前端框架。
- web_runner 的任务启动已有 RLock 和 running 检查；不是完全缺少互斥。
- web_app 下载已 resolve 并检查目录归属；没有仅凭接口存在就认定路径穿越。
- rule_engine 保持规则优先级与 manual-review 判定；V2 身份/证据冲突已有处理，旧链路和影子开关保持。
- dry-run 在写入和 auto-pass 入口已有门控，现有回归覆盖禁止读取编辑行/点击通过；本轮没有扩大真实执行权限。
- 分页已有页码/签名核验、spinner 和短轮询；没有以 sleep 数量替代正确性判断。

## 风险、推断与后续边界

1. **仍需补齐：** 决定与执行事件已进入统一审计，但尚未分配贯穿后台任务、决定、写入和通知的 run ID；UI 摘要仍有部分依赖英文日志文本解析。
2. **已确认限制：** 原子替换保证单文件发布完整性，不是 Excel/SQLite 多文件事务，也不解决多进程“读后改写”丢更新。候选晋升已修复可重复收尾，其他跨存储操作需要专门故障测试；本轮不做数据库迁移。
3. **待真实验证：** ERP 选择器、会话失效、部分保存后的重读与 API/页面一致性只能在授权的测试环境验证。本轮不会把 mock 成功当真实 ERP 验收。
4. **已确认策略：** 2026-09-20 用户确认所有“未知类”自动写入 ERP。规则引擎、结构化规则表、迁移器、旧记忆/V2 结果归一化、写入候选和人工队列入口已统一；已有队列数据未迁移或删除。实际写入仍受 dry-run、写入模式、ERP 映射和保存验证门控。
5. **UI 验证范围：** 本轮重点是状态读取和指标布局；空态合成数据不代表大批量、长证据、所有编辑保存流程已全面验证，也不代表 WCAG 全站合规。已有编辑保护未改；没有新增修改请求自动重试。
6. **部署边界：** 未构建或运行正式 Windows 安装包。未新增生产模块文件或静态资源，旧打包路径仍适用，但不把静态检查描述为打包验收。Node 仅用于可选开发测试，不是产品运行依赖。
7. **行为变化提醒：** 回调开始后包括最终截图失败在内的会话异常会停止，不再自动整段重跑。操作员应先核对 ERP 实际状态再手动重启；停止不会撤销已完成的业务写入。

## 运行与回退

运行方式不变；无需迁移数据。候选仍需人工标为 approved 才会晋升，不确定规则仍留在候选表。开发默认配置已收紧为 dry-run 和 headed。

```powershell
python -m pytest -q
python scripts/verify_dashboard_isolated.py --output .codex-ui-audit/recheck
```

浏览器脚本需要已安装 Playwright Chromium；始终显示浏览器，使用合成数据，不启动 ERP。缺少 Node 时单独 JS 测试会明确 skip；其他 Python 测试不受影响。

回退先查看本轮文件差异，只反向应用本轮补丁，保留后来新增的用户改动。不要对仓库执行 reset --hard/clean；JS 与 CSS 配套回退。回退前两项会恢复已确认的重复重放/误终态风险，需明确接受。正式规则、.env 和生产数据未变更，无需数据恢复。`settings.yaml` 仅将开发默认值改为 `dry_run: true`、`headless: false`；恢复旧值会重新引入误运行风险。
