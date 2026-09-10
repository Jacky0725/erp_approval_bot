# 插件、skills 与资料选择

## 本轮实际检查

已搜索当前工具元数据和本地 skills 目录，阅读 plugin-management、Product Design index/audit 及其关键约束、OpenAI Docs。已通过网络搜索并打开官方插件、Playwright 截图和 WCAG 资料。

当前未暴露 plugin-management 文档所述的 search_plugins / suggest_plugins 工具，也没有可调用的通用工具搜索入口；因此没有声称完成插件市场全量检索。可用性判断基于当前会话已列出的 skills、工具和本地可读文件。

本轮未安装新插件：已存在的能力覆盖方案所需工作，新增依赖没有得到实际收益证据。用户已允许自行安装；未来出现必要缺口时，可在工具能力允许范围内安装经核验来源的最小集合，不重复索要已获授权。账户连接和远端访问权限仍需实际验证，不能以安装成功代替连接成功。

## 选择矩阵

| 能力 | 当前证据 | 使用场景 | 结论 |
| --- | --- | --- | --- |
| plugin-management | 本地 SKILL.md 可读；目录检索工具未暴露 | 比较已有工具与缺口，按需发现插件 | 本轮已用于选择原则 |
| Product Design audit | skill 可读，浏览器控制工具已提供，连接/页面尚未验证 | 在隔离实例截图后审查操作流与无障碍风险 | P3 首选；本轮只评估流程，未完成审计 |
| Product Design ideate + imagegen | skills 和 imagegen 工具可用 | 明确需要新视觉方向时生成参考方案 | 可选；本次文字方案不需要生成三张概念图 |
| Figma | 多个 Figma 工具已提供；未测试账户访问 | 多人评审、可编辑设计稿、组件映射 | 可选；无设计文件需求时跳过 |
| Playwright Python | 项目现有依赖；本轮没有启动浏览器 | 本地回归、故障注入、截图与交互检查 | 复用；库不是 Codex 插件，安装与浏览器就绪分开验证 |
| Sites | skills 已列出 | 用户明确要求发布独立原型时 | 当前内部审批控制台不采用 |
| 外部 frontend/UI skills | 未核实具体候选来源及适配性 | 现有能力确有缺口时再查 | 不凭热度批量安装 |
| 邮件/日历/协作工具 | 与本轮 UI 目标无直接关系 | 另有明确工作流需求时 | 不安装 |

## 执行时的技能流程

1. 重新核对当前会话能力，不硬编码工具名或缓存版本。读取选中技能的 SKILL.md 后再调用关联工具。
2. Product Design 进入具体工作流前，按其 user-context/preflight 和浏览器选择要求执行。先截图再给 UX 结论；截图必须来自当前验证，不能拿旧材料当新证据。
3. 本项目已有代码目标，默认采用保留风格的改造，不因存在 ideate 就强行启动全新产品设计。若改成全新视觉探索，按所用 skill 的选稿流程执行。
4. Figma 的创建、写入、设计转代码分别先读对应 prerequisite skills；没有必要就不创建文件，也不上传业务截图。
5. 外部工具只使用合成或已获授权的脱敏数据。现有 ERP 账户、试剂业务记录和真实审计截图不自动上传到设计服务。
6. 若浏览器不可用，继续完成代码和本地非浏览器验证，明确将 UI 视觉验收记为未完成，不宣称整个 UI 阶段通过。

## 官方资料（检索日期 2026-09-10）

- [OpenAI 插件说明](https://learn.chatgpt.com/docs/plugins)：理解插件及 skills 的组合与工作流。官方 Codex 插件 URL 本轮重定向至此。不能从此推断用户账号已连接某插件。
- [Playwright Python Screenshots](https://playwright.dev/python/docs/screenshots)：支持页面及元素截图，作为本地视觉证据采集依据。
- [Playwright Python Locators](https://playwright.dev/python/docs/locators)：按角色/标签定位的参考；本轮检索到，执行采用前再打开核验当前版本。
- [W3C WCAG 2.2 新增标准说明](https://www.w3.org/WAI/standards-guidelines/wcag/new-in-22/)：焦点不被遮挡、最小目标尺寸等检查参考；不以此宣称项目已合规。

本地技能位置来自当前会话目录：plugin-management 0.1.0、product-design 0.1.54、Figma 2.0.21。只记录发现版本，不表示已验证每个外部连接；执行时重新发现，不复制整个技能缓存进项目。
