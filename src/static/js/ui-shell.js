/**
 * Lab Precision UI Shell Utilities
 * - Dual-theme toggle (meta[name="color-scheme"] + localStorage + storage sync)
 * - Accessible Toast notifications (popover="manual" with fallback)
 * - Native <dialog> confirmation modal
 * - Custom inline SVG empty-state illustrations
 * - Settings 4-tab navigation & dirty-state tracker
 * - Keyboard navigation for Review & Suggestions
 * - Todo selection persistence
 */
(() => {
  const THEME_STORAGE_KEY = "color-scheme";
  const TODO_STORAGE_KEY = "reagent_selected_todos_v1";

  function escapeHtmlText(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  /* ----------------------------------------------------------------------
     1. Dual-Theme Controller (two-state toggle matching modern-web-guidance)
     ---------------------------------------------------------------------- */
  function systemPrefersDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function currentRenderedScheme() {
    const meta = document.querySelector('meta[name="color-scheme"]');
    const content = meta?.getAttribute("content") || "light dark";
    if (content === "dark" || content === "light") return content;
    return systemPrefersDark() ? "dark" : "light";
  }

  function applySchemeMeta(scheme) {
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.setAttribute("content", scheme || "light dark");
  }

  function initThemeToggle() {
    const btn = document.getElementById("themeToggle");
    if (!btn) return;

    btn.addEventListener("click", () => {
      const target = currentRenderedScheme() === "dark" ? "light" : "dark";
      const system = systemPrefersDark() ? "dark" : "light";
      try {
        if (target === system) {
          localStorage.removeItem(THEME_STORAGE_KEY);
          applySchemeMeta("light dark");
        } else {
          localStorage.setItem(THEME_STORAGE_KEY, target);
          applySchemeMeta(target);
        }
      } catch (_) {
        applySchemeMeta(target);
      }
      window.uiToast?.(target === "dark" ? "已切换为深色值守模式" : "已切换为浅色日间模式", {
        tone: "info",
        duration: 2200,
      });
    });

    window.addEventListener("storage", (event) => {
      if (event.key !== THEME_STORAGE_KEY) return;
      const next = event.newValue === "light" || event.newValue === "dark" ? event.newValue : "light dark";
      applySchemeMeta(next);
    });
  }

  /* ----------------------------------------------------------------------
     2. Non-blocking Toast Notifications (popover="manual" + fallback)
     ---------------------------------------------------------------------- */
  const SUPPORTS_POPOVER = Object.hasOwn(HTMLElement.prototype, "popover");

  function announceToast(message) {
    const live = document.getElementById("toastLiveRegion");
    if (live) {
      live.textContent = "";
      window.requestAnimationFrame(() => {
        live.textContent = message;
      });
    }
  }

  window.uiToast = function uiToast(message, options = {}) {
    const region = document.getElementById("toastRegion");
    const text = String(message || "").trim();
    if (!region || !text) return;

    const tone = options.tone || "info";
    const duration = typeof options.duration === "number" ? options.duration : 4200;
    const iconId =
      tone === "success" ? "#icon-check" :
      tone === "warning" ? "#icon-warn" :
      tone === "error" ? "#icon-error" : "#icon-sliders";

    const toast = document.createElement("div");
    toast.className = "toast-item";
    toast.dataset.tone = tone;
    if (SUPPORTS_POPOVER) {
      toast.setAttribute("popover", "manual");
    }

    toast.innerHTML = `
      <svg width="18" height="18" aria-hidden="true"><use href="${iconId}"/></svg>
      <div class="toast-body">${escapeHtmlText(text)}</div>
      <button type="button" class="toast-close" aria-label="关闭通知">
        <svg width="14" height="14" aria-hidden="true"><use href="#icon-close"/></svg>
      </button>
    `;

    region.appendChild(toast);
    if (SUPPORTS_POPOVER && typeof toast.showPopover === "function") {
      try {
        toast.showPopover();
      } catch (_) {}
    }
    announceToast(text);

    // Cap visible stack at 4
    while (region.children.length > 4) {
      region.firstElementChild?.remove();
    }

    let timer = null;
    const dismiss = () => {
      if (timer) clearTimeout(timer);
      if (SUPPORTS_POPOVER && typeof toast.hidePopover === "function") {
        try {
          toast.hidePopover();
        } catch (_) {}
      }
      toast.remove();
    };

    const schedule = () => {
      if (duration > 0) {
        timer = window.setTimeout(dismiss, duration);
      }
    };

    toast.querySelector(".toast-close")?.addEventListener("click", dismiss);
    toast.addEventListener("mouseenter", () => { if (timer) clearTimeout(timer); });
    toast.addEventListener("mouseleave", schedule);
    toast.addEventListener("focusin", () => { if (timer) clearTimeout(timer); });
    toast.addEventListener("focusout", schedule);
    schedule();
  };

  /* ----------------------------------------------------------------------
     3. Native <dialog> Confirmation Modal
     ---------------------------------------------------------------------- */
  let activeConfirmResolve = null;

  window.uiConfirm = function uiConfirm(options = {}) {
    const dialog = document.getElementById("confirmDialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve(window.confirm(options.message || "确认继续？"));
    }

    const titleEl = document.getElementById("confirmDialogTitle");
    const msgEl = document.getElementById("confirmDialogMessage");
    const detailEl = document.getElementById("confirmDialogDetail");
    const cancelBtn = document.getElementById("confirmDialogCancel");
    const acceptBtn = document.getElementById("confirmDialogAccept");
    const iconUse = dialog.querySelector(".confirm-dialog-icon use");

    const danger = Boolean(options.danger);
    dialog.dataset.danger = danger ? "true" : "false";
    if (titleEl) titleEl.textContent = options.title || (danger ? "高危操作确认" : "确认操作");
    if (msgEl) msgEl.textContent = options.message || "";
    if (detailEl) {
      if (options.detail) {
        detailEl.textContent = options.detail;
        detailEl.hidden = false;
      } else {
        detailEl.textContent = "";
        detailEl.hidden = true;
      }
    }
    if (cancelBtn) cancelBtn.textContent = options.cancelText || "取消";
    if (acceptBtn) {
      acceptBtn.textContent = options.confirmText || (danger ? "确认执行" : "确认");
      acceptBtn.className = danger ? "danger-button" : "primary";
    }
    if (iconUse) {
      iconUse.setAttribute("href", danger ? "#icon-error" : "#icon-warn");
    }

    if (danger && cancelBtn) {
      cancelBtn.setAttribute("autofocus", "");
      acceptBtn?.removeAttribute("autofocus");
    } else if (acceptBtn) {
      acceptBtn.setAttribute("autofocus", "");
      cancelBtn?.removeAttribute("autofocus");
    }

    if (dialog.open) dialog.close();
    dialog.returnValue = "";

    return new Promise((resolve) => {
      activeConfirmResolve = resolve;
      const onClose = () => {
        dialog.removeEventListener("close", onClose);
        const confirmed = dialog.returnValue === "confirm";
        dialog.returnValue = "";
        const fn = activeConfirmResolve;
        activeConfirmResolve = null;
        fn?.(confirmed);
      };
      dialog.addEventListener("close", onClose);
      dialog.showModal();
    });
  };

  /* ----------------------------------------------------------------------
     4. Custom Inline SVG Empty-State Illustrations
     ---------------------------------------------------------------------- */
  const EMPTY_ILLUSTRATIONS = {
    "flask-done": `
      <svg class="empty-illustration" viewBox="0 0 84 60" aria-hidden="true">
        <path d="M35 10h14M38 10v11L27 42a5 5 0 0 0 4.3 7.5h21.4A5 5 0 0 0 57 42L46 21V10" stroke="currentColor" stroke-width="1.8"/>
        <path d="m35 37 5 5 10-10" stroke="var(--teal)" stroke-width="2.4"/>
      </svg>`,
    "search-empty": `
      <svg class="empty-illustration" viewBox="0 0 84 60" aria-hidden="true">
        <circle cx="38" cy="28" r="14" stroke="currentColor" stroke-width="1.8"/>
        <path d="M48 38l11 11" stroke="var(--teal)" stroke-width="2.4"/>
        <path d="M32 28h12" stroke="currentColor" stroke-width="1.6" opacity="0.5"/>
      </svg>`,
    "terminal-idle": `
      <svg class="empty-illustration" viewBox="0 0 84 60" aria-hidden="true">
        <rect x="14" y="10" width="56" height="40" rx="5" stroke="currentColor" stroke-width="1.8"/>
        <path d="M24 25l6 5-6 5" stroke="var(--teal)" stroke-width="2"/>
        <path d="M35 35h16" stroke="currentColor" stroke-width="1.8" opacity="0.6"/>
      </svg>`,
    "folder-empty": `
      <svg class="empty-illustration" viewBox="0 0 84 60" aria-hidden="true">
        <path d="M16 18a4 4 0 0 1 4-4h14l5 5h25a4 4 0 0 1 4 4v21a4 4 0 0 1-4 4H20a4 4 0 0 1-4-4V18z" stroke="currentColor" stroke-width="1.8"/>
        <path d="M34 33h16" stroke="var(--teal)" stroke-width="2" opacity="0.7"/>
      </svg>`,
  };

  window.uiEmptyStateHtml = function uiEmptyStateHtml(kind, title, subtitle) {
    const svg = EMPTY_ILLUSTRATIONS[kind] || EMPTY_ILLUSTRATIONS["search-empty"];
    return `
      <div class="table-empty-state">
        ${svg}
        <strong>${escapeHtmlText(title)}</strong>
        <span>${escapeHtmlText(subtitle)}</span>
      </div>
    `;
  };

  /* ----------------------------------------------------------------------
     5. Category Distribution Micro-Bar
     ---------------------------------------------------------------------- */
  const CATEGORY_PALETTE = [
    "#0d9488",
    "#163a70",
    "#0369a1",
    "#b45309",
    "#b91c1c",
    "#64748b",
  ];

  window.uiRenderCategoryBar = function uiRenderCategoryBar(containerId, categories) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const entries = Object.entries(categories || {}).filter(([, count]) => Number(count) > 0);
    const total = entries.reduce((sum, [, count]) => sum + Number(count), 0);
    if (!total) {
      container.innerHTML = "";
      container.hidden = true;
      return;
    }
    container.hidden = false;
    container.replaceChildren();
    const bar = document.createElement("div");
    bar.className = "category-bar";
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", "物化特性分类占比");
    const legend = document.createElement("div");
    legend.className = "category-bar-legend";
    entries.forEach(([name, count], idx) => {
      const pct = ((Number(count) / total) * 100).toFixed(1);
      const color = CATEGORY_PALETTE[idx % CATEGORY_PALETTE.length];
      const seg = document.createElement("div");
      seg.className = "category-bar-seg";
      seg.style.width = `${pct}%`;
      seg.style.backgroundColor = color;
      seg.title = `${name}: ${count} (${pct}%)`;
      bar.appendChild(seg);

      const item = document.createElement("span");
      const dot = document.createElement("i");
      dot.className = "category-bar-dot";
      dot.style.backgroundColor = color;
      const bold = document.createElement("b");
      bold.textContent = String(count);
      item.append(dot, `${name} `, bold, ` (${((Number(count) / total) * 100).toFixed(0)}%)`);
      legend.appendChild(item);
    });
    container.append(bar, legend);
  };

  /* ----------------------------------------------------------------------
     6. Settings 4-Tab Switcher & Dirty-State Tracker
     ---------------------------------------------------------------------- */
  function initSettingsTabs() {
    const tabButtons = Array.from(document.querySelectorAll("[data-settings-tab]"));
    const sections = Array.from(document.querySelectorAll("[data-settings-group]"));
    if (!tabButtons.length || !sections.length) return;

    function activateTab(tabId) {
      tabButtons.forEach((btn) => {
        btn.setAttribute("aria-selected", btn.dataset.settingsTab === tabId ? "true" : "false");
      });
      sections.forEach((sec) => {
        sec.dataset.tabHidden = sec.dataset.settingsGroup === tabId ? "false" : "true";
      });
    }

    tabButtons.forEach((btn) => {
      btn.addEventListener("click", () => activateTab(btn.dataset.settingsTab));
    });

    activateTab(tabButtons[0].dataset.settingsTab);
  }

  /* ----------------------------------------------------------------------
     7. Keyboard Navigation for Review & Suggestions
     ---------------------------------------------------------------------- */
  function initTableKeyboardNavigation() {
    const page = document.body?.dataset?.page;
    if (page !== "review" && page !== "suggestions") return;

    document.addEventListener("keydown", (event) => {
      const tag = event.target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (document.querySelector("dialog[open]")) return;

      if (event.key === "Escape" && page === "suggestions") {
        document.getElementById("closeEvidence")?.click();
        return;
      }

      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      const selector = page === "review" ? "#reviewTable .review-select-record" : "#suggestionTable .evidence-trigger";
      const items = Array.from(document.querySelectorAll(selector));
      if (!items.length) return;

      event.preventDefault();
      const activeRowSelector = page === "review" ? "#reviewTable tr.selected .review-select-record" : "#suggestionTable tr.is-selected .evidence-trigger";
      const current = document.querySelector(activeRowSelector);
      const idx = current ? items.indexOf(current) : -1;
      const nextIdx = event.key === "ArrowDown"
        ? Math.min(items.length - 1, idx + 1)
        : Math.max(0, idx <= 0 ? 0 : idx - 1);
      items[nextIdx]?.click();
      items[nextIdx]?.scrollIntoView({ block: "nearest" });
    });
  }

  /* ----------------------------------------------------------------------
     8. Todo Selection Persistence Across Page Switches
     ---------------------------------------------------------------------- */
  window.uiSaveSelectedTodos = function uiSaveSelectedTodos(selectedSet) {
    try {
      sessionStorage.setItem(TODO_STORAGE_KEY, JSON.stringify(Array.from(selectedSet || [])));
    } catch (_) {}
  };

  window.uiLoadSelectedTodos = function uiLoadSelectedTodos() {
    try {
      const raw = sessionStorage.getItem(TODO_STORAGE_KEY);
      const arr = JSON.parse(raw || "[]");
      return Array.isArray(arr) ? arr.map(String) : [];
    } catch (_) {
      return [];
    }
  };

  document.addEventListener("DOMContentLoaded", () => {
    initThemeToggle();
    initSettingsTabs();
    initTableKeyboardNavigation();
  });
})();
