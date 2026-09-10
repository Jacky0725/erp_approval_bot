# 项目整体优化与 Web UI 重构：Codex 执行包

日期：2026-09-10。目标仓库：`D:\codex\reagent-approval-bot`。审查时 HEAD：`567b2cf`，包含大量未提交的业务与 UI 改动。

本包交付的是基于当前代码的分析、执行提示词和验收约束；没有执行业务重构，没有启动 ERP，也没有完成视觉审计。执行包不是安装程序，不会自动运行审批。

## 使用方式

在当前项目的 Codex 任务中粘贴以下指令：

> 请阅读 deliverables/refactor_codex_package_20260910/CODEX_MASTER_PROMPT.md 及其引用文件，按其中的阶段和验收条件执行。先重新核对当前工作区与本包基线的差异，再完成 P0，随后推进范围内的兼容性重构。保留已有修改，不启动真实业务写入。不要把执行包中标为待验证的风险直接当作已确认缺陷。

如只希望执行 UI 部分：

> 请阅读本执行包，完成 P0 的隔离环境和契约基线，然后执行 P3、P4 的 Web UI 工作；仅做这些阶段需要的后端兼容修复。先取得当前页面截图，再确定视觉调整，不扩大为整个业务重写。

## 文件目录

| 文件 | 用途 |
| --- | --- |
| CODEX_MASTER_PROMPT.md | 总执行指令和授权边界 |
| 01_ANALYSIS.md | 代码证据、客观判断、方案比较 |
| 02_ARCHITECTURE.md | 目标职责、兼容策略、跨层数据约束 |
| 03_WEB_UI_PLAN.md | 八个页面的优化范围、交互与视觉验收 |
| 04_SKILLS_AND_SOURCES.md | 插件/skills 选择、可用性、来源与降级路径 |
| 05_PHASE_TASKS.md | 可逐阶段复制使用的任务指令 |
| 06_ACCEPTANCE_AND_ROLLBACK.md | 测试矩阵、安全隔离、回退 |
| 07_EXECUTION_STATUS.md | 本轮完成情况、基线测试、待完成工作 |
| evidence/baseline.json | 当前源文件哈希、规模、Git 状态及静态契约清单 |
| evidence/baseline-tests.txt | 本轮实际运行的测试及结果 |

## 核心建议

保留 FastAPI + Jinja + 原生 JavaScript；复用已存在的模板片段和 CSS tokens。先建立隔离验证环境，处理执行结果语义和请求可靠性，再进行页面级优化。暂不引入 React/Vue、独立前端构建、云端部署或数据库迁移。

现有 V2 补全方案是本项目的在建子项目，应整合而非重做。本包不批准 V2 生产接管、自动规则晋升或真实 ERP 写入。

本包可以复制到其他机器，但执行者必须重新发现可用 skills 和 Python 环境。不要把本机插件缓存路径写进产品代码或安装包。
