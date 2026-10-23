# 试剂审批网页自动化程序

这是一个用于 ERP 试剂审批流程的 Python + Playwright 自动化项目。项目会读取 ERP 待办、采集试剂详情、执行一键匹配、查询化学网站、调用大模型整理物性资料，并根据结构化审批规则输出建议结果。

## 技术栈

- Python 3.10+
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

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install
Copy-Item .env.example .env
```

填写 `.env` 后，命令行直接运行审批建议流程：

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
- `workbuddy.desktop_enabled` 和 `workbuddy.write_enabled` 初始均为 `false`。先使用准备/导入验证；自动循环还需验证 `workbuddy.executable` 对应客户端及控件。选择“禁用网页写入”或全局测试模式时，只预览一批，不入记忆、不写 ERP、不删结果。
- 正式写入需要专用写入开关与已验证的 ERP API 同时启用。失败原因保存在本批 `state.json` 和审计日志；修正配置后可再次导入原文件，成功记录不会重复写入。
- 判定入记忆时保留 WorkBuddy 来源，不冒充人工核实，不覆盖人工记忆；全部成功才清理结果 Excel，数据库和状态保留。
- Windows 客户端适配依赖 `pywinauto`，已列入依赖。执行时保持桌面解锁，先打开“试剂审批”项目或其聊天。不修改客户端权限。

验证范围见 [WorkBuddy 对接说明](docs/WORKBUDDY_INTEGRATION_DESIGN.md)。

项目固定指令见 `docs/WORKBUDDY_PROJECT_INSTRUCTIONS.txt`。先保存到 WorkBuddy“试剂审批”的项目配置并验证新任务继承，再将 `workbuddy.prompt_mode` 从 `full` 改为 `project`，每批即可只投递目录。项目资产不作为正式规则来源。

已确认的规则工作簿可以用 `python scripts/publish_reviewed_rules.py <待发布文件> --expected-sha256 <审阅时正式文件摘要> --reviewer <确认人> --reason <变更说明>` 发布。程序校验后备份并原子替换正式文件；运行中 WorkBuddy 工作进程持锁时拒绝发布，需停止当前运行后执行。规则发布后，旧版本结果会被阻止写入，需按新规则重新判定。校验不等于业务正确性审查，程序不自动发明规则。

## 安全约束

- 正式安装包默认并强制使用无头浏览器；源码开发调试时可在 `config/settings.yaml` 中临时改为 headed 浏览器。
- 正式安装包启动 Web UI 时优先使用 `127.0.0.1:8000`；如果端口被其他程序占用，会自动尝试后续端口并打开正确地址。
- 正式安装包的用户数据和日志与程序文件分开，默认位于 `%LOCALAPPDATA%\ReagentApprovalBot\data\`；启动日志位于其中的 `logs\launcher.log`，审批任务日志位于 `logs\runs\`。安装目录仅放程序版本和启动器，升级不应覆盖用户数据。
- Windows 安装包默认安装到当前用户的 `%LOCALAPPDATA%\Programs\ReagentApprovalBot`；默认不弹目录选择以避免安装进度窗遮挡。需要自定义目录时，可先设置 `REAGENT_APPROVAL_INSTALL_DIR`，或设置 `REAGENT_APPROVAL_PROMPT_INSTALL_DIR=1` 后再运行安装包。在线更新会沿用当前安装目录。
- 默认 `APPROVAL_WRITE_MODE=multi_page`，会在满足置信度与安全门条件时写入网页；`AUTO_PASS=false` 时不会点击顶部“通过”。
- 默认 `AUTO_PASS=false`，不会点击顶部“通过”。
- 所有审批建议和阻断原因应保存在 `data/logs/` 中。
- 真实账号密码、API Key 只放 `.env`，不要提交到 Git。
