# 试剂审批网页自动化程序

这是一个用于开发“试剂审批网页自动化程序”的 Python 项目骨架，使用 Playwright 自动化网页操作，并通过 Excel 配置审批规则。

## 技术栈

- Python 3.10+
- Playwright
- pandas / openpyxl
- python-dotenv
- PyYAML

## 目录结构

```text
reagent-approval-bot/
├─ AGENTS.md
├─ README.md
├─ requirements.txt
├─ .env.example
├─ config/
│  ├─ settings.yaml
│  └─ rules.xlsx
├─ src/
│  ├─ main.py
│  ├─ browser_bot.py
│  ├─ rule_engine.py
│  ├─ chemical_searcher.py
│  ├─ llm_extractor.py
│  └─ audit_logger.py
├─ data/
│  ├─ logs/
│  └─ review_queue.xlsx
└─ tests/
```

## 快速开始

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install
Copy-Item .env.example .env
python src/main.py
```

## 配置

- 账号密码：复制 `.env.example` 为 `.env` 后填写。
- 自动化配置：编辑 `config/settings.yaml`。
- 审批规则：维护 `config/rules.xlsx`。

默认启用 `dry_run`，用于开发和验证规则，不会真正提交审批动作。

