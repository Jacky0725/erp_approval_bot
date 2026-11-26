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

- [x] 用户授权后已正常重启本地 Web UI / 钉钉机器人后台进程（见第 9 节）；代码已重载，真实业务验收与重启完成分别记录，不混同。
- [ ] 核对 `SJ202611240003` 当前 ERP 状态：如仍在待办且需由系统自动通过，人工确认后清理 `data/dingtalk_state.sqlite3` 中 `workbuddy_submissions` 表对应的 `needs_review` 记录。
- [ ] 在真实 ERP 环境下通过钉钉触发一次正式 WorkBuddy 审批，验收「审批结果」二级弹窗自动确认与整单通过闭环。
- [ ] 确认无误后推送本地 `main` 分支至远端，如准备发新版本再按 `README.md` 流程更新 `VERSION` 并打标签。

---

## 6. Web UI 视觉升级与模块化重构（方案 A · Lab Precision 双主题）

- **设计规范**：新增 [UI_DESIGN_SPEC.md](file:///d:/codex/reagent-approval-bot/docs/UI_DESIGN_SPEC.md)，确立「Lab Precision · 精密实验控制台」视觉方向与浅色/深色双主题 Token（`<meta name="color-scheme" content="light dark">` + CSS `light-dark()`）。
- **样式与脚本架构**：
  - 重写 [dashboard.css](file:///d:/codex/reagent-approval-bot/src/static/dashboard.css) 为 `@layer tokens, base, layout, components, pages, utilities` 分层体系，消除历史重复选择器与硬编码色值，收敛为 `1280px / 960px / 680px` 3 套响应式断点，完整支持 `prefers-reduced-motion`。
  - 新增 [theme-init.js](file:///d:/codex/reagent-approval-bot/src/static/js/theme-init.js)（CSP `script-src 'self'` 兼容的同步防闪烁主题初始化）与 [ui-shell.js](file:///d:/codex/reagent-approval-bot/src/static/js/ui-shell.js)（双态主题切换、`popover="manual"` Toast 通知栈、原生 `<dialog>` 分级确认框、4 套定制 SVG 空状态插画、设置 4-Tab 切换、复核/建议键盘 `↑/↓/Esc` 导航、SVG 分类占比微图表及待办勾选 `sessionStorage` 持久化）。
  - 升级 [dashboard.js](file:///d:/codex/reagent-approval-bot/src/static/dashboard.js)：替换 20+ 处原生 `alert`/`confirm` 为 `uiToast`/`uiConfirm`，升级为自适应状态轮询（运行中 `2.5s` / 空闲 `10s` / 后台标签页暂停），更新检查增加 5 分钟 `sessionStorage` 缓存，记忆库仅在 `/memory` 页加载。
  - 更新 [web_app.py](file:///d:/codex/reagent-approval-bot/src/web_app.py) 的 `static_asset_version()` 递归扫描 `src/static` 下所有静态资源。
- **页面与素材升级**：
  - 精修 [reagent-logo.svg](file:///d:/codex/reagent-approval-bot/src/static/reagent-logo.svg)，升级 [layout.html](file:///d:/codex/reagent-approval-bot/src/templates/layout.html) 线性 SVG 图标侧边栏、紧凑顶栏与全局 `dry_run` 提示条。
  - 重构全部 8 个页面模板（总览指挥带与 4 组分组遥测、运行页 62/38 双栏与 Sticky 控制台、复核/建议 Master-Detail 工作台、设置页 4 分类 Tab、暗色终端风格日志视口与关键字高亮搜索），严格保留全部现有路由、表单字段与 DOM ID 契约。
- **验证结果**：
  - `python -m compileall -q src packaging\windows`：**已通过**。
  - `python -m pytest -q`：**1108 passed, 1 deselected**（全量回归通过）；随后针对 CSP 与 `/review` 布局微调后复测 Web UI 专项 5 个测试文件：**88 passed**。
  - **真实浏览器实机验收**（`http://127.0.0.1:8765`，`1440×900` 桌面端 & `390×844` 移动端）：浅色/深色双主题切换、侧栏图标折叠、运行页双栏、复核页分栏、设置页 4-Tab、日志终端过滤与滚动锁定均正常，桌面与移动端均无横向溢出，最终达成 **0 CSP / 控制台报错**。


## 7. 运行期间清单退回的局部修复

- 状态：生产源码已修改；模拟验收通过；本轮未启动真实审批、未重启服务、未修改 ERP 或清理历史检查点。
- `src/erp_api_client.py`：区分匹配写入前确认的非待审批状态与空明细、缺失/冲突状态。后者仍报错。写入后状态变化仍按原逻辑停止核对。
- `src/workbuddy_bridge.py`：每单读取后跳过已非待审批清单；匹配接口内部第二次读取发现退回时，仅移除本次尚未写入的匹配标记并保留审计。未知写入结果继续禁止重发。
- 验证：修改前相关四文件测试 156 passed；修改后 ERP 匹配、桥接、循环、自动通过及安全停止五文件回归 191 passed；追加网络读取失败案例后针对测试 9 passed。已有 Starlette/httpx 弃用提示一条。相关 diff 空白检查通过。
- 回退备份：`tmp/returned-list-fix-20261125`，仅备份本轮四个修改文件；不覆盖原有 UI 改动。
- 实际运行核查：12:46 结束任务 `6aa087c7cbd64a6db47a7228e7cb4f54` 成功；SJ202611250003 整单提交回读通过。SJ202611250009 的“冰烯酸”（ERP 明细 692868）试剂库 needs_confirmation，且该单有未知类；SJ202611250008 两条“二丙烷”（692253、692455）为未知类，被现行整单通过类别限制拦截。本轮未放宽限制。
- 下一步：下次用户正常发起任务时观察该跳过分支；无需为加载改动单独重启 Web UI（审批子进程每次重新加载源码）。历史错误匹配记录仍保留，退回清单在匹配历史检查前跳过；若清单重新送审，旧的待核对标记仍需单独核实。


## 8. 未知类自动通过与钉钉问题摘要（当前业务口径）

- 用户确认：未知类不再作为整单自动通过的拦截条件，替代第 7 节对此类别的旧限制描述；其他条件保持。
- `src/workbuddy_autopass.py`：允许 ERP 已配置的未知类参与完整校验并自动提交；试剂库待确认原因附具体试剂名和原错误。未放宽拒收、待人工复核、未保存或试剂库待确认限制。
- `src/dingtalk_notifier.py`、`src/web_runner.py`：复用原任务结束通知，从该任务独立日志汇总整单 blocked 原因，去重、限制 10 项及单项长度，仍使用原任务群绑定。不新增主动推送通道、不发送历史任务通知。
- 备份：`tmp/unknown-autopass-fix-20261125`。未覆盖已有 UI 或上一轮退回清单修复。
- 生效边界：自动通过变更由下次审批子进程加载；通知增强需要 Web UI/机器人服务重启。本轮未重启服务、未启动真实审批、未向钉钉实际发送消息。真实 ERP/群消息验收待执行。
- 最终验证：自动通过、WorkBuddy 循环、钉钉通知、Web Runner 四文件回归 144 passed（1 条已有依赖弃用提示）；包含模拟发送到原群绑定、未知类通过、未知类仍受试剂库校验及消息去重/长度限制。相关 diff 检查通过。

## 9. 钉钉聊天角色设置（已加载，真实群聊待验收）

- 用户确认：默认活泼健谈，称呼“客官”；可在设置页编辑并恢复默认。称呼是群内统一风格，不表示身份识别或管理员权限。
- 聊天模块新增独立表达偏好，固定 JSON 意图协议、安全提示、精确指令与二次确认保持不变；不修改化学提取提示词、模型或采样参数。
- 新配置为 `dingtalk_chat.persona_prompt`；未配置时使用默认角色。限制 2000 字符、统一换行；旧版表单未提交新字段时保留原值，非法值在设置写入前拒绝。配置加密迁移白名单同步支持该多行字段，未放宽其他字段限制。
- Web UI 复用现有集成设置与组件样式，增加角色文本框、恢复默认按钮；保存后持久化到 YAML。不覆盖本日已有 UI 重构、通知与 ERP 改动。
- 验证：聊天、精确指令、设置请求、安全停止与 Web 安全六文件回归 94 passed（1 条已有 Starlette/httpx 弃用提示），含角色迁移往返及非法字段测试；Python 编译、JS 语法与相关 diff 空白检查通过。
- 浏览器模拟验收：1440×1000 与 390×844，浅/深主题均验证编辑、保存、刷新回显、恢复默认与脚本文字转义；无页面横向溢出及 pageerror。截图位于本地忽略目录 `.codex_tmp/dingtalk-persona-*.png`。
- 当前生效状态：随后已按用户授权正常重启并加载新代码，Stream 已连接、自然语言对话开启，当前及默认角色均包含“客官”。未配置自定义角色时使用默认的活泼健谈设定；新版本中后续修改角色可通过设置保存生效。
- 验收边界：本轮未调用真实模型或发送钉钉消息，真实群聊语气尚未验收。提示词不能保证模型绝不偏离风格，执行安全仍依赖程序约束。
- 阶段 Git 基线：角色功能开发时基于 `main`（`ed6f7ae`）的既有脏工作区增量修改，当时未提交或推送；后续核对的基线见第 10 节，不将历史提交号视作当前 HEAD。
- 随后用户明确授权重启：重启前确认无运行任务、无待收尾批次，通过正常重启接口完成重载；重启后状态接口确认 Stream 已连接、聊天开启，默认与当前角色配置均已加载“客官”设定。未触发审批或实际模型/群消息测试。

## 10. 当前审批交接与“冰烯酸”冲突（待处理）

### 当前有效状态

- 实际运行项目：`D:/codex/reagent-approval-bot`；与隔离工作树 `C:/Users/admin/.codex/worktrees/workbuddy-approval-optimizations/reagent-approval-bot` 有差异，不得整文件覆盖同步。
- 本次只读核对的 Git 基线：`main` / `e0c2ae7`，工作区仍有未提交修改；前述章节中的旧提交号仅为各阶段历史基线。本次未提交或推送。
- 清单退回跳过修复：见第 7 节，代码及模拟测试已完成；真实运行中“退回后继续下一单”的修复验收尚未执行。
- 未知类不再单独拦截整单自动通过：以第 8 节为准。未保存、身份变化、拒收、人工复核及试剂库待确认等原校验继续保留。
- 钉钉未通过原因摘要：代码及模拟原群绑定发送测试已完成；真实群消息尚未验收。第 8 节“未重启”描述的是该轮结束时状态；第 9 节随后记录了另次授权重启，不能继续据此声称当前服务从未重载。本次记录未另行查询服务或启动任务。
- 钉钉聊天角色：活泼健谈、称呼“客官”的默认角色与可编辑设置已实现并经授权重启加载；六文件回归 94 passed，桌面/手机及浅深主题模拟验收通过。真实模型回复风格尚未验收，精确指令、执行权限及审批规则不变。
- 已有验证：退回修复相关回归 191 passed，追加针对测试 9 passed；未知类/通知改动最终四文件回归 144 passed。均为前轮实际执行记录，本次仅更新文档，未重新运行测试。

### 已确认的冲突证据

用户口述“丙烯酸”，实际保存的原始名称为“冰烯酸”，不得在未核实身份时自动改名。

| 清单 | 序号 | ERP 明细 ID | CAS | WorkBuddy 分类 | 试剂库结果 |
| --- | --- | --- | --- | --- | --- |
| SJ202611250009 | 313 | 692867 | 空 | 特殊酸 | 创建记录 138078，created_verified |
| SJ202611250009 | 314 | 692868 | 空 | 未知类 | 命中记录 138078，needs_confirmation，未覆盖 |

- 第一条位于批次 `20261125_122841_a5b569cb`；第二条位于批次 `20261125_123951_94103438`。证据为各批次 `data/workbuddy/<批次号>/state.json`。
- 相同试剂名称及空 CAS 在两个批次给出不同类别。第二条依据说明名称不规范、身份无法唯一确认，引用 `BUS-UNK-IDENTITY-COMPOSITION-001` 判未知类。第一条已建立特殊酸的试剂库记录；第二条生成试剂库时 ERP 返回已有记录修改确认。
- 已确认：两条类别不一致，第二条命中同一试剂库记录。未确认：ERP 修改确认的全部字段差异、当前实时 ERP 状态、哪一类别最终应采用。不得把历史检查点当作实时回读。
- 取消未知类限制不会自动解决已有试剂库修改冲突。本轮未修改规则、类别、试剂库或审批状态。

### 下一步（未执行）

1. 根据现行正式规则复核两条“冰烯酸”的依据，确定一致的业务类别；如需补充原标签/CAS，明确缺失项。当前没有授权将它直接视为“丙烯酸”。
2. 回读 ERP 两条明细及试剂库 138078 的当前值，确认具体差异，再按用户确认的处理范围修正；不自动覆盖试剂库。
3. 在用户发起或授权的正常审批中，验证已退回清单跳过、未知类满足其余条件后自动通过，以及待确认原因反馈到原钉钉群。
4. 由用户在群内 @机器人进行普通聊天，验收“活泼健谈、称呼客官”的实际回复；本次文档更新不代发消息、不调用模型、不触发审批。

本次交付仅更新此进度文件，保留既有章节和其他未提交修改；未修改业务代码、重启服务、发送外部消息或操作 ERP。

## 11. 试剂库重新核对与同单重复判定一致性（已开发，真实任务待验收）

- 本节更新第 10 节的后续状态；Git 基线 `main / e0c2ae7`，在既有脏工作区增量修改，未提交或推送。
- 前轮只读现场核对：`SJ202611250009` 的 313、314 行均已为特殊酸，ERP 试剂库 `138078` 亦为特殊酸，清单状态已为 `yes`。历史批次仍保留 `needs_confirmation`，导致之前的自动通过检查先被旧记录阻断。该只读核对未提交审批或覆盖 ERP。
- 用户随后授权实现两项改进，本轮未操作真实 ERP、业务数据库或历史批次检查点，未重启服务、触发审批或调用外部模型。
- **重新核对**：整单自动通过前，针对已完成批次中明确的 `needs_confirmation` 记录，按原 ERP 明细 ID 和原试剂库 ID 重新回读。检查名称、CAS、序号、判定相关输入、清单待审批状态、当前可提交类别及试剂库分类一致性；同单重复试剂当前类别冲突时保持拦截。成功后更新本地状态，并保存旧状态、旧类别、时间和观察值；原始模型建议不变，不将一致性回读声称为重新完成化学分类审核。其他 sending/failed/unverified 或人工停止状态不自动解锁，也不重发入库请求。
- **重复判定**：新增 `src/workbuddy_consistency.py`，按同一清单、原始名称、CAS、type、desc 精确分组，沿用现有不将包装规格用于化学判定的约定。结果导入前检查整批及同规则版本历史批次；写入前核对 ERP 已分类重复项，冲突进入待人工复核，保留双方依据，不自动选择一个类别。整单提交前再检查全部 ERP 明细，且提交前后快照包含 type/desc。人工在 ERP 统一分类后可通过既有显式复核确认流程恢复；不同名称、CAS、描述或清单不合并，不推测别名。
- 主要代码：`src/erp_api_client.py`、`src/workbuddy_bridge.py`、`src/workbuddy_autopass.py`、`src/workbuddy_consistency.py`；本轮未调整页面或配置，不新增依赖。
- 验证：修改前三文件基线 `124 passed`；最终七文件回归（重新核对、ERP 入库、自动通过、桥接、循环及安全停止）`228 passed`，1 条已有 Starlette/httpx 弃用提示。Python 编译与相关 diff 空白检查通过。测试覆盖重新核对恢复模拟整单通过、身份/库记录/类别不一致与网络失败保持拦截、同批与跨批冲突、不同身份不合并、人工确认恢复且原建议保留、已提交不重复发送及安全停止。
- 生效与验收：改动由后续正常审批子进程加载；未为验收启动真实任务。待用户正常发起下一次任务观察自动重新核对和重复分类冲突提示，不对已审批的上述清单重复提交。
