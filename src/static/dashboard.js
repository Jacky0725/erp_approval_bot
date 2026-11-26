    const csrfToken = document.querySelector('meta[name="reagent-csrf"]')?.content || "";
    const nativeFetch = window.fetch.bind(window);
    let csrfRefreshPending = false;

    async function recoverFromInvalidCsrf(response, isUnsafeLocalRequest) {
      if (!isUnsafeLocalRequest || response.status !== 403 || csrfRefreshPending) return response;
      let detail = "";
      try {
        const payload = await response.clone().json();
        detail = String(payload?.detail || "");
      } catch (_) {
        // A non-JSON 403 is handled by the caller as before.
      }
      if (!detail.includes("CSRF")) return response;

      // The console token belongs to the running server process. A process
      // restart can leave an already-open tab with the old token. Reload once
      // with a marker to receive the new token; do not loop if a fresh page
      // still cannot pass the server-side check.
      const recoveryUrl = new URL(window.location.href);
      if (recoveryUrl.searchParams.has("csrf_recovered")) return response;
      csrfRefreshPending = true;
      recoveryUrl.searchParams.set("csrf_recovered", "1");
      window.setTimeout(() => window.location.replace(recoveryUrl.toString()), 0);
      return response;
    }

    window.fetch = (input, init = {}) => {
      const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
      const url = new URL(input instanceof Request ? input.url : String(input), window.location.href);
      const isUnsafeLocalRequest = ["POST", "PUT", "PATCH", "DELETE"].includes(method)
        && url.origin === window.location.origin;
      if (isUnsafeLocalRequest) {
        const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
        headers.set("X-Reagent-CSRF", csrfToken);
        init = {...init, headers};
      }
      return nativeFetch(input, init).then(response => recoverFromInvalidCsrf(response, isUnsafeLocalRequest));
    };

    async function operationTicket(operation, options = {}) {
      const response = await fetch(`/api/${operation}/prepare`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(options),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "无法确认危险操作");
      return payload.ticket;
    }

    const runForm = document.querySelector("#runForm");
    const settingsForm = document.querySelector("#settingsForm");
    const reviewState = {
      rows: [],
      listNumber: "",
      sortDirection: "desc",
      pointerInside: false,
      page: 1,
      perPage: 20,
      pages: 1,
      total: 0,
      selectedKey: "",
      selectedRowElement: null,
    };
    const todoState = {
      selected: new Set(),
      tasks: [],
    };
    const memoryState = {
      rows: [],
      categories: [],
      page: 1,
      pages: 1,
      perPage: 10,
      total: 0,
      requestSeq: 0,
      loading: false,
    };
    const dashboardData = JSON.parse(document.querySelector("#dashboard-data")?.textContent || "{}");
    const activePage = dashboardData.activePage || document.body?.dataset?.page || "overview";
    let reviewCategories = dashboardData.reviewDecisionOptions || [];
    const llmProviders = dashboardData.llmProviderOptions || [];
    const currentLlm = dashboardData.currentLlm || { provider: "", baseUrl: "", model: "" };
    const initialRuntime = dashboardData.runtime || {};
    const initialScheduler = dashboardData.scheduler || {};
    const approvalWriteModeLabels = {
      disabled: "禁用网页写入",
      multi_page: "全清单分页保存",
      generate_library: "保存并生成试剂库",
    };
    const erpWriteBackendLabels = {
      web_ui: "页面写入",
      api_read_web_write: "接口读取 + 页面写入",
      api_write_with_web_verify: "接口写入 + 接口/页面校验",
    };
    const erpApiDiscoveryLabels = {
      disabled: "未启用",
      pending_capture: "采集中",
      candidate: "候选待确认",
      pending_canary: "待金丝雀验证",
      active: "已启用",
      verified: "已验证启用",
      rejected: "已回退",
    };
    const logState = {
      lines: [],
      filter: "all",
    };
    let approvalStateLoaded = false;
    let reviewStateLoaded = false;
    let artifactStateLoaded = false;
    let logStateLoaded = false;
    let lastFinishedAt = "";
    const $ = (selector) => document.querySelector(selector);

    function on(selector, eventName, handler) {
      const element = $(selector);
      if (element) element.addEventListener(eventName, handler);
    }

    function onAll(selector, eventName, handler) {
      document.querySelectorAll(selector).forEach((element) => element.addEventListener(eventName, handler));
    }

    function setText(selector, value) {
      const element = $(selector);
      if (element) element.textContent = value;
    }

    function setClass(selector, value) {
      const element = $(selector);
      if (element) element.className = value;
    }

    function setDisabled(selector, value) {
      const element = $(selector);
      if (element) element.disabled = value;
    }

    function confirmAction(options) {
      if (typeof window.uiConfirm === "function") return window.uiConfirm(options);
      return Promise.resolve(window.confirm(options?.message || "确认继续？"));
    }

    function notifyToast(message, tone = "info") {
      if (typeof window.uiToast === "function") {
        window.uiToast(message, {tone});
        return;
      }
      if (tone === "error") window.alert(message);
    }

    function initSettingsSections() {
      document.querySelectorAll(".settings-section").forEach((section) => {
        const head = section.querySelector(":scope > .settings-section-head");
        if (!head || head.classList.contains("compact-head") || head.querySelector(".settings-section-toggle")) return;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "settings-section-toggle";
        const expanded = section.dataset.defaultOpen === "true";
        section.classList.toggle("settings-section-collapsed", !expanded);
        button.textContent = expanded ? "收起" : "展开";
        button.setAttribute("aria-expanded", String(expanded));
        button.addEventListener("click", () => {
          const collapsed = section.classList.toggle("settings-section-collapsed");
          button.textContent = collapsed ? "展开" : "收起";
          button.setAttribute("aria-expanded", String(!collapsed));
        });
        head.appendChild(button);
      });
    }

    function initApprovalModeSwitch() {
      const switches = Array.from(document.querySelectorAll("[data-approval-mode-switch]"));
      const panels = Array.from(document.querySelectorAll("[data-approval-mode-panel]"));
      if (!switches.length || !panels.length) return;

      const setMode = (mode) => {
        if (runForm) runForm.dataset.approvalMode = mode;
        switches.forEach((button) => {
          button.setAttribute("aria-pressed", String(button.dataset.approvalModeSwitch === mode));
        });
        panels.forEach((panel) => {
          panel.hidden = panel.dataset.approvalModePanel !== mode;
        });
        updateExecutionSummary();
      };

      switches.forEach((button) => {
        button.addEventListener("click", () => setMode(button.dataset.approvalModeSwitch));
      });
      const selectedMode = switches.find((button) => button.getAttribute("aria-pressed") === "true")?.dataset.approvalModeSwitch;
      setMode(selectedMode || "workbuddy");
    }

    function setSelectValue(form, name, value) {
      if (!form) return;
      const field = form.querySelector(`[name="${name}"]`);
      if (!field) return;
      const candidate = value || field.value;
      if (field.tagName === "SELECT") {
        const hasOption = Array.from(field.options || []).some((option) => option.value === candidate);
        if (hasOption) {
          field.value = candidate;
        } else if (name.includes("approval_write_mode") && field.options.length) {
          field.value = "disabled";
        }
        return;
      }
      field.value = candidate;
    }

    function approvalWriteModeLabel(value) {
      return approvalWriteModeLabels[value] || safe(value);
    }

    function erpWriteBackendLabel(value) {
      return erpWriteBackendLabels[value] || safe(value);
    }

    function erpApiDiscoveryLabel(value) {
      return erpApiDiscoveryLabels[value] || safe(value);
    }

    function updateDryRunUi(value) {
      const enabled = String(value || "").toLowerCase() === "true";
      if (runForm) runForm.dataset.dryRun = String(enabled);
      setText("#dryRunText", enabled ? "开启" : "关闭");
      const pill = document.querySelector("#dryRunPill");
      if (pill) pill.classList.toggle("dry-run-active", enabled);
      const warning = document.querySelector("#dryRunWarning");
      if (warning) warning.hidden = !enabled;
      const globalBanner = document.querySelector("#globalDryRunBanner");
      if (globalBanner) globalBanner.hidden = !enabled;
      updateExecutionSummary();
    }

    function setCheckbox(form, name, value) {
      if (!form) return;
      const field = form.querySelector(`[name="${name}"]`);
      if (field) field.checked = String(value || "").toLowerCase() === "true";
    }

    setSelectValue(runForm, "approval_write_mode", initialRuntime.approval_write_mode);
    setSelectValue(runForm, "erp_write_backend", initialRuntime.erp_write_backend);
    setSelectValue(settingsForm, "approval_write_mode", initialRuntime.approval_write_mode);
    setSelectValue(settingsForm, "erp_write_backend", initialRuntime.erp_write_backend);
    setCheckbox(runForm, "process_all_todos", initialRuntime.process_all_todos);
    setCheckbox(runForm, "auto_pass", initialRuntime.auto_pass);
    setCheckbox(settingsForm, "process_all_todos", initialRuntime.process_all_todos);
    setCheckbox(settingsForm, "app_dry_run", initialRuntime.app_dry_run);
    setCheckbox(settingsForm, "auto_pass", initialRuntime.auto_pass);
    setCheckbox(settingsForm, "scheduler_enabled", initialRuntime.scheduler_enabled);
    setCheckbox(settingsForm, "scheduler_use_default_run_policy", initialRuntime.scheduler_use_default_run_policy || "true");
    setCheckbox(settingsForm, "scheduler_skip_manual_review_lists", initialRuntime.scheduler_skip_manual_review_lists || "true");
    setCheckbox(settingsForm, "dingtalk_notification_enabled", initialRuntime.dingtalk_notification_enabled);
    setCheckbox(settingsForm, "dingtalk_at_all", initialRuntime.dingtalk_at_all || "true");
    setCheckbox(settingsForm, "dingtalk_stream_enabled", initialRuntime.dingtalk_stream_enabled);
    setCheckbox(settingsForm, "dingtalk_chat_enabled", initialRuntime.dingtalk_chat_enabled);
    setCheckbox(settingsForm, "dingtalk_chat_use_main_llm", initialRuntime.dingtalk_chat_use_main_llm);
    function renderDingtalkConnection(stream = {}) {
      const labels = {disabled: "未启用", starting: "启动中", connecting: "连接中", connected: "已连接", reconnecting: "等待重连", disconnected: "已断开", stopping: "正在退出", stopped: "已停止", error: "启动失败"};
      setText("#dingtalkConnectionState", stream.connected ? "已连接" : (labels[stream.connection_state] || "未连接"));
      setText("#dingtalkThreadState", stream.running ? "存活" : "未运行");
      setText("#dingtalkReconnectCount", stream.reconnect_count || 0);
      setText("#dingtalkLastConnected", stream.last_connected_at || "-");
      setText("#dingtalkLastReceived", stream.last_received_at || "-");
      setText("#dingtalkConnectionError", stream.last_error || "");
    }
    renderDingtalkConnection(dashboardData.dingtalkStream || {});
    setCheckbox(settingsForm, "memory_sync_enabled", initialRuntime.memory_sync_enabled);
    setCheckbox(
      settingsForm,
      "memory_sync_auto_upload_after_memory_change",
      initialRuntime.memory_sync_auto_upload_after_memory_change
    );
    setCheckbox(settingsForm, "memory_sync_check_remote_on_startup", initialRuntime.memory_sync_check_remote_on_startup);
    setSelectValue(settingsForm, "scheduler_mode", initialRuntime.scheduler_mode);
    setSelectValue(settingsForm, "scheduler_pipeline", initialRuntime.scheduler_pipeline);
    setSelectValue(settingsForm, "scheduler_approval_write_mode", initialRuntime.scheduler_approval_write_mode);
    setText("#writeModeText", approvalWriteModeLabel(initialRuntime.approval_write_mode));
    setText("#erpApiDiscoveryText", erpApiDiscoveryLabel(initialRuntime.erp_api_discovery_status));
    updateDryRunUi(initialRuntime.app_dry_run);
    setText("#schedulerNextRun", safe(initialScheduler.next_run_at));
    setText("#schedulerLastRun", safe(initialScheduler.last_run_at));
    setText("#schedulerLastResult", safe(initialScheduler.last_result));
    setText("#schedulerLastMessage", safe(initialScheduler.last_message));
    initLlmSettings();
    initIdentityEnrichmentSettings();
    initSectionNav();
    initSidebarToggle();
    initApprovalModeSwitch();
    initSettingsSections();
    initSchedulerPolicyToggle();

    on("#resetDingtalkChatPersonaButton", "click", () => {
      const input = document.querySelector("#dingtalkChatPersonaPrompt");
      if (!input) return;
      input.value = initialRuntime.dingtalk_chat_default_persona_prompt || "";
      input.dispatchEvent(new Event("input", {bubbles: true}));
    });

    on("#refreshLogButton", "click", async () => {
      setText("#logCopyState", "刷新中...");
      await refreshLogTail({force: true});
      setText("#logCopyState", "已刷新");
    });

    on("#copyLogButton", "click", async () => {
      const text = $("#logBox")?.textContent || "";
      if (!text.trim()) {
        setText("#logCopyState", "暂无日志");
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
        setText("#logCopyState", "已复制");
      } catch (error) {
        setText("#logCopyState", "复制失败");
      }
    });

    document.querySelectorAll("[data-log-filter]").forEach((button) => {
      button.addEventListener("click", () => {
        logState.filter = button.dataset.logFilter || "all";
        document.querySelectorAll("[data-log-filter]").forEach((item) => {
          item.classList.toggle("active", item === button);
          item.setAttribute("aria-pressed", String(item === button));
        });
        renderLogBox();
      });
    });

    on("#refreshArtifactButton", "click", async () => {
      setText("#artifactRefreshState", "刷新中...");
      await refreshArtifacts({force: true});
      setText("#artifactRefreshState", "已刷新");
    });

    on("#repairDataHealthButton", "click", async () => {
      if (!(await confirmAction({title: "执行数据健康修复", message: "确认执行数据健康修复？", detail: "系统会先自动备份试剂记忆库和人工复核队列。"}))) return;
      setDisabled("#repairDataHealthButton", true);
      setText("#dataHealthRepairState", "修复中...");
      try {
        const response = await fetch("/api/data_health/repair", { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || payload.message || "修复失败");
        }
        refreshDataHealth.loaded = false;
        reviewStateLoaded = false;
        memoryState.loading = false;
        await refreshDataHealth({force: true});
        await refreshReviewQueue({force: true});
        if (document.querySelector("#memoryTable")) await loadMemory({force: true});
        const rebuilt = payload.review_queue?.rebuilt_memory ?? 0;
        const disabled = payload.memory?.disabled_records ?? 0;
        const unsafe = payload.memory?.unsafe_disabled ?? 0;
        const duplicates = payload.review_queue?.blocking_duplicates_resolved ?? 0;
        setText(
          "#dataHealthRepairState",
          `已修复：补入 ${rebuilt}，停用 ${Number(disabled) + Number(unsafe)}，重复 ${duplicates}`
        );
      } catch (error) {
        setText("#dataHealthRepairState", `修复失败：${error.message || error}`);
      } finally {
        setDisabled("#repairDataHealthButton", false);
      }
    });

    function buildRunRequestData(form, action, selectedNumbers) {
      const data = new FormData(form);
      data.set("action", action);
      data.set("target_list_numbers", selectedNumbers.join(","));
      data.set("process_all_todos", data.has("process_all_todos") ? "true" : "false");
      data.set("auto_pass", data.has("auto_pass") ? "true" : "false");
      if (action.startsWith("workbuddy_")) data.set("auto_pass", "false");
      if (action !== "workbuddy_loop") data.delete("workbuddy_batch_size");
      return data;
    }

    function updateExecutionSummary() {
      if (!runForm) return;
      const workbuddy = runForm.dataset.approvalMode !== "v1";
      const selected = selectedTodoNumbers();
      const field = (name) => runForm.elements.namedItem(name);
      const all = field("process_all_todos").checked;
      const mode = field("approval_write_mode").value;
      const dry = runForm.dataset.dryRun === "true";
      const rawLimit = field("process_all_todos_max").value.trim();
      const limit = /^[+-]?\d+$/.test(rawLimit) ? Math.max(1, Number(rawLimit)) : 50;
      const scope = selected.length
        ? `${selected.length} 个清单：${selected.join("、")}${!workbuddy && limit < selected.length ? `（本次最多处理 ${limit} 个）` : ""}`
        : all ? (workbuddy ? "全部待办（按 WorkBuddy 执行规则处理）" : `全部待办（最多处理 ${limit} 个）`)
          : workbuddy ? "尚未选择：请选择清单或启用全部待办"
            : "未选清单：按 V1 默认待办范围执行";
      const impact = dry || mode === "disabled"
        ? `${dry ? "测试模式" : "仅生成建议"} · 不写入 ERP、不点击通过`
        : `${approvalWriteModeLabel(mode)} · ${workbuddy || !field("auto_pass").checked ? "不点击通过" : "仅在安全校验通过后自动通过"}${workbuddy ? " · 回写须通过 WorkBuddy 和 ERP 接口校验" : ""}`;
      setText("#executionMode", workbuddy ? "WorkBuddy" : "V1");
      setText("#executionScope", scope);
      setText("#executionImpact", impact);
      setText('#run button[data-action="suggestions"]', dry || mode === "disabled" ? "生成审批建议" : "启动 V1 审批");
    }

    document.querySelectorAll("button[data-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (runLaunchPending) return;
        runLaunchPending = true;
        const feedback = document.querySelector("#runLaunchFeedback");
        const report = (message, tone = "") => {
          if (!feedback) return;
          feedback.hidden = false;
          feedback.textContent = message;
          feedback.dataset.tone = tone;
        };
        let submitted = false;
        report("正在请求启动…");
        try {
          const data = buildRunRequestData(runForm, button.dataset.action, selectedTodoNumbers());
          data.set("operation_ticket", await operationTicket("run", Object.fromEntries(data.entries())));
          submitted = true;
          const response = await fetch("/api/run", { method: "POST", body: data });
          const payload = await response.json();
          if (!response.ok || !payload.started) {
            report(apiErrorMessage(payload), "error");
          } else {
            report("任务已启动，可在运行日志查看进度。");
          }
          await Promise.allSettled([refreshStatus(), refreshReviewQueue({force: true})]);
        } catch (error) {
          report(submitted ? "未收到启动结果，请先检查运行状态再重试。" : dashboardRequestErrorMessage(error, "未能启动任务。"), "error");
        } finally {
          runLaunchPending = false;
        }
      });
    });
    runForm?.addEventListener("change", updateExecutionSummary);
    runForm?.addEventListener("input", updateExecutionSummary);
    let runLaunchPending = false;

    if (document.querySelector("#configTransfer")) {
      const passphrase = document.querySelector("#configTransferPassphrase");
      const profile = document.querySelector("#configTransferFile");
      const message = document.querySelector("#configTransferMessage");
      const changes = document.querySelector("#configTransferChanges");
      const apply = document.querySelector("#applyConfigButton");
      let previewTicket = "";
      let pending = false;
      function clearConfigPreview() {
        previewTicket = "";
        apply.hidden = true;
        changes.replaceChildren();
      }
      passphrase.addEventListener("input", clearConfigPreview);
      profile.addEventListener("change", clearConfigPreview);

      async function transferConfig(action) {
        if (pending) return;
        const data = new FormData();
        data.set("passphrase", passphrase.value);
        if (action !== "export") {
          if (!profile.files[0]) { message.textContent = "请先选择配置文件。"; return; }
          data.set("profile", profile.files[0]);
        }
        if (action === "import") {
          if (!previewTicket) return;
          data.set("ticket", previewTicket);
        }
        clearConfigPreview();
        pending = true;
        message.textContent = "";
        const endpoint = action === "preview" ? "import/preview" : action;
        try {
          const response = await fetch("/api/settings/" + endpoint, {method: "POST", body: data});
          if (!response.ok) {
            const error = await response.json();
            throw new Error(typeof error.detail === "string" ? error.detail : "配置操作失败。");
          }
          if (action === "export") {
            const url = URL.createObjectURL(await response.blob());
            const link = document.createElement("a");
            link.href = url;
            link.download = "reagent-approval.rabconfig";
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
            passphrase.value = "";
            message.textContent = "加密配置已下载。";
          } else if (action === "preview") {
            const result = await response.json();
            for (const row of result.changes) {
              const item = document.createElement("li");
              item.textContent = `${row.safety ? "运行策略 · " : ""}${row.field}：${row.before ?? "未配置"} → ${row.after ?? "未配置"}`;
              changes.append(item);
            }
            previewTicket = result.ticket;
            apply.hidden = !result.changes.length;
            message.textContent = result.changes.length ? "请核对差异后应用。预览有效期为 60 秒。" : "配置一致，无需导入。";
          } else {
            passphrase.value = "";
            window.location.reload();
          }
        } catch (error) {
          message.textContent = error.message;
        } finally {
          pending = false;
        }
      }
      on("#exportConfigButton", "click", () => transferConfig("export"));
      on("#previewConfigButton", "click", () => transferConfig("preview"));
      on("#applyConfigButton", "click", () => transferConfig("import"));
    }

    on("#stopRunButton", "click", async () => {
      if (!(await confirmAction({title: "紧急强制停止", message: "确定强制结束当前任务？", detail: "可能导致 ERP 或 WorkBuddy 结果待核对，且不会撤销已保存结果。", confirmText: "确认强制停止", danger: true}))) return;
      const response = await fetch("/api/stop", { method: "POST" });
      const payload = await response.json();
      await refreshStatus();
      await refreshReviewQueue({force: true});
      if (!payload.stopped) {
        notifyToast(payload.message || payload.detail || "当前没有运行中的任务", "warning");
      }
    });

    let currentControlTaskId = "";
    let controlRunning = false;
    const reviewedStoppedTasks = new Set();
    let stoppedTasksLoading = false;
    async function controlRequest(path, payload) {
      const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "任务控制请求失败");
      return data;
    }
    on("#safeStopRunButton", "click", async () => {
      const taskId = currentControlTaskId;
      if (!taskId || !(await confirmAction({title: "安全停止任务", message: `安全停止任务 ${taskId.slice(0, 12)}？`, detail: "已发出的操作会完成核对，已保存结果不会撤销。", confirmText: "确认安全停止"}))) return;
      try {
        const ticket = await controlRequest("/api/safe-stop/prepare", {task_id: taskId});
        const result = await controlRequest("/api/safe-stop", {task_id: taskId, ticket: ticket.ticket});
        notifyToast(result.message || "停止请求已提交", "success");
        await refreshStatus();
      } catch (error) { notifyToast(error.message || String(error), "error"); }
    });
    async function refreshStoppedTasks() {
      const body = document.querySelector("#stoppedTasksTable");
      if (!body || stoppedTasksLoading) return;
      stoppedTasksLoading = true;
      try {
        const data = await readDashboardJSON("/api/stopped-tasks");
        body.innerHTML = "";
        for (const task of data.tasks || []) {
          const row = document.createElement("tr");
          const details = task.details || {};
          row.innerHTML = `<td>${escapeHtml(task.task_id.slice(0, 12))}</td><td>${escapeHtml(task.action)}</td><td>${escapeHtml(task.phase)}</td><td>${escapeHtml(String(details.saved ?? "-"))} / ${escapeHtml(String(details.pending_library ?? "-"))}</td><td><label class="check"><input type="checkbox" aria-label="已人工核对 ERP、投递与结果文件">已核对</label></td><td><button type="button">核对恢复</button></td>`;
          const check = row.querySelector("input");
          const button = row.querySelector("button");
          if (task.state === "needs_review" || task.state === "failed") row.cells[2].textContent = `待核对 · ${task.phase}`;
          if (task.lists?.length) row.cells[2].append(` · ${task.lists.join("、")}`);
          if (task.review_issue) {
            row.cells[4].textContent = "待核对";
            row.cells[5].textContent = task.review_issue;
            body.appendChild(row);
            continue;
          }
          if (task.can_discard) button.textContent = "作废旧结果";
          check.checked = reviewedStoppedTasks.has(task.task_id);
          button.disabled = controlRunning || !check.checked;
          check.addEventListener("change", () => {
            if (check.checked) reviewedStoppedTasks.add(task.task_id); else reviewedStoppedTasks.delete(task.task_id);
            button.disabled = controlRunning || !check.checked;
          });
          button.addEventListener("click", async () => {
            const message = task.can_discard
              ? "作废这批旧结果？下一次批次循环会回读 ERP，只重新判定仍为空的明细。"
              : `已人工核对任务 ${task.task_id.slice(0, 12)} 的 ERP、投递状态与结果文件，并确认只恢复核对过的剩余步骤？结果不明的操作不会自动重发。`;
            if (!check.checked || !(await confirmAction({title: "恢复任务确认", message, confirmText: "确认操作"}))) return;
            button.disabled = true;
            try {
              const payload = {task_id: task.task_id, verified: true, discard: Boolean(task.can_discard)};
              const ticket = await controlRequest("/api/resume/prepare", payload);
              const result = await controlRequest("/api/resume", {...payload, ticket: ticket.ticket});
              if (result.discarded) notifyToast("旧批次已作废。请刷新待办后启动批次循环。", "success");
              else if (!result.started) notifyToast(result.message || "任务未恢复", "warning");
              reviewedStoppedTasks.delete(task.task_id);
              await refreshStatus();
            } catch (error) { notifyToast(error.message || String(error), "error"); }
          });
          body.appendChild(row);
        }
        if (!body.children.length) body.innerHTML = '<tr><td colspan="6">暂无停止或待核对任务</td></tr>';
      } catch (error) { body.innerHTML = `<tr><td colspan="6">${escapeHtml(error.message || String(error))}</td></tr>`; }
      finally { stoppedTasksLoading = false; }
    }
    function renderTaskControl(status = {}) {
      currentControlTaskId = status.task_id || "";
      controlRunning = Boolean(status.running);
      const control = status.control || {};
      setText("#controlTaskId", currentControlTaskId ? currentControlTaskId.slice(0, 12) : "-");
      setText("#controlTaskState", control.label || "-");
      setText("#controlTaskPhase", control.phase || "-");
      setDisabled("#safeStopRunButton", !status.running || !currentControlTaskId || control.state === "stop_requested");
      refreshStoppedTasks();
    }

    on("#restartRunButton", "click", async () => {
      if (!(await confirmAction({title: "重启 Web UI", message: "确认重启 Web UI 服务？", detail: "仅在没有运行任务时允许，页面将在重启完成后自动刷新。", confirmText: "确认重启"}))) return;
      try {
        const response = await fetch("/api/restart", {method: "POST"});
        const payload = await response.json();
        if (!response.ok || !payload.restarting) throw new Error(payload.detail || payload.message || "程序未重启");
        setText("#statusLine", "Web UI 正在重启，等待新服务启动。");
        for (let attempt = 0; attempt < 30; attempt++) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          try {
            const health = await fetch("/api/health", {cache: "no-store"});
            if (health.ok && (await health.json()).process_id !== payload.old_process_id) { window.location.reload(); return; }
          } catch (_) { /* Waiting for the replacement service. */ }
        }
        notifyToast("新服务未能在 30 秒内启动，请检查 data/logs/web_ui_restart.log。", "error");
      } catch (error) { notifyToast(error.message || String(error), "error"); }
    });
    on("#exitProgramButton", "click", async () => {
      if (!(await confirmAction({title: "退出程序", message: "退出 Web UI 并关闭项目后台进程？", detail: "已保存的数据与配置不会删除。", confirmText: "确认退出", danger: true}))) return;
      try {
        const prepared = await controlRequest("/api/exit/prepare", {});
        const result = await controlRequest("/api/exit", {ticket: prepared.ticket});
        if (!result.exiting) throw new Error(result.message || "退出未启动");
        setText("#statusLine", "Web UI 正在退出，可关闭此页面。");
      } catch (error) { notifyToast(error.message || String(error), "error"); }
    });

    let lastUpdateInfo = null;

    onAll('[data-update-action="check"]', "click", async (event) => {
      await checkForUpdate(event.currentTarget);
    });

    onAll('[data-update-action="install"]', "click", async () => {
      if (!lastUpdateInfo?.update_available) {
        notifyToast("当前没有可安装的更新。", "warning");
        return;
      }
      if (!(await confirmAction({title: "安装程序更新", message: `确认下载并安装 ${lastUpdateInfo.latest_version}？`, detail: "程序会自动退出并启动安装器。", confirmText: "下载并安装"}))) return;
      setUpdateMessage("正在下载更新包并启动安装器，请稍候...");
      setAllDisabled('[data-update-action="install"]', true);
      const ticket = await operationTicket("update", {});
      const response = await fetch("/api/update/install", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({operation_ticket: ticket}),
      });
      const payload = await response.json();
      if (!response.ok || !payload.started) {
        setUpdateMessage(payload.detail || payload.message || "更新未启动。");
        setAllDisabled('[data-update-action="install"]', !lastUpdateInfo?.update_available);
        return;
      }
      setUpdateState("", "安装器已启动");
      setUpdateMessage(payload.message || "安装器已启动，当前程序即将退出。");
    });

    if (document.querySelector('[data-update-action="check"]')) {
      window.setTimeout(() => {
        try {
          const cached = JSON.parse(sessionStorage.getItem("reagent_update_cache_v1") || "null");
          if (cached && Date.now() - cached.ts < 300000 && cached.payload) {
            lastUpdateInfo = cached.payload;
            if (cached.payload.update_available) {
              setUpdateState("update-available", `有新版本 ${cached.payload.latest_version || ""}`);
              document.querySelectorAll('[data-update-action="install"]').forEach((item) => {
                item.hidden = false;
                item.disabled = !initialRuntime.app_frozen;
              });
            } else if (cached.payload.ok) {
              setUpdateState("update-current", "已是最新版本");
            }
            return;
          }
        } catch (_) {}
        checkForUpdate();
      }, 800);
    }

    on("#testMemorySyncButton", "click", async () => {
      await runMemorySyncAction("/api/memory/sync/test", "正在测试连接...");
    });

    on("#uploadMemorySyncButton", "click", async () => {
      if (!(await confirmAction({title: "上传试剂记忆库", message: "确认上传本地试剂记忆库到坚果云？"}))) return;
      await runMemorySyncAction("/api/memory/sync/upload", "正在上传试剂库...");
    });

    on("#downloadMemorySyncButton", "click", async () => {
      if (!(await confirmAction({title: "下载试剂记忆库", message: "确认下载云端试剂记忆库？", detail: "下载前会自动备份本地库。"}))) return;
      await runMemorySyncAction("/api/memory/sync/download", "正在下载试剂库...");
    });

    on("#forceDownloadMemorySyncButton", "click", async () => {
      if (!(await confirmAction({title: "强制覆盖本地库", message: "确认强制用云端试剂库覆盖本地库？", detail: "覆盖前会自动备份当前本地库。", confirmText: "强制覆盖", danger: true}))) return;
      const data = new FormData();
      data.set("force", "true");
      await runMemorySyncAction("/api/memory/sync/download", "正在强制下载试剂库...", data);
    });

    if (document.querySelector("#memorySyncStateText")) {
      refreshMemorySyncStatus(Boolean(initialRuntime.memory_sync_check_remote_on_startup === "true"));
    }

    on("#selectAllTodos", "click", () => {
      todoState.selected = new Set(todoState.tasks.map((task) => task.list_number).filter(Boolean));
      if (todoState.snapshot) renderTodoTasks(todoState.snapshot, true);
    });

    on("#clearTodos", "click", () => {
      todoState.selected.clear();
      if (todoState.snapshot) renderTodoTasks(todoState.snapshot, true);
    });

    function providerById(providerId) {
      return llmProviders.find((provider) => provider.id === providerId) || llmProviders[0] || {};
    }

    function setModelOptions(models, selectedModel, selector = "#llmModelSelect") {
      const modelSelect = document.querySelector(selector);
      if (!modelSelect) return;
      const values = Array.from(new Set([selectedModel, ...(models || [])].filter(Boolean)));
      modelSelect.innerHTML = "";
      if (!values.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "请先获取模型列表";
        modelSelect.appendChild(option);
        return;
      }
      values.forEach((model) => {
        const option = document.createElement("option");
        option.value = model;
        option.textContent = model;
        modelSelect.appendChild(option);
      });
      modelSelect.value = selectedModel && values.includes(selectedModel) ? selectedModel : values[0];
    }

    function initLlmSettings() {
      const providerSelect = document.querySelector("#llmProviderSelect");
      const baseUrlInput = document.querySelector("#llmBaseUrlInput");
      if (!providerSelect || !baseUrlInput) return;
      providerSelect.innerHTML = "";
      llmProviders.forEach((provider) => {
        const option = document.createElement("option");
        option.value = provider.id;
        option.textContent = provider.label;
        providerSelect.appendChild(option);
      });
      providerSelect.value = currentLlm.provider || "siliconflow";
      setModelOptions(providerById(providerSelect.value).default_models || [], currentLlm.model);

      providerSelect.addEventListener("change", () => {
        const provider = providerById(providerSelect.value);
        if (provider.id !== "openai_compatible") {
          baseUrlInput.value = provider.base_url || "";
        }
        setModelOptions(provider.default_models || [], "");
        document.querySelector("#llmModelStatus").textContent = provider.notes || "填入 API Key 后可自动读取该平台模型列表。";
      });
    }

    function initIdentityEnrichmentSettings() {
      const modelSelect = document.querySelector("#identityEnrichmentModelSelect");
      const baseUrlInput = document.querySelector("#identityEnrichmentBaseUrlInput");
      if (!modelSelect || !baseUrlInput) return;
      const bailian = providerById("aliyun_bailian");
      const selectedModel = initialRuntime.identity_enrichment_model || "qwen3.7-flash";
      setModelOptions(bailian.default_models || [], selectedModel, "#identityEnrichmentModelSelect");

      on("#refreshIdentityEnrichmentModels", "click", async (event) => {
        const button = event.currentTarget;
        const status = document.querySelector("#identityEnrichmentModelStatus");
        const apiKeyInput = settingsForm?.querySelector('[name="identity_enrichment_api_key"]');
        button.disabled = true;
        status.textContent = "正在读取百炼模型列表...";
        try {
          const response = await fetch("/api/llm/models", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
              provider: "aliyun_bailian",
              base_url: baseUrlInput.value,
              api_key: apiKeyInput?.value || "",
            }),
          });
          const payload = await response.json();
          const currentModel = modelSelect.value || selectedModel;
          setModelOptions(payload.models || [], currentModel, "#identityEnrichmentModelSelect");
          status.textContent = payload.ok
            ? `已读取 ${payload.models.length} 个百炼模型。`
            : (payload.error || "未能读取模型列表；可从默认模型中选择。");
        } catch (error) {
          status.textContent = `读取模型列表失败：${String(error)}；当前仍可选择默认模型。`;
        } finally {
          button.disabled = false;
        }
      });
    }

    on("#refreshLlmModels", "click", async () => {
      const providerSelect = document.querySelector("#llmProviderSelect");
      const baseUrlInput = document.querySelector("#llmBaseUrlInput");
      const apiKeyInput = settingsForm?.querySelector('[name="llm_api_key"]');
      const status = document.querySelector("#llmModelStatus");
      status.textContent = "正在读取模型列表...";
      const response = await fetch("/api/llm/models", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          provider: providerSelect.value,
          base_url: baseUrlInput.value,
          api_key: apiKeyInput?.value || "",
        }),
      });
      const payload = await response.json();
      const currentModel = document.querySelector("#llmModelSelect").value;
      setModelOptions(payload.models || [], currentModel);
      status.textContent = payload.ok
        ? `已读取 ${payload.models.length} 个模型。`
        : (payload.error || "没有读取到模型列表，已保留默认模型。");
    });

    function initSectionNav() {
      const links = Array.from(document.querySelectorAll('nav a[href^="#"]'));
      const sections = links
        .map((link) => document.querySelector(link.getAttribute("href")))
        .filter(Boolean);
      if (!links.length || !sections.length) return;

      const setActive = (id) => {
        links.forEach((link) => {
          link.classList.toggle("active", link.getAttribute("href") === `#${id}`);
        });
      };

      const updateFromScroll = () => {
        const current = sections.reduce((active, section) => {
          return section.getBoundingClientRect().top <= 240 ? section : active;
        }, sections[0]);
        if (current?.id) setActive(current.id);
      };

      if (!("IntersectionObserver" in window)) {
        window.addEventListener("scroll", updateFromScroll, { passive: true });
        window.addEventListener("resize", updateFromScroll);
        updateFromScroll();
        return;
      }

      const observer = new IntersectionObserver((entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
        if (visible?.target?.id) setActive(visible.target.id);
      }, { rootMargin: "-18% 0px -66% 0px", threshold: [0.12, 0.24, 0.48] });

      sections.forEach((section) => observer.observe(section));
      window.addEventListener("scroll", updateFromScroll, { passive: true });
      window.addEventListener("resize", updateFromScroll);
      updateFromScroll();
    }

    function initSidebarToggle() {
      const button = document.querySelector("#sidebarToggle");
      if (!button) return;
      const storageKey = "reagent-dashboard-sidebar-collapsed";

      const applyCollapsed = (collapsed) => {
        document.body.classList.toggle("sidebar-collapsed", collapsed);
        button.setAttribute("aria-expanded", String(!collapsed));
        button.setAttribute("aria-label", collapsed ? "展开侧边栏" : "收起侧边栏");
        button.title = collapsed ? "展开侧边栏" : "收起侧边栏";
      };

      applyCollapsed(window.localStorage.getItem(storageKey) === "true");
      button.addEventListener("click", () => {
        const collapsed = !document.body.classList.contains("sidebar-collapsed");
        applyCollapsed(collapsed);
        window.localStorage.setItem(storageKey, String(collapsed));
      });
    }

    function initSchedulerPolicyToggle() {
      const field = settingsForm?.querySelector('[name="scheduler_use_default_run_policy"]');
      const advanced = document.querySelector("#schedulerAdvancedPolicy");
      if (!field || !advanced) return;
      const apply = () => {
        advanced.hidden = field.checked;
      };
      field.addEventListener("change", apply);
      apply();
    }

    const workbuddyCandidates = document.querySelector("#workbuddyCandidates");
    const detectWorkBuddy = document.querySelector("#detectWorkBuddy");
    const workbuddyDetectionState = document.querySelector("#workbuddyDetectionState");
    detectWorkBuddy?.addEventListener("click", async () => {
      detectWorkBuddy.disabled = true;
      workbuddyCandidates.hidden = true;
      workbuddyDetectionState.textContent = "检测中…";
      try {
        const response = await fetch("/api/settings/workbuddy/detect", { method: "POST" });
        const payload = await response.json();
        if (!response.ok) throw new Error(apiErrorMessage(payload));
        const candidates = payload.candidates;
        const sources = { running: "当前会话", user_registry: "用户注册", machine_registry: "系统注册" };
        workbuddyCandidates.replaceChildren(new Option("选择检测结果", ""), ...candidates.map(
          ({ path, source }) => new Option(`${path}（${sources[source] || source}）`, path),
        ));
        workbuddyCandidates.hidden = !candidates.length;
        workbuddyDetectionState.textContent = candidates.length ? `找到 ${candidates.length} 个候选` : "未检测到客户端，可填写完整路径。";
      } catch (error) {
        workbuddyDetectionState.textContent = error.message || "检测失败";
      } finally {
        detectWorkBuddy.disabled = false;
      }
    });
    workbuddyCandidates?.addEventListener("change", () => {
      if (!workbuddyCandidates.value) return;
      settingsForm.elements.namedItem("workbuddy_executable").value = workbuddyCandidates.value;
      workbuddyDetectionState.textContent = "已选定，尚未保存。";
    });

    let settingsRevision = 0;
    function clearSettingsErrors() {
      settingsForm?.querySelectorAll("[data-settings-error]").forEach((message) => {
        const field = settingsForm.elements.namedItem(message.dataset.settingsError);
        if (field) {
          field.removeAttribute("aria-invalid");
          field.removeAttribute("aria-errormessage");
        }
        message.remove();
      });
    }

    function showSettingsValidation(detail) {
      if (!Array.isArray(detail)) return false;
      let firstField;
      for (const error of detail) {
        const name = error.loc?.at(-1);
        if (typeof name !== "string") continue;
        const field = settingsForm.elements.namedItem(name);
        if (!(field instanceof HTMLElement) || field.hasAttribute("aria-invalid")) continue;
        const label = field.closest("label");
        if (!label) continue;
        const section = label.closest(".settings-section-collapsed");
        section?.querySelector(".settings-section-toggle")?.click();
        const message = document.createElement("span");
        message.id = `settings-error-${name}`;
        message.dataset.settingsError = name;
        message.className = "settings-field-error";
        message.textContent = error.msg || "请检查此设置值。";
        field.setAttribute("aria-invalid", "true");
        field.setAttribute("aria-errormessage", message.id);
        label.appendChild(message);
        firstField ||= field;
      }
      firstField?.focus();
      return Boolean(firstField);
    }

    function markSettingsDirty() {
      settingsRevision += 1;
      clearSettingsErrors();
      setText("#settingsSaveState", "有未保存更改");
      setClass("#settingsSaveState", "save-state");
    }
    settingsForm?.addEventListener("input", markSettingsDirty);
    settingsForm?.addEventListener("change", markSettingsDirty);
    document.querySelectorAll(".settings-jump a").forEach((link) => {
      link.addEventListener("click", () => {
        document.querySelector(link.hash + ".settings-section-collapsed .settings-section-toggle")?.click();
      });
    });
    settingsForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const state = document.querySelector("#settingsSaveState");
      state.textContent = "保存中";
      state.className = "save-state";
      const button = settingsForm.querySelector('button[type="submit"]');
      if (button.disabled) return;
      clearSettingsErrors();
      button.disabled = true;
      const revision = settingsRevision;
      let response, payload;
      try {
        response = await fetch("/api/settings", { method: "POST", body: new FormData(settingsForm) });
        payload = await response.json();
      } catch (_) {
        state.textContent = "未收到保存结果，请核对设置后再试。";
        state.className = "save-state failed";
        return;
      } finally {
        button.disabled = false;
      }
      if (!response.ok) {
        state.textContent = showSettingsValidation(payload.detail) ? "请修正标记的设置项。" : apiErrorMessage(payload);
        state.className = "save-state failed";
        return;
      }
      if (settingsRevision !== revision) {
        state.textContent = "已保存提交时的设置，仍有新的未保存更改。";
        return;
      }
      state.textContent = "已保存";
      state.className = "save-state saved";
      if (workbuddyDetectionState) {
        workbuddyDetectionState.textContent = payload.runtime?.workbuddy_executable && !payload.runtime.workbuddy_path_valid
          ? "已保存的客户端路径无效。" : "";
      }
      settingsForm.querySelector('[name="erp_password"]').value = "";
      settingsForm.querySelector('[name="llm_api_key"]').value = "";
      const enrichmentKey = settingsForm.querySelector('[name="identity_enrichment_api_key"]');
      if (enrichmentKey) enrichmentKey.value = "";
      settingsForm.querySelector('[name="dingtalk_stream_client_secret"]').value = "";
      settingsForm.querySelector('[name="dingtalk_stream_api_token"]').value = "";
      settingsForm.querySelector('[name="dingtalk_chat_api_key"]').value = "";
      settingsForm.querySelector('[name="memory_sync_password"]').value = "";
      settingsForm.querySelector('[name="update_token"]').value = "";
      await refreshStatus();
      await refreshMemorySyncStatus(false);
    });

    function safe(value, fallback = "-") {
      if (value === null || value === undefined || value === "") return fallback;
      return String(value);
    }

    function displayTime(value) {
      return safe(value).replace(/^(\d{4}-\d{2}-\d{2})T(?=\d{2}:\d{2})/, "$1 ");
    }

    function apiErrorMessage(payload) {
      const detail = payload?.detail;
      if (typeof detail === "string") return detail;
      if (detail?.message) return detail.message;
      return payload?.message || "操作失败。";
    }

    function setMemorySyncMessage(message, failed = false) {
      setText("#memorySyncMessage", message || "试剂库同步状态已更新。");
      const element = document.querySelector("#memorySyncMessage");
      if (element) element.classList.toggle("sync-failed", Boolean(failed));
    }

    function renderMemorySyncStatus(payload) {
      const local = payload?.local || {};
      const state = payload?.state || {};
      const remote = payload?.remote || {};
      setText("#memorySyncLocalRecords", safe(local.records, "0"));
      setText("#memorySyncLocalUpdated", safe(local.updated_at));
      setText("#memorySyncLastUpload", safe(state.last_upload_at));
      setText("#memorySyncLastDownload", safe(state.last_download_at));
      setText("#memorySyncRemoteVersion", safe(remote.version || state.last_remote_version));
      if (payload?.conflict) {
        setText("#memorySyncStateText", "可能冲突");
        document.querySelector("#forceDownloadMemorySyncButton")?.removeAttribute("hidden");
      } else if (payload?.remote_newer) {
        setText("#memorySyncStateText", "云端较新");
        document.querySelector("#forceDownloadMemorySyncButton")?.setAttribute("hidden", "hidden");
      } else if (payload?.ok === false) {
        setText("#memorySyncStateText", "检查失败");
      } else {
        setText("#memorySyncStateText", payload?.enabled ? "已启用" : "未启用");
        document.querySelector("#forceDownloadMemorySyncButton")?.setAttribute("hidden", "hidden");
      }
      if (payload?.message) setMemorySyncMessage(payload.message, payload.ok === false);
    }

    async function refreshMemorySyncStatus(checkRemote = false) {
      if (!document.querySelector("#memorySyncStateText")) return;
      try {
        const response = await fetch(`/api/memory/sync/status?check_remote=${checkRemote ? "true" : "false"}`);
        const payload = await response.json();
        renderMemorySyncStatus(payload);
      } catch (error) {
        setText("#memorySyncStateText", "检查失败");
        setMemorySyncMessage(`试剂库同步状态读取失败：${error}`, true);
      }
    }

    async function runMemorySyncAction(url, busyMessage, body = null) {
      setMemorySyncMessage(busyMessage);
      setText("#memorySyncStateText", "处理中");
      const buttons = [
        "#testMemorySyncButton",
        "#uploadMemorySyncButton",
        "#downloadMemorySyncButton",
        "#forceDownloadMemorySyncButton"
      ];
      buttons.forEach((selector) => setDisabled(selector, true));
      try {
        const response = await fetch(url, { method: "POST", body });
        const payload = await response.json();
        if (!response.ok || payload.ok === false) {
          const conflict = response.status === 409 || payload?.detail?.conflict;
          if (conflict) document.querySelector("#forceDownloadMemorySyncButton")?.removeAttribute("hidden");
          setMemorySyncMessage(apiErrorMessage(payload), true);
          await refreshMemorySyncStatus(false);
          return;
        }
        setMemorySyncMessage(payload.message || "试剂库同步操作完成。");
        renderMemorySyncStatus({ ok: true, enabled: true, local: payload.local, remote: payload.remote, state: {} });
        await refreshMemorySyncStatus(true);
      } catch (error) {
        setMemorySyncMessage(`试剂库同步操作失败：${error}`, true);
      } finally {
        buttons.forEach((selector) => setDisabled(selector, false));
      }
    }

    function escapeHtml(value) {
      return safe(value, "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char]));
    }

    function tableEmptyHtml(colspan, title, text = "") {
      return `
        <tr class="table-empty-row">
          <td colspan="${colspan}">
            <div class="table-empty-state">
              <strong>${escapeHtml(title)}</strong>
              ${text ? `<span>${escapeHtml(text)}</span>` : ""}
            </div>
          </td>
        </tr>
      `;
    }

    function renderTags(categories) {
      const holder = document.querySelector("#categoryTags");
      if (!holder) return;
      holder.innerHTML = "";
      Object.entries(categories || {}).forEach(([name, count]) => {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = `${name} ${count}`;
        holder.appendChild(tag);
      });
    }

    function renderSuggestions(rows) {
      const body = document.querySelector("#suggestionTable");
      if (!body) {
        renderEvidence(null);
        return;
      }
      body.innerHTML = "";
      if (!rows || rows.length === 0) {
        body.innerHTML = tableEmptyHtml(8, "暂无建议结果", "生成审批建议后，结果会显示在这里。");
        renderEvidence(null);
        return;
      }
      rows.forEach((row) => {
        const manual = String(row["需人工复核"] ?? "").trim().toLowerCase();
        const tr = document.createElement("tr");
        tr.className = "suggestion-row";
        tr.innerHTML = `
          <td>${escapeHtml(row["序号"])}</td>
          <td><button type="button" class="evidence-trigger" aria-controls="evidence" aria-expanded="false">${escapeHtml(row["试剂名称"])} <span>查看证据</span></button></td>
          <td>${escapeHtml(row["CAS号"])}</td>
          <td>${escapeHtml(row["标准化名称"])}</td>
          <td>${escapeHtml(suggestionSourceLabel(row["查询来源"]))}</td>
          <td><span class="category suggestion-category">${escapeHtml(row["最终建议类别"])}</span></td>
          <td>${escapeHtml(row["置信度"])}</td>
          <td><span class="status-badge ${manual === "true" ? "status-warning" : ""}">${manual === "true" ? "待人工复核" : manual === "false" ? "无需复核" : "未标注"}</span></td>
        `;
        tr.addEventListener("click", () => {
          document.querySelectorAll(".suggestion-row-selected").forEach((item) => item.classList.remove("suggestion-row-selected"));
          document.querySelectorAll('.evidence-trigger[aria-expanded="true"]').forEach((item) => item.setAttribute("aria-expanded", "false"));
          tr.classList.add("suggestion-row-selected");
          tr.querySelector(".evidence-trigger").setAttribute("aria-expanded", "true");
          renderEvidence(row);
          document.querySelector("#evidenceTitle")?.focus();
        });
        body.appendChild(tr);
      });
      renderEvidence(null);
    }

    function suggestionSourceLabel(source) {
      const labels = {business_rule: "业务规则", reagent_memory: "试剂记忆库"};
      return Object.hasOwn(labels, source) ? labels[source] : safe(source);
    }

    on("#closeEvidence", "click", () => {
      const row = document.querySelector(".suggestion-row-selected");
      const trigger = row?.querySelector(".evidence-trigger");
      renderEvidence(null);
      row?.classList.remove("suggestion-row-selected");
      trigger?.setAttribute("aria-expanded", "false");
      trigger?.focus();
    });

    function renderEvidence(row) {
      const box = document.querySelector("#evidenceBox");
      const panel = document.querySelector("#evidence");
      if (!box) return;
      if (panel) panel.hidden = !row;
      if (!row) {
        box.replaceChildren();
        return;
      }
      box.innerHTML = `
        <dl>
          <dt>试剂</dt><dd>${escapeHtml(row["试剂名称"])}</dd>
          <dt>CAS</dt><dd>${escapeHtml(row["CAS号"])}</dd>
          <dt>标准名称</dt><dd>${escapeHtml(row["标准化名称"])}</dd>
          <dt>查询来源</dt><dd>${escapeHtml(suggestionSourceLabel(row["查询来源"]))}</dd>
          <dt>建议类别</dt><dd>${escapeHtml(row["最终建议类别"])}</dd>
          <dt>置信度</dt><dd>${escapeHtml(row["置信度"])}</dd>
          <dt>规则原因</dt><dd>${escapeHtml(row["规则原因"])}</dd>
          <dt>证据</dt><dd>${escapeHtml(row["证据"])}</dd>
        </dl>
      `;
    }

    function renderReviewQueue(queue) {
      const meta = document.querySelector("#reviewMeta");
      if (!meta) return;
      const rows = queue.preview || [];
      meta.textContent = queue.exists
        ? `共 ${safe(queue.rows, 0)} 条历史记录，当前待处理 ${safe(queue.pending, 0)} 条；最近更新：${displayTime(queue.modified)}`
        : "尚未生成复核队列";
      reviewState.rows = rows;
      reviewState.page = Number(queue.page || reviewState.page || 1);
      reviewState.pages = Number(queue.pages || 1);
      reviewState.total = Number(queue.filtered ?? rows.length);
      reviewState.perPage = Number(queue.per_page || reviewState.perPage || 20);
      updateReviewListFilter(queue.list_numbers || rows.map((row) => row.list_number).filter(Boolean));
      renderReviewRows();
    }

    function selectedTodoNumbers() {
      return Array.from(document.querySelectorAll('input[name="todo_list_number"]:checked'))
        .map((input) => input.value)
        .filter(Boolean);
    }

    function renderTodoTasks(todoTasks, force = false) {
      const holder = document.querySelector("#todoListPicker");
      const meta = document.querySelector("#todoMeta");
      if (!holder || !meta) return;
      const tasks = todoTasks.tasks || [];
      const signature = JSON.stringify([todoTasks.exists, todoTasks.rows, todoTasks.modified, tasks]);
      if (!force && signature === todoState.signature) return;
      todoState.signature = signature;
      todoState.snapshot = todoTasks;
      todoState.tasks = tasks;
      meta.textContent = todoTasks.exists
        ? `共 ${safe(todoTasks.rows, tasks.length)} 条；最近刷新：${safe(todoTasks.modified)}`
        : "点击“获取最新清单号”读取 ERP 待审批列表。";
      holder.innerHTML = "";
      const updateScope = () => updateExecutionSummary();
      if (!tasks.length) {
        holder.innerHTML = "<p>暂无待办清单缓存。</p>";
        updateScope();
        return;
      }
      if (!todoState.restored && typeof window.uiLoadSelectedTodos === "function") {
        todoState.restored = true;
        window.uiLoadSelectedTodos().forEach((num) => todoState.selected.add(num));
      }
      const query = String(document.querySelector("#todoSearchInput")?.value || "").trim().toLowerCase();
      const visibleTasks = query
        ? tasks.filter((t) => `${t.list_number} ${t.customer_name} ${t.applicant}`.toLowerCase().includes(query))
        : tasks;
      visibleTasks.forEach((task) => {
        const listNumber = task.list_number || "";
        const label = document.createElement("label");
        label.className = "todo-option";
        label.innerHTML = `
          <input type="checkbox" name="todo_list_number" value="${escapeHtml(listNumber)}" ${todoState.selected.has(listNumber) ? "checked" : ""}>
          <span>
            <strong>${escapeHtml(listNumber)}</strong>
            <em>${escapeHtml(task.customer_name)} ${escapeHtml(task.applicant)}</em>
          </span>
        `;
        label.querySelector("input").addEventListener("change", (event) => {
          if (event.target.checked) {
            todoState.selected.add(listNumber);
          } else {
            todoState.selected.delete(listNumber);
          }
          window.uiSaveSelectedTodos?.(todoState.selected);
          updateScope();
        });
        holder.appendChild(label);
      });
      updateScope();
    }

    function updateReviewListFilter(listNumbers) {
      const filter = document.querySelector("#reviewListFilter");
      if (!filter) return;
      const current = reviewState.listNumber;
      const unique = Array.from(new Set(listNumbers || [])).filter(Boolean).sort();
      filter.innerHTML = '<option value="">全部清单</option>';
      unique.forEach((listNumber) => {
        const option = document.createElement("option");
        option.value = listNumber;
        option.textContent = listNumber;
        filter.appendChild(option);
      });
      if (current && unique.includes(current)) {
        filter.value = current;
      } else {
        reviewState.listNumber = "";
      }
    }

    function reviewEvidenceLabel(row) {
      const type = row.evidence_source_type || "";
      if (type === "llm_manual_review_advice") return "LLM第二意见";
      if (type === "llm_rule_fallback") return "LLM规则辅助";
      if (type === "llm_fallback") return "LLM辅助";
      if (type === "trusted_web") return "可信网站";
      if (type === "web_fallback") return "网页兜底";
      if (type === "erp_write_verification") return "写入待核验";
      if (row.review_kind === "erp_write_verification") return "写入待核验";
      return "证据不足";
    }

    function reviewAllowsSuggestionPreselect(row) {
      if (String(row.llm_advisory_only || "").toLowerCase() === "true") return false;
      return String(row.allow_suggestion_preselect || "").toLowerCase() === "true";
    }

    function reviewEvidenceHtml(row) {
      const suggestion = row.display_suggestion || row.suggested_category || "暂无可靠建议";
      const reason = row.display_reason || row.review_advice || "需人工核对物化特性。";
      const evidenceStatus = row.evidence_status || reviewEvidenceLabel(row);
      const withoutAddresses = (value) => String(value || "").replace(/https?:\/\/[^\s，；,)]+/g, "[来源]");
      const detail = localizeReviewDetailText(withoutAddresses(row.detail_summary || row.property_summary || ""));
      const properties = [
        ["闪点", row.flash_point],
        ["沸点", row.boiling_point],
        ["毒性", row.toxicity],
        ["腐蚀", row.corrosive],
        ["氧化", row.oxidizing],
        ["易燃", row.flammable],
        ["遇水反应", row.water_reactive],
        ["爆炸风险", row.explosive_risk],
      ].filter((item) => item[1] !== undefined && item[1] !== null && String(item[1]).trim() !== "");
      const sourceLink = row.source_url
        ? `<a href="${escapeHtml(row.source_url)}" target="_blank" rel="noreferrer">来源链接</a>`
        : "";
      const parseJson = (value) => {
        try { return value ? JSON.parse(value) : null; } catch (_) { return null; }
      };
      const identityResolution = parseJson(row.identity_resolution);
      const sourceEvidenceItems = parseJson(row.source_evidence_items) || [];
      const propertyEnrichment = parseJson(row.property_enrichment) || {};
      const nameIdentity = parseJson(row.name_identity) || identityResolution?.name_identity;
      const casIdentity = parseJson(row.cas_identity) || identityResolution?.cas_identity;
      const identityStatus = row.identity_status || identityResolution?.status || "";
      const hasNameIdentity = nameIdentity && Object.keys(nameIdentity).length > 0;
      const hasCasIdentity = casIdentity && Object.keys(casIdentity).length > 0;
      const candidateText = (candidate) => {
        if (!candidate) return "";
        const label = candidate.name || candidate.query || candidate.cid || "未知";
        return escapeHtml(withoutAddresses(label));
      };
      const candidateCasAutomaticallyVerified = String(row.candidate_cas_memory_eligible || "").toLowerCase() === "true"
        || String(row.candidate_cas_memory_eligible || "") === "1"
        || ["pubchem_confirmed", "cross_source_confirmed"].includes(String(row.candidate_cas_validation_status || "").toLowerCase());
      const identityBlock = (identityStatus || hasNameIdentity || hasCasIdentity) ? `
        <div class="review-detail-section"><strong>身份确认：</strong>
          ${identityStatus ? `状态：${escapeHtml(identityStatus)}；` : ""}
          ${row.cas ? `ERP CAS：${escapeHtml(row.cas)}；` : "ERP CAS：未提供；"}
          ${row.candidate_cas && candidateCasAutomaticallyVerified ? `网页候选 CAS：${escapeHtml(row.candidate_cas)}；已通过自动校验，已写入试剂身份记忆库。` : ""}
          ${hasNameIdentity ? `名称身份：${candidateText(nameIdentity)}；` : ""}
          ${hasCasIdentity ? `CAS 身份：${candidateText(casIdentity)}` : ""}
        </div>` : "";
      const correctionCandidate = String(row.cas_correction_candidate || row.cas_name_conflict || "").toLowerCase() === "true"
        || String(row.cas_correction_candidate || row.cas_name_conflict || "") === "1";
      const correctionBlock = correctionCandidate ? `
        <div class="review-detail-section review-detail-warning"><strong>CAS 修正候选：</strong>
          原 ERP CAS：${escapeHtml(row.original_erp_cas || row.cas || "-")}；
          名称对应 CAS：${escapeHtml(row.corrected_cas || "-")}；
          判定依据：${escapeHtml(row.identity_decision_basis || "名称身份")}。
          ${row.cas_correction_source ? `来源：${escapeHtml(row.cas_correction_source)}；` : ""}
          <div class="review-detail-advisory">人工确认后才写入试剂记忆库；不自动修改 ERP CAS。</div>
        </div>` : "";
      const evidenceItemsBlock = Array.isArray(sourceEvidenceItems) && sourceEvidenceItems.length ? `
        <div class="review-detail-section"><strong>来源与物化证据：</strong>
          <ul>${sourceEvidenceItems.map((item) => `<li>${escapeHtml(item.field || "属性")}：${escapeHtml(formatReviewEvidenceValue(item.value || "-", item.unit || ""))}；状态：${escapeHtml(item.status || "unknown")}；来源：${escapeHtml(item.source || "-")}${item.source_updated_at ? `；来源时间：${escapeHtml(item.source_updated_at)}` : ""}${item.selected ? "；当前采用" : ""}${item.conflict ? `；存在来源冲突${item.selection_basis ? `（${escapeHtml(item.selection_basis)}）` : ""}` : ""}</li>`).join("")}</ul>
        </div>` : "";
      const enrichmentBlock = Object.keys(propertyEnrichment).length ? `
        <div class="review-detail-section"><strong>物性补全：</strong>
          已发起 ${escapeHtml(propertyEnrichment.requests ?? 0)} 次请求；耗时 ${escapeHtml(propertyEnrichment.elapsed_ms ?? 0)} ms；
          ${Array.isArray(propertyEnrichment.missing_fields) && propertyEnrichment.missing_fields.length ? `仍缺失：${escapeHtml(propertyEnrichment.missing_fields.join(", "))}。` : "字段已覆盖或无待补字段。"}
          ${Array.isArray(propertyEnrichment.diagnostics) && propertyEnrichment.diagnostics.length ? `<div class="review-detail-advisory">${escapeHtml(propertyEnrichment.diagnostics.map((item) => `${item.provider || "来源"}: ${item.status || "unknown"}`).join("；"))}</div>` : ""}
        </div>` : "";
      const ruleBlock = (row.matched_rule_ids || row.rule_version) ? `
        <div class="review-detail-section"><strong>规则判定：</strong>
          命中规则：${escapeHtml(row.matched_rule_ids || "-")}；规则版本/指纹：${escapeHtml(row.rule_version || "-")}；正式结果：${escapeHtml(row.suggested_category || "需人工复核")}；复核原因：${escapeHtml(withoutAddresses(row.reason_full || row.reason || "-"))}。
        </div>` : "";
      const identityOpinion = [
        row.llm_name_identity_opinion ? `名称身份意见：${row.llm_name_identity_opinion}` : "",
        row.llm_cas_identity_opinion ? `CAS 身份意见：${row.llm_cas_identity_opinion}` : "",
        row.llm_identity_opinion ? `总体意见：${row.llm_identity_opinion}` : "",
        row.llm_identity_trigger_status ? `触发状态：${row.llm_identity_trigger_status}` : "",
        row.llm_advisory_category ? `候选类别：${row.llm_advisory_category}` : "",
        row.llm_advisory_summary_cn ? `物化摘要：${row.llm_advisory_summary_cn}` : "",
        row.llm_advisory_reason_cn ? `理由：${row.llm_advisory_reason_cn}` : "",
        row.llm_advisory_rule_cn ? `可能涉及规则：${row.llm_advisory_rule_cn}` : "",
        row.llm_advisory_uncertainties_cn ? `不确定性：${row.llm_advisory_uncertainties_cn}` : "",
      ].filter(Boolean).join("；");
      const opinionBlock = identityOpinion ? `
          <div class="review-detail-section"><strong>LLM 第二意见：</strong>${escapeHtml(withoutAddresses(identityOpinion))}
          <div class="review-detail-advisory">仅供人工参考，不作为自动审批依据；依据类型：${escapeHtml(row.llm_advisory_evidence_basis || "证据不足")}；置信度：${escapeHtml(row.llm_advisory_confidence || "-")}；模型：${escapeHtml(row.llm_provider || "-")} / ${escapeHtml(row.llm_model || "-")}；生成时间：${escapeHtml(row.llm_generated_at || "-")}；规则指纹：${escapeHtml(row.llm_rules_fingerprint || "-")}。</div>
        </div>` : "";
      return `
        <div class="review-evidence ${["llm_fallback", "llm_rule_fallback", "llm_manual_review_advice"].includes(row.evidence_source_type) ? "llm-evidence" : ""} ${row.review_kind === "erp_write_verification" ? "write-verification-evidence" : ""}">
          <div class="review-evidence-head">
            <span>${escapeHtml(evidenceStatus)}</span>
            ${sourceLink}
          </div>
          <p><strong>建议：</strong>${escapeHtml(suggestion)}</p>
          <p><strong>原因：</strong>${escapeHtml(reason)}</p>
          ${(detail || properties.length || identityBlock || enrichmentBlock || opinionBlock) ? `
            <details class="review-evidence-detail">
              <summary>查看详情</summary>
              ${identityBlock}
              ${correctionBlock}
              ${evidenceItemsBlock}
              ${enrichmentBlock}
              ${ruleBlock}
              ${detail ? `<p>${escapeHtml(detail)}</p>` : ""}
              ${properties.length ? `<p>${properties.map((item) => `${escapeHtml(item[0])}：${escapeHtml(localizeReviewDetailText(withoutAddresses(item[1])))}`).join(" · ")}</p>` : ""}
              ${opinionBlock}
            </details>
          ` : ""}
        </div>
      `;
    }

    function localizeReviewDetailText(value) {
      let text = String(value || "").trim();
      if (!text) return "";
      const keyLabels = {
        flash_point: "闪点",
        boiling_point: "沸点",
        toxicity: "毒性",
        corrosive: "腐蚀性",
        oxidizing: "氧化性",
        flammable: "易燃性",
        water_reactive: "遇水反应",
        explosive_risk: "爆炸风险",
        evidence: "证据",
        llm_rule_reason: "LLM规则辅助依据",
        llm_rule_matched_rule: "LLM匹配规则",
      };
      text = text.replace(/\b(flash_point|boiling_point|toxicity|corrosive|oxidizing|flammable|water_reactive|explosive_risk|evidence|llm_rule_reason|llm_rule_matched_rule)=/g, (_, key) => `${keyLabels[key] || key}：`);
      text = normalizeReviewTemperatureText(text);
      const replacements = [
        ["No specific acute toxicity data provided.", "未提供明确的急性毒性数据。"],
        ["No specific acute toxicity data provided", "未提供明确的急性毒性数据"],
        ["LLM fallback evidence is advisory only and requires manual review.", "LLM 辅助证据仅供参考，需要人工复核。"],
        ["LLM fallback evidence is advisory only and requires manual review", "LLM 辅助证据仅供参考，需要人工复核"],
        ["No trusted web evidence was found, so low-confidence LLM knowledge fallback was used.", "未找到可信网页证据，因此使用低置信度的 LLM 知识兜底。"],
        ["No trusted web evidence was found", "未找到可信网页证据"],
        ["No web evidence found.", "未找到网页证据。"],
        ["Summary is based on conservative LLM fallback.", "摘要基于保守的 LLM 兜底判断。"],
        ["Based on general chemical knowledge", "根据通用化学知识"],
        ["it is expected to be", "预计为"],
        ["It is likely highly soluble in water", "可能高度溶于水"],
        ["due to its ionic nature and polar sulfonate group", "因为其离子特性和极性磺酸基"],
        ["It is probably hygroscopic.", "可能具有吸湿性。"],
        ["Its melting point is uncertain", "熔点不确定"],
        ["white to off-white crystalline solid or powder", "白色至类白色结晶固体或粉末"],
        ["at room temperature", "在室温下"],
      ];
      replacements.forEach(([source, target]) => {
        text = text.split(source).join(target);
      });
      return text;
    }

    function formatReviewEvidenceValue(value, unit) {
      const raw = String(value || "").trim();
      const rawUnit = String(unit || "").trim();
      if (rawUnit.toLowerCase() === "degc" && /^[-+]?\d+(?:\.\d+)?$/.test(raw)) {
        return `${formatCelsius(Number(raw))}℃`;
      }
      return normalizeReviewTemperatureText(rawUnit ? `${raw} ${rawUnit}` : raw);
    }

    function normalizeReviewTemperatureText(value) {
      return String(value || "").replace(/([<>≤≥~约]?\s*)([-+]?\d+(?:\.\d+)?)\s*(°\s*[CF]|[CFK])\b/gi, (match, prefix, numberText, rawUnit) => {
        const numeric = Number(numberText);
        if (!Number.isFinite(numeric)) return match;
        const unit = String(rawUnit || "").replace(/\s+/g, "").toUpperCase();
        let celsius = numeric;
        if (unit === "K") {
          celsius = numeric - 273.15;
        } else if (unit === "F" || unit === "°F") {
          celsius = (numeric - 32) * 5 / 9;
        } else if (unit !== "C" && unit !== "°C") {
          return match;
        }
        return `${prefix || ""}${formatCelsius(celsius)}℃`;
      });
    }

    function formatCelsius(value) {
      if (!Number.isFinite(value)) return "";
      const rounded = Math.round(value * 10) / 10;
      return Number.isInteger(rounded) ? String(rounded) : String(rounded.toFixed(1)).replace(/\.0$/, "");
    }

    function reviewTimestampHtml(value) {
      const timestamp = String(value || "-");
      const match = timestamp.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)/);
      if (!match) return `<strong>${escapeHtml(timestamp)}</strong>`;
      return `<strong>${escapeHtml(match[1])}</strong><span class="review-time-clock">${escapeHtml(match[2])}</span>`;
    }

    function renderReviewRows() {
      const body = document.querySelector("#reviewTable");
      const visibleCount = document.querySelector("#reviewVisibleCount");
      if (!body || !visibleCount) return;
      restoreReviewControls(reviewState.selectedRowElement);
      reviewState.selectedRowElement = null;
      const rows = reviewState.rows || [];
      const sortIcon = document.querySelector("#reviewSortIcon");
      if (sortIcon) sortIcon.textContent = reviewState.sortDirection === "desc" ? "↓" : "↑";
      reviewState.page = Math.min(Math.max(1, reviewState.page || 1), reviewState.pages);
      const pageRows = rows;
      visibleCount.textContent = `显示 ${pageRows.length} / ${reviewState.total} 条`;
      body.innerHTML = "";
      if (!rows.length) {
        body.innerHTML = tableEmptyHtml(4, "暂无人工复核项", "需要人工确认的审批记录会显示在这里。");
        clearReviewInspector();
        document.querySelector("#review")?.classList.remove("review-detail-open");
        updateReviewPagination();
        return;
      }
      pageRows.forEach((row) => {
        const tr = document.createElement("tr");
        tr.className = "review-row";
        tr.dataset.reviewKey = row.review_key || [row.list_number, row.sequence, row.cas, row.reagent_name].join("|");
        const preselectedCategory = reviewAllowsSuggestionPreselect(row) ? (row.suggested_category || "") : "";
        const options = categoryOptionsHtml(preselectedCategory);
        const candidateCasAutomaticallyVerified = String(row.candidate_cas_memory_eligible || "").toLowerCase() === "true"
          || String(row.candidate_cas_memory_eligible || "") === "1"
          || ["pubchem_confirmed", "cross_source_confirmed"].includes(String(row.candidate_cas_validation_status || "").toLowerCase());
        tr.innerHTML = `
          <td class="review-time-cell">
            ${reviewTimestampHtml(row.timestamp)}
            <span>清单 ${escapeHtml(row.list_number || "-")}</span>
          </td>
          <td class="review-name-cell">
            <button type="button" class="review-select-record" aria-pressed="false">
              <strong>${escapeHtml(row.reagent_name || "-")}</strong>
              <span>CAS ${escapeHtml(row.cas || "-")}</span>
            </button>
          </td>
          <td class="reason-cell" title="${escapeHtml(row.reason_full || row.reason)}">
            <div class="review-reason-short">${escapeHtml(row.reason || "需要核对判定依据。")}</div>
          </td>
          <td class="review-status-cell"><span class="status-badge status-warning">待复核</span></td>
          <td class="review-category-cell">
            <label class="review-category-label">确认类别<select class="review-category">${options}</select></label>
            <span class="review-selected">未选择</span>
          </td>
         <td class="review-action-cell">
            ${row.candidate_cas && candidateCasAutomaticallyVerified ? '<span class="review-cas-auto-verified">候选 CAS 已写入身份记忆库</span>' : ""}
           ${String(row.used_llm_manual_review_advice || "").toLowerCase() === "true" || row.llm_advisory_summary_cn || row.llm_advisory_reason_cn || row.llm_identity_opinion || row.llm_name_identity_opinion || row.llm_cas_identity_opinion ? "" : '<button type="button" class="review-llm-advice review-secondary-action">生成LLM第二意见</button>'}
            <button type="button" class="review-confirm review-primary-action" disabled>确认分类并加入记忆库</button>
            <button type="button" class="review-delete review-danger-action">删除</button>
            <span class="review-row-message"></span>
          </td>
        `;
        const categorySelect = tr.querySelector(".review-category");
        const selectedText = tr.querySelector(".review-selected");
       const confirmButton = tr.querySelector(".review-confirm");
        const confirmedCasInput = tr.querySelector(".review-confirmed-cas");
        const adviceButton = tr.querySelector(".review-llm-advice");
        const deleteButton = tr.querySelector(".review-delete");
        const rowMessage = tr.querySelector(".review-row-message");
        const updateReviewSelection = () => {
          const value = categorySelect.value;
          selectedText.textContent = value ? `已选择：${value}` : "未选择";
          confirmButton.disabled = !value;
          rowMessage.textContent = "";
          rowMessage.className = "review-row-message";
        };
        categorySelect.addEventListener("change", updateReviewSelection);
        updateReviewSelection();
        adviceButton?.addEventListener("click", async () => {
          adviceButton.disabled = true;
          rowMessage.textContent = "正在生成 LLM 第二意见...";
          rowMessage.className = "review-row-message";
          try {
            const {response, payload} = await fetchDashboardJSON("/api/review/llm_advice", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({
                review_key: row.review_key,
                list_number: row.list_number,
                sequence: row.sequence,
                reagent_name: row.reagent_name,
                cas: row.cas,
                standard_name: row.standard_name,
              }),
            }, 120000);
            if (!response.ok) throw new Error(apiErrorMessage(payload) || "LLM 第二意见生成失败。");
            rowMessage.textContent = payload.cached ? "已加载缓存的第二意见。" : "LLM 第二意见已生成。";
            rowMessage.className = "review-row-message ok";
            try {
              await refreshReviewQueue({force: true});
            } catch (_) {
              rowMessage.textContent += " 列表刷新超时，请稍后手动刷新页面。";
            }
          } catch (error) {
            rowMessage.textContent = dashboardRequestErrorMessage(error, "LLM 第二意见生成失败。");
            rowMessage.className = "review-row-message failed";
            adviceButton.disabled = false;
          }
        });
        confirmButton.addEventListener("click", async () => {
          const finalCategory = categorySelect.value;
          if (!finalCategory) {
            rowMessage.textContent = "请先选择人工确认后的物化特性。";
            rowMessage.className = "review-row-message failed";
            return;
          }
          confirmButton.disabled = true;
          categorySelect.disabled = true;
          deleteButton.disabled = true;
          if (adviceButton) adviceButton.disabled = true;
          rowMessage.textContent = "正在确认...";
          rowMessage.className = "review-row-message";
          let response, payload;
          try {
            response = await fetch("/api/review/confirm", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
              review_key: row.review_key,
              list_number: row.list_number,
              sequence: row.sequence,
              reagent_name: row.reagent_name,
             cas: row.cas,
              confirmed_cas: confirmedCasInput ? confirmedCasInput.value.trim() : "",
             standard_name: row.standard_name,
              cleaned_name: row.cleaned_name,
              specification: row.specification,
              unit: row.unit,
              final_category: finalCategory,
              reason: row.reason || row.reason_full || ""
            }),
            });
            payload = await response.json();
          } catch (_) {
            rowMessage.textContent = "未收到确认结果。请刷新队列核对该条是否仍待处理，再决定是否重试。";
            rowMessage.className = "review-row-message failed";
            return;
          }
          if (!response.ok || !payload.confirmed) {
            rowMessage.textContent = apiErrorMessage(payload);
            rowMessage.className = "review-row-message failed";
            confirmButton.disabled = false;
            categorySelect.disabled = false;
            deleteButton.disabled = false;
            if (adviceButton) adviceButton.disabled = false;
            return;
          }
          const active = reviewState.selectedRowElement === tr;
          if (active) reviewState.selectedKey = tr.nextElementSibling?.dataset.reviewKey
            || tr.previousElementSibling?.dataset.reviewKey || "";
          const progress = document.querySelector("#reviewProgress");
          const message = payload.message || "已确认分类并加入试剂记忆库。";
          progress.hidden = false;
          progress.textContent = message;
          try {
            await refreshReviewQueue({force: true});
            if (active && reviewState.selectedRowElement) {
              progress.textContent += " 已定位到下一条待复核记录。";
              focusReviewTitle();
            } else if (active) {
              progress.focus();
            }
          } catch (_) {
            progress.textContent += " 队列刷新失败，请刷新页面，不要重复提交。";
          }
          refreshStatus().catch(() => {});
        });
        deleteButton.addEventListener("click", async () => {
          if (!(await confirmAction({title: "删除人工复核项", message: `确认删除「${row.reagent_name || row.cas || row.review_key}」？`, confirmText: "确认删除", danger: true}))) return;
          deleteButton.disabled = true;
          rowMessage.textContent = "正在删除...";
          rowMessage.className = "review-row-message";
          const response = await fetch("/api/review", {
            method: "DELETE",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
              review_key: row.review_key,
              list_number: row.list_number,
              sequence: row.sequence,
              reagent_name: row.reagent_name,
              cas: row.cas,
              standard_name: row.standard_name,
            }),
          });
          const payload = await response.json();
          if (!response.ok || !payload.deleted) {
            rowMessage.textContent = payload.detail || payload.message || "删除失败。";
            rowMessage.className = "review-row-message failed";
            deleteButton.disabled = false;
            return;
          }
          rowMessage.textContent = payload.message || "已删除";
          rowMessage.className = "review-row-message ok";
          await Promise.all([refreshStatus(), refreshReviewQueue({force: true})]);
        });
        tr._reviewRecord = row;
        tr.addEventListener("click", () => {
          selectReviewRecord(row, tr);
          document.querySelector("#review")?.classList.add("review-detail-open");
          focusReviewTitle();
        });
        body.appendChild(tr);
      });
      const selected = Array.from(body.querySelectorAll(".review-row")).find((item) => item.dataset.reviewKey === reviewState.selectedKey)
        || body.querySelector(".review-row");
      if (selected?._reviewRecord) selectReviewRecord(selected._reviewRecord, selected);
      updateReviewPagination();
    }

    function restoreReviewControls(rowElement) {
      if (!rowElement) return;
      const categorySlot = document.querySelector("#reviewCategorySlot");
      const actionSlot = document.querySelector("#reviewActionSlot");
      const categoryCell = rowElement.querySelector(".review-category-cell");
      const actionCell = rowElement.querySelector(".review-action-cell");
      if (categorySlot && categoryCell) Array.from(categorySlot.childNodes).forEach((node) => categoryCell.appendChild(node));
      if (actionSlot && actionCell) Array.from(actionSlot.childNodes).forEach((node) => actionCell.appendChild(node));
    }

    function focusReviewTitle() {
      const title = document.querySelector("#reviewInspectorTitle");
      title?.focus();
    }

    on("#reviewBackToQueue", "click", () => {
      document.querySelector("#review")?.classList.remove("review-detail-open");
      reviewState.selectedRowElement?.querySelector(".review-select-record")?.focus();
    });

    function clearReviewInspector() {
      const content = document.querySelector("#reviewInspectorContent");
      const decision = document.querySelector("#reviewDecision");
      const categorySlot = document.querySelector("#reviewCategorySlot");
      const actionSlot = document.querySelector("#reviewActionSlot");
      if (content) content.innerHTML = `
        <div class="review-inspector-empty">
          <strong id="reviewInspectorTitle">暂无待复核记录</strong>
          <span>新的人工复核项出现后，会在这里显示试剂信息和判定依据。</span>
        </div>`;
      if (decision) decision.hidden = true;
      if (categorySlot) categorySlot.innerHTML = "";
      if (actionSlot) actionSlot.innerHTML = "";
      reviewState.selectedKey = "";
    }

    function selectReviewRecord(row, rowElement) {
      const changingSelection = reviewState.selectedRowElement !== rowElement;
      if (reviewState.selectedRowElement && changingSelection) {
        restoreReviewControls(reviewState.selectedRowElement);
      }
      reviewState.selectedRowElement = rowElement;
      reviewState.selectedKey = rowElement.dataset.reviewKey || "";
      document.querySelectorAll(".review-row-selected").forEach((item) => item.classList.remove("review-row-selected"));
      document.querySelectorAll(".review-select-record").forEach((button) => button.setAttribute("aria-pressed", "false"));
      rowElement.classList.add("review-row-selected");
      rowElement.querySelector(".review-select-record")?.setAttribute("aria-pressed", "true");

      const content = document.querySelector("#reviewInspectorContent");
      const decision = document.querySelector("#reviewDecision");
      const categorySlot = document.querySelector("#reviewCategorySlot");
      const actionSlot = document.querySelector("#reviewActionSlot");
      const categoryCell = rowElement.querySelector(".review-category-cell");
      const actionCell = rowElement.querySelector(".review-action-cell");
      if (!content || !decision || !categorySlot || !actionSlot || !categoryCell || !actionCell) return;

      const metadata = [
        ["CAS", row.cas || "-"],
        ["清单号", row.list_number || "-"],
        ["序号", row.sequence || "-"],
        ["标准名称", row.standard_name || "-"],
        ["规格", [row.specification, row.unit].filter(Boolean).join(" ") || "-"],
      ];
      content.innerHTML = `
        <div class="review-inspector-header">
          <div class="review-inspector-status"><span class="status-badge status-warning">待人工复核</span><span>${escapeHtml(displayTime(row.timestamp))}</span></div>
          <h3 id="reviewInspectorTitle" tabindex="-1">${escapeHtml(row.reagent_name || "未命名试剂")}</h3>
          <p>${escapeHtml(row.cleaned_name || row.standard_name || "待确认身份")}</p>
        </div>
        <dl class="review-inspector-meta">${metadata.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl>
        <div class="review-inspector-evidence">${reviewEvidenceHtml(row)}</div>`;
      if (changingSelection) {
        categorySlot.innerHTML = "";
        actionSlot.innerHTML = "";
        Array.from(categoryCell.childNodes).forEach((node) => categorySlot.appendChild(node));
        Array.from(actionCell.childNodes).forEach((node) => actionSlot.appendChild(node));
      }
      decision.hidden = false;
    }

    function updateReviewPagination() {
      const prev = document.querySelector("#reviewPrevPage");
      const next = document.querySelector("#reviewNextPage");
      const info = document.querySelector("#reviewPageInfo");
      if (!prev || !next || !info) return;
      prev.disabled = reviewState.page <= 1;
      next.disabled = reviewState.page >= reviewState.pages;
      info.textContent = `第 ${reviewState.page} / ${reviewState.pages} 页，每页 ${reviewState.perPage} 条，共 ${reviewState.total} 条`;
    }

    function reviewInteractionActive() {
      const table = document.querySelector("#reviewTable");
      if (!table) return false;
      if (reviewState.pointerInside) return true;
      const inspector = document.querySelector("#reviewInspector");
      if (table.contains(document.activeElement) || inspector?.contains(document.activeElement)) return true;
      if (Array.from(document.querySelectorAll(".review-category")).some((field) => field.value)) return true;
      if (Array.from(document.querySelectorAll(".review-row-message")).some((node) => node.textContent.trim())) return true;
      return false;
    }

    on("#review", "mouseenter", () => {
      reviewState.pointerInside = true;
    });

    on("#review", "mouseleave", () => {
      reviewState.pointerInside = false;
    });

    on("#reviewListFilter", "change", (event) => {
      reviewState.listNumber = event.target.value;
      reviewState.page = 1;
      refreshReviewQueue({force: true});
    });

    on("#reviewTimeSort", "click", () => {
      reviewState.sortDirection = reviewState.sortDirection === "desc" ? "asc" : "desc";
      reviewState.page = 1;
      refreshReviewQueue({force: true});
    });

    on("#reviewPrevPage", "click", () => {
      if (reviewState.page <= 1) return;
      reviewState.page -= 1;
      refreshReviewQueue({force: true});
    });

    on("#reviewNextPage", "click", () => {
      if (reviewState.page >= reviewState.pages) return;
      reviewState.page += 1;
      refreshReviewQueue({force: true});
    });

    let memorySearchTimer = null;
    let memorySearchComposing = false;
    const memorySearchInput = document.querySelector("#memorySearch");
    const memoryStatusDrafts = new Map();
    let memoryStatusPending = false;
    let memoryBulkDeletePending = false;
    const memoryFilterIds = ["memoryCategoryFilter", "memoryReusableFilter", "memoryConflictFilter"];
    const hasMemoryFilters = () => Boolean(memorySearchInput?.value.trim()
      || memoryFilterIds.some((id) => document.getElementById(id)?.value));
    const updateMemoryFilterActions = () => setDisabled("#clearMemoryFilters", !hasMemoryFilters());
    const resetMemoryPage = () => {
      window.clearTimeout(memorySearchTimer);
      memoryState.page = 1;
    };
    const searchMemory = () => {
      resetMemoryPage();
      updateMemoryFilterActions();
      return refreshMemory();
    };
    const scheduleMemorySearch = () => {
      window.clearTimeout(memorySearchTimer);
      memorySearchTimer = window.setTimeout(searchMemory, 350);
    };
    on("#refreshMemoryButton", "click", () => refreshMemory());
    on("#clearMemoryFilters", "click", () => {
      memorySearchInput.value = "";
      memoryFilterIds.forEach((id) => { document.getElementById(id).value = ""; });
      memorySearchInput.focus();
      searchMemory();
    });
    on("#memoryPageSize", "change", (event) => {
      memoryState.perPage = Number(event.target.value);
      searchMemory();
    });
    async function changeMemoryPage(offset) {
      memoryState.page += offset;
      if (await refreshMemory()) document.querySelector(".memory-table-wrap")?.focus();
    }

    on("#memoryPrevPage", "click", () => {
      if (memoryState.page <= 1) return;
      changeMemoryPage(-1);
    });
    on("#memoryNextPage", "click", () => {
      if (memoryState.page >= memoryState.pages) return;
      changeMemoryPage(1);
    });
    memorySearchInput?.addEventListener("compositionstart", () => {
      memorySearchComposing = true;
      window.clearTimeout(memorySearchTimer);
    });
    memorySearchInput?.addEventListener("compositionend", () => {
      memorySearchComposing = false;
      scheduleMemorySearch();
    });
    memorySearchInput?.addEventListener("input", () => {
      updateMemoryFilterActions();
      if (!memorySearchComposing) scheduleMemorySearch();
    });
    memorySearchInput?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.isComposing && !memorySearchComposing) {
        event.preventDefault();
        searchMemory();
      }
    });
    memoryFilterIds.forEach((id) => on(`#${id}`, "change", searchMemory));
    on("#saveReusableMemoryButton", "click", () => saveReusableMemoryRows());
    on("#deleteConflictMemoryButton", "click", () => deleteConflictingMemory());
    on("#deleteNonReusableMemoryButton", "click", () => deleteNonReusableMemory());
    on("#importMemoryButton", "click", async () => {
      const response = await fetch("/api/memory/import_suggestions", { method: "POST" });
      const payload = await response.json();
      if (!response.ok) {
        notifyToast(payload.detail || "历史审批建议导入失败。", "error");
        return;
      }
      const message = [
        `扫描 ${payload.scanned || 0} 条`,
        `新增 ${payload.imported || 0} 条`,
        `已存在 ${payload.existing || 0} 条`,
        `冲突 ${payload.conflicts || 0} 条`,
        `人工复核候选 ${payload.candidate_manual_review || 0} 条`,
        `低置信度候选 ${payload.candidate_low_confidence || 0} 条`,
        `类别未映射候选 ${payload.candidate_unmapped_category || 0} 条`
      ].join("；");
      resetMemoryPage();
      await refreshMemory();
      reportMemoryFeedback(`导入完成：${message}`);
    });

    function reportMemoryFeedback(message, tone = "") {
      const feedback = document.querySelector("#memoryFeedback");
      if (!feedback) return;
      feedback.hidden = !message;
      feedback.dataset.tone = tone;
      feedback.textContent = message;
    }

    async function refreshMemory() {
      if (!document.querySelector("#memoryTable")) return;
      const requestId = memoryState.requestSeq + 1;
      memoryState.requestSeq = requestId;
      window.clearTimeout(memorySearchTimer);
      reportMemoryFeedback("");
      setMemoryLoading(true);
      const filtered = hasMemoryFilters();
      const params = new URLSearchParams({
        q: memorySearchInput.value.trim(),
        category: document.querySelector("#memoryCategoryFilter").value || "",
        reusable: document.querySelector("#memoryReusableFilter").value || "",
        conflict: document.querySelector("#memoryConflictFilter").value || "",
        page: String(memoryState.page || 1),
        per_page: String(memoryState.perPage || 10),
        limit: String(memoryState.perPage || 10)
      });
      try {
        const payload = await readDashboardJSON(`/api/memory?${params.toString()}`);
        if (requestId !== memoryState.requestSeq) return false;
        memoryState.rows = payload.preview || [];
        memoryState.categories = payload.categories || [];
        memoryState.page = Number(payload.page || 1);
        memoryState.pages = Number(payload.pages || 1);
        memoryState.perPage = Number(payload.per_page || 10);
        memoryState.total = Number(payload.rows || 0);
        if (payload.review_decision_options && payload.review_decision_options.length) {
          reviewCategories = payload.review_decision_options;
        }
        renderMemoryFilters();
        renderMemoryRows(payload, filtered);
        updateMemoryFilterActions();
        return true;
      } catch (_) {
        if (requestId !== memoryState.requestSeq) return false;
        const wrap = document.querySelector(".memory-table-wrap");
        const hasResults = wrap.dataset.loaded === "true";
        reportMemoryFeedback(hasResults ? "刷新失败，仍显示上一次加载的记录。请重试。"
          : "记忆库暂时无法加载，请重试。", "error");
        if (!hasResults) {
          document.querySelector("#memoryTable").innerHTML = tableEmptyHtml(4, "无法加载记录", "可点击上方「刷新」重试。");
          setText("#memoryPageInfo", "加载失败");
          setText("#memoryMeta", "加载失败");
        }
        return false;
      } finally {
        if (requestId === memoryState.requestSeq) {
          setMemoryLoading(false);
        }
      }
    }

    function renderMemoryFilters() {
      const select = document.querySelector("#memoryCategoryFilter");
      if (!select) return;
      const current = select.value;
      select.innerHTML = '<option value="">全部类别</option>';
      Array.from(new Set([...(memoryState.categories || []), ...reviewCategories].filter(Boolean))).sort().forEach((category) => {
        const option = document.createElement("option");
        option.value = category;
        option.textContent = category;
        select.appendChild(option);
      });
      if (current) select.value = current;
    }

    function boolValue(value) {
      return String(value || "").toLowerCase() === "1" || String(value || "").toLowerCase() === "true";
    }

    function categoryOptionsHtml(selectedValue) {
      const selected = safe(selectedValue, "");
      const options = ['<option value="">请选择确认类别</option>'];
      if (selected && !reviewCategories.includes(selected)) {
        options.push(`<option value="" selected>${escapeHtml(selected)}（未映射，请重选）</option>`);
      }
      reviewCategories.forEach((category) => {
        options.push(`<option value="${escapeHtml(category)}" ${selected === category ? "selected" : ""}>${escapeHtml(category)}</option>`);
      });
      return options.join("");
    }

    function renderMemoryRows(payload, filtered) {
      const body = document.querySelector("#memoryTable");
      const meta = document.querySelector("#memoryMeta");
      if (!body || !meta) return;
      const rows = memoryState.rows || [];
      meta.textContent = payload.exists ? `${filtered ? "匹配 " : "共 "}${safe(payload.rows, 0)} 条` : "尚未生成记忆库";
      document.querySelector(".memory-table-wrap").dataset.loaded = "true";
      renderMemoryPagination(payload);
      body.innerHTML = "";
      if (!rows.length) {
        renderMemoryDetail(null);
        body.innerHTML = filtered ? tableEmptyHtml(4, "没有匹配的记录", "修改关键词或清空筛选后重试。")
          : tableEmptyHtml(4, "暂无记忆记录", "可从维护操作中导入审批建议。");
        return;
      }
      rows.forEach((row) => {
        const tr = document.createElement("tr");
        tr.className = "memory-row";
        tr.setAttribute("role", "row");
        tr.dataset.id = row.id;
        const categoryOptions = categoryOptionsHtml(row.final_category);
        const reusable = boolValue(row.reusable);
        const manualVerified = boolValue(row.manual_verified);
        const conflict = boolValue(row.conflict);
        const savedFlags = {reusable, manual_verified: manualVerified, conflict};
        const flags = memoryStatusDrafts.get(String(row.id)) || savedFlags;
        const name = row.standard_name || row.cleaned_name || row.raw_name || "未命名试剂";
        tr.innerHTML = `
          <td class="memory-name-cell" role="cell">
            <strong class="memory-standard-name-view">${escapeHtml(name)}</strong>
            ${row.raw_name && row.raw_name !== name ? `<span class="memory-raw-name-view">原名：${escapeHtml(row.raw_name)}</span>` : ""}
            <div class="memory-identity-meta">
              <span>CAS ${escapeHtml(safe(row.cas, "未记录"))}</span>
              <span>#${escapeHtml(row.id)}</span>
            </div>
            <input type="hidden" class="memory-cas" value="${escapeHtml(row.cas)}">
            <input type="hidden" class="memory-raw-name" value="${escapeHtml(row.raw_name)}">
            <input type="hidden" class="memory-cleaned-name" value="${escapeHtml(row.cleaned_name)}">
            <input type="hidden" class="memory-standard-name" value="${escapeHtml(row.standard_name)}">
            <input type="hidden" class="memory-confidence" value="${escapeHtml(row.confidence)}">
            <textarea class="memory-reason memory-hidden-field">${escapeHtml(row.reason)}</textarea>
          </td>
          <td class="memory-category-cell" role="cell">
            <select class="memory-category" aria-label="${escapeHtml(name)}：物化特性">${categoryOptions}</select>
          </td>
          <td class="memory-state-cell" role="cell">
            <div class="memory-row-flags">
              <label><input type="checkbox" class="memory-reusable" ${flags.reusable ? "checked" : ""}>可复用</label>
              <label><input type="checkbox" class="memory-manual" ${flags.manual_verified ? "checked" : ""} ${flags.reusable ? "disabled" : ""}>人工确认</label>
              <label><input type="checkbox" class="memory-conflict" ${flags.conflict ? "checked" : ""} ${flags.reusable ? "disabled" : ""}>有冲突</label>
              <button type="button" class="memory-save-flags" disabled>保存更改</button>
            </div>
          </td>
          <td class="memory-action-cell" role="cell">
            <div class="memory-row-actions">
              <button type="button" class="memory-delete memory-delete-action" aria-label="删除记录" title="删除记录">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18m-2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6m3 0V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m-6 4v7m4-7v7"/></svg>
              </button>
              <button type="button" class="memory-open" aria-haspopup="dialog">编辑</button>
            </div>
            <span class="memory-row-status"></span>
          </td>
        `;
        bindMemoryRowFlags(tr, savedFlags);
        tr.querySelector(".memory-open").addEventListener("click", () => {
          if (memoryState.loading) return;
          selectMemoryRow(tr);
          document.querySelector("#memoryRecordDialog").showModal();
        });
        tr.querySelector(".memory-delete").addEventListener("click", () => deleteMemoryRow(tr));
        body.appendChild(tr);
      });
    }

    function memoryRowFlags(tr) {
      const reusable = tr.querySelector(".memory-reusable").checked;
      return {
        reusable,
        manual_verified: reusable || tr.querySelector(".memory-manual").checked,
        conflict: !reusable && tr.querySelector(".memory-conflict").checked,
      };
    }

    function bindMemoryRowFlags(tr, savedFlags) {
      const save = tr.querySelector(".memory-save-flags");
      const fields = tr.querySelectorAll(".memory-row-flags input");
      const category = tr.querySelector(".memory-category");
      category.dataset.savedValue = category.value;
      if (category.value) category.options[0].disabled = true;
      const draft = memoryStatusDrafts.get(tr.dataset.id);
      if (draft?.final_category && Array.from(category.options).some((option) => option.value === draft.final_category)) {
        category.value = draft.final_category;
      }
      const updateDraft = () => {
        const flags = memoryRowFlags(tr);
        for (const [key, selector] of [["manual_verified", ".memory-manual"], ["conflict", ".memory-conflict"]]) {
          const field = tr.querySelector(selector);
          if (flags.reusable) field.checked = flags[key];
          field.disabled = flags.reusable;
        }
        const changed = category.value !== category.dataset.savedValue || Object.keys(savedFlags).some((key) => flags[key] !== savedFlags[key]);
        if (changed) memoryStatusDrafts.set(tr.dataset.id, {...flags, final_category: category.value});
        else memoryStatusDrafts.delete(tr.dataset.id);
        tr.classList.toggle("memory-row-dirty", changed);
        save.disabled = !changed;
      };
      fields.forEach((field) => field.addEventListener("change", updateDraft));
      category.addEventListener("change", updateDraft);
      if (memoryStatusDrafts.has(tr.dataset.id)) updateDraft();
      save.addEventListener("click", async () => {
        if (memoryState.loading || memoryStatusPending || memoryBulkDeletePending) return;
        memoryStatusPending = true;
        save.disabled = true;
        category.disabled = true;
        fields.forEach((field) => { field.disabled = true; });
        save.textContent = "保存中…";
        try {
          const {response, payload} = await fetchDashboardJSON(`/api/memory/${tr.dataset.id}/flags`, {
            method: "PATCH",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(memoryRowUpdatePayload(tr)),
          });
          if (!response.ok || !payload.updated) {
            reportMemoryFeedback(response.status === 405 || payload.detail === "Not Found"
              ? "当前服务尚未加载行内状态接口，请重启 Web UI 后再试。" : apiErrorMessage(payload), "error");
            return;
          }
          memoryStatusDrafts.delete(tr.dataset.id);
          Object.assign(savedFlags, {
            reusable: boolValue(payload.record?.reusable ?? memoryRowFlags(tr).reusable),
            manual_verified: boolValue(payload.record?.manual_verified ?? memoryRowFlags(tr).manual_verified),
            conflict: boolValue(payload.record?.conflict ?? memoryRowFlags(tr).conflict),
          });
          fields[0].checked = savedFlags.reusable;
          fields[1].checked = savedFlags.manual_verified;
          fields[2].checked = savedFlags.conflict;
          category.dataset.savedValue = category.value;
          if (category.value) category.options[0].disabled = true;
          if (await refreshMemory()) reportMemoryFeedback("已保存列表更改。");
        } catch (_) {
          reportMemoryFeedback("未收到保存结果，请刷新核对后再试。", "error");
        } finally {
          memoryStatusPending = false;
          category.disabled = false;
          fields[0].disabled = false;
          save.textContent = "保存更改";
          if (tr.isConnected) updateDraft();
        }
      });
    }

    function memoryRowUpdatePayload(tr) {
      const flags = memoryRowFlags(tr);
      const category = tr.querySelector(".memory-category");
      if (category.value && category.value !== category.dataset.savedValue) flags.final_category = category.value;
      return flags;
    }

    on("#closeMemoryRecord", "click", () => document.querySelector("#memoryRecordDialog").close());

    function selectMemoryRow(tr) {
      document.querySelectorAll(".memory-row-selected").forEach((item) => item.classList.remove("memory-row-selected"));
      tr.classList.add("memory-row-selected");
      renderMemoryDetail(memoryRowPayload(tr), tr);
    }

    function renderMemoryDetail(row, tr = null) {
      const panel = document.querySelector("#memoryDetailPanel");
      if (!panel) return;
      if (!row) {
        panel.innerHTML = `
          <div class="memory-detail-empty">
            <strong>\u5f53\u524d\u8bb0\u5f55</strong>
            <span>\u6682\u65e0\u8bb0\u5fc6\u5e93\u8bb0\u5f55\u3002\u5bfc\u5165\u5ba1\u6279\u5efa\u8bae\u6216\u5237\u65b0\u6570\u636e\u540e\u53ef\u5728\u8fd9\u91cc\u67e5\u770b\u6458\u8981\u3002</span>
          </div>
        `;
        return;
      }
      panel.innerHTML = `
        <div class="memory-detail-head">
          <div>
            <span>\u5f53\u524d\u8bb0\u5f55</span>
            <strong>${escapeHtml(row.standard_name || row.cleaned_name || row.raw_name || "-")}</strong>
          </div>
          <small>ID ${escapeHtml(row.id || tr?.dataset.id || "-")}</small>
        </div>
        <div class="memory-detail-edit-grid">
          <label>CAS <input data-memory-field="cas" value="${escapeHtml(row.cas || "")}"></label>
          <label>ERP \u539f\u59cb\u540d <input data-memory-field="raw_name" value="${escapeHtml(row.raw_name || "")}"></label>
          <label>\u6e05\u6d17\u540d <input data-memory-field="cleaned_name" value="${escapeHtml(row.cleaned_name || "")}"></label>
          <label>\u6807\u51c6\u540d <input data-memory-field="standard_name" value="${escapeHtml(row.standard_name || "")}"></label>
          <label>置信度 <input data-memory-field="confidence" value="${escapeHtml(safe(row.confidence, ""))}"></label>
          <label class="memory-detail-reason">\u5907\u6ce8 <textarea data-memory-field="reason">${escapeHtml(row.reason || "")}</textarea></label>
        </div>
        <div class="memory-detail-actions">
          <button type="button" id="memoryDetailSave" class="memory-save-action">\u4fdd\u5b58\u5f53\u524d\u8bb0\u5f55</button>
          <span id="memoryDetailStatus" role="status"></span>
        </div>
      `;
      if (!tr) return;
      panel.querySelector("#memoryDetailSave")?.addEventListener("click", () => {
        const payload = Object.fromEntries(Array.from(panel.querySelectorAll("[data-memory-field]"),
          (field) => [field.dataset.memoryField, field.value]));
        saveMemoryRow(tr, {...payload, source: "web_ui_manual_edit"});
      });
    }

    function renderMemoryPagination(payload) {
      const prev = document.querySelector("#memoryPrevPage");
      const next = document.querySelector("#memoryNextPage");
      const info = document.querySelector("#memoryPageInfo");
      const page = Number(payload.page || 1);
      const pages = Number(payload.pages || 1);
      const total = Number(payload.rows || 0);
      const perPage = Number(payload.per_page || 10);
      prev.disabled = !payload.exists || page <= 1;
      next.disabled = !payload.exists || page >= pages;
      const start = total ? (page - 1) * perPage + 1 : 0;
      info.textContent = total ? `${start}–${Math.min(page * perPage, total)} / 共 ${total} 条 · 第 ${page} / ${pages} 页`
        : "0 条记录";
    }

    function setMemoryLoading(loading) {
      memoryState.loading = loading;
      const prev = document.querySelector("#memoryPrevPage");
      const next = document.querySelector("#memoryNextPage");
      const wrap = document.querySelector(".memory-table-wrap");
      if (!prev || !next || !wrap) return;
      wrap.setAttribute("aria-busy", String(loading));
      setText("#refreshMemoryButton", loading ? "刷新中…" : "刷新");
      setDisabled("#refreshMemoryButton", loading);
      prev.disabled = loading || memoryState.page <= 1;
      next.disabled = loading || memoryState.page >= memoryState.pages;
    }

    async function saveMemoryRow(tr, payload) {
      const recordId = tr.dataset.id;
      const button = document.querySelector("#memoryDetailSave");
      const status = tr.querySelector(".memory-row-status");
      const feedback = document.querySelector("#memoryDetailStatus");
      if (!button || button.disabled) return false;
      button.disabled = true;
      status.textContent = "保存中...";
      status.className = "memory-row-status";
      if (feedback) feedback.textContent = "保存中…";
      let response, result;
      try {
        response = await fetch(`/api/memory/${recordId}`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        result = await response.json();
      } catch (_) {
        if (feedback) feedback.textContent = "未收到保存结果，请关闭后刷新记录核对。";
        return false;
      }
      if (!response.ok || !result.updated) {
        button.disabled = false;
        status.textContent = "保存失败";
        status.className = "memory-row-status failed";
        if (feedback) feedback.textContent = apiErrorMessage(result);
        return false;
      }
      status.textContent = "已保存";
      status.className = "memory-row-status saved";
      tr.classList.remove("memory-row-dirty");
      if (button.isConnected) document.querySelector("#memoryRecordDialog").close();
      const refreshed = await refreshMemory();
      if (refreshed) reportMemoryFeedback("已保存记录。");
      if (!document.querySelector("#memoryRecordDialog").open) {
        const restored = Array.from(document.querySelectorAll(".memory-row")).find((row) => row.dataset.id === recordId);
        (restored?.querySelector(".memory-open") || memorySearchInput)?.focus({preventScroll: true});
      }
      return true;
    }

    function memoryRowPayload(tr) {
      return {
        cas: tr.querySelector(".memory-cas").value,
        raw_name: tr.querySelector(".memory-raw-name").value,
        cleaned_name: tr.querySelector(".memory-cleaned-name").value,
        standard_name: tr.querySelector(".memory-standard-name").value,
        confidence: tr.querySelector(".memory-confidence").value,
        reason: tr.querySelector(".memory-reason").value,
      };
    }

    async function saveReusableMemoryRows() {
      if (memoryState.loading || memoryStatusPending || memoryBulkDeletePending) return;
      const rows = Array.from(document.querySelectorAll("#memoryTable tr"))
        .filter((tr) => tr.dataset.id && tr.querySelector(".memory-reusable")?.checked);
      if (!rows.length) {
        reportMemoryFeedback("本页没有勾选「可复用」的记录。");
        return;
      }
      memoryStatusPending = true;
      let saved = 0;
      try {
        for (const tr of rows) {
          const {response, payload} = await fetchDashboardJSON(`/api/memory/${tr.dataset.id}/flags`, {
            method: "PATCH",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(memoryRowUpdatePayload(tr)),
          });
          if (!response.ok || !payload.updated) {
            await refreshMemory();
            reportMemoryFeedback(`已保存 ${saved} 条，批量保存已停止：${apiErrorMessage(payload)}`, "error");
            return;
          }
          memoryStatusDrafts.delete(tr.dataset.id);
          saved += 1;
        }
        if (await refreshMemory()) reportMemoryFeedback(`已保存 ${saved} 条可复用记录。`);
      } catch (_) {
        reportMemoryFeedback(`已确认保存 ${saved} 条，后续结果未知。请刷新核对。`, "error");
      } finally {
        memoryStatusPending = false;
      }
    }

    async function deleteMemoryRow(tr) {
      if (memoryState.loading) return;
      const recordId = tr.dataset.id;
      const name = tr.querySelector(".memory-standard-name-view")?.textContent;
      const button = tr.querySelector(".memory-delete");
      if (button.disabled || !(await confirmAction({title: "删除记忆记录", message: `确认删除「${name || recordId}」？`, confirmText: "确认删除", danger: true}))) return;
      button.disabled = true;
      reportMemoryFeedback("正在删除记录…");
      try {
        const {response, payload} = await fetchDashboardJSON(`/api/memory/${recordId}`, {method: "DELETE"});
        if (!response.ok || !payload.deleted) {
          reportMemoryFeedback(apiErrorMessage(payload), "error");
          return;
        }
        if (await refreshMemory()) {
          memoryStatusDrafts.delete(recordId);
          reportMemoryFeedback("已删除记录。");
          document.querySelector(".memory-table-wrap")?.focus({preventScroll: true});
        }
      } catch (_) {
        reportMemoryFeedback("未收到删除结果，请先刷新核对记录是否仍存在。", "error");
      } finally {
        button.disabled = false;
      }
    }

    async function deleteConflictingMemory() {
      const message = [
        "确认删除所有标记为“冲突”的试剂记忆记录？",
        "包括已经人工确认过的冲突记录。",
        "删除前会自动备份 SQLite 数据库。"
      ].join("\n");
      if (!(await confirmAction({title: "批量删除冲突记录", message, confirmText: "确认删除", danger: true}))) return;
      const response = await fetch("/api/memory/delete_conflicting", { method: "POST" });
      const result = await response.json();
      if (!response.ok) {
        notifyToast(result.detail || "批量删除冲突记录失败。", "error");
        return;
      }
      resetMemoryPage();
      await refreshMemory();
      notifyToast(`已删除 ${result.deleted || 0} 条冲突记录。`, "success");
    }

    async function deleteNonReusableMemory() {
      if (memoryStatusPending || memoryBulkDeletePending) return;
      if (memoryStatusDrafts.size) {
        reportMemoryFeedback("请先保存列表中的状态修改，或将它们改回原值，再批量删除。", "error");
        return;
      }
      if (!(await confirmAction({title: "删除不可复用记录", message: "确认删除整个试剂记忆库中所有不可复用记录？", detail: "此操作不受当前筛选或页码限制，包含已人工确认的记录；删除前会自动备份。", confirmText: "全部删除", danger: true}))) return;
      const button = document.querySelector("#deleteNonReusableMemoryButton");
      memoryBulkDeletePending = true;
      button.disabled = true;
      reportMemoryFeedback("正在备份并删除不可复用记录…");
      try {
        const {response, payload} = await fetchDashboardJSON("/api/memory/delete_non_reusable", {method: "POST"}, 120000);
        if (!response.ok) {
          reportMemoryFeedback(response.status === 405 || payload.detail === "Not Found"
            ? "当前服务尚未加载批量删除接口，请重启 Web UI 后再试。" : apiErrorMessage(payload), "error");
          return;
        }
        resetMemoryPage();
        const refreshed = await refreshMemory();
        const message = payload.deleted ? `已删除 ${payload.deleted} 条不可复用记录，备份已保存在运行日志目录。` : "没有需要删除的不可复用记录。";
        reportMemoryFeedback(refreshed ? message : `${message} 列表刷新失败，请手动刷新。`, refreshed ? "" : "error");
      } catch (_) {
        reportMemoryFeedback("未收到批量删除结果，请先刷新核对，不要重复提交。", "error");
      } finally {
        memoryBulkDeletePending = false;
        button.disabled = false;
      }
    }

    function logLineMatchesFilter(line) {
      const text = String(line || "").toLowerCase();
      if (logState.filter === "error") {
        return /error|exception|traceback|failed|failure|失败|错误|异常/.test(text);
      }
      if (logState.filter === "warning") {
        return /warn|warning|警告|注意/.test(text);
      }
      return true;
    }

    function renderLogBox() {
      const box = document.querySelector("#logBox");
      if (!box) return;
      const keyword = String(document.querySelector("#logSearchInput")?.value || "").trim();
      const lowerKeyword = keyword.toLowerCase();
      const lines = logState.lines.filter((line) => {
        if (!logLineMatchesFilter(line)) return false;
        if (lowerKeyword && !String(line).toLowerCase().includes(lowerKeyword)) return false;
        return true;
      });
      box.classList.toggle("log-empty", !lines.length);
      if (!lines.length) {
        box.textContent = logState.lines.length ? "当前过滤条件下暂无日志。" : "暂无可显示日志。运行任务后会显示最近一次输出。";
        return;
      }
      const html = lines.map((line, idx) => {
        const lower = String(line).toLowerCase();
        const cls = /error|exception|traceback|failed|失败|错误|异常/.test(lower)
          ? "log-line log-line-error"
          : /warn|warning|警告|注意/.test(lower)
            ? "log-line log-line-warn"
            : /\[flow\]|start |end /.test(lower)
              ? "log-line log-line-stage"
              : "log-line";
        let escaped = escapeHtml(line);
        if (keyword) {
          const safeRe = new RegExp(`(${keyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
          escaped = escaped.replace(safeRe, '<mark class="log-highlight">$1</mark>');
        }
        return `<div class="${cls}"><span class="log-line-num">${idx + 1}</span><span>${escaped}</span></div>`;
      }).join("");
      box.innerHTML = html;
      const autoScroll = document.querySelector("#logAutoScrollToggle");
      if (!autoScroll || autoScroll.checked) {
        box.scrollTop = box.scrollHeight;
      }
    }

    function artifactGroupName(item) {
      const name = String(item?.name || "").toLowerCase();
      if (/\.(xlsx|xls|csv|tsv)$/.test(name)) return "Excel / 数据";
      if (/\.(png|jpg|jpeg|webp)$/.test(name)) return "截图";
      if (/\.(html|htm)$/.test(name)) return "HTML 页面";
      if (/\.(log|txt)$/.test(name)) return "运行日志";
      return "其他";
    }

    let artifactItems = [];
    on("#artifactTypeFilter", "change", () => renderArtifacts(artifactItems));
    function renderArtifacts(items) {
      const holder = document.querySelector("#artifactList");
      const meta = document.querySelector("#artifactMeta");
      if (!holder) return;
      artifactItems = items || [];
      const type = document.querySelector("#artifactTypeFilter")?.value;
      items = artifactItems.filter((item) => !type || artifactGroupName(item) === type);
      holder.innerHTML = "";
      if (!items || items.length === 0) {
        if (meta) meta.textContent = type ? "此类型暂无产物" : "暂无可下载产物";
        holder.innerHTML = `
          <div class="artifact-empty">
            <strong>暂无产物</strong>
            <span>运行采集或审批建议任务后，下载项会显示在这里。</span>
          </div>
        `;
        return;
      }
      if (meta) meta.textContent = `显示 ${items.length} / ${artifactItems.length} 个产物，组内按最新更新排序。`;
      const groups = new Map();
      [...items].sort((a, b) => String(b.modified || "").localeCompare(String(a.modified || ""))).forEach((item) => {
        const groupName = artifactGroupName(item);
        if (!groups.has(groupName)) groups.set(groupName, []);
        groups.get(groupName).push(item);
      });
      groups.forEach((groupItems, groupName) => {
        const section = document.createElement("section");
        section.className = "artifact-group";
        const title = document.createElement("h3");
        title.className = "artifact-group-title";
        title.textContent = `${groupName} · ${groupItems.length}`;
        const list = document.createElement("div");
        list.className = "artifact-group-list";
        groupItems.forEach((item) => {
          const link = document.createElement("a");
          link.href = item.download_url;
          link.className = "artifact";
          link.innerHTML = `
            <strong>${escapeHtml(item.name)}</strong>
            <span>${escapeHtml(displayTime(item.modified))} · ${Math.ceil(item.size / 1024)} KB</span>
          `;
          list.appendChild(link);
        });
        section.appendChild(title);
        section.appendChild(list);
        holder.appendChild(section);
      });
    }

    function renderWorkflow(workflow) {
      const holder = document.querySelector("#workflowSteps");
      if (!holder) return;
      const steps = workflow && workflow.steps ? workflow.steps : [];
      holder.hidden = !steps.length;
      if (!steps.length) return;
      holder.innerHTML = "";
      steps.forEach((step) => {
        const item = document.createElement("div");
        item.className = `step ${escapeHtml(step.state || "waiting")}`;
        item.textContent = step.label || "";
        holder.appendChild(item);
      });
    }

    async function refreshApprovalSummary(options = {}) {
      if (approvalStateLoaded && !options.force) return;
      if (!document.querySelector("#suggestionTable") && !document.querySelector("#suggestionRows")) return;
      const approval = await readDashboardJSON("/api/approval_summary");
      approvalStateLoaded = true;
      setText("#suggestionRows", safe(approval.rows, "0"));
      setText("#suggestionMeta", approval.exists ? `最近更新：${displayTime(approval.modified)}` : "尚未生成审批建议");
      renderTags(approval.categories || {});
      window.uiRenderCategoryBar?.("overviewCategoryBar", approval.categories || {});
      window.uiRenderCategoryBar?.("suggestionsCategoryBar", approval.categories || {});
      renderSuggestions(approval.preview || []);
    }

    async function refreshReviewQueue(options = {}) {
      if (reviewStateLoaded && !options.force) return;
      if (!document.querySelector("#reviewTable") && !document.querySelector("#manualReview")) return;
      const params = new URLSearchParams({
        page: String(reviewState.page || 1),
        per_page: String(reviewState.perPage || 20),
        list_number: reviewState.listNumber || "",
        sort: reviewState.sortDirection || "desc",
      });
      const reviewQueue = await readDashboardJSON(`/api/review_queue?${params.toString()}`);
      reviewStateLoaded = true;
      setText("#manualReview", safe(reviewQueue.pending, "0"));
      if (options.force || !reviewInteractionActive()) {
        renderReviewQueue(reviewQueue);
      }
    }

    async function refreshArtifacts(options = {}) {
      if (artifactStateLoaded && !options.force) return;
      if (!document.querySelector("#artifactList")) return;
      const payload = await readDashboardJSON("/api/artifacts");
      artifactStateLoaded = true;
      renderArtifacts(payload.artifacts || []);
    }

    async function refreshDataHealth(options = {}) {
      if (!document.querySelector("#dataHealthPanel")) return;
      if (refreshDataHealth.loaded && !options.force) return;
      try {
        const payload = await readDashboardJSON("/api/data_health");
        refreshDataHealth.loaded = true;
        setText("#dataHealthStatus", `最近检查：${safe(payload.last_checked_at)}`);
        setText("#healthMemoryMojibake", safe(payload.memory_mojibake_records, "0"));
        setText("#healthReusableMojibake", safe(payload.reusable_mojibake_records, "0"));
        setText("#healthUnrecoverableReusable", safe(payload.unrecoverable_reusable_mojibake_records, "0"));
        setText("#healthConflicts", safe(payload.conflicting_memory_records, "0"));
        setText("#healthConfirmedMissingMemory", safe(payload.confirmed_review_missing_memory, "0"));
        setText("#healthDuplicateReviews", safe(payload.blocking_duplicate_review_items, "0"));
      } catch (error) {
        setText("#dataHealthStatus", `检查失败：${error}`);
      }
    }
    refreshDataHealth.loaded = false;

    async function refreshLogTail(options = {}) {
      if (logStateLoaded && !options.force) return;
      if (!document.querySelector("#logBox")) return;
      const payload = await readDashboardJSON("/api/log_tail");
      logStateLoaded = true;
      logState.lines = payload.log_tail || [];
      renderLogBox();
    }

    let statusRefreshPromise = null;

    async function fetchDashboardJSON(url, options = {}, timeoutMs = 10000) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(url, {...options, signal: controller.signal});
        const payload = await response.json();
        return {response, payload};
      } finally {
        clearTimeout(timeout);
      }
    }

    async function readDashboardJSON(url, options = {}, timeoutMs = 10000) {
      const {response, payload} = await fetchDashboardJSON(url, options, timeoutMs);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return payload;
    }

    function dashboardRequestErrorMessage(error, fallback = "操作失败。") {
      const message = String(error?.message || error || "");
      if (
        error?.name === "AbortError"
        || /aborted|aborterror|signal is aborted/i.test(message)
      ) {
        return "请求等待时间较长，前端已停止等待；请稍后刷新查看结果，或检查 LLM 服务是否响应较慢。";
      }
      return message || fallback;
    }

    function refreshStatus(options = {}) {
      if (statusRefreshPromise) return statusRefreshPromise;
      statusRefreshPromise = readAndRenderStatus(options)
        .then(() => {
          const badge = document.querySelector("#runBadge");
          if (badge) badge.removeAttribute("title");
          return true;
        })
        .catch(() => {
          setText("#runBadge", "刷新失败 · 数据可能已过期");
          const badge = document.querySelector("#runBadge");
          if (badge) badge.title = "暂时无法刷新状态，保留上次数据；稍后自动重试。";
          return false;
        })
        .finally(() => { statusRefreshPromise = null; });
      return statusRefreshPromise;
    }

    async function readAndRenderStatus(options = {}) {
      const data = await readDashboardJSON("/api/status");
      renderDingtalkConnection(data.dingtalk_stream || {});
      const status = data.status || {};
      renderTaskControl(status);
      const runSummary = status.summary || {};
      const runtime = data.runtime || {};
      const todoTasks = data.todo_tasks || {};
      const scheduler = data.scheduler || {};
      const runJustFinished = !status.running && status.finished_at && status.finished_at !== lastFinishedAt;
      if (runtime.review_decision_options && runtime.review_decision_options.length) {
        reviewCategories = runtime.review_decision_options;
      }

      setText("#runBadge", status.control?.state === "stopped" ? "已人工停止" : status.control?.state === "stop_requested" ? "等待安全停止" : status.running ? "运行中" : (status.success === false ? "上次运行失败" : (status.success === true ? "上次运行成功" : "空闲")));
      setClass("#runBadge", status.running ? "badge running" : (status.success === false ? "badge failed" : (status.success === true ? "badge succeeded" : "badge")));
      setDisabled("#stopRunButton", !status.running);
      const programControl = data.program_control || {};
      const programBusy = status.running || programControl.pending_workbuddy;
      setDisabled("#restartRunButton", !programControl.available || programBusy);
      setDisabled("#exitProgramButton", !programControl.available || programBusy);
      setDisabled("#safeStopRunButton", !status.running || !["suggestions", "workbuddy_loop", "workbuddy_prepare", "workbuddy_import"].includes(status.action) || !status.task_id || status.control?.state === "stop_requested");
      const unavailableTitle = !programControl.available ? "热重载模式请在启动终端停止程序" : programBusy ? "请先安全停止并核对任务结果" : "";
      for (const id of ["#restartRunButton", "#exitProgramButton"]) {
        const button = document.querySelector(id);
        if (button) button.title = unavailableTitle;
      }
      setText("#statusLine", status.running ? "任务执行中，请保持 Playwright 浏览器可见。" : safe(status.error || runSummary.outcome, "当前没有运行中的任务。"));
      setText("#erpUrl", runtime.erp_url_configured ? "已配置" : "未配置");
      setText("#erpUser", runtime.erp_username_configured ? "已配置" : "未配置");
      setText("#erpPassword", runtime.erp_password_configured ? "已配置" : "未配置");
      setText("#apiKey", runtime.llm_api_key_configured ? "已配置" : "未配置");
      setText("#currentAction", safe(status.action_label || status.action));
      setText("#lastRunResult", safe(status.result_label));
      setClass("#lastRunResult", status.success === true ? "result-ok" : (status.success === false ? "result-failed" : ""));
      setText("#startedAt", displayTime(status.started_at));
      setText("#finishedAt", displayTime(status.finished_at));
      const summaryTargets = runSummary.target_list_numbers || [];
      setText("#runTargetLists", summaryTargets.length ? summaryTargets.join("，") : "-");
      setText("#runPages", safe(runSummary.processed_pages, "-"));
      setText("#runPageSuggestions", safe(runSummary.page_suggestion_count, "-"));
      setText("#runWritableCandidates", safe(runSummary.writable_candidate_count, "-"));
      setText("#runManualReviewCandidates", safe(runSummary.manual_review_candidate_count, "-"));
      setText("#runSearchFailures", safe(runSummary.search_failure_count, "-"));
      setText("#runChemicalCacheHits", safe(runSummary.chemical_cache_hit_count, "-"));
      setText("#runChemicalFresh", safe(runSummary.chemical_fresh_count, "-"));
      setText("#runChemicalStale", safe(runSummary.chemical_stale_count, "-"));
      setText("#runChemicalUnavailable", safe(runSummary.chemical_unavailable_count, "-"));
      setText("#runChemicalConflicts", safe(runSummary.chemical_identity_conflict_count, "-"));
      setText("#runChemicalMixtures", safe(runSummary.chemical_mixture_count, "-"));
      setText("#runChemicalPublicRequests", safe(runSummary.chemical_public_request_count, "-"));
      setText("#runChemicalSearchSeconds", runSummary.chemical_search_seconds === undefined ? "-" : `${safe(runSummary.chemical_search_seconds, "0")}s`);
      setText("#runPubChemResults", safe(runSummary.pubchem_result_count, "-"));
      setText("#runLlmBatches", safe(runSummary.llm_batch_count, "-"));
      setText("#runLlmSeconds", runSummary.llm_seconds === undefined ? "-" : `${safe(runSummary.llm_seconds, "0")}s`);
      setText("#runLlmSkips", safe(runSummary.llm_skip_count, "-"));
      setText("#runDuplicateSearchReuse", safe(runSummary.duplicate_search_reuse_count, "-"));
      setText("#runDuplicateLlmReuse", safe(runSummary.duplicate_llm_reuse_count, "-"));
      setText("#runIdentityEnrichmentCalls", safe(runSummary.identity_enrichment_call_count, "-"));
      setText("#runIdentityEnrichmentEnglish", safe(runSummary.identity_enrichment_english_candidate_count, "-"));
      setText("#runIdentityEnrichmentCas", safe(runSummary.identity_enrichment_cas_candidate_count, "-"));
      setText("#runIdentityEnrichmentVerified", safe(runSummary.identity_enrichment_verified_count, "-"));
      setText("#runIdentityEnrichmentFailures", safe(runSummary.identity_enrichment_failure_count, "-"));
      setText("#runManualReviewQueued", safe(runSummary.manual_review_queued_count, "-"));
      setText("#runManualReviewUpdated", safe(runSummary.manual_review_updated_count, "-"));
      setText("#runManualReviewFlushes", safe(runSummary.manual_review_batch_flush_count, "-"));
      setText("#runApiReadDetail", safe(runSummary.api_read_detail_count, "-"));
      setText("#runApiRecordResolve", safe(runSummary.api_record_resolve_count, "-"));
      setText("#runApiRecordResolveFailure", safe(runSummary.api_record_resolve_failure_count, "-"));
      setText("#runApiWriteAttempt", safe(runSummary.api_write_attempt_count, "-"));
      setText("#runApiWriteSuccess", safe(runSummary.api_write_success_count, "-"));
      setText("#runApiVerifyFailure", safe(runSummary.api_verify_failure_count, "-"));
      setText("#runApiPageStaleWarning", safe(runSummary.api_page_stale_warning_count, "-"));
      setText("#runApiFallbackWebWrite", safe(runSummary.api_fallback_web_write_count, "-"));
      setText("#runWriteSuccess", safe(runSummary.write_success_count, "-"));
      setText("#runWriteFailure", safe(runSummary.write_failure_count, "-"));
      setText("#runDeferredWrites", safe(runSummary.deferred_write_count, "-"));
      setText("#runNotFoundAfterReread", safe(runSummary.not_found_after_reread_count, "-"));
      setText("#runLogPath", safe(status.run_log_path, "-"));
      updateDryRunUi(runtime.app_dry_run);
      const effective = status.running ? (status.effective_policy || {}) : {};
      setText("#policySourcePill", status.running ? "本次任务" : "默认设置");
      setText("#pipelineText", status.running ? (effective.pipeline === "workbuddy" ? "WorkBuddy" : effective.pipeline === "v1" ? (effective.fallback_reason ? "V1（回退）" : "V1") : "非审批任务") : "WorkBuddy");
      const pipelineElement = document.querySelector("#pipelineText");
      if (pipelineElement) pipelineElement.title = effective.fallback_reason || "";
      setText("#autoPassText", safe(status.running ? effective.auto_pass : runtime.auto_pass));
      setText("#writeModeText", approvalWriteModeLabel(status.running ? effective.write_mode : runtime.approval_write_mode));
      setText("#erpWriteBackendText", erpWriteBackendLabel(status.running ? effective.write_backend : runtime.erp_write_backend));
      setText("#erpApiDiscoveryText", erpApiDiscoveryLabel(runtime.erp_api_discovery_status));
      setText("#processScopeText", (status.running ? effective.process_all_todos : runtime.process_all_todos) === "true" ? "全部待办" : (status.running ? (effective.target_list_numbers || "指定清单") : "勾选清单/首条"));
      const selectedLists = selectedTodoNumbers();
      setText("#targetList", selectedLists.length ? `${selectedLists.length} 个清单` : "-");
      setText("#llmModel", safe(runtime.llm_model));
      const llmModel = document.querySelector("#llmModel");
      if (llmModel) llmModel.title = safe(runtime.llm_model);
      if (status.running) {
        setText("#suggestionRows", safe(runSummary.suggestion_count, "0"));
        setText("#manualReview", safe(runSummary.manual_review_count, "0"));
      }
      setText("#safetyGate", (status.running ? effective.auto_pass : runtime.auto_pass) === "true" ? "启用校验" : "默认关闭");
      setText("#schedulerNextRun", safe(scheduler.next_run_at));
      setText("#schedulerLastRun", safe(scheduler.last_run_at));
      setText("#schedulerLastResult", safe(scheduler.last_result));
      setText("#schedulerLastMessage", safe(scheduler.last_message));
      setText("#currentVersionText", safe(runtime.app_version));
      setText("#appModeText", runtime.app_frozen ? "安装版" : "源码模式");

      renderTodoTasks(todoTasks);
      renderWorkflow(status.workflow || {});
      if (activePage === "overview" || activePage === "suggestions" || options.forceApproval) {
        await refreshApprovalSummary({force: options.forceApproval || runJustFinished});
      }
      if (activePage === "overview" || activePage === "review" || options.forceReview) {
        if (runJustFinished) reviewStateLoaded = false;
        if (options.forceReview || !reviewInteractionActive() || !document.querySelector(".review-row")) {
          await refreshReviewQueue({force: true === options.forceReview || !reviewStateLoaded});
        }
      }
      if (activePage === "overview") {
        await refreshDataHealth({force: runJustFinished});
      }
      if (activePage === "artifacts") {
        await refreshArtifacts({force: runJustFinished});
      }
      if (activePage === "logs") {
        await refreshLogTail({force: status.running || runJustFinished});
      }
      // Retry completion-dependent reads if any of them failed this time.
      if (status.finished_at) lastFinishedAt = status.finished_at;
    }

    function setUpdateState(state, label) {
      document.querySelectorAll(".update-pill").forEach((pill) => {
        pill.classList.remove("update-available", "update-current", "update-failed");
        if (state) pill.classList.add(state);
      });
      document.querySelectorAll("[data-update-state]").forEach((target) => { target.textContent = label; });
    }

    function setAllDisabled(selector, disabled) {
      document.querySelectorAll(selector).forEach((element) => { element.disabled = disabled; });
    }

    function setUpdateMessage(message) {
      document.querySelectorAll("[data-update-message]").forEach((target) => {
        target.textContent = message || "";
      });
    }

    async function checkForUpdate(clickedButton = null) {
      const buttons = Array.from(document.querySelectorAll('[data-update-action="check"]'));
      const button = clickedButton || buttons[0];
      if (!button) return;
      buttons.forEach((item) => { item.disabled = true; });
      setAllDisabled('[data-update-action="install"]', true);
      document.querySelectorAll('[data-update-action="install"]').forEach((item) => { item.hidden = true; });
      setUpdateState("", "检查中");
      try {
        const response = await fetch("/api/update/check");
        const payload = await response.json();
        lastUpdateInfo = payload;
        try { sessionStorage.setItem("reagent_update_cache_v1", JSON.stringify({ts: Date.now(), payload})); } catch (_) {}
        document.querySelectorAll("[data-update-release]").forEach((releaseLink) => {
          if (payload.release_url) {
            releaseLink.href = payload.release_url;
            releaseLink.hidden = false;
          }
        });
        if (!response.ok || !payload.ok) {
          setUpdateState("update-failed", "检查失败");
          setUpdateMessage(payload.detail || payload.error || "检查更新失败。");
          return;
        }
        if (payload.update_available) {
          setUpdateState("update-available", `有新版本 ${payload.latest_version || ""}`);
          document.querySelectorAll('[data-update-action="install"]').forEach((item) => {
            item.hidden = false;
            item.disabled = !initialRuntime.app_frozen;
          });
          const assetSize = payload.asset?.size ? `${Math.ceil(payload.asset.size / 1024 / 1024)} MB` : "";
          setUpdateMessage(`发现 ${payload.latest_version}，安装包 ${assetSize}。`);
          return;
        }
        setUpdateState("update-current", "已是最新版本");
        setUpdateMessage("当前已经是最新版本。");
      } catch (error) {
        setUpdateState("update-failed", "检查失败");
        setUpdateMessage(String(error));
      } finally {
        buttons.forEach((item) => { item.disabled = false; });
      }
    }

    on("#todoSearchInput", "input", () => {
      if (todoState.snapshot) renderTodoTasks(todoState.snapshot, true);
    });
    on("#logSearchInput", "input", () => renderLogBox());
    on("#logAutoScrollToggle", "change", () => renderLogBox());

    let adaptivePollTimer = null;
    let nextPollDelayMs = 10000;
    const nativeRefreshStatus = refreshStatus;
    async function scheduleNextPoll() {
      if (adaptivePollTimer) {
        clearTimeout(adaptivePollTimer);
        adaptivePollTimer = null;
      }
      if (document.visibilityState === "hidden") return;
      adaptivePollTimer = window.setTimeout(async () => {
        await nativeRefreshStatus();
        scheduleNextPoll();
      }, nextPollDelayMs);
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        nativeRefreshStatus().finally(scheduleNextPoll);
      } else if (adaptivePollTimer) {
        clearTimeout(adaptivePollTimer);
        adaptivePollTimer = null;
      }
    });

    const origRenderStatus = readAndRenderStatus;
    readAndRenderStatus = async function wrappedReadAndRenderStatus(options = {}) {
      await origRenderStatus(options);
      const badge = document.querySelector("#runBadge");
      const isRunning = badge?.classList.contains("running");
      nextPollDelayMs = isRunning ? 2500 : 10000;
    };

    refreshStatus().finally(scheduleNextPoll);
    if (activePage === "memory") refreshMemory();
