# 目标架构与兼容约束

## 目标调用关系

页面布局/partials + dashboard.css + dashboard.js
→ web_app 路由与参数校验
→ web_runner 的任务入口、状态查询和应用服务协调
→ approval_flow 的审批编排
→ erp_session / reagent_page / rule_engine / chemical_searcher / approval_writer
→ excel_exports / review_queue / reagent_memory / audit_logger

新补全链路继续复用 reagent_identity、chemical_sources、evidence_resolver、enrichment_v2；沿用既有影子门控。不要为了图示把所有模块强行串成一条线，读模型和日志查询可直接使用对应服务。

## 实施边界

- 保留 browser_bot 的旧导出与入口；保留 web_app:app、原 URL、请求字段和现有 JSON 字段。
- web_runner 继续承担 UI 与自动化的桥接。若抽取内部 helper，优先私有函数或小型服务，在旧模块保留兼容导出；未经必要性分析不引入新的服务框架。
- approval_flow 保留业务顺序和安全门控；先提取纯逻辑，再考虑编排整理。审批写入集中在 approval_writer，不在路由或前端重建决策逻辑。
- rule_engine 是唯一规则解析/决策入口；LLM 仍隔离在 llm_extractor。补全或 UI 改造不得自动提升规则候选或改变未知/冲突字段语义。
- dashboard.html 保留路由模板入口，layout 和现有 partials 继续使用。允许在原 static 目录增加按职责划分的小型 JS 文件；必须检查静态资源版本、加载顺序和 Windows 打包收集。
- dashboard.css 继续作为 token 来源。先消除已验证的重复/覆盖冲突，不做仅降低行数的压缩或重排。

## 需要明确的契约

| 契约 | 要求 | 默认实施方式 |
| --- | --- | --- |
| 写入结果 | attempted / handled / failed / deferred 的含义可区分；保存成功不得从终态集合直接推算 | 先写行为测试与调用链记录；必要时内部适配，不直接改外部返回 |
| 运行状态 | 运行、停止请求、失败、完成与结果时间戳一致；UI 不能把失联显示为新成功 | 先派生 UI 展示状态；需要增加 API 字段时列出兼容影响再确认 |
| 前端请求 | 统一 HTTP/JSON 错误，读请求超时与乱序处理，最后成功更新时间 | 共用请求工具；保持原 endpoint、参数和返回结构 |
| 修改动作 | 双击期间禁用；结果未知不自动重复提交；读取后确认实际结果 | 保留服务端门控，前端防重复只是辅助 |
| 规则证据 | 原始输入、来源、冲突、规则版本和建议可追溯 | 复用已有字段；缺字段显示未知，禁止编造解释 |
| 人工复核 | 编辑输入不被轮询覆盖；翻页、筛选、保存后当前行可定位 | 保留现有 ID/name，补充 dirty 状态与交互测试 |
| 持久化 | 写入失败可诊断；不把某一步完成当全部成功 | 评估同目录临时文件+替换、唯一备用名、恢复记录；迁移数据须另行确认 |
| 审计 | 决策记录与执行结果关联；异常记录脱敏 | 以 audit_logger 为业务审计入口，保留 stage_logger 和性能指标各自用途 |

## 启动与配置

开发验证必须通过独立临时数据目录、测试配置和明确的副作用替身实现。仅设置 dry-run 不足以禁止所有同步、更新、调度和消息行为。

优先使用测试 fixture/临时预览入口替换 scheduler、DingTalk、worker、远程同步和更新入口；若需要生产 app factory 或注入配置，只做兼容扩展并保留默认 app 导出。禁止直接改用户 .env 来“方便测试”。

settings.yaml 的现有键保持；新增键必须有默认值和老配置回退，不能通过覆盖文件合并。不要在本轮改变 V2 enabled/shadow_mode 或真实写入后端的选择。
