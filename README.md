# 试剂审批网页自动化程序

这是一个用于 ERP 试剂审批流程的 Python + Playwright 自动化项目。项目会读取 ERP 待办、采集试剂详情、执行一键匹配、查询化学网站、调用大模型整理物性资料，并根据结构化审批规则输出建议结果。

## 技术栈

- Python 3.12（Windows CI 和发布构建使用的版本）
- Playwright
- pandas / openpyxl
- python-dotenv
- PyYAML
- FastAPI / Uvicorn / Jinja2

## 核心模块

- `src/erp_session.py`：登录、打开页面、基础等待。
- `src/reagent_page.py`：试剂判定页面、列表、详情、分页、排序。
- `src/approval_flow.py`：半自动审批主流程编排。
- `src/approval_writer.py`：网页填写、保存、生成试剂库能力，默认不启用。
- `src/rule_engine.py`：读取规则并判断物化特性。
- `src/chemical_searcher.py`：Chemsrc / ChemicalBook / PubChem 查询。
- `src/name_normalizer.py`：ERP 试剂名称清洗与标准化。
- `src/llm_extractor.py`：大模型物性信息整理。
- `src/review_queue.py`：人工复核队列读写。
- `src/excel_exports.py`：Excel 安全写入。
- `src/web_app.py`：本地 FastAPI 管理控制台。
- `src/web_runner.py`：Web 控制台后台任务运行器。

## 快速开始

### Windows 安装包

发布下载：[GitHub Releases](https://github.com/Jacky0725/erp_approval_bot/releases)。以 Release 页面实际上传的资产为准，Git 标签或仓库 `VERSION` 不代表安装包已发布。当前仓库版本为 `0.1.22`，采用核心程序与浏览器拆包方案。

- `ReagentApprovalBot-setup-<版本>-windows-amd64.exe`：当前用户安装器，无需管理员权限。
- `ReagentApprovalBot-core-<版本>-windows-amd64.zip`：供安装器/更新器使用的核心包，不含浏览器，不是完整便携包。
- `ReagentApprovalBot-browser-chromium-headless-<revision>-windows-amd64.zip`：独立浏览器运行时，可由多个应用版本复用。
- `release-manifest.json`：核心包和浏览器包的大小、SHA-256、架构及浏览器 revision 清单。
- `*.size.json`：对应产物的体积检查报告。

在线安装运行 Setup；缺失浏览器时安装器从同一 Release 获取。离线安装请把同一 Release 的 Setup、核心 ZIP、浏览器 ZIP 放在同一目录，勿混用不同发布的资产。缺少浏览器且无法联网时，补齐资产后重试，不要把不完整安装视为成功。

`0.1.19` 及更旧版本须手工运行新版 Setup 迁移，勿使用旧更新器无人值守跨越安装架构。升级前备份 `%LOCALAPPDATA%\ReagentApprovalBot\`，升级后先确认测试模式、写入开关、规则和凭据，再连接隔离 ERP 验证。

### 源码运行

源码开发建议使用独立 Python 3.12 虚拟环境：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
```

填写 `.env`，保持 `APP_DRY_RUN=true`、`APPROVAL_WRITE_MODE=disabled`、`AUTO_PASS=false`。仓库配置可能包含业务运行参数；开发前同时检查 `config/settings.yaml` 中的调度与 WorkBuddy 写入开关，调试时使用有界面浏览器。然后运行审批建议流程：

```powershell
python src/main.py
```

启动本地 Web 控制台：

```powershell
python -m uvicorn web_app:app --app-dir src --host 127.0.0.1 --port 8000 --reload
```

打开：

```text
http://127.0.0.1:8000
```

## 运行模式

当前正式审批入口固定使用 V1 查询链路。V2/V3 执行链路、影子运行及其专属测试/重试入口已停用；历史日志和建议文件保留原样。旧配置中的链路版本在任务入口统一按 V1 处理，Web API 对显式 V2/V3 请求返回停用错误。V1 的缺 CAS 名称补全仍由基础设置单独配置。

常用 `.env` 开关：

- `TARGET_LIST_NUMBER`：指定清单号，例如 `SJ202606170003`。
- `PROCESS_ALL_TODOS=true`：同一登录会话内逐条处理当前待办列表。
- `PROCESS_ALL_TODOS_MAX=50`：全量待办处理上限。
- `APPROVAL_WRITE_MODE=multi_page`：默认按清单分页保存；Web UI 仅保留 `multi_page` 和 `generate_library` 两个正式写入模式。
- `APPROVAL_WRITE_MIN_CONFIDENCE=0.8`：网页填写最低置信度。
- `AUTO_PASS=false`：默认不点击顶部“通过”。
- `APP_DRY_RUN=true`：先以测试模式验证；关闭顶部“通过”不等于禁止保存字段。
- `APPROVAL_WRITE_MODE=disabled`：禁止常规网页写入；WorkBuddy 另有专用写入门禁，需单独核对。

## 部署与更新

Windows 安装采用当前用户目录：

```text
%LOCALAPPDATA%\Programs\ReagentApprovalBot\
  ReagentApprovalBot.exe
  ReagentApprovalBotUpdater.exe
  current.json
  previous.json
  versions\<应用版本>\
  shared\browsers\<浏览器revision>\
```

配置、凭据、SQLite、日志及 Excel 等运行数据位于 `%LOCALAPPDATA%\ReagentApprovalBot\`，不要与版本目录混放。升级或修复前仍应自行备份；备份包含敏感信息，应限制访问。

通过管理页检查更新并确认安装。新版链路使用发布清单验证包的版本、大小和 SHA-256，再暂存、切换版本并执行健康检查；失败时尝试恢复旧版本。浏览器 revision 未变化时可复用已有运行时，不需要每次重新下载浏览器。`GET /api/update/status` 可查看更新阶段。不要在 ERP 保存过程中更新，也不要手工删除当前或回滚所需版本和浏览器。

服务器部署应使用 Windows x64 专用账户，管理页仅监听回环地址，不要直接暴露到公网。WorkBuddy 依赖同一普通用户、相同桌面会话及解锁的交互桌面，不适合作为无桌面 Windows 服务运行；无头浏览器不代表 WorkBuddy 可以无人登录运行。Linux 服务部署不在当前 Windows 安装包支持范围。

未签名程序仍可能触发 SmartScreen。SHA-256 只能证明文件与清单一致，不能证明发布者身份，不能抵御发布账户整体失陷；建议仅用于受控内部部署。CI 通过不等于完成所有 Windows 10/11、安装中断、回滚和真实桌面验收。

### 发布维护

先更新 `VERSION` 和 README，将改动推送 `main` 并等待 CI 通过，再推送对应 `v<版本>` 标签。`.github/workflows/release.yml` 在 Windows Runner 执行编译、pytest、拆包构建、内容/哈希校验及 Defender 扫描，成功后上传 Release 资产。体积硬上限为核心 ZIP 115 MiB、Setup 130 MiB、浏览器 ZIP 120 MiB；超限不能跳过检查发布。已正式发布的版本不要移动标签或替换同名资产，后续修复使用新版本。

## Web 控制台

控制台提供：

- 查看 ERP 配置状态、AUTO_PASS、写入模式、目标清单。
- 编辑 ERP 登录地址、账号、密码。
- 编辑 SiliconFlow API Key、LLM Provider、Base URL、模型名、超时和重试次数。
- 编辑默认目标清单、是否处理所有待办、写入模式、最低置信度和 AUTO_PASS。
- 启动“生成审批建议”“导出待办”“采集首页”“采集试剂判定页”。
- 查看流程步骤、当前状态和建议表首条判定证据。
- 实时查看最近运行日志。
- 预览 `data/logs/approval_suggestions.xlsx`。
- 下载截图、HTML、Excel 和日志产物。

基础设置保存后会写入 `.env` 和 `config/settings.yaml`，下一次启动任务时生效。ERP 密码和 API Key 在页面中不会回显，输入框留空表示保留原值。若已有自动化任务正在运行，控制台会拒绝保存基础设置，避免运行中配置被改动。

后续如果审批模块、规则模块、查询模块或人工复核模块发生变化，需要同步维护 `src/web_app.py`、`src/web_runner.py`、`src/templates/dashboard.html` 和 `src/static/dashboard.css` 中对应的入口、状态展示和说明。

## 记忆库维护

`data/reagent_memory.sqlite` 是本地运行数据，不会提交到 Git。迁移到其他电脑或安装包环境后，如果需要复现“删除全部冲突记忆记录”的维护动作，可以运行：

```powershell
python scripts/cleanup_reagent_memory_conflicts.py --dry-run
python scripts/cleanup_reagent_memory_conflicts.py --yes
```

脚本会先备份 SQLite 到 `data/logs/`，然后删除所有 `conflict=1` 的试剂记忆记录。

## WorkBuddy 文件对接（试运行）

- 运行页新增“WorkBuddy 准备20条”“WorkBuddy 导入结果”“WorkBuddy 批次循环”。WorkBuddy 独立判定，项目只校验文件、身份和类别映射，只保存物化特性，不执行顶部“通过”。
- 每批文件位于 `data/workbuddy/<批次ID>/`。`prompt.txt` 是本批任务，输入、规则快照和模板位于同一目录。结果固定为 `result.xlsx`，五列：批次ID、记录ID、试剂名称、物化特性类别、判断依据。
- 首次启用前应将 `workbuddy.desktop_enabled` 和 `workbuddy.write_enabled` 设为 `false`，先使用准备/导入验证；不要假定现有业务配置仍为关闭状态。自动循环还需验证 `workbuddy.executable` 对应客户端及控件。测试模式只预览一批，不入记忆、不写 ERP、不删结果。
- 正式写入需要专用写入开关与已验证的 ERP API 同时启用。失败原因保存在本批 `state.json` 和审计日志；修正配置后可再次导入原文件，成功记录不会重复写入。
- 判定入记忆时保留 WorkBuddy 来源，不冒充人工核实，不覆盖人工记忆；全部成功才清理结果 Excel，数据库和状态保留。
- Windows 客户端适配依赖 `pywinauto`，已列入依赖。执行时保持桌面解锁，客户端与项目使用同一普通用户和权限级别。适配器核对窗口就绪、项目身份、空草稿和发送回执；结果文件出现后还需确认 `【批次完成】<批次ID>`。发送状态不明时暂停，不自动重发，也不自动提权。

验证范围见 [WorkBuddy 对接说明](docs/WORKBUDDY_INTEGRATION_DESIGN.md)。

项目固定指令见 `docs/WORKBUDDY_PROJECT_INSTRUCTIONS.txt`。先保存到 WorkBuddy“试剂审批”的项目配置并验证新任务继承，再将 `workbuddy.prompt_mode` 从 `full` 改为 `project`，每批即可只投递目录。项目资产不作为正式规则来源。

已确认的规则工作簿可以用 `python scripts/publish_reviewed_rules.py <待发布文件> --expected-sha256 <审阅时正式文件摘要> --reviewer <确认人> --reason <变更说明>` 发布。程序校验后备份并原子替换正式文件；运行中 WorkBuddy 工作进程持锁时拒绝发布，需停止当前运行后执行。规则发布后，旧版本结果会被阻止写入，需按新规则重新判定。校验不等于业务正确性审查，程序不自动发明规则。

## 安全约束

- 正式安装包默认并强制使用无头浏览器；源码开发调试时可在 `config/settings.yaml` 中临时改为 headed 浏览器。
- 正式安装包启动 Web UI 时优先使用 `127.0.0.1:8000`；如果端口被其他程序占用，会自动尝试后续端口并打开正确地址。
- 正式安装包的用户数据和日志与程序文件分开，默认位于 `%LOCALAPPDATA%\ReagentApprovalBot\data\`；启动日志位于其中的 `logs\launcher.log`，审批任务日志位于 `logs\runs\`。安装目录仅放程序版本和启动器，升级不应覆盖用户数据。
- Windows 安装包默认安装到当前用户的 `%LOCALAPPDATA%\Programs\ReagentApprovalBot`；默认不弹目录选择以避免安装进度窗遮挡。需要自定义目录时，可先设置 `REAGENT_APPROVAL_INSTALL_DIR`，或设置 `REAGENT_APPROVAL_PROMPT_INSTALL_DIR=1` 后再运行安装包。在线更新会沿用当前安装目录。
- `.env.example` 使用 `APP_DRY_RUN=true`、`APPROVAL_WRITE_MODE=disabled`。安全迁移会重置常规写入和调度；已有运行环境或自定义配置仍需逐项检查。启用 `multi_page` 后可能保存 ERP 字段；`AUTO_PASS=false` 仅表示不点击顶部“通过”。
- 默认 `AUTO_PASS=false`，不会点击顶部“通过”。
- 所有审批建议和阻断原因应保存在 `data/logs/` 中。
- 真实账号密码、API Key 只放 `.env`，不要提交到 Git。
