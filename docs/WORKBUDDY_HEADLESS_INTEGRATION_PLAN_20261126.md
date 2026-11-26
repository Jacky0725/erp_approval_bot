# WorkBuddy 无头模式（CodeBuddy CLI）冒烟测试报告与接入方案（v2 修订版）

> **状态**：v2 修订版（已吸收 Codex `WORKBUDDY_HEADLESS_REVIEW_20261126.md` 全部 5 项修订意见与 3 项 CLI 验证决策；阶段 1 获批实施隔离适配器，正式 ERP 写入与整单自动通过暂缓）  
> **日期**：2026-11-26  
> **基准版本**：`1.0.0`（分支 `main`，当前提交 `350d059`）

---

## 一、CodeBuddy CLI 无头模式冒烟测试运行情况

### 1. 测试环境与隔离边界
- **本机环境**：Windows + Python `3.12.10` + Node.js `v24.18.0` + `@tencent-ai/codebuddy-code` `2.187.0`（全局安装于 `%APPDATA%\npm\node_modules\@tencent-ai\codebuddy-code\bin\codebuddy`）。
- **认证与模型**：使用本机已完成登录的 CodeBuddy 账号，默认模型为 `hy4flash-free high`（未额外传 `--model`）。
- **隔离边界**：全程在被 `.gitignore` 忽略的 `tmp/workbuddy_headless_smoke/` 中执行；复制正式规则 `config/rules_structured.xlsx` 到隔离目录，使用 3 条虚构试剂（`乙醇 64-17-5`、`氯化钠 7647-14-5`、`未知神秘试剂XYZ-999 -`），**ERP 写入次数 = 0，正式 SQLite 记忆库写入次数 = 0**。

### 2. 实测迭代中暴露并验证的关键坑点（4 轮实测记录）

| 轮次 | 调用方式 | 现象与根因 | 结论 |
| :---: | :--- | :--- | :--- |
| 第 1 轮 | `codebuddy.cmd -p ... -y`（传统 `WB-EXCEL-v1`） | 45.2s 正常退出，但非交互模式下 `Bash`/`PowerShell` 工具被权限拦截，模型拒绝伪造结果并报错退出。 | 无头模式不应依赖模型执行 Shell/Python。 |
| 第 2 轮 | `codebuddy.cmd -p ... --permission-mode acceptEdits --allowedTools Read,Write,WebSearch,WebFetch` | 90.2s 完整读完 `rules_compact` 全部 13 个规则分段并给出正确判定，但非交互模式下 `Write` 仍被权限拦截。 | 模型只读工具畅通，但不需要依赖模型写文件。 |
| 第 3 轮 | `codebuddy.cmd` → 改为直调 `node .../bin/codebuddy` + `--json-schema` + `--tools Read,WebSearch,WebFetch` | ① Windows 下经 `codebuddy.cmd`（`cmd.exe`）传含 `{}"` 参数会被转义破坏，直调 `node` 可规避；② `--output-format json` 返回的是 **JSON 事件数组**；③ `--json-schema` 内部依赖 `StructuredOutput` 工具，被 `--tools` 排除后导致空转。 | 必须直调 `node` 绕过 `cmd.exe`；需按事件数组解析 `type == "result"`；需单独实测 `StructuredOutput` 工具组合。 |
| **第 4 轮（最终通过）** | `node .../bin/codebuddy -p <prompt> --output-format json --tools Read,WebSearch,WebFetch --no-session-persistence --add-dir <smoke_root>` | **exit=0，总耗时 40.3s，`is_error=False`，`permission_denials=[]`**；stdout 提取纯 JSON → Python 落盘 `judgments.json` → `write_result.py`（`exit=0`）→ **`WorkBuddyBridge.load_results(state)` 100% 通过**。 | **纯只读无头链路端到端验证通过**。 |

---

## 二、WorkBuddy 无头模式接入方案（吸收 Codex 审查修订）

### 1. 硬性红线与核心原则
- **现有正常功能零影响**：
  - `workbuddy.dispatch_mode` **默认保持 `desktop`**；未显式改为 `headless` 时，桌面版 WorkBuddy、V1、钉钉、定时任务、Web UI、ERP 回写、`WorkBuddyAutoPass` 与 Windows 安装包行为 **100% 保持原样**。
  - `src/workbuddy_desktop.py` 与 `src/workbuddy_recovery.py` **零修改**；新能力隔离在 `src/workbuddy_headless.py`。
  - 随时改回 `desktop` 只影响后续新批次，在途批次按自身记录的 `dispatch_mode` 安全收尾。
- **最小权限与最小数据暴露**：
  - 仅开放只读与联网工具（禁用 `Write` / `Bash` / `PowerShell`）；
  - `--add-dir` **仅限定当前批次目录与只读规则目录**（不再暴露整个项目或数据根目录）；
  - 使用独立受控工作目录与 `--strict-mcp-config`，避免隐式加载无关用户/项目 MCP、插件或 hooks；
  - 诊断日志仅提取必要元数据（耗时、退出码、错误分类、turns、denials），不无界留存敏感原始业务输出。
- **分步放开写入与自动通过**：
  - 首轮无头接入默认 `workbuddy.headless.auto_pass_enabled: false`，即使 Web UI / 定时任务已默认开启桌面版自动通过，无头模式也必须按 **只读验收 → 受控分类写入验收 → 单独确认开放自动通过** 三步推进。

### 2. 配置项设计（`config/settings.yaml`）
```yaml
workbuddy:
  dispatch_mode: desktop          # "desktop"（默认）或 "headless"
  headless:
    enabled: false                # 显式启用开关（与 desktop_enabled 解耦，旧配置零破坏）
    auto_pass_enabled: false      # 首轮接入默认关闭整单自动通过，待专项验收后再开启
    node_executable: ""           # 留空自动从 PATH 查找 node.exe
    cli_script: ""                # 留空自动探测全局 @tencent-ai/codebuddy-code/bin/codebuddy
    model: ""                     # 留空沿用账号默认模型；可显式锁定模型 ID
    fallback_model: ""            # 主模型过载/限流时的备用模型
    tools: "Read,WebSearch,WebFetch"
    use_json_schema: false        # 依据 CLI 实测结果决定是否默认启用 StructuredOutput + --json-schema
    timeout_seconds: 900          # 单批总预算（与 20/30/50 批大小映射的 900/1200/1500 秒对齐，含重试）
```

### 3. 崩溃恢复与模式切换状态机（修订项 1 & 3）
- **批次级持久化**：准备/启动无头批次前在 `state.json` 记录 `dispatch_mode: "headless"`、`headless_pid`、`dispatch_started_at`。
- **复用桥接层摘要冻结与原子发布**：不绕过 `WorkBuddyBridge` 直接调写表脚本，而是复用 `WorkBuddyBridge._read_json_judgments` / 摘要冻结 / 原子发布机制，保留 `WB-JSON-v1` 试运行边界与规则快照 SHA-256 校验。
- **崩溃恢复顺序**（按批次自身 `dispatch_mode` 分流）：
  1. 若 `result.xlsx` 已存在：先按 `WorkBuddyBridge.load_results(state)` 校验完整性与规则摘要，通过后直接继续同步 ERP；
  2. 若无 `result.xlsx` 但存在已冻结 `judgments.json` 且摘要一致：验证后继续发布 `result.xlsx`；
  3. 若 `dispatch_state == "sending"` 且无可恢复产物：先核查 `headless_pid` 对应进程是否已退出（未退出则报错阻塞或按停止指令清理进程树），确认旧进程已终止后才允许按只读幂等语义重跑。
- **配置切回 `desktop`**：仅影响后续新准备的批次；已在途的 `headless` 批次仍按 `state["dispatch_mode"]` 恢复处理。

### 4. CLI 技术验证决策（已于 2026-11-26 实测确认）
1. **长输入（已验证通过）**：使用 `node .../bin/codebuddy -p --output-format json --strict-mcp-config --no-session-persistence`，经 **UTF-8 stdin 管道** 传入 30,103 字符（62,177 字节）脱敏中文文本，`exit=0`、耗时 **6.5s**、`is_error=False`，准确返回 `{"count": 500, "last_index": "500", "utf8_ok": true}`，彻底规避 Windows 32KB 命令行长度上限。
2. **结构化输出（已验证通过）**：经 UTF-8 stdin 配合 `--tools Read,WebSearch,WebFetch,StructuredOutput --json-schema <schema> --strict-mcp-config`，`exit=0`、耗时 **7.1s**、`is_error=False`，原生返回 `structured_output: {'batch_id': 'DEMO-001', 'judgments': [...]}`。`src/workbuddy_headless.py` 默认启用 `StructuredOutput` + `--json-schema`，并兼容纯 JSON 文本回退解析；Python 侧独立校验退出码、`is_error`、`batch_id`、记录全集（拒绝截断/缺项/重复/跨批）、类别与判断依据非空。
3. **认证与失败分类**：区分本地依赖检查（`preflight()`）与无业务数据认证探针（`probe_auth()`）；将 `dependency_missing`、`auth_failed`、`rate_limited`、`timeout`、`protocol_error`、`process_failed` 分别归类上报；超时与 `SafeStop` 均调用 `terminate_process_tree()`（Windows 下 `taskkill /T /F`）清理子进程树。

### 5. 统一模式可用性入口（修订项 4 & 5，阶段 2 实施）
- 抽出统一函数 `workbuddy_mode_available(settings)` 供 `approval_flow.py`、`web_app.py`、`scheduler.py` 和 `run.html` 共用（保留 `desktop_enabled` 对旧配置的兼容语义）。
- 定时任务显式配置 `dispatch_mode: headless` 且投递前预检失败时，沿用现有约定：可回退 V1 并强制 `AUTO_PASS=false`；投递后失败绝不切换链路。
- 锁屏与无桌面能力需按目标 Windows 账号分别验证 CLI 判定与 Playwright 无头登录 ERP 的完整只读链路。

### 6. 分阶段验收路线
1. **阶段 1（当前获批）**：完成 2 项 CLI 专项实测 + 实现 `src/workbuddy_headless.py` 与 `tests/test_workbuddy_headless.py`（全部模拟 CLI + 脱敏输入，覆盖中文长输入、异常响应、超时、强停、子进程树清理与摘要冻结原子发布）。
2. **阶段 2（待阶段 1 验收后）**：接入 `approval_flow.py`、`scheduler.py`、`web_app.py`、`web_runner.py`、`run.html` 的双模分流与崩溃恢复测试。
3. **阶段 3（只读业务验收）**：在目标任务环境（含锁屏）重复验证 20 条和 50 条批次，记录首轮协议成功率、端到端耗时、异常恢复与人工复核对比。
4. **阶段 4（正式启用）**：验收通过后另行确认，先开启受控分类写入，再单独确认开放自动通过。
