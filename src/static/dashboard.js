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
      multi_page: "全清单分页保存",
      generate_library: "保存并生成试剂库",
    };
    const logState = {
      lines: [],
      filter: "all",
    };
    let approvalStateLoaded = activePage === "suggestions" || activePage === "overview";
    let reviewStateLoaded = activePage === "review" || activePage === "overview";
    let artifactStateLoaded = activePage === "artifacts";
    let logStateLoaded = activePage === "logs";
    let lastFinishedAt = "";
    const $ = (selector) => document.querySelector(selector);

    function on(selector, eventName, handler) {
      const element = $(selector);
      if (element) element.addEventListener(eventName, handler);
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

    function initSettingsSections() {
      document.querySelectorAll(".settings-section").forEach((section) => {
        const head = section.querySelector(":scope > .settings-section-head");
        if (!head || head.classList.contains("compact-head") || head.querySelector(".settings-section-toggle")) return;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "settings-section-toggle";
        button.textContent = "收起";
        button.setAttribute("aria-expanded", "true");
        button.addEventListener("click", () => {
          const collapsed = section.classList.toggle("settings-section-collapsed");
          button.textContent = collapsed ? "展开" : "收起";
          button.setAttribute("aria-expanded", String(!collapsed));
        });
        head.appendChild(button);
      });
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

    function updateDryRunUi(value) {
      const enabled = String(value || "").toLowerCase() === "true";
      setText("#dryRunText", enabled ? "开启" : "关闭");
      const pill = document.querySelector("#dryRunPill");
      if (pill) pill.classList.toggle("dry-run-active", enabled);
      const warning = document.querySelector("#dryRunWarning");
      if (warning) warning.hidden = !enabled;
    }

    function setCheckbox(form, name, value) {
      if (!form) return;
      const field = form.querySelector(`[name="${name}"]`);
      if (field) field.checked = String(value || "").toLowerCase() === "true";
    }

    setSelectValue(runForm, "approval_write_mode", initialRuntime.approval_write_mode);
    setSelectValue(settingsForm, "approval_write_mode", initialRuntime.approval_write_mode);
    setCheckbox(runForm, "process_all_todos", initialRuntime.process_all_todos);
    setCheckbox(runForm, "auto_pass", initialRuntime.auto_pass);
    setCheckbox(settingsForm, "process_all_todos", initialRuntime.process_all_todos);
    setCheckbox(settingsForm, "app_dry_run", initialRuntime.app_dry_run);
    setCheckbox(settingsForm, "auto_pass", initialRuntime.auto_pass);
    setCheckbox(settingsForm, "scheduler_enabled", initialRuntime.scheduler_enabled);
    setCheckbox(settingsForm, "scheduler_use_default_run_policy", initialRuntime.scheduler_use_default_run_policy || "true");
    setCheckbox(settingsForm, "scheduler_auto_pass", initialRuntime.scheduler_auto_pass);
    setCheckbox(settingsForm, "scheduler_skip_manual_review_lists", initialRuntime.scheduler_skip_manual_review_lists || "true");
    setCheckbox(settingsForm, "dingtalk_notification_enabled", initialRuntime.dingtalk_notification_enabled);
    setCheckbox(settingsForm, "dingtalk_at_all", initialRuntime.dingtalk_at_all || "true");
    setCheckbox(settingsForm, "dingtalk_stream_enabled", initialRuntime.dingtalk_stream_enabled);
    setSelectValue(settingsForm, "scheduler_mode", initialRuntime.scheduler_mode);
    setSelectValue(settingsForm, "scheduler_approval_write_mode", initialRuntime.scheduler_approval_write_mode);
    setText("#writeModeText", approvalWriteModeLabel(initialRuntime.approval_write_mode));
    updateDryRunUi(initialRuntime.app_dry_run);
    setText("#schedulerNextRun", safe(initialScheduler.next_run_at));
    setText("#schedulerLastRun", safe(initialScheduler.last_run_at));
    setText("#schedulerLastResult", safe(initialScheduler.last_result));
    initLlmSettings();
    initSectionNav();
    initSidebarToggle();
    initSettingsSections();
    initSchedulerPolicyToggle();

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

    document.querySelectorAll("button[data-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const data = new FormData(runForm);
        data.set("action", button.dataset.action);
        data.set("target_list_numbers", selectedTodoNumbers().join(","));
        const response = await fetch("/api/run", { method: "POST", body: data });
        const payload = await response.json();
        await refreshStatus();
        await refreshReviewQueue({force: true});
        if (!payload.started) {
          alert(payload.message || payload.detail || "任务未启动");
        }
      });
    });

    on("#stopRunButton", "click", async () => {
      const response = await fetch("/api/stop", { method: "POST" });
      const payload = await response.json();
      await refreshStatus();
      await refreshReviewQueue({force: true});
      if (!payload.stopped) {
        alert(payload.message || payload.detail || "当前没有运行中的任务");
      }
    });

    on("#restartRunButton", "click", async () => {
      const response = await fetch("/api/restart", { method: "POST" });
      const payload = await response.json();
      if (!payload.restarting) {
        alert(payload.message || payload.detail || "程序未重启");
        return;
      }
      setText("#statusLine", "Web UI 正在重启，几秒后自动刷新页面。");
      setTimeout(() => window.location.reload(), 4500);
    });

    let lastUpdateInfo = null;

    on("#checkUpdateButton", "click", async () => {
      await checkForUpdate();
    });

    on("#installUpdateButton", "click", async () => {
      if (!lastUpdateInfo?.update_available) {
        alert("当前没有可安装的更新。");
        return;
      }
      if (!confirm(`确认下载并安装 ${lastUpdateInfo.latest_version}？程序会自动退出并启动安装器。`)) return;
      setUpdateMessage("正在下载更新包并启动安装器，请稍候...");
      setDisabled("#installUpdateButton", true);
      const response = await fetch("/api/update/install", { method: "POST" });
      const payload = await response.json();
      if (!response.ok || !payload.started) {
        setUpdateMessage(payload.detail || payload.message || "更新未启动。");
        setDisabled("#installUpdateButton", !lastUpdateInfo?.update_available);
        return;
      }
      setText("#updateStateText", "安装器已启动");
      setUpdateMessage(payload.message || "安装器已启动，当前程序即将退出。");
    });

    if (document.querySelector("#checkUpdateButton")) {
      window.setTimeout(() => checkForUpdate(), 800);
    }

    on("#selectAllTodos", "click", () => {
      todoState.selected = new Set(todoState.tasks.map((task) => task.list_number).filter(Boolean));
      renderTodoTasks({ tasks: todoState.tasks, exists: todoState.tasks.length > 0 });
    });

    on("#clearTodos", "click", () => {
      todoState.selected.clear();
      renderTodoTasks({ tasks: todoState.tasks, exists: todoState.tasks.length > 0 });
    });

    function providerById(providerId) {
      return llmProviders.find((provider) => provider.id === providerId) || llmProviders[0] || {};
    }

    function setModelOptions(models, selectedModel) {
      const modelSelect = document.querySelector("#llmModelSelect");
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

    function setUpdateMessage(message) {
      const target = document.querySelector("#updateMessage");
      if (target) target.textContent = message || "";
    }

    async function checkForUpdate() {
      const button = document.querySelector("#checkUpdateButton");
      const installButton = document.querySelector("#installUpdateButton");
      if (!button) return;
      button.disabled = true;
      if (installButton) installButton.disabled = true;
      setText("#updateStateText", "检查中");
      setUpdateMessage("正在连接 GitHub Releases...");
      try {
        const response = await fetch("/api/update/check");
        const payload = await response.json();
        lastUpdateInfo = payload;
        setText("#currentVersionText", payload.current_version || initialRuntime.app_version || "-");
        setText("#latestVersionText", payload.latest_version || "-");
        const releaseLink = document.querySelector("#releaseLink");
        if (releaseLink && payload.release_url) {
          releaseLink.href = payload.release_url;
          releaseLink.hidden = false;
        }
        if (!response.ok || !payload.ok) {
          setText("#updateStateText", "检查失败");
          setUpdateMessage(payload.detail || payload.error || "检查更新失败。");
          return;
        }
        if (payload.update_available) {
          setText("#updateStateText", "有新版本");
          const assetSize = payload.asset?.size ? `${Math.ceil(payload.asset.size / 1024 / 1024)} MB` : "";
          setUpdateMessage(`发现 ${payload.latest_version}，安装包 ${assetSize}。`);
          if (installButton) installButton.disabled = !initialRuntime.app_frozen;
          if (!initialRuntime.app_frozen) {
            setUpdateMessage(`发现 ${payload.latest_version}，但当前是源码模式，请在正式安装版中自动更新。`);
          }
        } else {
          setText("#updateStateText", "已是最新版本");
          setUpdateMessage(payload.error || "当前已经是最新版本。");
        }
      } catch (error) {
        setText("#updateStateText", "检查失败");
        setUpdateMessage(String(error));
      } finally {
        button.disabled = false;
      }
    }

    settingsForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const state = document.querySelector("#settingsSaveState");
      state.textContent = "保存中";
      state.className = "save-state";
      const response = await fetch("/api/settings", { method: "POST", body: new FormData(settingsForm) });
      const payload = await response.json();
      if (!response.ok) {
        state.textContent = payload.detail || "保存失败";
        state.className = "save-state failed";
        return;
      }
      state.textContent = "已保存";
      state.className = "save-state saved";
      settingsForm.querySelector('[name="erp_password"]').value = "";
      settingsForm.querySelector('[name="llm_api_key"]').value = "";
      settingsForm.querySelector('[name="dingtalk_stream_client_secret"]').value = "";
      settingsForm.querySelector('[name="dingtalk_stream_api_token"]').value = "";
      settingsForm.querySelector('[name="update_token"]').value = "";
      await refreshStatus();
    });

    function safe(value, fallback = "-") {
      if (value === null || value === undefined || value === "") return fallback;
      return String(value);
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
        const manual = String(row["需人工复核"] || "").toLowerCase();
        const tr = document.createElement("tr");
        tr.className = "suggestion-row";
        tr.innerHTML = `
          <td>${escapeHtml(row["序号"])}</td>
          <td>${escapeHtml(row["试剂名称"])}</td>
          <td>${escapeHtml(row["CAS号"])}</td>
          <td>${escapeHtml(row["标准化名称"])}</td>
          <td>${escapeHtml(row["查询来源"])}</td>
          <td><span class="category suggestion-category">${escapeHtml(row["最终建议类别"])}</span></td>
          <td>${escapeHtml(row["置信度"])}</td>
          <td><span class="status-badge ${manual === "true" ? "status-warning" : "status-success"}">${escapeHtml(row["需人工复核"])}</span></td>
        `;
        tr.addEventListener("click", () => {
          document.querySelectorAll(".suggestion-row-selected").forEach((item) => item.classList.remove("suggestion-row-selected"));
          tr.classList.add("suggestion-row-selected");
          renderEvidence(row);
        });
        body.appendChild(tr);
      });
      renderEvidence(null);
    }

    function renderEvidence(row) {
      const box = document.querySelector("#evidenceBox");
      const panel = document.querySelector("#evidence");
      if (!box) return;
      if (panel) panel.classList.toggle("evidence-collapsed", !row);
      if (!row) {
        box.innerHTML = "<p>点击审批建议表中的记录，查看该条的规则原因、证据和来源摘要。</p>";
        return;
        box.innerHTML = "<p>暂无建议结果。</p>";
        return;
      }
      box.innerHTML = `
        <dl>
          <dt>试剂</dt><dd>${escapeHtml(row["试剂名称"])}</dd>
          <dt>CAS</dt><dd>${escapeHtml(row["CAS号"])}</dd>
          <dt>标准名称</dt><dd>${escapeHtml(row["标准化名称"])}</dd>
          <dt>查询来源</dt><dd>${escapeHtml(row["查询来源"])}</dd>
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
        ? `共 ${safe(queue.rows, 0)} 条历史记录，当前待处理 ${safe(queue.pending, 0)} 条；最近更新：${safe(queue.modified)}`
        : "尚未生成 data/review_queue.xlsx";
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

    function renderTodoTasks(todoTasks) {
      const holder = document.querySelector("#todoListPicker");
      const meta = document.querySelector("#todoMeta");
      if (!holder || !meta) return;
      const tasks = todoTasks.tasks || [];
      todoState.tasks = tasks;
      meta.textContent = todoTasks.exists
        ? `共 ${safe(todoTasks.rows, tasks.length)} 条；最近刷新：${safe(todoTasks.modified)}`
        : "点击“获取最新清单号”读取 ERP 待审批列表。";
      holder.innerHTML = "";
      if (!tasks.length) {
        holder.innerHTML = "<p>暂无待办清单缓存。</p>";
        return;
      }
      tasks.forEach((task) => {
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
        });
        holder.appendChild(label);
      });
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
      if (type === "llm_fallback") return "LLM辅助";
      if (type === "trusted_web") return "可信网站";
      if (type === "web_fallback") return "网页兜底";
      return "证据不足";
    }

    function reviewAllowsSuggestionPreselect(row) {
      return String(row.allow_suggestion_preselect || "").toLowerCase() === "true";
    }

    function reviewEvidenceHtml(row) {
      const suggestion = row.display_suggestion || row.suggested_category || "暂无可靠建议";
      const reason = row.display_reason || row.review_advice || "需人工核对物化特性。";
      const evidenceStatus = row.evidence_status || reviewEvidenceLabel(row);
      const detail = localizeReviewDetailText(row.detail_summary || row.property_summary || "");
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
      return `
        <div class="review-evidence ${row.evidence_source_type === "llm_fallback" ? "llm-evidence" : ""}">
          <div class="review-evidence-head">
            <span>${escapeHtml(evidenceStatus)}</span>
            ${sourceLink}
          </div>
          <p><strong>建议：</strong>${escapeHtml(suggestion)}</p>
          <p><strong>原因：</strong>${escapeHtml(reason)}</p>
          ${(detail || properties.length) ? `
            <details class="review-evidence-detail">
              <summary>查看详情</summary>
              ${detail ? `<p>${escapeHtml(detail)}</p>` : ""}
              ${properties.length ? `<p>${properties.map((item) => `${escapeHtml(item[0])}：${escapeHtml(localizeReviewDetailText(item[1]))}`).join(" · ")}</p>` : ""}
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
      };
      text = text.replace(/\b(flash_point|boiling_point|toxicity|corrosive|oxidizing|flammable|water_reactive|explosive_risk|evidence)=/g, (_, key) => `${keyLabels[key] || key}：`);
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

    function renderReviewRows() {
      const body = document.querySelector("#reviewTable");
      const visibleCount = document.querySelector("#reviewVisibleCount");
      if (!body || !visibleCount) return;
      const rows = reviewState.rows || [];
      document.querySelector("#reviewSortIcon").textContent = reviewState.sortDirection === "desc" ? "↓" : "↑";
      reviewState.page = Math.min(Math.max(1, reviewState.page || 1), reviewState.pages);
      const pageRows = rows;
      visibleCount.textContent = `显示 ${pageRows.length} / ${reviewState.total} 条`;
      body.innerHTML = "";
      if (!rows.length) {
        body.innerHTML = tableEmptyHtml(6, "暂无人工复核项", "需要人工确认的审批记录会显示在这里。");
        updateReviewPagination();
        return;
      }
      pageRows.forEach((row) => {
        const tr = document.createElement("tr");
        tr.className = "review-row";
        const preselectedCategory = reviewAllowsSuggestionPreselect(row) ? (row.suggested_category || "") : "";
        const options = categoryOptionsHtml(preselectedCategory);
        tr.innerHTML = `
          <td class="review-time-cell">
            <strong>${escapeHtml(row.timestamp || "-")}</strong>
            <span>${escapeHtml(row.list_number || "-")}</span>
          </td>
          <td class="review-name-cell">
            <strong>${escapeHtml(row.reagent_name || "-")}</strong>
            <span>${escapeHtml(row.standard_name || "-")}</span>
          </td>
          <td class="review-cas-status-cell">
            <span>${escapeHtml(row.cas || "-")}</span>
            <span class="status-badge status-warning">${escapeHtml(row.status || "待复核")}</span>
          </td>
          <td class="reason-cell" title="${escapeHtml(row.reason_full || row.reason)}">
            <div>${escapeHtml(row.reason)}</div>
            ${reviewEvidenceHtml(row)}
          </td>
          <td class="review-category-cell">
            <select class="review-category">${options}</select>
            <span class="review-selected">未选择</span>
          </td>
          <td class="review-action-cell">
            <button type="button" class="review-confirm review-primary-action" disabled>确认入库</button>
            <button type="button" class="review-delete review-danger-action">删除</button>
            <span class="review-row-message"></span>
          </td>
        `;
        const categorySelect = tr.querySelector(".review-category");
        const selectedText = tr.querySelector(".review-selected");
        const confirmButton = tr.querySelector(".review-confirm");
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
        confirmButton.addEventListener("click", async () => {
          const finalCategory = categorySelect.value;
          if (!finalCategory) {
            rowMessage.textContent = "请先选择人工确认后的物化特性。";
            rowMessage.className = "review-row-message failed";
            return;
          }
          confirmButton.disabled = true;
          rowMessage.textContent = "正在确认...";
          rowMessage.className = "review-row-message";
          const response = await fetch("/api/review/confirm", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
              review_key: row.review_key,
              list_number: row.list_number,
              sequence: row.sequence,
              reagent_name: row.reagent_name,
              cas: row.cas,
              standard_name: row.standard_name,
              cleaned_name: row.cleaned_name,
              specification: row.specification,
              unit: row.unit,
              final_category: finalCategory,
              reason: row.reason || row.reason_full || ""
            }),
          });
          const payload = await response.json();
          if (!response.ok || !payload.confirmed) {
            rowMessage.textContent = payload.detail || payload.message || "人工复核确认失败。";
            rowMessage.className = "review-row-message failed";
            confirmButton.disabled = false;
            return;
          }
          rowMessage.textContent = payload.message || "已确认入库。";
          rowMessage.className = "review-row-message ok";
          await Promise.all([refreshStatus(), refreshReviewQueue({force: true})]);
        });
        deleteButton.addEventListener("click", async () => {
          if (!confirm(`确认删除人工复核项：${row.reagent_name || row.cas || row.review_key}？`)) return;
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
        body.appendChild(tr);
      });
      updateReviewPagination();
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
      if (table.contains(document.activeElement)) return true;
      if (Array.from(table.querySelectorAll(".review-category")).some((field) => field.value)) return true;
      if (Array.from(table.querySelectorAll(".review-row-message")).some((node) => node.textContent.trim())) return true;
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
    const resetMemoryPage = () => {
      memoryState.page = 1;
    };
    const scheduleMemorySearch = () => {
      window.clearTimeout(memorySearchTimer);
      memorySearchTimer = window.setTimeout(() => {
        resetMemoryPage();
        refreshMemory();
      }, 350);
    };
    on("#refreshMemoryButton", "click", () => refreshMemory());
    on("#memoryPrevPage", "click", () => {
      if (memoryState.page <= 1) return;
      memoryState.page -= 1;
      refreshMemory();
    });
    on("#memoryNextPage", "click", () => {
      if (memoryState.page >= memoryState.pages) return;
      memoryState.page += 1;
      refreshMemory();
    });
    memorySearchInput?.addEventListener("compositionstart", () => {
      memorySearchComposing = true;
    });
    memorySearchInput?.addEventListener("compositionend", () => {
      memorySearchComposing = false;
      scheduleMemorySearch();
    });
    memorySearchInput?.addEventListener("input", () => {
      if (!memorySearchComposing) scheduleMemorySearch();
    });
    memorySearchInput?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        window.clearTimeout(memorySearchTimer);
        resetMemoryPage();
        refreshMemory();
      }
    });
    on("#memoryCategoryFilter", "change", () => {
      resetMemoryPage();
      refreshMemory();
    });
    on("#memoryReusableFilter", "change", () => {
      resetMemoryPage();
      refreshMemory();
    });
    on("#memoryConflictFilter", "change", () => {
      resetMemoryPage();
      refreshMemory();
    });
    on("#saveReusableMemoryButton", "click", () => saveReusableMemoryRows());
    on("#deleteConflictMemoryButton", "click", () => deleteConflictingMemory());
    on("#importMemoryButton", "click", async () => {
      const response = await fetch("/api/memory/import_suggestions", { method: "POST" });
      const payload = await response.json();
      if (!response.ok) {
        alert(payload.detail || "历史审批建议导入失败。");
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
      document.querySelector("#memoryMeta").textContent = `导入完成：${message}`;
      resetMemoryPage();
      await refreshMemory();
    });

    async function refreshMemory() {
      if (!document.querySelector("#memoryTable")) return;
      const requestId = memoryState.requestSeq + 1;
      memoryState.requestSeq = requestId;
      setMemoryLoading(true);
      const params = new URLSearchParams({
        q: document.querySelector("#memorySearch").value || "",
        category: document.querySelector("#memoryCategoryFilter").value || "",
        reusable: document.querySelector("#memoryReusableFilter").value || "",
        conflict: document.querySelector("#memoryConflictFilter").value || "",
        page: String(memoryState.page || 1),
        per_page: String(memoryState.perPage || 10),
        limit: String(memoryState.perPage || 10)
      });
      try {
        const response = await fetch(`/api/memory?${params.toString()}`);
        const payload = await response.json();
        if (requestId !== memoryState.requestSeq) return;
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
        renderMemoryRows(payload);
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

    function renderMemoryRows(payload) {
      const body = document.querySelector("#memoryTable");
      const meta = document.querySelector("#memoryMeta");
      if (!body || !meta) return;
      const rows = memoryState.rows || [];
      meta.textContent = payload.exists
        ? `\u5171 ${safe(payload.rows, 0)} \u6761\uff1b\u7b2c ${safe(payload.page, 1)} / ${safe(payload.pages, 1)} \u9875\uff1b\u6bcf\u9875 ${safe(payload.per_page, 10)} \u6761\uff1b\u6570\u636e\u5e93\uff1a${safe(payload.path)}`
        : "\u5c1a\u672a\u751f\u6210 data/reagent_memory.sqlite";
      renderMemoryPagination(payload);
      body.innerHTML = "";
      if (!rows.length) {
        renderMemoryDetail(null);
        body.innerHTML = tableEmptyHtml(7, "\u6682\u65e0\u8bb0\u5fc6\u5e93\u8bb0\u5f55", "\u5bfc\u5165\u5ba1\u6279\u5efa\u8bae\u540e\uff0c\u53ef\u5728\u8fd9\u91cc\u7ef4\u62a4\u53ef\u590d\u7528\u7684\u5386\u53f2\u5224\u5b9a\u3002");
        return;
      }
      rows.forEach((row) => {
        const tr = document.createElement("tr");
        tr.className = "memory-row";
        tr.dataset.id = row.id;
        const categoryOptions = categoryOptionsHtml(row.final_category);
        tr.innerHTML = `
          <td>${escapeHtml(row.id)}</td>
          <td>
            <input class="memory-cas" value="${escapeHtml(row.cas)}">
            <input type="hidden" class="memory-raw-name" value="${escapeHtml(row.raw_name)}">
            <input type="hidden" class="memory-cleaned-name" value="${escapeHtml(row.cleaned_name)}">
            <input type="hidden" class="memory-standard-name" value="${escapeHtml(row.standard_name)}">
            <textarea class="memory-reason memory-hidden-field">${escapeHtml(row.reason)}</textarea>
          </td>
          <td class="memory-name-cell">
            <strong class="memory-standard-name-view">${escapeHtml(row.standard_name || row.cleaned_name || row.raw_name || "-")}</strong>
            <span class="memory-raw-name-view">${escapeHtml(row.raw_name || "-")}</span>
          </td>
          <td><select class="memory-category">${categoryOptions}</select></td>
          <td><input class="memory-confidence" value="${escapeHtml(row.confidence)}"></td>
          <td class="memory-state-cell">
            <label class="mini-check"><input type="checkbox" class="memory-reusable" ${boolValue(row.reusable) ? "checked" : ""}>\u53ef\u590d\u7528</label>
            <label class="mini-check"><input type="checkbox" class="memory-conflict" ${boolValue(row.conflict) ? "checked" : ""}>\u51b2\u7a81</label>
            <label class="mini-check"><input type="checkbox" class="memory-manual" ${boolValue(row.manual_verified) ? "checked" : ""}>\u4eba\u5de5\u786e\u8ba4</label>
          </td>
          <td class="memory-action-cell">
            <button type="button" class="memory-save memory-save-action">\u4fdd\u5b58</button>
            <button type="button" class="memory-delete memory-delete-action">\u5220\u9664</button>
            <span class="memory-row-status"></span>
          </td>
        `;
        tr.addEventListener("click", (event) => {
          if (event.target.closest("button")) return;
          selectMemoryRow(tr);
        });
        tr.querySelector(".memory-save").addEventListener("click", () => saveMemoryRow(tr));
        tr.querySelector(".memory-delete").addEventListener("click", () => deleteMemoryRow(tr));
        tr.querySelectorAll("input, select, textarea").forEach((field) => {
          field.addEventListener("input", () => {
            markMemoryRowDirty(tr);
            updateMemoryRowPreview(tr);
            if (tr.classList.contains("memory-row-selected")) renderMemoryDetail(memoryRowPayload(tr), tr);
          });
          field.addEventListener("change", () => {
            markMemoryRowDirty(tr);
            updateMemoryRowPreview(tr);
            if (tr.classList.contains("memory-row-selected")) renderMemoryDetail(memoryRowPayload(tr), tr);
          });
          field.addEventListener("keydown", (event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
              event.preventDefault();
              saveMemoryRow(tr);
            }
          });
        });
        const reusableCheck = tr.querySelector(".memory-reusable");
        reusableCheck.addEventListener("change", () => {
          if (!reusableCheck.checked) return;
          tr.querySelector(".memory-conflict").checked = false;
          tr.querySelector(".memory-manual").checked = true;
          updateMemoryRowPreview(tr);
        });
        body.appendChild(tr);
      });
      const firstRow = body.querySelector(".memory-row");
      if (firstRow) selectMemoryRow(firstRow);
    }

    function selectMemoryRow(tr) {
      document.querySelectorAll(".memory-row-selected").forEach((item) => item.classList.remove("memory-row-selected"));
      tr.classList.add("memory-row-selected");
      renderMemoryDetail(memoryRowPayload(tr), tr);
    }

    function updateMemoryRowPreview(tr) {
      const payload = memoryRowPayload(tr);
      const title = tr.querySelector(".memory-standard-name-view");
      const raw = tr.querySelector(".memory-raw-name-view");
      if (title) title.textContent = payload.standard_name || payload.cleaned_name || payload.raw_name || "-";
      if (raw) raw.textContent = payload.raw_name || "-";
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
      const flags = [
        boolValue(row.reusable) ? "\u53ef\u590d\u7528" : "\u4e0d\u53ef\u590d\u7528",
        boolValue(row.conflict) ? "\u51b2\u7a81" : "\u65e0\u51b2\u7a81",
        boolValue(row.manual_verified) ? "\u4eba\u5de5\u786e\u8ba4" : "\u672a\u4eba\u5de5\u786e\u8ba4",
      ];
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
          <label class="memory-detail-reason">\u5907\u6ce8 <textarea data-memory-field="reason">${escapeHtml(row.reason || "")}</textarea></label>
        </div>
        <div class="memory-detail-grid">
          <div><span>\u5efa\u8bae\u7c7b\u522b</span><strong>${escapeHtml(row.final_category || "-")}</strong></div>
          <div><span>\u7f6e\u4fe1\u5ea6</span><strong>${escapeHtml(row.confidence || "-")}</strong></div>
          <div><span>\u72b6\u6001</span><strong>${escapeHtml(flags.join(" / "))}</strong></div>
        </div>
        <div class="memory-detail-actions">
          <button type="button" id="memoryDetailSave" class="memory-save-action">\u4fdd\u5b58\u5f53\u524d\u8bb0\u5f55</button>
          <span class="memory-detail-note">\u4e5f\u53ef\u4ee5\u5728\u8868\u683c\u884c\u5185\u4fdd\u5b58\u3002</span>
        </div>
      `;
      if (!tr) return;
      panel.querySelectorAll("[data-memory-field]").forEach((field) => {
        const name = field.dataset.memoryField;
        const rowField = tr.querySelector(`.memory-${name.replace(/_/g, "-")}`);
        field.addEventListener("input", () => {
          if (rowField) rowField.value = field.value;
          markMemoryRowDirty(tr);
          updateMemoryRowPreview(tr);
        });
      });
      panel.querySelector("#memoryDetailSave")?.addEventListener("click", () => saveMemoryRow(tr));
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
      info.textContent = `第 ${page} / ${pages} 页，共 ${total} 条，每页 ${perPage} 条`;
    }

    function setMemoryLoading(loading) {
      memoryState.loading = loading;
      const prev = document.querySelector("#memoryPrevPage");
      const next = document.querySelector("#memoryNextPage");
      const info = document.querySelector("#memoryPageInfo");
      if (!prev || !next || !info) return;
      prev.disabled = loading || memoryState.page <= 1;
      next.disabled = loading || memoryState.page >= memoryState.pages;
      if (loading) {
        const body = document.querySelector("#memoryTable");
        if (body) body.innerHTML = tableEmptyHtml(7, "正在读取记忆库", "请稍候，正在同步筛选条件和分页数据。");
        info.textContent = `第 ${memoryState.page} / ${memoryState.pages} 页，正在加载...`;
      }
    }

    async function saveMemoryRow(tr) {
      const recordId = tr.dataset.id;
      const payload = memoryRowPayload(tr);
      const button = tr.querySelector(".memory-save");
      const status = tr.querySelector(".memory-row-status");
      button.disabled = true;
      status.textContent = "保存中...";
      status.className = "memory-row-status";
      const response = await fetch(`/api/memory/${recordId}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      const result = await response.json();
      if (!response.ok || !result.updated) {
        button.disabled = false;
        status.textContent = "保存失败";
        status.className = "memory-row-status failed";
        alert(result.detail || "记忆库保存失败。");
        return false;
      }
      status.textContent = "已保存";
      status.className = "memory-row-status saved";
      tr.classList.remove("memory-row-dirty");
      await refreshMemory();
      return true;
    }

    function markMemoryRowDirty(tr) {
      tr.classList.add("memory-row-dirty");
      const status = tr.querySelector(".memory-row-status");
      if (status) {
        status.textContent = "未保存";
        status.className = "memory-row-status unsaved";
      }
      const button = tr.querySelector(".memory-save");
      if (button) button.disabled = false;
    }

    function memoryRowPayload(tr) {
      const reusable = tr.querySelector(".memory-reusable").checked;
      return {
        cas: tr.querySelector(".memory-cas").value,
        raw_name: tr.querySelector(".memory-raw-name").value,
        cleaned_name: tr.querySelector(".memory-cleaned-name").value,
        standard_name: tr.querySelector(".memory-standard-name").value,
        final_category: tr.querySelector(".memory-category").value,
        confidence: tr.querySelector(".memory-confidence").value,
        reusable,
        conflict: reusable ? false : tr.querySelector(".memory-conflict").checked,
        manual_verified: reusable ? true : tr.querySelector(".memory-manual").checked,
        need_manual_review: false,
        reason: tr.querySelector(".memory-reason").value,
        source: "web_ui_manual_edit"
      };
    }

    async function saveReusableMemoryRows() {
      const rows = Array.from(document.querySelectorAll("#memoryTable tr"))
        .filter((tr) => tr.dataset.id && tr.querySelector(".memory-reusable")?.checked);
      if (!rows.length) {
        alert("当前表格中没有勾选“可复用”的记录。");
        return;
      }
      let saved = 0;
      for (const tr of rows) {
        const response = await fetch(`/api/memory/${tr.dataset.id}`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(memoryRowPayload(tr))
        });
        const result = await response.json();
        if (!response.ok || !result.updated) {
          alert(result.detail || `记录 ${tr.dataset.id} 保存失败，已停止批量保存。`);
          await refreshMemory();
          return;
        }
        saved += 1;
      }
      document.querySelector("#memoryMeta").textContent = `已一键保存 ${saved} 条可复用记录。`;
      await refreshMemory();
    }

    async function deleteMemoryRow(tr) {
      const recordId = tr.dataset.id;
      if (!confirm(`确认删除记忆库记录 ${recordId}？`)) return;
      const response = await fetch(`/api/memory/${recordId}`, { method: "DELETE" });
      const result = await response.json();
      if (!response.ok || !result.deleted) {
        alert(result.detail || "记忆库记录删除失败。");
        return;
      }
      await refreshMemory();
    }

    async function deleteConflictingMemory() {
      const message = [
        "确认删除所有标记为“冲突”的试剂记忆记录？",
        "包括已经人工确认过的冲突记录。",
        "删除前会自动备份 SQLite 数据库。"
      ].join("\n");
      if (!confirm(message)) return;
      const response = await fetch("/api/memory/delete_conflicting", { method: "POST" });
      const result = await response.json();
      if (!response.ok) {
        alert(result.detail || "批量删除冲突记录失败。");
        return;
      }
      resetMemoryPage();
      await refreshMemory();
      alert(`已删除 ${result.deleted || 0} 条冲突记录。`);
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
      const lines = logState.lines.filter(logLineMatchesFilter);
      const emptyText = logState.lines.length ? "当前过滤条件下暂无日志。" : "暂无可显示日志。运行任务后会显示最近一次输出。";
      setText("#logBox", lines.join("\n") || emptyText);
    }

    function artifactGroupName(item) {
      const name = String(item?.name || "").toLowerCase();
      if (/\.(xlsx|xls|csv|tsv)$/.test(name)) return "Excel / 数据";
      if (/\.(png|jpg|jpeg|webp)$/.test(name)) return "截图";
      if (/\.(html|htm)$/.test(name)) return "HTML 页面";
      if (/\.(log|txt)$/.test(name)) return "运行日志";
      return "其他";
    }

    function renderArtifacts(items) {
      const holder = document.querySelector("#artifactList");
      const meta = document.querySelector("#artifactMeta");
      if (!holder) return;
      holder.innerHTML = "";
      if (!items || items.length === 0) {
        if (meta) meta.textContent = "暂无可下载产物。运行任务后会生成截图、HTML、Excel 或日志文件。";
        holder.innerHTML = `
          <div class="artifact-empty">
            <strong>暂无产物</strong>
            <span>运行采集或审批建议任务后，下载项会显示在这里。</span>
          </div>
        `;
        return;
      }
      if (meta) meta.textContent = `共 ${items.length} 个可下载产物，按文件类型分组展示。`;
      const groups = new Map();
      items.forEach((item) => {
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
            <span>${escapeHtml(item.modified)} · ${Math.ceil(item.size / 1024)} KB</span>
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
      const response = await fetch("/api/approval_summary");
      const approval = await response.json();
      approvalStateLoaded = true;
      setText("#suggestionRows", safe(approval.rows, "0"));
      setText("#suggestionMeta", approval.exists ? `最近更新：${safe(approval.modified)}` : "尚未生成 approval_suggestions.xlsx");
      renderTags(approval.categories || {});
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
      const response = await fetch(`/api/review_queue?${params.toString()}`);
      const reviewQueue = await response.json();
      reviewStateLoaded = true;
      setText("#manualReview", safe(reviewQueue.pending, "0"));
      if (options.force || !reviewInteractionActive()) {
        renderReviewQueue(reviewQueue);
      }
    }

    async function refreshArtifacts(options = {}) {
      if (artifactStateLoaded && !options.force) return;
      if (!document.querySelector("#artifactList")) return;
      const response = await fetch("/api/artifacts");
      const payload = await response.json();
      artifactStateLoaded = true;
      renderArtifacts(payload.artifacts || []);
    }

    async function refreshLogTail(options = {}) {
      if (logStateLoaded && !options.force) return;
      if (!document.querySelector("#logBox")) return;
      const response = await fetch("/api/log_tail");
      const payload = await response.json();
      logStateLoaded = true;
      logState.lines = payload.log_tail || [];
      renderLogBox();
    }

    async function refreshStatus(options = {}) {
      const response = await fetch("/api/status");
      const data = await response.json();
      const status = data.status || {};
      const runSummary = status.summary || {};
      const runtime = data.runtime || {};
      const todoTasks = data.todo_tasks || {};
      const scheduler = data.scheduler || {};
      const runJustFinished = !status.running && status.finished_at && status.finished_at !== lastFinishedAt;
      if (status.finished_at) lastFinishedAt = status.finished_at;
      if (runtime.review_decision_options && runtime.review_decision_options.length) {
        reviewCategories = runtime.review_decision_options;
      }

      setText("#runBadge", status.running ? "运行中" : (status.success === false ? "失败" : (status.success === true ? "成功" : "空闲")));
      setClass("#runBadge", status.running ? "badge running" : (status.success === false ? "badge failed" : (status.success === true ? "badge succeeded" : "badge")));
      setDisabled("#stopRunButton", !status.running);
      setDisabled("#restartRunButton", false);
      setText("#statusLine", status.running ? "任务执行中，请保持 Playwright 浏览器可见。" : safe(status.error || runSummary.outcome, "当前没有运行中的任务。"));
      setText("#erpUrl", runtime.erp_url_configured ? "已配置" : "未配置");
      setText("#erpUser", runtime.erp_username_configured ? "已配置" : "未配置");
      setText("#erpPassword", runtime.erp_password_configured ? "已配置" : "未配置");
      setText("#apiKey", runtime.llm_api_key_configured ? "已配置" : "未配置");
      setText("#currentAction", safe(status.action_label || status.action));
      setText("#lastRunResult", safe(status.result_label));
      setClass("#lastRunResult", status.success === true ? "result-ok" : (status.success === false ? "result-failed" : ""));
      setText("#startedAt", safe(status.started_at));
      setText("#finishedAt", safe(status.finished_at));
      const summaryTargets = runSummary.target_list_numbers || [];
      setText("#runTargetLists", summaryTargets.length ? summaryTargets.join("，") : "-");
      setText("#runPages", safe(runSummary.processed_pages, "-"));
      setText("#runPageSuggestions", safe(runSummary.page_suggestion_count, "-"));
      setText("#runWritableCandidates", safe(runSummary.writable_candidate_count, "-"));
      setText("#runManualReviewCandidates", safe(runSummary.manual_review_candidate_count, "-"));
      setText("#runSearchFailures", safe(runSummary.search_failure_count, "-"));
      setText("#runWriteSuccess", safe(runSummary.write_success_count, "-"));
      setText("#runWriteFailure", safe(runSummary.write_failure_count, "-"));
      setText("#runDeferredWrites", safe(runSummary.deferred_write_count, "-"));
      setText("#runNotFoundAfterReread", safe(runSummary.not_found_after_reread_count, "-"));
      setText("#runLogPath", safe(status.run_log_path, "-"));
      updateDryRunUi(runtime.app_dry_run);
      setText("#autoPassText", safe(runtime.auto_pass));
      setText("#writeModeText", approvalWriteModeLabel(runtime.approval_write_mode));
      setText("#processScopeText", runtime.process_all_todos === "true" ? "全部待办" : "勾选清单/首条");
      const selectedLists = selectedTodoNumbers();
      setText("#targetList", selectedLists.length ? `${selectedLists.length} 个清单` : "-");
      setText("#llmModel", safe(runtime.llm_model));
      const llmModel = document.querySelector("#llmModel");
      if (llmModel) llmModel.title = safe(runtime.llm_model);
      if (status.running) {
        setText("#suggestionRows", safe(runSummary.suggestion_count, "0"));
        setText("#manualReview", safe(runSummary.manual_review_count, "0"));
      }
      setText("#safetyGate", runtime.auto_pass === "true" ? "启用校验" : "默认关闭");
      setText("#schedulerNextRun", safe(scheduler.next_run_at));
      setText("#schedulerLastRun", safe(scheduler.last_run_at));
      setText("#schedulerLastResult", safe(scheduler.last_result));
      setText("#currentVersionText", safe(runtime.app_version));
      setText("#appModeText", runtime.app_frozen ? "安装版" : "源码模式");

      renderTodoTasks(todoTasks);
      renderWorkflow(status.workflow || {});
      if (activePage === "overview" || activePage === "suggestions" || options.forceApproval) {
        await refreshApprovalSummary({force: options.forceApproval || runJustFinished});
      }
      if (activePage === "overview" || activePage === "review" || options.forceReview) {
        await refreshReviewQueue({force: options.forceReview || runJustFinished});
      }
      if (activePage === "artifacts") {
        await refreshArtifacts({force: runJustFinished});
      }
      if (activePage === "logs") {
        await refreshLogTail({force: status.running || runJustFinished});
      }
    }

    function setUpdateState(state, label) {
      const pill = document.querySelector("#updatePill");
      if (pill) {
        pill.classList.remove("update-available", "update-current", "update-failed");
        if (state) pill.classList.add(state);
      }
      setText("#updateStateText", label);
    }

    async function checkForUpdate() {
      const button = document.querySelector("#checkUpdateButton");
      const installButton = document.querySelector("#installUpdateButton");
      if (!button) return;
      button.disabled = true;
      if (installButton) {
        installButton.disabled = true;
        installButton.hidden = true;
      }
      setUpdateState("", "检查中");
      try {
        const response = await fetch("/api/update/check");
        const payload = await response.json();
        lastUpdateInfo = payload;
        const releaseLink = document.querySelector("#releaseLink");
        if (releaseLink && payload.release_url) {
          releaseLink.href = payload.release_url;
          releaseLink.hidden = false;
        }
        if (!response.ok || !payload.ok) {
          setUpdateState("update-failed", "检查失败");
          setUpdateMessage(payload.detail || payload.error || "检查更新失败。");
          return;
        }
        if (payload.update_available) {
          setUpdateState("update-available", `有新版本 ${payload.latest_version || ""}`);
          if (installButton) {
            installButton.hidden = false;
            installButton.disabled = !initialRuntime.app_frozen;
          }
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
        button.disabled = false;
      }
    }

    refreshStatus();
    refreshMemory();
    setInterval(refreshStatus, 2500);
