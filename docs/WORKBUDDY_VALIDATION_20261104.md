# 2026-11-04 WorkBuddy 审批链路验证

## 已完成的第一轮

- 使用本地 Web UI 的真实任务接口 /api/run/prepare → /api/run，保留 Origin、CSRF 和单次操作票据检查。不是直接调用 worker，也未关闭写入。
- 清单 SJ202611040011、SJ202611040012；ERP 匹配149条，剩余9条送 WorkBuddy。
- 批次 20261104_145945_92c8c081；9条类别保存并核对、8条库新建并核对、重复支链淀粉1条已有无需修改；9条记忆同步；result.xlsx 自动删除。
- 原有未发送20条批次经核对19条已被ERP分类，保留原文件并结束过时快照，剩余空项重新按ERP当前状态组批。
- 第六条入库发送前本地状态文件替换遭 WinError 5；没有发送第六条入库请求。增加本地原子替换的有限重试（最多约1.55秒）；不重试ERP请求。
- 恢复运行完成剩余4条入库处理；前9条保存均仅尝试1次，前5条库生成没有重复调用。
- 只读复核两张清单空类别均为0，审批状态仍为wait。没有点击顶部通过。

## 正式配置

默认 generate_library；WorkBuddy desktop_enabled/write_enabled=true；ERP API 已验证并启用；dry_run=false；AUTO_PASS=false；scheduler.enabled=false 保持不变。browser.headless=false 用于本轮开发验证。

## 效率观察

- 首次匹配约2秒处理149条空类别，减少约94%的模型输入量，收益最大。
- 首次投递与客户端检查约20秒；结果出现后重新做客户端就绪检查约18秒。可进一步复用发送时已验证的窗口，但须保留批次ID完成标记检查；本轮未为提速削减此检查。
- WorkBuddy 本批从提交到文件发布约133秒，含规则/示例读取及判定，是主要耗时。
- 9条ERP保存约3秒，库处理约1.5秒（剔除中断与人工诊断恢复间隔）；优先保留身份与回读核对，减少这些请求收益有限。
- 最终截图/HTML及结构输出为每次运行一次；先前约12秒，本轮恢复约3秒；相较模型处理优先级较低。
- 每批规则快照、输入、模板、状态及审计保留；仅成功批次删除result.xlsx。仍会累积证据文件，可另设保留期，不宜在本轮未确认追溯需求前批量删除。
- 入库后的模型结论会被ERP后续一键匹配复用；规则更新不会自动追溯修改ERP试剂库，本轮不自动覆盖已有差异记录。

## 边界

技术链路通过不等于化学判定无误；无法唯一映射、模型无法判定、已有库需修改确认等情况应记录并留待核对。此前桌面浏览器工具因URL识别策略限制未能操作，现已使用项目现有Playwright在有头浏览器中补验真正的前端按钮、参数加载和任务结束；未修改浏览器安全设置。

更多原始证据：tmp/erp_discovery/final_live_verification.json；data/workbuddy/20261104_145945_92c8c081/state.json；data/logs/runs/20261104_145933_workbuddy_loop.log、20261104_150340_workbuddy_loop.log。

## 全部待办连续执行结果（15:32后独立复核）

- 后续真实任务按20、20、8三批处理48条；批次20261104_150821_78be4a68、20261104_151734_630541b4、20261104_152817_41c05e30均completed、cleaned=true。
- 合计4批57条模型判定，ERP保存并回读57条，54条库新建并回读，3条重复库已有且无需修改，记忆同步57条；每条类别保存attempts均为1。
- 当前ERP返回10张待办，空类别均为0；此前另外两张已不在待办查询中，不能将其状态变化归因于本程序。
- Web UI最终真实状态success=true、running=false，默认generate_library。最终恢复运行计数28条，前次运行20条与最初9条须结合原日志/批次状态查看，不能把单次恢复运行计数当作全程总数。
- 证据：tmp/erp_discovery/final_all_live_verification.json、webui_formal_all_completed.json及4批state.json。

## 第二处故障及恢复

第二批点击发送后，Electron替换输入框时Value接口出现NULL COM pointer / NoPatternInterfaceError，原消息实际已发送。发送确认改为有限时间内重新枚举并读取控件；失败仍不得重发。增加恢复核对：只在当前项目对话中确认唯一批次ID和空输入框后，将sending校正为submitted，仅继续等待。第二批经此路径恢复，第三批发送确认正常。

## 更具体的效率瓶颈

- 第二批WorkBuddy选择表格子代理及editor_sdk，子任务完成20条判定后因持有Excel文件句柄，多次保存/关闭/重命名失败。最终由WorkBuddy自行完成交接，本程序未结束其进程或读取pending文件。
- 这证明本次Excel交接慢的重要因素是编辑工具链及锁，不是仅因.xlsx格式；没有充分依据直接换为JSON。
- 新准备的项目模式短指令已明确使用本机Python/openpyxl、避免表格子代理/编辑器SDK，分段完整读规则及示例，不创建额外工作日志；不更改判定规则或五列协议。该提示在下一次worker启动后的新批次生效。本轮4批已在此提示更新前投递，未进行同批A/B，不承诺固定提速比例。
- 规则一次性输出截断在客户端真实出现，随后补读；这是读取工具输出截断的证据，不能据此断言模型300k上下文已满。
- 正式流程当前仍依赖可交互的Windows桌面与WorkBuddy窗口；抢焦点、锁屏、权限不一致仍可能影响客户端投递，不能把正式启用等同于纯后台服务。

## 前端补验及页面缓存

- Playwright有头打开真实/run页面，确认默认generate_library和接口后端，点击WorkBuddy模式、勾选SJ202611040012、点击批次循环，前端正常获取CSRF/操作票据并启动任务。该清单已无空项，任务正常结束，无重复审批写入。
- 页面JavaScript pageerror为空。桌面1366px操作正常；390px检查无页面水平滚动，但原有执行参数右列在窄屏有裁切，移动端参数布局仍可另行改进，不影响本轮桌面审批链路。未扩大本轮范围重做布局。
- 再通过前端点击“获取最新清单号”，任务成功、页面缓存由12张刷新为当前10张待办。
- 证据：tmp/erp_discovery/webui_frontend_verified.json、webui_todo_refreshed.json、webui_desktop_verified.png、webui_mobile_verified.png、webui_final_todos.png。
- 前端“最低置信度”和“每批写入条数”是V1参数；WorkBuddy批次大小实际由workbuddy.batch_size=20控制。当前标签未区分两种模式，后续可澄清，不能误认为调整V1每批5条会改变WorkBuddy每批20条。

相关测试：包含ERP API、文件桥、桌面适配、流程和Web状态的178项通过；最后仅项目模式提示调整后，相关141项复验通过。只有FastAPI测试客户端依赖的既有弃用提示，无测试失败。git diff --check通过。
