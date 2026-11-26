# UI Design Specification — Lab Precision 精密实验控制台

## 1. 设计定位与风格
- **产品定位**：本地高密度化学试剂审批自动化与运维控制台（FastAPI + Jinja2 零构建架构）。
- **视觉气质**：**Lab Precision（精密实验控制台）** — 从 `reagent-logo.svg` 提取深海蓝与实验薄荷青基因，克制、高对比度、低视觉疲劳，支持浅色（默认）与深色双主题平滑切换。
- **核心约束**：卡片与面板圆角 `≤ 8px`；保留全部既有路由、表单 `name` 与 DOM `id` 契约；不引入外部字体或第三方前端构建依赖。

---

## 2. 色彩与双主题 Token（`light-dark()`）

采用 CSS `<meta name="color-scheme" content="light dark">` + `:root { color-scheme: light dark; }` + `light-dark(light, dark)`：

| 语义 Token | 浅色模式 (Light) | 深色模式 (Dark) | 用途 |
| :--- | :--- | :--- | :--- |
| `--bg` | `#F3F6F9` | `#0B1120` | 页面画布底色 |
| `--surface` | `#FFFFFF` | `#111827` | 主卡片、面板、弹窗 |
| `--surface-muted` | `#F8FAFC` | `#172033` | 表头、次级区块、输入框底色 |
| `--surface-elevated` | `#FFFFFF` | `#1E293B` | 浮层、Popover、Dialog |
| `--surface-tint` | `#EEF6F5` | `rgba(45, 212, 191, 0.08)` | 选中行、执行摘要微强调底 |
| `--ink` | `#0F172A` | `#F1F5F9` | 主文本 |
| `--ink-soft` | `#334155` | `#CBD5E1` | 次级文本、表单标签 |
| `--muted` | `#64748B` | `#94A3B8` | 辅助说明、占位符 |
| `--line` | `#D8E2EC` | `#1F2937` | 主边框 |
| `--line-soft` | `#E9EFF5` | `#293548` | 内部分割线 |
| `--accent` | `#163A70` | `#38BDF8` | 主操作按钮、链接、焦点环 |
| `--accent-hover` | `#1E4D91` | `#7DD3FC` | 主操作悬停 |
| `--accent-soft` | `#EAF1FB` | `rgba(56, 189, 248, 0.14)` | 浅强调标签底 |
| `--teal` | `#0D9488` | `#2DD4BF` | 实验薄荷青点缀（侧栏激活条、进度节点） |
| `--teal-soft` | `#E6F7F5` | `rgba(45, 212, 191, 0.14)` | 薄荷青浅底 |
| `--green` / `--green-soft` | `#15803D` / `#ECFDF3` | `#4ADE80` / `rgba(74, 222, 128, 0.14)` | 成功、已通过、健康 |
| `--amber` / `--amber-soft` | `#B45309` / `#FFFBEB` | `#FBBF24` / `rgba(251, 191, 36, 0.14)` | 警告、dry_run 测试模式 |
| `--red` / `--red-soft` | `#B91C1C` / `#FEF2F2` | `#F87171` / `rgba(248, 113, 113, 0.14)` | 危险操作、失败、冲突 |
| `--running` / `--running-soft` | `#0369A1` / `#F0F9FF` | `#38BDF8` / `rgba(56, 189, 248, 0.14)` | 运行中、处理中 |

侧边栏固定深海蓝精密基调：`--sidebar-bg: #0F1D32`（Dark 下 `#080E1A`），激活项左侧带 `3px` `--teal` 指示条。

---

## 3. 排版、间距与圆角
- **字体栈**：`"Inter", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", Arial, sans-serif`；等宽栈 `"JetBrains Mono", "Cascadia Mono", Consolas, monospace`。
- **标准字重**：`400`（正文）、`500`（标签）、`600`（小标题/按钮）、`700`（标题/KPI 数字）。
- **等宽数字**：所有表格单元格、KPI、CAS 号、时间戳启用 `font-variant-numeric: tabular-nums`。
- **间距尺度**：`4px` / `8px` / `12px` / `16px` / `20px` / `24px`。
- **圆角**：`--radius-sm: 4px`、`--radius: 8px`（严格 `≤ 8px`）、`--radius-pill: 999px`（仅用于胶囊徽章）。

---

## 4. 布局与响应式断点
统一 **3 套标准断点**：
- `1280px`：中屏笔记本收窄侧边栏或双栏比例微调。
- `960px`：平板与小窗口，运行页/复核页/设置页切换为单栏堆叠，侧边栏自适应顶部或抽屉折叠。
- `680px`：移动端，隐藏次要表格列或转为卡片行布局，禁止横向页面溢出。

---

## 5. 图标、空态插画与动效规范
- **线性 SVG Sprite**：统一 `1.75px` 线宽、`stroke-linecap="round"`，内联在 `layout.html`。
- **4 套定制空态线稿 SVG**：`flask-done`（队列清空）、`search-empty`（无结果）、`terminal-idle`（待运行日志）、`folder-empty`（无产物）。
- **动效 Token**：`--motion-fast: 120ms`、`--motion-base: 180ms`、`--motion-panel: 240ms`，缓动 `cubic-bezier(0.2, 0, 0, 1)`。完整支持 `@media (prefers-reduced-motion: reduce)`。
