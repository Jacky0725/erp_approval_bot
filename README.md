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

常用 `.env` 开关：

- `TARGET_LIST_NUMBER`：指定清单号，例如 `SJ202606170003`。
- `PROCESS_ALL_TODOS=true`：同一登录会话内逐条处理当前待办列表。
- `PROCESS_ALL_TODOS_MAX=50`：全量待办处理上限。
- `APPROVAL_WRITE_MODE=disabled`：默认只生成建议，不写网页。
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

## 安全约束

- 开发阶段使用 headed 浏览器。
- 默认 `APPROVAL_WRITE_MODE=disabled`，不修改网页数据。
- 默认 `AUTO_PASS=false`，不会点击顶部“通过”。
- 所有审批建议和阻断原因应保存在 `data/logs/` 中。
- 真实账号密码、API Key 只放 `.env`，不要提交到 Git。
