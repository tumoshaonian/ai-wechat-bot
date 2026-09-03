(function () {
  "use strict";

  const { api, ApiError, AdminEventStream, asPage, unwrap, uuid } = window.AdminConsole;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const NAV = [
    { section: "工作台" },
    { id: "dashboard", label: "运行总览", icon: "⌂", subtitle: "机器人运行态势与关键指标", permission: "dashboard.read" },
    { id: "connections", label: "微信连接", icon: "◎", subtitle: "企业微信机器人配置与切换", permission: "connections.read" },
    { id: "users", label: "微信用户", icon: "♙", subtitle: "终端用户授权与能力策略", permission: "users.read" },
    { section: "Agent 运营" },
    { id: "conversations", label: "对话记录", icon: "◫", subtitle: "会话消息与 Agent 上下文", permission: "conversations.read" },
    { id: "tasks", label: "任务中心", icon: "◇", subtitle: "任务状态、时间线与工具调用", permission: "tasks.read" },
    { id: "configs", label: "Agent 配置", icon: "◈", subtitle: "模型、提示词与工具策略版本", permission: "configs.read" },
    { id: "deliveries", label: "文件交付", icon: "▱", subtitle: "文件产物与微信发送结果", permission: "artifacts.read" },
    { section: "系统治理" },
    { id: "alerts", label: "告警中心", icon: "△", subtitle: "异常告警确认、处置与关联追踪", permission: "alerts.read" },
    { id: "logs", label: "实时日志", icon: "≡", subtitle: "结构化日志与实时事件", permission: "logs.read" },
    { id: "health", label: "系统状态", icon: "◉", subtitle: "后台服务与执行组件健康状态" },
    { id: "maintenance", label: "系统维护", icon: "⊙", subtitle: "节点、服务、备份与数据保留", permission: "runtime.read" },
    { id: "admins", label: "管理员", icon: "♜", subtitle: "后台账号、角色与权限分配", permission: "admins.read" },
    { id: "audit", label: "审计记录", icon: "⌕", subtitle: "管理员和系统关键操作追溯", permission: "audit.read" },
    { id: "settings", label: "系统设置", icon: "⚙", subtitle: "安全运行参数与能力配置", permission: "settings.read" },
  ];

  const STATUS = {
    ACTIVE: ["正常", "success"], ONLINE: ["在线", "success"], READY: ["就绪", "success"], HEALTHY: ["健康", "success"],
    SUCCEEDED: ["成功", "success"], SENT: ["已发送", "success"], AVAILABLE: ["可用", "success"], ALLOWED: ["已授权", "success"],
    RUNNING: ["运行中", "info"], QUEUED: ["排队中", "info"], RECEIVED: ["已接收", "info"], CONNECTING: ["连接中", "info"],
    UPLOADING: ["上传中", "info"], SENDING: ["发送中", "info"], RETRYING: ["重试中", "info"], CANCEL_REQUESTED: ["正在停止", "info"],
    WAITING_CONFIRMATION: ["等待确认", "warning"], PARTIAL_SUCCEEDED: ["部分成功", "warning"], DEGRADED: ["降级", "warning"],
    PENDING: ["待处理", "warning"], DRAFT: ["草稿", "neutral"], VALIDATING: ["验证中", "info"], RECONNECTING: ["重连中", "warning"],
    FAILED: ["失败", "danger"], TIMED_OUT: ["已超时", "danger"], INTERRUPTED: ["异常中断", "danger"], ERROR: ["异常", "danger"],
    CANCELLED: ["已取消", "neutral"], DISABLED: ["已禁用", "danger"], DENIED: ["已禁用", "danger"], BLOCKED: ["已阻止", "danger"],
    OBSERVE: ["观察中", "warning"], SUCCESS: ["成功", "success"], WARNING: ["警告", "warning"], INFO: ["信息", "info"],
    OPEN: ["待处理", "danger"], ACKNOWLEDGED: ["处理中", "warning"], RESOLVED: ["已解决", "success"], ARCHIVED: ["已归档", "neutral"], PUBLISHED: ["已发布", "success"],
    CRITICAL: ["严重", "danger"], UNHEALTHY: ["不健康", "danger"], STOPPED: ["已停止", "neutral"], STARTING: ["启动中", "info"], STOPPING: ["停止中", "warning"],
  };

  const state = {
    user: null,
    route: "dashboard",
    query: new URLSearchParams(),
    renderId: 0,
    controller: null,
    eventStream: new AdminEventStream(api),
    eventUnsubscribe: null,
    refreshTimer: null,
    health: null,
    currentLogs: [],
  };

  function h(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function text(value, fallback = "—") {
    return value === undefined || value === null || value === "" ? fallback : String(value);
  }

  function clipped(value, length = 90) {
    const source = text(value, "");
    return source.length > length ? `${source.slice(0, length)}…` : source;
  }

  function parseJson(value, fallback = {}) {
    if (value && typeof value === "object") return value;
    try { return JSON.parse(value || "{}"); } catch (_) { return fallback; }
  }

  function formatDate(value, withSeconds = false) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      ...(withSeconds ? { second: "2-digit" } : {}), hour12: false,
    }).format(date);
  }

  function formatFullDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
  }

  function formatDuration(ms) {
    if (ms === null || ms === undefined || ms === "") return "—";
    const value = Number(ms);
    if (!Number.isFinite(value) || value < 0) return "—";
    if (value < 1000) return `${Math.round(value)} ms`;
    if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)} 秒`;
    return `${Math.floor(value / 60000)}分 ${Math.round((value % 60000) / 1000)}秒`;
  }

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) return "—";
    const units = ["B", "KiB", "MiB", "GiB"];
    let index = 0, size = value;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return `${size.toFixed(index === 0 ? 0 : size >= 10 ? 1 : 2)} ${units[index]}`;
  }

  function shortId(id) {
    const value = text(id, "");
    return value.length > 13 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value || "—";
  }

  function statusBadge(status) {
    const key = String(status || "UNKNOWN").toUpperCase();
    const [label, kind] = STATUS[key] || [key === "UNKNOWN" ? "未知" : key, "neutral"];
    return `<span class="badge badge-${kind}">${h(label)}</span>`;
  }

  function boolLabel(value) { return value ? '<span class="text-success">是</span>' : '<span class="muted">否</span>'; }

  function roleLabel(user) {
    const roles = user?.roles || [];
    const role = typeof roles[0] === "string" ? roles[0] : roles[0]?.name;
    return ({ SUPER_ADMIN: "超级管理员", super_admin: "超级管理员", OPS_ADMIN: "运维管理员", ops_admin: "运维管理员", BOT_ADMIN: "机器人管理员", bot_admin: "机器人管理员", AUDITOR: "审计员", auditor: "审计员", VIEWER: "只读观察员", viewer: "只读观察员" })[role] || role || "管理员";
  }

  function can(permission) {
    if (!permission) return true;
    const permissions = state.user?.permissions || [];
    return permissions.includes("*") || permissions.includes(permission);
  }

  function pageHeading(title, description, actions = "") {
    return `<div class="page-heading"><div><h2>${h(title)}</h2><p>${h(description)}</p></div><div class="heading-actions">${actions}</div></div>`;
  }

  function loadingPanel(rows = 7) {
    return `<section class="panel"><div class="loading-table">${Array.from({ length: rows }, (_, index) => `<div class="skeleton skeleton-line" style="width:${index % 3 === 0 ? 92 : 68}%"></div>`).join("")}</div></section>`;
  }

  function emptyState(title, description, options = {}) {
    const action = options.action ? `<button class="btn btn-primary btn-sm" data-action="${h(options.action)}">${h(options.actionLabel || "开始操作")}</button>` : "";
    return `<div class="empty-state ${options.error ? "error-state" : ""}"><span class="empty-icon">${options.error ? "!" : "◇"}</span><h3>${h(title)}</h3><p>${h(description)}</p>${action}</div>`;
  }

  function errorState(error, retry = true) {
    const request = error?.requestId ? ` 请求编号：${error.requestId}` : "";
    return emptyState("页面加载失败", `${error?.message || "无法读取数据。"}${request}`, { error: true, action: retry ? "refresh" : "", actionLabel: "重新加载" });
  }

  function pagination(page) {
    const current = Math.max(1, page.page || 1);
    const size = Math.max(1, page.page_size || 20);
    const pages = Math.max(1, Math.ceil((page.total || 0) / size));
    const start = page.total ? (current - 1) * size + 1 : 0;
    const end = Math.min(current * size, page.total || 0);
    return `<div class="pagination"><span>显示 ${start}–${end}，共 ${page.total || 0} 条</span><div class="pagination-controls"><button data-page="${current - 1}" ${current <= 1 ? "disabled" : ""}>‹</button><button class="active" disabled>${current} / ${pages}</button><button data-page="${current + 1}" ${current >= pages ? "disabled" : ""}>›</button></div></div>`;
  }

  function filterBar(options = {}) {
    const statusOptions = (options.statuses || []).map((item) => {
      const [value, label] = Array.isArray(item) ? item : [item, STATUS[item]?.[0] || item];
      return `<option value="${h(value)}" ${state.query.get("status") === value ? "selected" : ""}>${h(label)}</option>`;
    }).join("");
    return `<form class="filter-bar" data-filter-form>
      <label class="search-control"><input class="filter-control" name="q" value="${h(state.query.get("q") || "")}" placeholder="${h(options.placeholder || "搜索…")}" aria-label="搜索"></label>
      ${statusOptions ? `<select class="filter-control" name="status" data-auto-filter aria-label="状态"><option value="">全部状态</option>${statusOptions}</select>` : ""}
      ${options.extra || ""}
      <button class="btn btn-sm" type="submit">筛选</button>
      ${(state.query.get("q") || state.query.get("status") || state.query.get("trace_id")) ? '<button class="btn btn-quiet btn-sm" type="button" data-action="clear-filters">清除</button>' : ""}
      <span class="filter-spacer"></span><span class="filter-summary">${h(options.summary || "")}</span>
    </form>`;
  }

  function parseRoute() {
    const raw = location.hash.replace(/^#\/?/, "") || "dashboard";
    const [name, query = ""] = raw.split("?");
    const valid = NAV.some((item) => item.id === name && can(item.permission)) ? name : (NAV.find((item) => item.id && can(item.permission))?.id || "health");
    return { name: valid, query: new URLSearchParams(query) };
  }

  function navigate(route, query) {
    const params = query instanceof URLSearchParams ? query : new URLSearchParams(query || {});
    const suffix = params.toString() ? `?${params}` : "";
    location.hash = `#/${route}${suffix}`;
  }

  function updateQuery(changes) {
    const next = new URLSearchParams(state.query);
    Object.entries(changes || {}).forEach(([key, value]) => value === "" || value === null || value === undefined ? next.delete(key) : next.set(key, value));
    if (!("page" in (changes || {}))) next.delete("page");
    navigate(state.route, next);
  }

  function queryParams(extra = {}) {
    return {
      page: Number(state.query.get("page") || 1), page_size: 20,
      q: state.query.get("q") || undefined, status: state.query.get("status") || undefined,
      connection_id: state.query.get("connection_id") || undefined, trace_id: state.query.get("trace_id") || undefined,
      ...extra,
    };
  }

  function renderNavigation() {
    $("#primary-nav").innerHTML = NAV.filter((item) => item.section || can(item.permission)).map((item) => item.section
      ? `<div class="nav-section-label">${h(item.section)}</div>`
      : `<button class="nav-link ${item.id === state.route ? "active" : ""}" data-route="${h(item.id)}" title="${h(item.label)}"><span class="nav-icon" aria-hidden="true">${item.icon}</span><span class="nav-label">${h(item.label)}</span>${item.id === "tasks" ? '<span id="nav-task-badge" class="nav-badge" hidden>0</span>' : ""}</button>`).join("");
    const meta = NAV.find((item) => item.id === state.route) || NAV.find((item) => item.id);
    $("#page-title").textContent = meta.label;
    $("#page-subtitle").textContent = meta.subtitle;
    document.title = `${meta.label} · 微信电脑 Agent`;
  }

  function setPage(html) {
    $("#page-content").innerHTML = html;
    $("#page-content").focus({ preventScroll: true });
  }

  function showToast(title, message, kind = "info", duration = 4500) {
    const toast = document.createElement("div");
    toast.className = `toast ${kind}`;
    toast.innerHTML = `<span class="toast-icon">${kind === "success" ? "✓" : kind === "error" ? "!" : kind === "warning" ? "△" : "i"}</span><div><strong>${h(title)}</strong><p>${h(message)}</p></div><button aria-label="关闭通知">×</button>`;
    const close = () => toast.remove();
    $("button", toast).addEventListener("click", close);
    $("#toast-region").append(toast);
    if (duration) setTimeout(close, duration);
  }

  function closeDrawer() {
    $("#drawer").classList.remove("open");
    $("#drawer").setAttribute("aria-hidden", "true");
    $("#drawer-scrim").hidden = true;
  }

  function openDrawer(title, eyebrow, content) {
    $("#drawer-title").textContent = title;
    $("#drawer-eyebrow").textContent = eyebrow || "DETAIL";
    $("#drawer-body").innerHTML = content;
    $("#drawer-scrim").hidden = false;
    requestAnimationFrame(() => {
      $("#drawer").classList.add("open");
      $("#drawer").setAttribute("aria-hidden", "false");
      $("#drawer-close").focus();
    });
  }

  function drawerLoading(title, eyebrow) { openDrawer(title, eyebrow, `<div class="loading-table">${loadingPanel(8)}</div>`); }

  function showModal({ title, eyebrow = "ACTION", body, confirmLabel = "确认", confirmKind = "primary", onConfirm, cancelLabel = "取消" }) {
    const modal = $("#modal");
    $("#modal-title").textContent = title;
    $("#modal-eyebrow").textContent = eyebrow;
    $("#modal-body").innerHTML = body;
    $("#modal-footer").innerHTML = `<button class="btn" type="button" data-modal-cancel>${h(cancelLabel)}</button><button class="btn btn-${h(confirmKind)}" type="button" data-modal-confirm>${h(confirmLabel)}</button>`;
    const cancel = () => modal.close();
    $("[data-modal-cancel]", modal).addEventListener("click", cancel, { once: true });
    $("[data-modal-confirm]", modal).addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      const original = button.textContent;
      button.textContent = "处理中…";
      try {
        const close = await onConfirm?.(modal);
        if (close !== false) modal.close();
      } catch (error) {
        showToast("操作失败", error.message, "error");
      } finally {
        button.disabled = false;
        button.textContent = original;
      }
    });
    modal.showModal();
    setTimeout(() => $("input,select,textarea", modal)?.focus(), 30);
  }

  function confirmAction(title, message, confirmLabel, onConfirm, danger = false) {
    showModal({
      title, eyebrow: danger ? "RISK CONFIRMATION" : "CONFIRMATION",
      body: `<p style="margin:0;color:var(--text-soft);line-height:1.7">${h(message)}</p>`,
      confirmLabel, confirmKind: danger ? "danger" : "primary", onConfirm,
    });
  }

  function authScreen(mode, error = "") {
    clearTimeout(state.refreshTimer); state.refreshTimer = null;
    state.controller?.abort();
    $("#boot-screen").hidden = true;
    $("#app-shell").hidden = true;
    $("#auth-screen").hidden = false;
    $("#setup-view").hidden = mode !== "setup";
    $("#login-view").hidden = mode === "setup";
    const alert = mode === "setup" ? $("#setup-error") : $("#login-error");
    alert.textContent = error;
    alert.hidden = !error;
    setTimeout(() => $(mode === "setup" ? "#setup-form input" : "#login-username")?.focus(), 50);
  }

  function showApplication(user) {
    state.user = user || {};
    $("#boot-screen").hidden = true;
    $("#auth-screen").hidden = true;
    $("#app-shell").hidden = false;
    const name = user?.display_name || user?.username || "管理员";
    $("#user-display-name").textContent = name;
    $("#user-role").textContent = roleLabel(user);
    $("#user-avatar").textContent = name.slice(0, 1);
    state.eventUnsubscribe?.();
    state.eventUnsubscribe = state.eventStream.subscribe(handleStreamSignal);
    state.eventStream.connect();
    routeChanged();
  }

  async function boot() {
    applyTheme(localStorage.getItem("wecom.admin.theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
    bindGlobalEvents();
    try {
      const setup = unwrap(await api.get("/setup/status", null, { authEvent: false, timeout: 8000 }));
      if (setup?.setup_required ?? setup?.required ?? false) return authScreen("setup");
      try {
        const me = unwrap(await api.get("/auth/me", null, { authEvent: false, timeout: 8000 }));
        api.setAuth(me || {});
        if (!api.csrfToken && !api.accessToken) return authScreen("login", "为了恢复安全操作权限，请重新验证管理员密码。");
        return showApplication(me?.user || me);
      } catch (error) {
        if (error.status !== 401) return authScreen("login", error.message);
        return authScreen("login");
      }
    } catch (error) {
      authScreen("login", `${error.message}。请确认管理服务已经启动。`);
    }
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("wecom.admin.theme", theme);
    $("meta[name='theme-color']")?.setAttribute("content", theme === "dark" ? "#0d1118" : "#f5f6f8");
  }

  function bindGlobalEvents() {
    window.addEventListener("hashchange", routeChanged);
    window.addEventListener("admin:unauthorized", () => {
      if (!$("#app-shell").hidden) showToast("登录已过期", "请重新登录后继续操作。", "warning");
      state.eventStream.close();
      authScreen("login");
    });
    document.addEventListener("click", handleClick);
    document.addEventListener("submit", handleSubmit);
    document.addEventListener("change", (event) => {
      if (event.target.matches("[data-auto-filter]")) {
        const form = event.target.closest("form");
        const data = Object.fromEntries(new FormData(form));
        updateQuery(data);
      }
    });
    $("#drawer-close").addEventListener("click", closeDrawer);
    $("#drawer-scrim").addEventListener("click", closeDrawer);
    $("[data-modal-close]").addEventListener("click", () => $("#modal").close());
    $("#sidebar-close").addEventListener("click", closeMobileMenu);
    $("#mobile-scrim").addEventListener("click", closeMobileMenu);
    $("#menu-toggle").addEventListener("click", () => {
      $("#sidebar").classList.add("open"); $("#mobile-scrim").hidden = false;
    });
    $("#collapse-sidebar").addEventListener("click", () => {
      $("#app-shell").classList.toggle("sidebar-collapsed");
      localStorage.setItem("wecom.admin.sidebar", $("#app-shell").classList.contains("sidebar-collapsed") ? "collapsed" : "expanded");
    });
    if (localStorage.getItem("wecom.admin.sidebar") === "collapsed") $("#app-shell").classList.add("sidebar-collapsed");
  }

  function closeMobileMenu() { $("#sidebar").classList.remove("open"); $("#mobile-scrim").hidden = true; }

  async function handleSubmit(event) {
    if (event.target.id === "login-form") {
      event.preventDefault();
      const form = event.target, button = $("button[type=submit]", form), data = Object.fromEntries(new FormData(form));
      if (!data.username || !data.password) return setFormError("login-error", "请输入账号和密码。");
      button.disabled = true; button.firstElementChild.textContent = "正在登录…";
      try {
        const result = unwrap(await api.post("/auth/login", { username: data.username.trim(), password: data.password, mode: "cookie" }, { authEvent: false, idempotent: false }));
        api.setAuth(result || {});
        showApplication(result?.user || result);
      } catch (error) { setFormError("login-error", error.message); }
      finally { button.disabled = false; button.firstElementChild.textContent = "登录控制台"; }
      return;
    }
    if (event.target.id === "setup-form") {
      event.preventDefault();
      const form = event.target, button = $("button[type=submit]", form), data = Object.fromEntries(new FormData(form));
      if (!data.display_name || !data.username || !data.password) return setFormError("setup-error", "请完整填写所有字段。");
      if (data.username.trim().length < 3) return setFormError("setup-error", "管理员账号至少需要 3 个字符。");
      if (data.password.length < 12) return setFormError("setup-error", "密码至少需要 12 个字符。");
      if (data.password !== data.password_confirm) return setFormError("setup-error", "两次输入的密码不一致。");
      button.disabled = true; button.firstElementChild.textContent = "正在初始化…";
      try {
        await api.post("/setup", { username: data.username.trim(), password: data.password, display_name: data.display_name.trim() }, { authEvent: false, idempotent: false });
        const result = unwrap(await api.post("/auth/login", { username: data.username.trim(), password: data.password, mode: "cookie" }, { authEvent: false, idempotent: false }));
        api.setAuth(result || {}); showApplication(result?.user || result);
      } catch (error) { setFormError("setup-error", error.message); }
      finally { button.disabled = false; button.firstElementChild.textContent = "创建并进入控制台"; }
      return;
    }
    if (event.target.matches("[data-filter-form]")) {
      event.preventDefault();
      updateQuery(Object.fromEntries(new FormData(event.target)));
    }
  }

  function setFormError(id, message) { const element = $(`#${id}`); element.textContent = message; element.hidden = false; }

  async function handleClick(event) {
    const routeButton = event.target.closest("[data-route]");
    if (routeButton) { navigate(routeButton.dataset.route); closeMobileMenu(); $("#user-menu").hidden = true; return; }
    const pageButton = event.target.closest("[data-page]");
    if (pageButton && !pageButton.disabled) { updateQuery({ page: pageButton.dataset.page }); return; }
    const passwordToggle = event.target.closest("[data-toggle-password]");
    if (passwordToggle) {
      const input = $(`#${passwordToggle.dataset.togglePassword}`); input.type = input.type === "password" ? "text" : "password"; passwordToggle.textContent = input.type === "password" ? "显示" : "隐藏"; return;
    }
    if (event.target.closest("#refresh-page")) { await renderCurrentRoute(); return; }
    if (event.target.closest("#theme-toggle")) { applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"); return; }
    if (event.target.closest("#user-menu-button")) {
      const menu = $("#user-menu"); menu.hidden = !menu.hidden; $("#user-menu-button").setAttribute("aria-expanded", String(!menu.hidden)); return;
    }
    if (event.target.closest("#logout-button")) {
      try { await api.post("/auth/logout", {}, { idempotent: false }); } catch (_) { /* Local logout still applies. */ }
      api.clearAuth(); state.eventStream.close(); authScreen("login"); return;
    }
    if (event.target.closest("#alerts-button")) { navigate(can("alerts.read") ? "alerts" : "tasks", { status: can("alerts.read") ? "OPEN" : "FAILED" }); return; }
    const action = event.target.closest("[data-action]");
    if (action) await performAction(action.dataset.action, action.dataset);
  }

  function routeChanged() {
    if ($("#app-shell").hidden) return;
    const parsed = parseRoute(); state.route = parsed.name; state.query = parsed.query;
    renderNavigation(); closeDrawer(); closeMobileMenu(); renderCurrentRoute();
  }

  async function renderCurrentRoute() {
    const id = ++state.renderId;
    state.controller?.abort(); state.controller = new AbortController();
    const renderers = { dashboard: renderDashboard, connections: renderConnections, users: renderUsers, conversations: renderConversations, tasks: renderTasks, configs: renderConfigs, deliveries: renderDeliveries, alerts: renderAlerts, logs: renderLogs, health: renderHealth, maintenance: renderMaintenance, admins: renderAdmins, audit: renderAudit, settings: renderSettings };
    try { await renderers[state.route](id, state.controller.signal); }
    catch (error) { if (error?.code !== "REQUEST_TIMEOUT" && state.renderId === id) setPage(pageHeading("无法加载页面", "后台返回了错误") + `<section class="panel">${errorState(error)}</section>`); }
  }

  function stillRendering(id) { return state.renderId === id; }

  function handleStreamSignal(type, detail) {
    const indicator = $("#live-state");
    if (type === "connected") { indicator.className = "live-state connected"; $(".live-label", indicator).textContent = "实时连接"; return; }
    if (type === "disconnected") { indicator.className = "live-state offline"; $(".live-label", indicator).textContent = "正在重连"; return; }
    if (type === "unsupported") { indicator.className = "live-state"; $(".live-label", indicator).textContent = "定时刷新"; return; }
    if (type !== "event") return;
    if (String(detail?.event_type || "").startsWith("log.") && state.route === "logs") appendLiveLog(detail);
    if (["dashboard", "tasks", "health", "maintenance", "alerts", "connections", "users", "conversations", "deliveries"].includes(state.route) && !state.refreshTimer) {
      state.refreshTimer = setTimeout(() => { state.refreshTimer = null; renderCurrentRoute(); }, 900);
    }
  }

  function performAction(action, data) {
    const actions = {
      refresh: () => renderCurrentRoute(),
      "clear-filters": () => navigate(state.route, state.route === "deliveries" && state.query.get("tab") ? { tab: state.query.get("tab") } : {}),
      "new-connection": () => connectionForm(),
      "edit-connection": () => connectionForm(data.id),
      "test-connection": () => testConnection(data.id),
      "activate-connection": () => activateConnection(data.id, data.name),
      "delete-connection": () => deleteConnection(data.id, data.name),
      "view-user": () => userDetail(data.id),
      "edit-user": () => userForm(data.id),
      "view-conversation": () => conversationDetail(data.id),
      "view-task": () => taskDetail(data.id),
      "cancel-task": () => cancelTask(data.id),
      "view-delivery": () => deliveryDetail(data.id, data.kind),
      "retry-delivery": () => retryDelivery(data.id),
      "view-audit": () => auditDetail(data.id),
      "copy": async () => { await navigator.clipboard.writeText(data.value || ""); showToast("已复制", "内容已复制到剪贴板。", "success"); },
      "log-pause": () => toggleLogPause(),
      "log-clear": () => { state.currentLogs = []; renderLogLines(); },
      "save-settings": () => saveSettings(),
      "scroll-setting": () => document.getElementById(data.target)?.scrollIntoView({ behavior: "smooth" }),
      "end-conversation": () => endConversation(data.id),
      "new-config-profile": () => configProfileForm(),
      "view-config-profile": () => configProfileDetail(data.id),
      "edit-config-profile": () => configProfileForm(data.id),
      "new-config-revision": () => configRevisionForm(data.id),
      "publish-config": () => publishConfig(data.profileId, data.revisionId, data.version),
      "rollback-config": () => rollbackConfig(data.profileId, data.revisionId, data.version),
      "view-alert": () => alertDetail(data.id),
      "ack-alert": () => alertAction(data.id, "acknowledge"),
      "resolve-alert": () => alertAction(data.id, "resolve"),
      "service-control": () => controlService(data.service, data.operation),
      "system-backup": () => backupSystem(),
      "system-retention": () => retentionForm(),
      "new-admin": () => adminForm(),
      "edit-admin-roles": () => adminRoleForm(data.id),
    };
    return actions[action]?.();
  }

  window.addEventListener("DOMContentLoaded", boot);

  // View implementations ---------------------------------------------------

  async function renderDashboard(id, signal) {
    setPage(pageHeading("运行总览", "今天的消息、任务和执行组件状态", `<span class="last-updated">正在刷新…</span>`) + `<div class="metric-grid">${Array.from({ length: 4 }, () => '<div class="metric-card"><div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line" style="height:28px;width:45%"></div></div>').join("")}</div><div class="grid-2">${loadingPanel(6)}${loadingPanel(6)}</div>`);
    const [summaryResult, healthResult, alertsResult] = await Promise.allSettled([
      api.get("/dashboard/summary", null, { signal }), api.get("/health", null, { signal }),
      can("alerts.read") ? api.get("/alerts", { page: 1, page_size: 1, status: "OPEN" }, { signal }) : Promise.resolve({ items: [], page: 1, page_size: 1, total: 0 }),
    ]);
    if (summaryResult.status === "rejected") throw summaryResult.reason;
    if (!stillRendering(id)) return;
    const summary = unwrap(summaryResult.value) || {};
    const health = healthResult.status === "fulfilled" ? unwrap(healthResult.value) : null;
    state.health = health;
    const today = summary.today || {};
    const counts = today.by_status || {};
    const succeeded = Number(counts.SUCCEEDED || 0);
    const failed = Number(counts.FAILED || 0) + Number(counts.TIMED_OUT || 0) + Number(counts.INTERRUPTED || 0);
    const running = Number(summary.running_tasks ?? counts.RUNNING ?? 0);
    const totalTasks = Number(today.tasks || Object.values(counts).reduce((sum, value) => sum + Number(value || 0), 0));
    const successRate = totalTasks ? `${Math.round(succeeded / totalTasks * 100)}% 成功率` : "今日暂无任务";
    const active = summary.active_connection;
    const failures = summary.recent_failures || [];
    const components = normalizeHealth(health);
    const overallHealthy = health && !components.some((item) => ["FAILED", "ERROR", "OFFLINE", "UNHEALTHY"].includes(String(item.status).toUpperCase()));
    const distribution = Object.entries(counts).filter(([, value]) => Number(value) > 0);
    const maxCount = Math.max(1, ...distribution.map(([, value]) => Number(value)));
    const distributionHtml = distribution.length ? distribution.map(([name, value]) => `<div style="display:grid;grid-template-columns:110px 1fr 34px;align-items:center;gap:10px;margin:12px 0"><span style="font-size:10px;color:var(--text-soft)">${statusBadge(name)}</span><span style="height:7px;border-radius:99px;background:var(--surface-soft);overflow:hidden"><span style="display:block;height:100%;width:${Math.max(4, Number(value) / maxCount * 100)}%;border-radius:99px;background:var(--accent)"></span></span><strong style="text-align:right;font-size:11px">${Number(value)}</strong></div>`).join("") : emptyState("暂无任务数据", "今天还没有收到需要 Agent 处理的任务。");

    setPage(`${pageHeading("运行总览", "今天的消息、任务和执行组件状态", `<span class="last-updated">更新于 ${h(formatFullDate(summary.generated_at || new Date()))}</span>`)}
      <div class="metric-grid">
        ${metricCard("今日消息", today.messages || 0, "微信入站与机器人出站", "消")}
        ${metricCard("今日任务", totalTasks, successRate, "任", "success")}
        ${metricCard("正在执行", running, running ? "包含排队和等待确认" : "当前没有积压", "执", "warning")}
        ${metricCard("异常任务", failed, failed ? "需要检查最近失败" : "今日执行正常", "异", failed ? "danger" : "success")}
      </div>
      <div class="grid-2">
        <section class="panel">
          <div class="panel-header"><div><h3>今日任务分布</h3><p>按最终状态统计，不以文本回复替代执行结果</p></div><div class="chart-legend"><span>任务数量</span></div></div>
          <div class="panel-body">${distributionHtml}</div>
        </section>
        <section class="panel">
          <div class="panel-header"><div><h3>当前微信连接</h3><p>当前主执行槽连接状态</p></div><button class="panel-link" data-route="connections">管理连接 →</button></div>
          ${active ? `<div class="status-summary"><span class="status-ring">企微</span><div class="status-copy"><strong>${h(active.name)}</strong><small>${h(shortId(active.id))} · 更新于 ${h(formatDate(active.updated_at))}</small></div>${statusBadge(active.status)}</div>
            <div class="status-list"><div class="status-item"><span class="status-dot ${active.is_active ? "status-dot-ok" : ""}"></span><strong>主连接</strong><small>企业微信智能机器人</small><span>${active.is_active ? "已启用" : "未启用"}</span></div></div>`
            : emptyState("尚未配置微信连接", "创建连接并完成凭据验证后，机器人才能接收消息。", can("connections.write") ? { action: "new-connection", actionLabel: "创建连接" } : {})}
        </section>
      </div>
      <div class="grid-2">
        <section class="panel">
          <div class="panel-header"><div><h3>最近失败任务</h3><p>优先处理最新的失败、超时和异常中断</p></div><button class="panel-link" data-route="tasks">查看全部 →</button></div>
          <div class="panel-body flush">${failures.length ? recentFailuresTable(failures) : emptyState("暂时没有失败任务", "系统未记录到需要处理的异常任务。")}</div>
        </section>
        <section class="panel">
          <div class="panel-header"><div><h3>组件健康</h3><p>${health ? "深度健康检查的最新结果" : "健康接口暂时不可用"}</p></div><button class="panel-link" data-route="health">系统状态 →</button></div>
          ${health ? `<div class="status-summary"><span class="status-ring ${overallHealthy ? "" : "text-danger"}">${overallHealthy ? "正常" : "异常"}</span><div class="status-copy"><strong>${overallHealthy ? "核心组件运行正常" : "部分组件需要检查"}</strong><small>${components.length} 个检查项</small></div>${statusBadge(overallHealthy ? "HEALTHY" : "DEGRADED")}</div><div class="status-list">${components.slice(0, 4).map(healthRow).join("")}</div>` : emptyState("健康检查不可用", healthResult.reason?.message || "无法读取系统健康信息。", { error: true })}
        </section>
      </div>`);
    const openAlerts = alertsResult.status === "fulfilled" ? asPage(alertsResult.value).total : null;
    updateAlertBadges(failed, running, openAlerts);
    updateSidebarHealth(health, components);
  }

  function metricCard(label, value, meta, icon, kind = "") {
    return `<article class="metric-card ${kind ? `metric-${kind}` : ""}"><div class="metric-label"><span>${h(label)}</span><span class="metric-icon">${h(icon)}</span></div><div class="metric-value">${h(value)}</div><div class="metric-meta">${h(meta)}</div></article>`;
  }

  function recentFailuresTable(items) {
    return `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>请求</th><th>状态</th><th>错误</th><th>时间</th></tr></thead><tbody>${items.map((item) => `<tr><td><button class="table-link wrap-cell" data-action="view-task" data-id="${h(item.id)}">${h(clipped(item.request_summary, 42) || "未命名任务")}</button><div class="mono muted">${h(shortId(item.trace_id))}</div></td><td>${statusBadge(item.status || "FAILED")}</td><td class="text-danger">${h(item.error_code || clipped(item.error_message, 35) || "UNKNOWN")}</td><td>${h(formatDate(item.updated_at))}</td></tr>`).join("")}</tbody></table></div>`;
  }

  function normalizeHealth(health) {
    if (!health) return [];
    const source = health.components || health.services || health.checks;
    if (Array.isArray(source)) return source.map((item) => ({ name: item.name || item.service || item.component || "组件", status: item.status || (item.ok ? "HEALTHY" : "FAILED"), detail: item.detail || item.message || item.version || "" }));
    if (source && typeof source === "object") return Object.entries(source).map(([name, value]) => typeof value === "object" ? ({ name, status: value.status || (value.ok === false ? "FAILED" : "HEALTHY"), detail: value.detail || value.message || value.version || "" }) : ({ name, status: value === true ? "HEALTHY" : value === false ? "FAILED" : String(value), detail: "" }));
    const apiStatus = String(health.status || (health.ok === false ? "FAILED" : "HEALTHY")).toUpperCase();
    const rows = [{ name: "Admin API", status: apiStatus === "OK" ? "HEALTHY" : apiStatus, detail: health.version || health.message || "管理接口可访问" }];
    if (health.database !== undefined) rows.push({ name: "管理数据库", status: String(health.database).toUpperCase() === "OK" ? "HEALTHY" : health.database, detail: "持久化与 WAL 状态检查" });
    return rows;
  }

  function healthRow(item) {
    const key = String(item.status || "UNKNOWN").toUpperCase();
    const className = ["HEALTHY", "ONLINE", "RUNNING", "OK"].includes(key) ? "status-dot-ok" : ["DEGRADED", "WARNING"].includes(key) ? "" : "";
    return `<div class="status-item"><span class="status-dot ${className}"></span><strong>${h(item.name)}</strong><small>${h(item.detail || "未提供详情")}</small>${statusBadge(key === "OK" ? "HEALTHY" : key)}</div>`;
  }

  function updateAlertBadges(failed = 0, running = 0, openAlerts = null) {
    const count = openAlerts === null ? failed : openAlerts;
    const badge = $("#alerts-badge"); badge.textContent = count; badge.hidden = !count;
    const navBadge = $("#nav-task-badge"); if (navBadge) { navBadge.textContent = running; navBadge.hidden = !running; }
  }

  function updateSidebarHealth(health, components = normalizeHealth(health)) {
    const panel = $("#sidebar-health"), orb = $(".health-orb", panel), title = $("strong", panel), detail = $("small", panel);
    const failed = !health || components.some((item) => ["FAILED", "ERROR", "OFFLINE", "UNHEALTHY"].includes(String(item.status).toUpperCase()));
    const degraded = components.some((item) => ["DEGRADED", "WARNING"].includes(String(item.status).toUpperCase()));
    orb.className = `health-orb ${!health ? "offline" : failed ? "offline" : degraded ? "warning" : "online"}`;
    title.textContent = !health ? "后台状态未知" : failed ? "服务存在异常" : degraded ? "服务降级运行" : "核心服务正常";
    detail.textContent = !health ? "健康检查不可用" : `${components.length} 个检查项`;
  }

  async function renderConnections(id, signal) {
    const create = can("connections.write") ? '<button class="btn btn-primary" data-action="new-connection">＋ 新建连接</button>' : "";
    setPage(pageHeading("微信连接", "保存多套企业微信机器人配置，并安全选择当前主连接", create) + filterBar({ placeholder: "搜索连接名称、Bot ID…", statuses: ["ONLINE", "READY", "DRAFT", "FAILED", "DISABLED"] }) + loadingPanel());
    const page = asPage(await api.get("/connections", queryParams(), { signal }));
    if (!stillRendering(id)) return;
    setPage(`${pageHeading("微信连接", "保存多套企业微信机器人配置，并安全选择当前主连接", create)}
      ${filterBar({ placeholder: "搜索连接名称、Bot ID…", statuses: ["ONLINE", "READY", "DRAFT", "FAILED", "DISABLED"], summary: `共 ${page.total} 套配置` })}
      ${page.items.length ? `<div class="connection-card-grid">${page.items.map(connectionCard).join("")}</div><div class="mt-16 panel">${pagination(page)}</div>` : `<section class="panel">${emptyState("还没有微信连接", "创建第一套企业微信智能机器人配置。", can("connections.write") ? { action: "new-connection", actionLabel: "新建连接" } : {})}</section>`}`);
  }

  function connectionCard(item) {
    return `<article class="connection-card ${item.is_active ? "primary" : ""}">
      <header><div class="connection-ident"><span class="connection-logo">微</span><div><h3>${h(item.name)}</h3><p>${h(item.environment || "local")} · ${h(item.channel_type || "WECOM_AIBOT")}</p></div></div>${statusBadge(item.status)}</header>
      <div class="connection-meta">
        <div><span>Bot ID</span><strong class="mono">${h(item.bot_id ? clipped(item.bot_id, 25) : "未配置")}</strong></div>
        <div><span>Secret</span><strong>${item.secret_configured ? "已安全配置" : "尚未配置"}</strong></div>
        <div><span>配置版本</span><strong>v${h(item.version || 1)}</strong></div>
        <div><span>更新时间</span><strong>${h(formatDate(item.updated_at))}</strong></div>
      </div>
      <div class="connection-actions">${can("connections.write") ? `<button class="btn btn-sm" data-action="test-connection" data-id="${h(item.id)}">测试</button><button class="btn btn-sm" data-action="edit-connection" data-id="${h(item.id)}">编辑</button>` : ""}${item.is_active ? '<span class="badge badge-success">当前主连接</span>' : can("connections.write") ? `<button class="btn btn-primary btn-sm" data-action="activate-connection" data-id="${h(item.id)}" data-name="${h(item.name)}">设为主连接</button>` : ""}<span class="filter-spacer"></span>${can("connections.write") && !item.is_active ? `<button class="row-menu-btn danger-text" title="删除" data-action="delete-connection" data-id="${h(item.id)}" data-name="${h(item.name)}">×</button>` : ""}</div>
    </article>`;
  }

  async function connectionForm(id) {
    let item = null;
    if (id) {
      try { item = unwrap(await api.get(`/connections/${encodeURIComponent(id)}`)); }
      catch (error) { return showToast("读取失败", error.message, "error"); }
    }
    showModal({
      title: id ? "编辑微信连接" : "新建微信连接", eyebrow: "WECOM CONNECTION", confirmLabel: id ? "保存修改" : "创建连接",
      body: `<form id="connection-form" class="form-stack" novalidate>
        <label class="field"><span>连接名称</span><input name="name" required maxlength="100" value="${h(item?.name || "")}" placeholder="例如：小屠魔"><small class="field-error"></small></label>
        <label class="field"><span>Bot ID</span><input name="bot_id" maxlength="200" value="${h(item?.bot_id || "")}" placeholder="企业微信智能机器人的 Bot ID"><small class="field-error"></small></label>
        <label class="field"><span>${id ? "轮换 Secret（留空则不修改）" : "Secret"}</span><input name="secret" type="password" autocomplete="new-password" maxlength="500" placeholder="${id ? "留空保留现有凭据" : "只会加密保存，不会再次回显"}"><small class="field-error"></small></label>
        <label class="field"><span>运行环境</span><select name="environment"><option value="local" ${(item?.environment || "local") === "local" ? "selected" : ""}>本机</option><option value="test" ${item?.environment === "test" ? "selected" : ""}>测试</option><option value="production" ${item?.environment === "production" ? "selected" : ""}>生产</option></select></label>
        <label class="field"><span>备注</span><textarea name="notes" maxlength="500" placeholder="用途、负责人或切换注意事项">${h(item?.notes || "")}</textarea></label>
        <div class="form-alert" data-form-error hidden></div>
      </form>`,
      onConfirm: async (modal) => {
        const form = $("#connection-form", modal), values = Object.fromEntries(new FormData(form));
        if (!values.name.trim()) return showInlineFormError(form, "连接名称不能为空。");
        if (!id && (!values.bot_id.trim() || !values.secret)) return showInlineFormError(form, "首次创建需要填写 Bot ID 和 Secret。");
        const body = { name: values.name.trim(), bot_id: values.bot_id.trim() || null, environment: values.environment, notes: values.notes.trim() };
        if (values.secret) body.secret = values.secret;
        if (id) {
          body.version = item.version;
          await api.patch(`/connections/${encodeURIComponent(id)}`, body, { headers: { "If-Match": String(item.version || 1) } });
          showToast("连接已更新", "修改已保存，Secret 不会在页面中回显。", "success");
        } else {
          await api.post("/connections", body);
          showToast("连接已创建", "请先执行连接测试，再设为主连接。", "success");
        }
        renderCurrentRoute();
      },
    });
  }

  function showInlineFormError(form, message) {
    const alert = $("[data-form-error]", form); alert.textContent = message; alert.hidden = false; return false;
  }

  async function testConnection(id) {
    const button = $(`[data-action="test-connection"][data-id="${CSS.escape(id)}"]`); if (button) { button.disabled = true; button.textContent = "测试中…"; }
    try {
      const result = unwrap(await api.post(`/connections/${encodeURIComponent(id)}/test`, {}));
      const stages = result?.stages || {};
      showModal({ title: result?.ok ? "真实认证通过" : "连接验证未通过", eyebrow: h(result?.mode || "CONNECTION TEST"), body: `<div class="detail-grid">${Object.entries(stages).map(([name, value]) => { const display = value && typeof value === "object" ? `${value.status || "UNKNOWN"}${value.code ? ` · ${value.code}` : ""}` : (typeof value === "boolean" ? (value ? "通过" : "失败") : value); return `<div class="detail-field"><span>${h(name)}</span><strong>${h(display)}</strong></div>`; }).join("")}</div><p style="color:var(--text-soft);line-height:1.7;margin:16px 0 0">${h(result?.message || "验证完成")}</p>`, confirmLabel: "知道了", onConfirm: () => true });
    } catch (error) { showToast("连接测试失败", error.message, "error"); }
    finally { if (button) { button.disabled = false; button.textContent = "测试"; } }
  }

  function activateConnection(id, name) {
    confirmAction("切换主微信连接", `将“${name || "此连接"}”设为主连接。配置选择成功后，连接 Worker 仍需重新认证才能真正上线。`, "确认切换", async () => {
      const result = unwrap(await api.post(`/connections/${encodeURIComponent(id)}/activate`, {}));
      showToast(result?.activation_state === "ALREADY_ACTIVE" ? "连接已是当前连接" : "已验证并开始切换", result?.message || "等待 Bridge 重启并完成在线认证。", "success", 6500); renderCurrentRoute();
    });
  }

  function deleteConnection(id, name) {
    confirmAction("删除连接配置", `将软删除“${name || "此连接"}”。历史会话和任务不会被删除，当前主连接不能删除。`, "删除配置", async () => {
      await api.delete(`/connections/${encodeURIComponent(id)}`); showToast("连接已删除", "历史记录仍然保留。", "success"); renderCurrentRoute();
    }, true);
  }

  async function renderUsers(id, signal) {
    setPage(pageHeading("微信用户", "管理哪些企业微信用户可以聊天、操作电脑和读取文件") + filterBar({ placeholder: "搜索 UserID 或显示名称…", statuses: [["ALLOWED", "已授权"], ["PENDING", "待审批"], ["OBSERVE", "观察中"], ["DISABLED", "已禁用"]] }) + loadingPanel());
    const page = asPage(await api.get("/users", queryParams(), { signal }));
    if (!stillRendering(id)) return;
    const rows = page.items.map((item) => {
      const policy = parseJson(item.policy_json || item.policy);
      const capabilities = [policy.can_use_computer && "电脑", policy.can_read_files && "读文件", policy.can_send_files && "发文件", policy.can_execute_commands && "命令"].filter(Boolean);
      return `<tr>
        <td><button class="table-link" data-action="view-user" data-id="${h(item.id)}">${h(item.display_name || item.external_user_id || "未知用户")}</button><div class="mono muted">${h(clipped(item.external_user_id, 28))}</div></td>
        <td>${statusBadge(item.status)}</td><td>${capabilities.length ? capabilities.map((value) => `<span class="tag">${h(value)}</span>`).join(" ") : '<span class="muted">仅普通聊天</span>'}</td>
        <td>${h(item.message_count || 0)}</td><td>${h(formatDate(item.last_seen_at))}</td>
        <td><div class="row-actions"><button class="btn btn-sm" data-action="view-user" data-id="${h(item.id)}">查看</button>${can("users.write") ? `<button class="btn btn-sm" data-action="edit-user" data-id="${h(item.id)}">权限</button>` : ""}</div></td>
      </tr>`;
    }).join("");
    setPage(`${pageHeading("微信用户", "管理哪些企业微信用户可以聊天、操作电脑和读取文件")}${filterBar({ placeholder: "搜索 UserID 或显示名称…", statuses: [["ALLOWED", "已授权"], ["PENDING", "待审批"], ["OBSERVE", "观察中"], ["DISABLED", "已禁用"]], summary: `共 ${page.total} 位用户` })}
      <section class="panel">${page.items.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>用户</th><th>授权状态</th><th>能力</th><th>消息数</th><th>最近出现</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>${pagination(page)}` : emptyState("没有匹配的微信用户", "用户首次向机器人发送消息后，会自动登记在这里。")}</section>`);
  }

  async function userDetail(id) {
    drawerLoading("用户详情", "WECOM USER");
    try {
      const item = unwrap(await api.get(`/users/${encodeURIComponent(id)}`));
      const policy = parseJson(item.policy_json || item.policy);
      $("#drawer-title").textContent = item.display_name || item.external_user_id || "用户详情";
      $("#drawer-body").innerHTML = `<div class="detail-grid">
        ${detailField("状态", statusBadge(item.status), true)}${detailField("外部 UserID", `<span class="mono">${h(item.external_user_id)}</span>`, true)}
        ${detailField("连接 ID", `<span class="mono">${h(shortId(item.connection_id))}</span>`, true)}${detailField("消息数量", h(item.message_count || 0), true)}
        ${detailField("首次出现", h(formatFullDate(item.first_seen_at)), true)}${detailField("最近出现", h(formatFullDate(item.last_seen_at)), true)}
      </div><section class="detail-section"><h3 class="detail-section-title">能力策略</h3><div class="detail-grid">${policyFields(policy)}</div></section>
      ${can("users.write") ? `<div class="mt-16"><button class="btn btn-primary" data-action="edit-user" data-id="${h(item.id)}">编辑授权和权限</button></div>` : ""}`;
    } catch (error) { $("#drawer-body").innerHTML = errorState(error); }
  }

  function detailField(label, value, raw = false, wide = false) {
    return `<div class="detail-field ${wide ? "wide" : ""}"><span>${h(label)}</span><strong>${raw ? value : h(value)}</strong></div>`;
  }

  function policyFields(policy) {
    const fields = [
      ["普通聊天", policy.can_chat !== false], ["电脑工具", Boolean(policy.can_use_computer)], ["读取文件", Boolean(policy.can_read_files)],
      ["发送文件", Boolean(policy.can_send_files)], ["执行命令", Boolean(policy.can_execute_commands)], ["最高风险等级", policy.max_risk_level || "low"],
    ];
    return fields.map(([label, value]) => detailField(label, typeof value === "boolean" ? boolLabel(value) : h(value), true)).join("");
  }

  async function userForm(id) {
    let item;
    try { item = unwrap(await api.get(`/users/${encodeURIComponent(id)}`)); }
    catch (error) { return showToast("读取失败", error.message, "error"); }
    const policy = parseJson(item.policy_json || item.policy);
    const check = (name, label, description, checked) => `<div class="setting-row" style="padding-inline:0"><div class="setting-copy"><strong>${h(label)}</strong><small>${h(description)}</small></div><label class="switch"><input type="checkbox" name="${h(name)}" ${checked ? "checked" : ""}><span></span></label></div>`;
    showModal({
      title: "用户授权与能力", eyebrow: "ACCESS POLICY", confirmLabel: "保存策略",
      body: `<form id="user-policy-form" class="form-stack"><label class="field"><span>显示名称</span><input name="display_name" maxlength="100" value="${h(item.display_name || "")}" placeholder="便于识别的备注名称"></label><label class="field"><span>授权状态</span><select name="status"><option value="ALLOWED" ${item.status === "ALLOWED" ? "selected" : ""}>已授权</option><option value="PENDING" ${item.status === "PENDING" ? "selected" : ""}>待审批</option><option value="OBSERVE" ${item.status === "OBSERVE" ? "selected" : ""}>观察中</option><option value="DISABLED" ${item.status === "DISABLED" ? "selected" : ""}>已禁用</option></select></label>
        <div>${check("can_chat", "普通聊天", "允许向统一 Agent 提问", policy.can_chat !== false)}${check("can_use_computer", "操作电脑", "允许 Agent 调用桌面和电脑工具", policy.can_use_computer)}${check("can_read_files", "读取本地文件", "允许访问策略范围内的文件", policy.can_read_files)}${check("can_send_files", "发送本地文件", "允许通过企业微信交付文件", policy.can_send_files)}${check("can_execute_commands", "执行命令", "允许使用命令行和脚本工具", policy.can_execute_commands)}</div>
        <label class="field"><span>最高风险等级</span><select name="max_risk_level"><option value="low" ${(policy.max_risk_level || "low") === "low" ? "selected" : ""}>低风险</option><option value="medium" ${policy.max_risk_level === "medium" ? "selected" : ""}>中风险</option><option value="high" ${policy.max_risk_level === "high" ? "selected" : ""}>高风险（仍需任务确认）</option></select></label><div class="form-alert" data-form-error hidden></div></form>`,
      onConfirm: async (modal) => {
        const form = $("#user-policy-form", modal), values = Object.fromEntries(new FormData(form));
        const nextPolicy = { can_chat: Boolean(values.can_chat), can_use_computer: Boolean(values.can_use_computer), can_read_files: Boolean(values.can_read_files), can_send_files: Boolean(values.can_send_files), can_execute_commands: Boolean(values.can_execute_commands), max_risk_level: values.max_risk_level };
        await api.patch(`/users/${encodeURIComponent(id)}`, { display_name: values.display_name.trim() || null, status: values.status, policy: nextPolicy });
        showToast("用户策略已更新", "新权限将用于后续收到的任务。", "success"); closeDrawer(); renderCurrentRoute();
      },
    });
  }

  async function renderConversations(id, signal) {
    setPage(pageHeading("对话记录", "查看用户消息、机器人进度、最终回复和会话状态") + filterBar({ placeholder: "搜索会话或聊天 ID…", statuses: ["ACTIVE", "CLOSED", "INTERRUPTED"] }) + loadingPanel());
    const page = asPage(await api.get("/conversations", queryParams(), { signal }));
    if (!stillRendering(id)) return;
    const rows = page.items.map((item) => `<tr>
      <td><button class="table-link" data-action="view-conversation" data-id="${h(item.id)}">${h(shortId(item.external_chat_id || item.id))}</button><div class="mono muted">${h(shortId(item.id))}</div></td>
      <td>${statusBadge(item.status)}</td><td>${h(item.chat_type === "single" ? "单聊" : item.chat_type || "单聊")}</td><td>${h(item.message_count || 0)}</td>
      <td class="mono">${h(shortId(item.user_id))}</td><td>${h(formatDate(item.last_message_at))}</td><td><button class="btn btn-sm" data-action="view-conversation" data-id="${h(item.id)}">查看消息</button></td>
    </tr>`).join("");
    setPage(`${pageHeading("对话记录", "查看用户消息、机器人进度、最终回复和会话状态")}${filterBar({ placeholder: "搜索会话或聊天 ID…", statuses: ["ACTIVE", "CLOSED", "INTERRUPTED"], summary: `共 ${page.total} 个会话` })}<section class="panel">${page.items.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>会话</th><th>状态</th><th>类型</th><th>消息数</th><th>用户</th><th>最近活跃</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>${pagination(page)}` : emptyState("没有匹配的会话", "用户与机器人产生消息后，会话会显示在这里。")}</section>`);
  }

  async function conversationDetail(id) {
    drawerLoading("会话消息", "CONVERSATION");
    try {
      const [conversationResult, messagesResult] = await Promise.all([api.get(`/conversations/${encodeURIComponent(id)}`), api.get(`/conversations/${encodeURIComponent(id)}/messages`, { page: 1, page_size: 100 })]);
      const item = unwrap(conversationResult), messages = asPage(messagesResult).items;
      $("#drawer-title").textContent = `会话 ${shortId(item.external_chat_id || item.id)}`;
      $("#drawer-body").innerHTML = `<div class="detail-grid">${detailField("状态", statusBadge(item.status), true)}${detailField("消息数", item.message_count || messages.length)}${detailField("类型", item.chat_type || "single")}${detailField("最近活跃", formatFullDate(item.last_message_at))}${detailField("会话 ID", `<span class="mono">${h(item.id)}</span>`, true, true)}</div>
        ${can("tasks.control") && String(item.status).toUpperCase() === "ACTIVE" ? `<div class="mt-16"><button class="btn btn-danger" data-action="end-conversation" data-id="${h(item.id)}">结束 Agent 会话</button></div>` : ""}
        <section class="detail-section"><h3 class="detail-section-title">消息时间线</h3>${messages.length ? `<div class="message-list">${messages.map(messageBubble).join("")}</div>` : emptyState("会话暂无消息", "历史消息尚未写入管理数据库。")}</section>`;
    } catch (error) { $("#drawer-body").innerHTML = errorState(error); }
  }

  function messageBubble(item) {
    const outbound = String(item.direction).toUpperCase() === "OUTBOUND";
    return `<article class="message ${outbound ? "outbound" : "inbound"}"><span class="message-avatar">${outbound ? "机" : "用"}</span><div class="message-bubble"><div class="message-meta"><span>${outbound ? "机器人" : "用户"} · ${h(item.message_type || "text")}</span><time>${h(formatFullDate(item.created_at))}</time></div><div class="message-text">${h(item.content || (item.message_type === "file" ? "[文件消息]" : "[空消息]"))}</div>${item.error_code ? `<div class="text-danger mt-16">${h(item.error_code)}</div>` : ""}</div></article>`;
  }

  function endConversation(id) {
    confirmAction("结束 Agent 会话", "系统将保留当前会话及 Harness 历史，并为下一条消息创建全新的上下文。结束会话不会删除任何对话记录。", "结束会话", async () => {
      const result = unwrap(await api.post(`/conversations/${encodeURIComponent(id)}/end`, { reason: "Ended by administrator", fresh_session: true }));
      showToast("结束请求已提交", `控制命令 ${shortId(result?.id)} 正在处理。`, "success"); closeDrawer(); renderCurrentRoute();
    }, true);
  }

  async function renderTasks(id, signal) {
    setPage(pageHeading("任务中心", "定位每个请求的真实执行状态、失败阶段和工具调用") + filterBar({ placeholder: "搜索请求、结果或错误…", statuses: ["RUNNING", "WAITING_CONFIRMATION", "SUCCEEDED", "PARTIAL_SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "INTERRUPTED"] }) + loadingPanel());
    const page = asPage(await api.get("/tasks", queryParams(), { signal }));
    if (!stillRendering(id)) return;
    const activeStatuses = ["RECEIVED", "QUEUED", "RUNNING", "WAITING_CONFIRMATION", "CANCEL_REQUESTED"];
    const rows = page.items.map((item) => `<tr>
      <td><button class="table-link wrap-cell" data-action="view-task" data-id="${h(item.id)}">${h(clipped(item.request_summary, 66) || "未命名任务")}</button><div class="mono muted">${h(shortId(item.trace_id))}</div></td>
      <td>${statusBadge(item.status)}</td><td>${h(formatDuration(item.duration_ms))}</td><td class="${item.error_code ? "text-danger" : "muted"}">${h(item.error_code || "—")}</td><td>${h(formatDate(item.updated_at || item.created_at))}</td>
      <td><div class="row-actions"><button class="btn btn-sm" data-action="view-task" data-id="${h(item.id)}">详情</button>${can("tasks.control") && activeStatuses.includes(String(item.status).toUpperCase()) ? `<button class="btn btn-sm danger-text" data-action="cancel-task" data-id="${h(item.id)}">停止</button>` : ""}</div></td>
    </tr>`).join("");
    setPage(`${pageHeading("任务中心", "定位每个请求的真实执行状态、失败阶段和工具调用")}${filterBar({ placeholder: "搜索请求、结果或错误…", statuses: ["RUNNING", "WAITING_CONFIRMATION", "SUCCEEDED", "PARTIAL_SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "INTERRUPTED"], summary: `共 ${page.total} 个任务` })}<section class="panel">${page.items.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>请求 / Trace</th><th>状态</th><th>耗时</th><th>错误码</th><th>更新时间</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>${pagination(page)}` : emptyState("没有匹配的 Agent 任务", "调整筛选条件，或等待用户发来新的请求。")}</section>`);
  }

  async function taskDetail(id) {
    drawerLoading("任务详情", "AGENT TASK");
    try {
      const task = unwrap(await api.get(`/tasks/${encodeURIComponent(id)}`));
      $("#drawer-title").textContent = clipped(task.request_summary, 34) || "Agent 任务";
      const events = task.events || [];
      const tools = task.tool_calls || [];
      const active = ["RECEIVED", "QUEUED", "RUNNING", "WAITING_CONFIRMATION", "CANCEL_REQUESTED"].includes(String(task.status).toUpperCase());
      $("#drawer-body").innerHTML = `<div class="detail-grid">
        ${detailField("状态", statusBadge(task.status), true)}${detailField("耗时", formatDuration(task.duration_ms))}
        ${detailField("创建时间", formatFullDate(task.created_at))}${detailField("完成时间", formatFullDate(task.finished_at))}
        ${detailField("Task ID", `<span class="mono">${h(task.id)}</span>`, true, true)}${detailField("Trace ID", `<span class="mono">${h(task.trace_id)}</span>`, true, true)}
        ${detailField("请求摘要", h(task.request_summary || "—"), true, true)}${detailField("结果摘要", h(task.result_summary || "—"), true, true)}
        ${task.error_code || task.error_message ? detailField("失败信息", `<span class="text-danger">${h(task.error_code || "UNKNOWN")} · ${h(task.error_message || "未提供错误详情")}</span>`, true, true) : ""}
      </div>
      ${can("tasks.control") && active ? `<div class="mt-16"><button class="btn btn-danger" data-action="cancel-task" data-id="${h(task.id)}">停止当前任务</button></div>` : ""}
      <section class="detail-section"><h3 class="detail-section-title">执行时间线</h3>${events.length ? `<div class="timeline">${events.map(eventTimelineItem).join("")}</div>` : emptyState("暂无结构化事件", "任务存在，但执行阶段尚未写入事件流。")}</section>
      <section class="detail-section"><h3 class="detail-section-title">工具调用（${tools.length}）</h3>${tools.length ? `<div class="timeline">${tools.map(toolTimelineItem).join("")}</div>` : emptyState("没有工具调用", "这可能是一条纯文本回答，或工具事件尚未接入。")}</section>
      ${task.artifacts?.length ? `<section class="detail-section"><h3 class="detail-section-title">文件产物</h3>${task.artifacts.map((file) => `<div class="detail-field" style="margin-bottom:8px"><span>${h(file.kind || "file")}</span><strong>${h(file.name)} · ${h(formatBytes(file.size_bytes))}</strong></div>`).join("")}</section>` : ""}`;
    } catch (error) { $("#drawer-body").innerHTML = errorState(error); }
  }

  function eventTimelineItem(item) {
    const payload = item.payload || parseJson(item.payload_json);
    const type = item.event_type || "event";
    const status = payload.status || (type.includes("failed") ? "FAILED" : type.includes("succeeded") || type.includes("finished") ? "SUCCEEDED" : type.includes("started") ? "RUNNING" : "INFO");
    const summary = payload.message || payload.summary || payload.error || payload.tool_name || clipped(JSON.stringify(payload), 160) || "事件已记录";
    return `<article class="timeline-item"><span class="timeline-dot ${String(status).toLowerCase() === "failed" ? "failed" : String(status).toLowerCase() === "running" ? "running" : String(status).toLowerCase() === "succeeded" ? "success" : ""}"></span><div class="timeline-content"><header><strong>${h(type)}</strong><time>${h(formatFullDate(item.occurred_at))}</time></header><p>${h(summary)}</p></div></article>`;
  }

  function toolTimelineItem(item) {
    const input = parseJson(item.input_json), output = parseJson(item.output_json);
    const failed = String(item.status).toUpperCase() === "FAILED";
    const running = ["RUNNING", "STARTED"].includes(String(item.status).toUpperCase());
    return `<article class="timeline-item"><span class="timeline-dot ${failed ? "failed" : running ? "running" : "success"}">${failed ? "!" : running ? "…" : "✓"}</span><div class="timeline-content"><header><strong>${h(item.tool_name)}</strong><time>${h(formatDuration(item.duration_ms))}</time></header><p>${h(item.category || "agent")} · ${h(STATUS[String(item.status).toUpperCase()]?.[0] || item.status)}</p>${Object.keys(input).length ? `<pre class="code-block">输入 ${h(JSON.stringify(input, null, 2))}</pre>` : ""}${failed ? `<p class="text-danger">${h(item.error_code || "TOOL_FAILED")} · ${h(item.error_message || "工具执行失败")}</p>` : Object.keys(output).length ? `<pre class="code-block">输出 ${h(JSON.stringify(output, null, 2))}</pre>` : ""}</div></article>`;
  }

  function cancelTask(id) {
    confirmAction("停止 Agent 任务", "停止请求会由 Bridge 异步处理。当前 Runtime 或桌面工具可能需要短暂时间才能结束；系统会保留原任务记录。", "请求停止", async () => {
      const result = unwrap(await api.post(`/tasks/${encodeURIComponent(id)}/cancel`, {}));
      showToast("停止请求已提交", `控制命令 ${shortId(result?.id)} 正在处理。`, "success"); closeDrawer(); renderCurrentRoute();
    }, true);
  }

  async function renderConfigs(id, signal) {
    const create = can("configs.write") ? '<button class="btn btn-primary" data-action="new-config-profile">＋ 新建配置档案</button>' : "";
    setPage(pageHeading("Agent 配置", "以不可变版本管理模型、系统提示词、超时和工具策略", create) + filterBar({ placeholder: "搜索配置名称或说明…" }) + loadingPanel());
    const page = asPage(await api.get("/config-profiles", queryParams(), { signal }));
    if (!stillRendering(id)) return;
    const rows = page.items.map((item) => `<tr>
      <td><button class="table-link" data-action="view-config-profile" data-id="${h(item.id)}">${h(item.name)}</button><div class="muted wrap-cell">${h(clipped(item.description, 75) || "暂无说明")}</div></td>
      <td>${item.active_revision_id ? '<span class="badge badge-success">已有发布版本</span>' : '<span class="badge badge-neutral">尚未发布</span>'}</td>
      <td class="mono">${h(shortId(item.active_revision_id))}</td><td>${h(formatDate(item.updated_at))}</td>
      <td><div class="row-actions"><button class="btn btn-sm" data-action="view-config-profile" data-id="${h(item.id)}">版本</button>${can("configs.write") ? `<button class="btn btn-sm" data-action="new-config-revision" data-id="${h(item.id)}">新版本</button>` : ""}</div></td>
    </tr>`).join("");
    setPage(`${pageHeading("Agent 配置", "以不可变版本管理模型、系统提示词、超时和工具策略", create)}${filterBar({ placeholder: "搜索配置名称或说明…", summary: `共 ${page.total} 个配置档案` })}<section class="panel">${page.items.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>配置档案</th><th>发布状态</th><th>生效版本 ID</th><th>更新时间</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>${pagination(page)}` : emptyState("尚未创建 Agent 配置", "创建配置档案后，再添加并发布第一个不可变版本。", can("configs.write") ? { action: "new-config-profile", actionLabel: "新建配置档案" } : {})}</section>`);
  }

  async function configProfileForm(id) {
    let item = null;
    if (id) {
      try { item = unwrap(await api.get(`/config-profiles/${encodeURIComponent(id)}`)); }
      catch (error) { return showToast("读取失败", error.message, "error"); }
    }
    showModal({
      title: id ? "编辑配置档案" : "新建配置档案", eyebrow: "AGENT CONFIG PROFILE", confirmLabel: id ? "保存档案" : "创建档案",
      body: `<form id="config-profile-form" class="form-stack"><label class="field"><span>档案名称</span><input name="name" required maxlength="100" value="${h(item?.name || "")}" placeholder="例如：默认电脑 Agent"></label><label class="field"><span>说明</span><textarea name="description" maxlength="2000" placeholder="说明适用机器人、用户或工作场景">${h(item?.description || "")}</textarea></label><div class="form-alert" data-form-error hidden></div></form>`,
      onConfirm: async (modal) => {
        const form = $("#config-profile-form", modal), values = Object.fromEntries(new FormData(form));
        if (!values.name.trim()) return showInlineFormError(form, "档案名称不能为空。");
        if (id) await api.patch(`/config-profiles/${encodeURIComponent(id)}`, { name: values.name.trim(), description: values.description.trim() });
        else await api.post("/config-profiles", { name: values.name.trim(), description: values.description.trim() });
        showToast(id ? "档案已更新" : "档案已创建", id ? "现有版本内容没有被修改。" : "下一步请创建第一个配置版本。", "success"); closeDrawer(); renderCurrentRoute();
      },
    });
  }

  async function configProfileDetail(id) {
    drawerLoading("Agent 配置", "CONFIG PROFILE");
    try {
      const item = unwrap(await api.get(`/config-profiles/${encodeURIComponent(id)}`));
      state.configProfile = item;
      $("#drawer-title").textContent = item.name;
      const revisions = item.revisions || [];
      $("#drawer-body").innerHTML = `<div class="detail-grid">${detailField("状态", item.active_revision_id ? statusBadge("PUBLISHED") : statusBadge("DRAFT"), true)}${detailField("版本数", revisions.length)}${detailField("更新时间", formatFullDate(item.updated_at))}${detailField("当前版本", `<span class="mono">${h(shortId(item.active_revision_id))}</span>`, true)}${detailField("说明", h(item.description || "—"), true, true)}</div>
        <div class="mt-16 flex gap-8">${can("configs.write") ? `<button class="btn btn-primary" data-action="new-config-revision" data-id="${h(item.id)}">创建新版本</button><button class="btn" data-action="edit-config-profile" data-id="${h(item.id)}">编辑档案</button>` : ""}</div>
        <section class="detail-section"><h3 class="detail-section-title">不可变版本历史</h3>${revisions.length ? revisions.map((revision) => configRevisionCard(item, revision)).join("") : emptyState("暂无配置版本", "创建版本后才可以发布给 Agent 使用。")}</section>`;
    } catch (error) { $("#drawer-body").innerHTML = errorState(error); }
  }

  function configRevisionCard(profile, revision) {
    const active = profile.active_revision_id === revision.id;
    const toolPolicy = parseJson(revision.tool_policy_json || revision.tool_policy);
    return `<article class="panel" style="margin-bottom:12px"><div class="panel-header"><div><h3>v${h(revision.version)} · ${h(revision.provider)} / ${h(revision.model)}</h3><p>${h(formatFullDate(revision.created_at))}</p></div>${active ? '<span class="badge badge-success">当前生效</span>' : statusBadge(revision.status)}</div><div class="panel-body"><div class="detail-grid">${detailField("请求超时", `${revision.request_timeout_seconds} 秒`)}${detailField("任务超时", `${revision.task_timeout_seconds} 秒`)}${detailField("系统提示词", h(clipped(revision.system_prompt, 260)), true, true)}${detailField("工具策略", `<span class="mono">${h(clipped(JSON.stringify(toolPolicy), 220) || "{}")}</span>`, true, true)}</div>${can("configs.publish") && !active ? `<div class="mt-16 flex gap-8"><button class="btn btn-primary btn-sm" data-action="publish-config" data-profile-id="${h(profile.id)}" data-revision-id="${h(revision.id)}" data-version="${h(revision.version)}">发布此版本</button>${String(revision.status).toUpperCase() !== "DRAFT" ? `<button class="btn btn-sm" data-action="rollback-config" data-profile-id="${h(profile.id)}" data-revision-id="${h(revision.id)}" data-version="${h(revision.version)}">回滚到此版本</button>` : ""}</div>` : ""}</div></article>`;
  }

  function configRevisionForm(profileId) {
    showModal({
      title: "创建 Agent 配置版本", eyebrow: "IMMUTABLE REVISION", confirmLabel: "创建草稿版本",
      body: `<form id="config-revision-form" class="form-stack"><div class="detail-grid"><label class="field"><span>Provider</span><input name="provider" required maxlength="100" value="deepseek-harness"></label><label class="field"><span>模型</span><input name="model" required maxlength="200" placeholder="模型标识"></label></div><label class="field"><span>系统提示词</span><textarea name="system_prompt" required maxlength="200000" style="min-height:180px" placeholder="描述 Agent 的职责、安全边界和工作方式"></textarea></label><div class="detail-grid"><label class="field"><span>Harness 请求超时（秒）</span><input name="request_timeout_seconds" type="number" min="1" max="7200" value="450"></label><label class="field"><span>单任务超时（秒）</span><input name="task_timeout_seconds" type="number" min="1" max="589" value="480"></label></div><label class="field"><span>工具策略（JSON）</span><textarea name="tool_policy" class="mono" placeholder='{"allow_desktop": true}'>{}</textarea></label><div class="form-alert" data-form-error hidden></div></form>`,
      onConfirm: async (modal) => {
        const form = $("#config-revision-form", modal), values = Object.fromEntries(new FormData(form));
        if (!values.provider.trim() || !values.model.trim() || !values.system_prompt.trim()) return showInlineFormError(form, "Provider、模型和系统提示词不能为空。");
        let policy; try { policy = JSON.parse(values.tool_policy || "{}"); } catch (_) { return showInlineFormError(form, "工具策略必须是合法 JSON。"); }
        if (!policy || Array.isArray(policy) || typeof policy !== "object") return showInlineFormError(form, "工具策略必须是 JSON 对象。");
        const requestTimeout = Number(values.request_timeout_seconds), taskTimeout = Number(values.task_timeout_seconds);
        if (!(requestTimeout > 0 && requestTimeout <= 7200 && taskTimeout > 0 && taskTimeout < 590)) return showInlineFormError(form, "超时数值超出允许范围。");
        await api.post(`/config-profiles/${encodeURIComponent(profileId)}/revisions`, { provider: values.provider.trim(), model: values.model.trim(), system_prompt: values.system_prompt, request_timeout_seconds: requestTimeout, task_timeout_seconds: taskTimeout, tool_policy: policy });
        showToast("配置版本已创建", "草稿尚未生效，请检查后再发布。", "success"); closeDrawer(); renderCurrentRoute();
      },
    });
  }

  function publishConfig(profileId, revisionId, version) {
    confirmAction("发布 Agent 配置", `确认发布 v${version}。发布会更新生效版本，但运行中的 Bridge 需要重启后才会加载新配置。`, "确认发布", async () => {
      const result = unwrap(await api.post(`/config-profiles/${encodeURIComponent(profileId)}/revisions/${encodeURIComponent(revisionId)}/publish`, {}));
      showToast("配置已发布", result?.needs_restart ? "版本已发布，请重启 Bridge 后生效。" : "版本已发布。", "success", 6500); closeDrawer(); renderCurrentRoute();
    });
  }

  function rollbackConfig(profileId, revisionId, version) {
    confirmAction("回滚 Agent 配置", `系统不会篡改历史版本，而是复制 v${version} 创建一个新版本并发布。Bridge 仍需重启后生效。`, "创建回滚版本", async () => {
      const result = unwrap(await api.post(`/config-profiles/${encodeURIComponent(profileId)}/rollback/${encodeURIComponent(revisionId)}`, {}));
      showToast("回滚版本已发布", `已生成并发布 v${result?.version || "新"}，请重启 Bridge。`, "success", 6500); closeDrawer(); renderCurrentRoute();
    }, true);
  }

  async function renderDeliveries(id, signal) {
    const tab = state.query.get("tab") === "artifacts" ? "artifacts" : "deliveries";
    const tabs = `<div class="heading-actions"><button class="btn ${tab === "deliveries" ? "btn-primary" : ""}" data-route="deliveries">发送记录</button><button class="btn ${tab === "artifacts" ? "btn-primary" : ""}" data-route="deliveries?tab=artifacts" data-action="delivery-tab">文件产物</button></div>`;
    setPage(pageHeading("文件交付", "追踪文件生成、校验、上传和企业微信发送结果", tabs) + filterBar({ placeholder: tab === "deliveries" ? "搜索错误或 Trace ID…" : "搜索文件名或脱敏路径…", statuses: tab === "deliveries" ? ["PENDING", "UPLOADING", "SENDING", "SENT", "FAILED"] : ["AVAILABLE", "FAILED", "EXPIRED"] }) + loadingPanel());
    const page = asPage(await api.get(`/${tab}`, queryParams(), { signal }));
    if (!stillRendering(id)) return;
    state.deliveryRecords = new Map(page.items.map((item) => [item.id, item]));
    const rows = tab === "deliveries" ? page.items.map((item) => `<tr>
      <td><button class="table-link mono" data-action="view-delivery" data-kind="delivery" data-id="${h(item.id)}">${h(shortId(item.id))}</button><div class="muted mono">${h(shortId(item.trace_id))}</div></td>
      <td>${statusBadge(item.status)}</td><td class="mono">${h(shortId(item.artifact_id))}</td><td>${h(item.retry_count || 0)}</td><td class="${item.error_code ? "text-danger" : "muted"}">${h(item.error_code || "—")}</td><td>${h(formatDate(item.updated_at))}</td><td><button class="btn btn-sm" data-action="view-delivery" data-kind="delivery" data-id="${h(item.id)}">详情</button></td>
    </tr>`).join("") : page.items.map((item) => `<tr>
      <td><button class="table-link" data-action="view-delivery" data-kind="artifact" data-id="${h(item.id)}">${h(item.name)}</button><div class="muted">${h(clipped(item.path_redacted, 45))}</div></td>
      <td>${statusBadge(item.status)}</td><td>${h(item.kind || "file")}</td><td>${h(item.mime_type || "—")}</td><td>${h(formatBytes(item.size_bytes))}</td><td>${h(formatDate(item.created_at))}</td><td><button class="btn btn-sm" data-action="view-delivery" data-kind="artifact" data-id="${h(item.id)}">详情</button></td>
    </tr>`).join("");
    const headers = tab === "deliveries" ? "<th>交付 / Trace</th><th>状态</th><th>文件 ID</th><th>重试</th><th>错误码</th><th>更新时间</th><th></th>" : "<th>文件</th><th>状态</th><th>类型</th><th>MIME</th><th>大小</th><th>创建时间</th><th></th>";
    setPage(`${pageHeading("文件交付", "追踪文件生成、校验、上传和企业微信发送结果", tabs)}${filterBar({ placeholder: tab === "deliveries" ? "搜索错误或 Trace ID…" : "搜索文件名或脱敏路径…", statuses: tab === "deliveries" ? ["PENDING", "UPLOADING", "SENDING", "SENT", "FAILED"] : ["AVAILABLE", "FAILED", "EXPIRED"], summary: `共 ${page.total} 条${tab === "deliveries" ? "发送记录" : "文件产物"}` })}<section class="panel">${page.items.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>${pagination(page)}` : emptyState(tab === "deliveries" ? "暂无文件发送记录" : "暂无文件产物", "Agent 生成、读取或发送文件后，记录会显示在这里。")}</section>`);
  }

  function deliveryDetail(id, kind) {
    const item = state.deliveryRecords?.get(id);
    if (!item) return showToast("记录已变化", "请刷新列表后重试。", "warning");
    const isArtifact = kind === "artifact";
    openDrawer(isArtifact ? item.name : `交付 ${shortId(item.id)}`, isArtifact ? "FILE ARTIFACT" : "FILE DELIVERY", `<div class="detail-grid">
      ${detailField("状态", statusBadge(item.status), true)}${detailField(isArtifact ? "文件类型" : "重试次数", isArtifact ? item.kind || "file" : item.retry_count || 0)}
      ${detailField("Task ID", `<span class="mono">${h(shortId(item.task_id))}</span>`, true)}${detailField("Trace ID", `<span class="mono">${h(shortId(item.trace_id))}</span>`, true)}
      ${isArtifact ? detailField("脱敏路径", `<span class="mono">${h(item.path_redacted || "—")}</span>`, true, true) : detailField("Artifact ID", `<span class="mono">${h(item.artifact_id || "—")}</span>`, true, true)}
      ${isArtifact ? detailField("文件大小", formatBytes(item.size_bytes)) + detailField("MIME 类型", item.mime_type || "—") : detailField("媒体 ID", item.media_id_masked || "—") + detailField("更新时间", formatFullDate(item.updated_at))}
      ${isArtifact && item.sha256 ? detailField("SHA-256", `<span class="mono">${h(item.sha256)}</span>`, true, true) : ""}
      ${item.error_code || item.error_message ? detailField("失败信息", `<span class="text-danger">${h(item.error_code || "DELIVERY_FAILED")} · ${h(item.error_message || "未提供详情")}</span>`, true, true) : ""}
    </div><section class="detail-section"><div class="form-alert">重新发送会在 Bridge 端校验加密保存的原始路径、文件存在性、当前微信连接和 50 MiB 限制；请求受理不等于发送完成，最终状态会异步更新。</div>${!isArtifact && can("artifacts.send") && ["FAILED", "SENT"].includes(String(item.status).toUpperCase()) ? `<div class="mt-16"><button class="btn btn-primary" data-action="retry-delivery" data-id="${h(item.id)}">重新发送此文件</button></div>` : ""}</section>`);
  }

  function retryDelivery(id) {
    confirmAction("重新发送文件", "系统会重新校验本地文件及当前微信连接，并创建一条新的交付记录。此操作不会覆盖原失败记录。", "提交重发", async () => {
      const result = unwrap(await api.post(`/deliveries/${encodeURIComponent(id)}/retry`, {}));
      showToast("重发请求已提交", `控制命令 ${shortId(result?.id)} 正在由 Bridge 处理。`, "success");
      closeDrawer();
      renderCurrentRoute();
    });
  }

  async function renderAlerts(id, signal) {
    setPage(pageHeading("告警中心", "确认、处置并追踪运行异常，不让关键故障淹没在文本日志中") + filterBar({ placeholder: "搜索标题、类型或消息…", statuses: ["OPEN", "ACKNOWLEDGED", "RESOLVED"] }) + loadingPanel());
    const page = asPage(await api.get("/alerts", queryParams(), { signal }));
    if (!stillRendering(id)) return;
    state.alertRecords = new Map(page.items.map((item) => [item.id, item]));
    const rows = page.items.map((item) => `<tr><td><button class="table-link wrap-cell" data-action="view-alert" data-id="${h(item.id)}">${h(item.title)}</button><div class="muted wrap-cell">${h(clipped(item.message, 100))}</div></td><td>${statusBadge(item.severity)}</td><td>${statusBadge(item.status)}</td><td>${h(item.alert_type)}</td><td>${h(item.resource_type || "—")} · <span class="mono">${h(shortId(item.resource_id))}</span></td><td>${h(formatFullDate(item.created_at))}</td><td><div class="row-actions"><button class="btn btn-sm" data-action="view-alert" data-id="${h(item.id)}">详情</button>${can("alerts.write") && item.status === "OPEN" ? `<button class="btn btn-sm" data-action="ack-alert" data-id="${h(item.id)}">接手</button>` : ""}${can("alerts.write") && item.status !== "RESOLVED" ? `<button class="btn btn-sm" data-action="resolve-alert" data-id="${h(item.id)}">解决</button>` : ""}</div></td></tr>`).join("");
    setPage(`${pageHeading("告警中心", "确认、处置并追踪运行异常，不让关键故障淹没在文本日志中")}${filterBar({ placeholder: "搜索标题、类型或消息…", statuses: ["OPEN", "ACKNOWLEDGED", "RESOLVED"], summary: `共 ${page.total} 条告警` })}<section class="panel">${page.items.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>告警</th><th>级别</th><th>状态</th><th>类型</th><th>关联资源</th><th>发生时间</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>${pagination(page)}` : emptyState("当前没有告警", "服务运行正常，或当前筛选条件下没有告警记录。")}</section>`);
    const openCount = page.items.filter((item) => item.status === "OPEN").length;
    const badge = $("#alerts-badge"); badge.textContent = openCount; badge.hidden = !openCount;
  }

  function alertDetail(id) {
    const item = state.alertRecords?.get(id);
    if (!item) return showToast("告警已变化", "请刷新列表后重试。", "warning");
    openDrawer(item.title, "SYSTEM ALERT", `<div class="detail-grid">${detailField("严重程度", statusBadge(item.severity), true)}${detailField("处理状态", statusBadge(item.status), true)}${detailField("告警类型", item.alert_type)}${detailField("发生时间", formatFullDate(item.created_at))}${detailField("资源", `${h(item.resource_type || "—")} · <span class="mono">${h(item.resource_id || "—")}</span>`, true, true)}${item.trace_id ? detailField("Trace ID", `<span class="mono">${h(item.trace_id)}</span>`, true, true) : ""}${detailField("告警说明", h(item.message), true, true)}${item.resolution_note ? detailField("处理备注", h(item.resolution_note), true, true) : ""}</div><div class="mt-16 flex gap-8">${can("alerts.write") && item.status === "OPEN" ? `<button class="btn" data-action="ack-alert" data-id="${h(item.id)}">确认并接手</button>` : ""}${can("alerts.write") && item.status !== "RESOLVED" ? `<button class="btn btn-primary" data-action="resolve-alert" data-id="${h(item.id)}">标记已解决</button>` : ""}</div>`);
  }

  function alertAction(id, operation) {
    const resolving = operation === "resolve";
    showModal({
      title: resolving ? "解决告警" : "确认并接手告警", eyebrow: "ALERT WORKFLOW", confirmLabel: resolving ? "标记已解决" : "确认接手",
      body: `<form id="alert-action-form" class="form-stack"><label class="field"><span>处理备注${resolving ? "（建议填写）" : ""}</span><textarea name="note" maxlength="2000" placeholder="记录判断依据、处理动作或后续计划"></textarea></label></form>`,
      onConfirm: async (modal) => {
        const note = new FormData($("#alert-action-form", modal)).get("note")?.trim() || "";
        await api.post(`/alerts/${encodeURIComponent(id)}/${operation}`, { note });
        showToast(resolving ? "告警已解决" : "告警已接手", resolving ? "处置结果已写入审计记录。" : "告警状态已更新为处理中。", "success"); closeDrawer(); renderCurrentRoute();
      },
    });
  }

  async function renderLogs(id, signal) {
    setPage(pageHeading("实时日志", "按服务、级别、Trace ID 检索脱敏后的结构化日志", `<span class="badge badge-info">SSE 实时</span>`) + loadingPanel(12));
    const page = asPage(await api.get("/logs", queryParams({ page_size: 200 }), { signal }));
    if (!stillRendering(id)) return;
    state.currentLogs = page.items.slice().reverse();
    state.logsPaused = false;
    setPage(`${pageHeading("实时日志", "按服务、级别、Trace ID 检索脱敏后的结构化日志", `<span class="badge badge-info">SSE 实时</span>`)}
      <section class="log-viewer"><form class="log-toolbar" data-filter-form><label>级别</label><select class="filter-control" name="status" data-auto-filter><option value="">全部</option>${["INFO", "WARNING", "ERROR", "CRITICAL"].map((value) => `<option value="${value}" ${state.query.get("status") === value ? "selected" : ""}>${value}</option>`).join("")}</select><label>搜索</label><input class="filter-control" style="flex:1;min-width:180px" name="q" value="${h(state.query.get("q") || "")}" placeholder="消息、事件或服务"><label>Trace</label><input class="filter-control mono" name="trace_id" value="${h(state.query.get("trace_id") || "")}" placeholder="trace_id"><button class="btn btn-sm" type="submit">应用</button><button class="btn btn-sm" type="button" data-action="log-pause">暂停滚动</button><button class="btn btn-sm" type="button" data-action="log-clear">清屏</button></form><div id="log-lines" class="log-lines"></div></section>
      <div style="margin-top:8px" class="flex justify-between"><span class="muted">当前加载最近 ${page.items.length} 条，日志已经过服务端脱敏。</span><span class="muted">共 ${page.total} 条匹配记录</span></div>`);
    renderLogLines();
  }

  function logLine(item) {
    const level = String(item.level || item.severity || "INFO").toUpperCase();
    return `<div class="log-line" data-log-id="${h(item.id || item.event_id || "")}"><span class="log-time">${h(formatFullDate(item.created_at || item.occurred_at))}</span><span class="log-level ${h(level)}">${h(level)}</span><span class="log-service">${h(item.service || item.resource_type || "system")}</span><span class="log-message">${h(item.message || item.event_name || item.event_type || "事件")}${item.trace_id ? ` <span style="color:#60738a">[${h(shortId(item.trace_id))}]</span>` : ""}</span></div>`;
  }

  function renderLogLines() {
    const container = $("#log-lines"); if (!container) return;
    container.innerHTML = state.currentLogs.length ? state.currentLogs.map(logLine).join("") : '<div class="log-empty">当前筛选条件下没有日志<br><small>新的实时事件会自动出现在这里</small></div>';
    if (!state.logsPaused) container.scrollTop = container.scrollHeight;
  }

  function appendLiveLog(event) {
    const payload = event.payload || {};
    const item = { id: event.event_id, event_id: event.event_id, created_at: event.occurred_at, level: payload.level || event.severity, service: payload.service || event.resource_type, event_name: payload.event_name || event.event_type, message: payload.message || event.event_type, trace_id: event.trace_id };
    state.currentLogs.push(item);
    if (state.currentLogs.length > 500) state.currentLogs.splice(0, state.currentLogs.length - 500);
    const container = $("#log-lines"); if (!container) return;
    $(".log-empty", container)?.remove(); container.insertAdjacentHTML("beforeend", logLine(item));
    if (!state.logsPaused) container.scrollTop = container.scrollHeight;
  }

  function toggleLogPause() {
    state.logsPaused = !state.logsPaused;
    const button = $("[data-action='log-pause']"); if (button) button.textContent = state.logsPaused ? "继续滚动" : "暂停滚动";
    showToast(state.logsPaused ? "实时滚动已暂停" : "实时滚动已恢复", state.logsPaused ? "日志仍在接收，只是不再自动滚动。" : "已跳转到最新日志。", "info", 2200);
    if (!state.logsPaused) { const container = $("#log-lines"); if (container) container.scrollTop = container.scrollHeight; }
  }

  async function renderHealth(id, signal) {
    setPage(pageHeading("系统状态", "检查管理 API、数据库、事件存储和本机执行组件") + loadingPanel());
    const health = unwrap(await api.get("/health", null, { signal }));
    if (!stillRendering(id)) return;
    state.health = health;
    const components = normalizeHealth(health);
    const cards = components.map((item) => `<article class="connection-card"><header><div class="connection-ident"><span class="connection-logo">${["HEALTHY", "ONLINE", "RUNNING", "OK"].includes(String(item.status).toUpperCase()) ? "✓" : "!"}</span><div><h3>${h(item.name)}</h3><p>${h(item.detail || "没有附加说明")}</p></div></div>${statusBadge(String(item.status).toUpperCase() === "OK" ? "HEALTHY" : item.status)}</header></article>`).join("");
    setPage(`${pageHeading("系统状态", "检查管理 API、数据库、事件存储和本机执行组件", `<span class="last-updated">检查于 ${h(formatFullDate(health.generated_at || health.timestamp || new Date()))}</span>`)}
      <div class="connection-card-grid">${cards}</div>
      <section class="panel mt-16"><div class="panel-header"><div><h3>运行信息</h3><p>管理接口返回的非敏感诊断信息</p></div><span class="badge badge-outline">只读</span></div><div class="panel-body"><pre class="code-block">${h(JSON.stringify(health, null, 2))}</pre></div></section>
      <section class="panel mt-16"><div class="panel-body"><div class="form-alert">进程启动、停止和重启最终应由独立 Local Supervisor 执行。当前管理 API 不会直接终止自身，也不会根据进程名猜测并关闭其他程序。</div></div></section>`);
    updateSidebarHealth(health, components);
  }

  async function renderMaintenance(id, signal) {
    const actions = `${can("system.backup") ? '<button class="btn" data-action="system-backup">创建数据库备份</button>' : ""}${can("system.retention") ? '<button class="btn btn-primary" data-action="system-retention">数据保留清理</button>' : ""}`;
    setPage(pageHeading("系统维护", "查看本机节点与受管服务，并执行可审计的维护操作", actions) + `<div class="grid-2">${loadingPanel()}${loadingPanel()}</div>`);
    const [nodeResult, serviceResult] = await Promise.allSettled([api.get("/nodes", { page: 1, page_size: 100 }, { signal }), api.get("/services", { page: 1, page_size: 100 }, { signal })]);
    if (!stillRendering(id)) return;
    if (nodeResult.status === "rejected" && serviceResult.status === "rejected") throw nodeResult.reason;
    const nodes = nodeResult.status === "fulfilled" ? asPage(nodeResult.value).items : [];
    const services = serviceResult.status === "fulfilled" ? asPage(serviceResult.value).items : [];
    const nodeCards = nodes.length ? nodes.map((node) => {
      const capabilities = parseJson(node.capabilities_json || node.capabilities);
      return `<article class="connection-card"><header><div class="connection-ident"><span class="connection-logo">机</span><div><h3>${h(node.name)}</h3><p>${h(node.hostname || node.os_name || "本机节点")}</p></div></div>${statusBadge(node.status)}</header><div class="connection-meta"><div><span>操作系统</span><strong>${h(node.os_name || "—")}</strong></div><div><span>最后心跳</span><strong>${h(formatFullDate(node.last_heartbeat_at))}</strong></div><div><span>能力</span><strong>${h(Object.keys(capabilities).filter((key) => capabilities[key]).join("、") || "等待上报")}</strong></div></div></article>`;
    }).join("") : `<section class="panel">${emptyState("没有节点心跳", "Bridge 或 Supervisor 上报心跳后，本机节点会显示在这里。")}</section>`;
    const serviceRows = services.map((service) => {
      const managedName = managedServiceName(service.service_type);
      return `<tr><td><span class="primary-cell">${h(service.service_type)}</span><div class="mono muted">${h(shortId(service.id))}</div></td><td>${statusBadge(service.status)}</td><td class="mono">${h(service.pid || "—")}</td><td>${h(service.version || "—")}</td><td>${h(formatFullDate(service.last_heartbeat_at))}</td><td><div class="row-actions">${can("runtime.control") && managedName ? `<button class="btn btn-sm" data-action="service-control" data-service="${h(managedName)}" data-operation="start">启动</button><button class="btn btn-sm" data-action="service-control" data-service="${h(managedName)}" data-operation="restart">重启</button><button class="btn btn-sm danger-text" data-action="service-control" data-service="${h(managedName)}" data-operation="stop">停止</button>` : '<span class="muted">只读</span>'}</div></td></tr>`;
    }).join("");
    setPage(`${pageHeading("系统维护", "查看本机节点与受管服务，并执行可审计的维护操作", actions)}
      <section class="panel"><div class="panel-header"><div><h3>电脑节点</h3><p>交互式 Windows 会话和桌面能力</p></div><span class="badge badge-outline">${nodes.length} 个节点</span></div><div class="panel-body"><div class="connection-card-grid">${nodeCards}</div></div></section>
      <section class="panel mt-16"><div class="panel-header"><div><h3>受管服务</h3><p>控制请求进入 Supervisor 命令队列，202 不等于进程已经完成操作</p></div><span class="badge badge-outline">${services.length} 个实例</span></div>${services.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>服务</th><th>状态</th><th>PID</th><th>版本</th><th>最后心跳</th><th></th></tr></thead><tbody>${serviceRows}</tbody></table></div>` : emptyState("尚无服务实例", "Supervisor、Bridge 或 Desktop Worker 上报状态后会显示在这里。")}</section>
      <section class="panel mt-16"><div class="panel-body"><div class="form-alert">“请求已提交”仅表示控制命令已进入队列。服务最终状态以 Supervisor 后续心跳为准；后台不会根据进程名猜测并结束无关进程。</div></div></section>`);
  }

  function managedServiceName(serviceType) {
    const value = String(serviceType || "").toLowerCase();
    if (value.includes("bridge")) return "bridge";
    return "";
  }

  function controlService(service, operation) {
    const labels = { start: "启动", stop: "停止", restart: "重启" };
    const dangerous = operation === "stop" || operation === "restart";
    confirmAction(`${labels[operation] || operation}服务`, `将向 Local Supervisor 提交“${labels[operation] || operation} ${service}”控制命令。任务返回 PENDING 只代表命令已受理。`, `提交${labels[operation] || "操作"}`, async () => {
      const result = unwrap(await api.post(`/runtime/services/${encodeURIComponent(service)}/${encodeURIComponent(operation)}`, {}));
      showToast("控制命令已提交", `命令 ${shortId(result?.id)} 状态为 ${result?.status || "PENDING"}，请等待服务心跳更新。`, "success", 6500); renderCurrentRoute();
    }, dangerous);
  }

  function backupSystem() {
    confirmAction("创建在线数据库备份", "系统将使用 SQLite 在线备份 API 创建一致性副本，并在返回前执行完整性快速检查。备份文件不会包含主密钥文件。", "创建备份", async () => {
      const result = unwrap(await api.post("/system/backup", {}));
      setTimeout(() => showModal({ title: "备份创建成功", eyebrow: "BACKUP VERIFIED", body: `<div class="detail-grid">${detailField("文件名", h(result.file_name), true, true)}${detailField("文件大小", formatBytes(result.size_bytes))}${detailField("完整性", statusBadge(String(result.integrity).toUpperCase() === "OK" ? "SUCCESS" : "FAILED"), true)}${detailField("创建时间", formatFullDate(result.created_at), false, true)}</div>`, confirmLabel: "完成", onConfirm: () => true }), 80);
    });
  }

  function retentionForm() {
    showModal({
      title: "数据保留策略预检", eyebrow: "RETENTION DRY RUN", confirmLabel: "预览清理范围",
      body: `<form id="retention-form" class="form-stack"><div class="form-alert">第一步只统计符合条件的数据，不会删除。确认预检结果后，才可以执行不可恢复的清理。</div><div class="detail-grid"><label class="field"><span>事件保留天数</span><input name="event_days" type="number" min="1" max="3650" value="90"></label><label class="field"><span>日志保留天数</span><input name="log_days" type="number" min="1" max="3650" value="30"></label><label class="field"><span>过期登录会话保留天数</span><input name="session_days" type="number" min="1" max="3650" value="30"></label><label class="field"><span>审计记录保留天数</span><input name="audit_days" type="number" min="30" max="3650" value="365"></label></div><div class="form-alert" data-form-error hidden></div></form>`,
      onConfirm: async (modal) => {
        const form = $("#retention-form", modal), values = Object.fromEntries(new FormData(form));
        const policy = { event_days: Number(values.event_days), log_days: Number(values.log_days), session_days: Number(values.session_days), audit_days: Number(values.audit_days) };
        if (policy.event_days < 1 || policy.log_days < 1 || policy.session_days < 1 || policy.audit_days < 30) return showInlineFormError(form, "保留天数低于系统允许的安全下限。");
        const preview = unwrap(await api.post("/system/retention", { ...policy, dry_run: true }));
        setTimeout(() => confirmRetention(policy, preview), 80);
      },
    });
  }

  function confirmRetention(policy, preview) {
    const counts = preview.eligible || {};
    showModal({ title: "确认执行数据清理", eyebrow: "DESTRUCTIVE OPERATION", confirmLabel: `删除 ${preview.total || 0} 条记录`, confirmKind: "danger", body: `<div class="form-alert">该操作不可撤销。建议确认最近备份可用后再继续。</div><div class="detail-grid mt-16">${Object.entries(counts).map(([table, count]) => detailField(table, `${count} 条`)).join("")}</div><p class="muted" style="line-height:1.7">清理范围只包括事件流、结构化日志、过期登录会话和达到保留期的审计记录，不删除对话、任务或文件本体。</p>`, onConfirm: async () => {
      const result = unwrap(await api.post("/system/retention", { ...policy, dry_run: false }));
      showToast("数据清理完成", `已删除 ${result.total || 0} 条达到保留期限的记录。`, "success", 6500); renderCurrentRoute();
    }});
  }

  async function renderAdmins(id, signal) {
    const create = can("admins.write") ? '<button class="btn btn-primary" data-action="new-admin">＋ 新建管理员</button>' : "";
    setPage(pageHeading("管理员", "通过角色分配后台权限，不共享超级管理员账号", create) + filterBar({ placeholder: "搜索账号或显示名称…" }) + loadingPanel());
    const [usersResult, rolesResult] = await Promise.all([api.get("/admin-users", queryParams(), { signal }), api.get("/roles", null, { signal })]);
    if (!stillRendering(id)) return;
    const page = asPage(usersResult), roles = asPage(rolesResult).items;
    state.adminRecords = new Map(page.items.map((item) => [item.id, item])); state.adminRoles = roles;
    const rows = page.items.map((item) => `<tr><td><span class="primary-cell">${h(item.display_name || item.username)}</span><div class="muted">@${h(item.username)}</div></td><td>${statusBadge(item.status)}</td><td>${(item.roles || []).map((role) => `<span class="tag">${h(roleName(role))}</span>`).join(" ") || '<span class="muted">未分配</span>'}</td><td>${h(formatFullDate(item.last_login_at))}</td><td>${h(formatDate(item.created_at))}</td><td>${can("admins.write") ? `<button class="btn btn-sm" data-action="edit-admin-roles" data-id="${h(item.id)}">分配角色</button>` : '<span class="muted">只读</span>'}</td></tr>`).join("");
    setPage(`${pageHeading("管理员", "通过角色分配后台权限，不共享超级管理员账号", create)}${filterBar({ placeholder: "搜索账号或显示名称…", summary: `${page.total} 个账号 · ${roles.length} 个角色` })}<section class="panel">${page.items.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>管理员</th><th>状态</th><th>角色</th><th>最后登录</th><th>创建时间</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>${pagination(page)}` : emptyState("没有匹配的管理员", "使用超级管理员创建其他后台账号并分配最小必要权限。")}</section>
      <section class="panel mt-16"><div class="panel-header"><div><h3>角色权限矩阵</h3><p>角色权限由系统定义，账号可以分配一个或多个角色</p></div></div><div class="panel-body"><div class="connection-card-grid">${roles.map((role) => `<article class="connection-card"><header><div><h3>${h(roleName(role.name))}</h3><p>${h(role.description)}</p></div><span class="badge badge-outline">${role.permissions?.length || 0} 项权限</span></header><div class="connection-meta">${(role.permissions || []).slice(0, 7).map((permission) => `<div><span class="mono">${h(permission)}</span><strong>允许</strong></div>`).join("")}${role.permissions?.length > 7 ? `<div><span>其他</span><strong>＋${role.permissions.length - 7}</strong></div>` : ""}</div></article>`).join("")}</div></div></section>`);
  }

  function roleName(role) {
    const value = typeof role === "string" ? role : role?.name || "";
    return ({ super_admin: "超级管理员", ops_admin: "运维管理员", bot_admin: "机器人管理员", auditor: "审计员", viewer: "只读观察员" })[value] || value;
  }

  function roleOptions(roles, selected) {
    const chosen = new Set((selected || []).map((role) => typeof role === "string" ? role : role.name));
    return roles.map((role) => `<label style="display:flex;align-items:flex-start;gap:10px;padding:11px;border:1px solid var(--border);border-radius:8px"><input class="checkbox" type="checkbox" name="roles" value="${h(role.name)}" ${chosen.has(role.name) ? "checked" : ""}><span><strong style="display:block;font-size:12px">${h(roleName(role.name))}</strong><small class="muted">${h(role.description)} · ${role.permissions?.length || 0} 项权限</small></span></label>`).join("");
  }

  function adminForm() {
    const roles = state.adminRoles || [];
    showModal({
      title: "新建后台管理员", eyebrow: "ADMINISTRATOR", confirmLabel: "创建管理员",
      body: `<form id="admin-form" class="form-stack"><label class="field"><span>显示名称</span><input name="display_name" maxlength="100" placeholder="例如：运维管理员"></label><label class="field"><span>登录账号</span><input name="username" required minlength="3" maxlength="64" pattern="[A-Za-z0-9_.@-]+" autocomplete="off" placeholder="3–64 位字母、数字或 ._@-"></label><label class="field"><span>初始密码</span><input name="password" type="password" required minlength="12" maxlength="256" autocomplete="new-password" placeholder="至少 12 个字符"></label><div class="field"><span>角色（至少一个）</span><div style="display:grid;gap:8px">${roleOptions(roles, ["viewer"])}</div></div><div class="form-alert" data-form-error hidden></div></form>`,
      onConfirm: async (modal) => {
        const form = $("#admin-form", modal), values = Object.fromEntries(new FormData(form)), selected = new FormData(form).getAll("roles");
        if (!values.username?.trim() || !values.password || values.password.length < 12) return showInlineFormError(form, "请填写有效账号和至少 12 位的密码。");
        if (!selected.length) return showInlineFormError(form, "请至少分配一个角色。");
        await api.post("/admin-users", { username: values.username.trim(), display_name: values.display_name.trim(), password: values.password, roles: selected });
        showToast("管理员已创建", "请通过安全渠道向管理员提供初始密码。", "success"); renderCurrentRoute();
      },
    });
  }

  function adminRoleForm(id) {
    const item = state.adminRecords?.get(id), roles = state.adminRoles || [];
    if (!item) return showToast("账号已变化", "请刷新列表后重试。", "warning");
    showModal({
      title: `分配角色 · ${item.display_name || item.username}`, eyebrow: "ROLE ASSIGNMENT", confirmLabel: "保存角色",
      body: `<form id="admin-role-form" class="form-stack"><div class="form-alert">修改会影响该管理员后续接口权限。系统不会允许移除最后一位超级管理员的超级管理员角色。</div><div style="display:grid;gap:8px">${roleOptions(roles, item.roles)}</div><div class="form-alert" data-form-error hidden></div></form>`,
      onConfirm: async (modal) => {
        const form = $("#admin-role-form", modal), selected = new FormData(form).getAll("roles");
        if (!selected.length) return showInlineFormError(form, "请至少保留一个角色。");
        await api.request(`/admin-users/${encodeURIComponent(id)}/roles`, { method: "PUT", body: { roles: selected } });
        showToast("管理员角色已更新", "新权限将在后续请求中生效。", "success"); renderCurrentRoute();
      },
    });
  }

  async function renderAudit(id, signal) {
    setPage(pageHeading("审计记录", "不可变更地记录登录、配置、权限和任务控制操作") + filterBar({ placeholder: "搜索动作或资源 ID…", statuses: [["SUCCESS", "成功"], ["FAILED", "失败"], ["DENIED", "拒绝"]] }) + loadingPanel());
    const page = asPage(await api.get("/audit", queryParams(), { signal }));
    if (!stillRendering(id)) return;
    state.auditRecords = new Map(page.items.map((item) => [item.id, item]));
    const rows = page.items.map((item) => `<tr>
      <td><button class="table-link" data-action="view-audit" data-id="${h(item.id)}">${h(item.action)}</button><div class="muted">${h(item.actor_type || "system")} · ${h(shortId(item.actor_id))}</div></td>
      <td>${statusBadge(item.result)}</td><td>${h(item.resource_type || "—")}</td><td class="mono">${h(shortId(item.resource_id))}</td><td class="mono">${h(item.ip_address || "—")}</td><td>${h(formatFullDate(item.created_at))}</td><td><button class="btn btn-sm" data-action="view-audit" data-id="${h(item.id)}">详情</button></td>
    </tr>`).join("");
    setPage(`${pageHeading("审计记录", "不可变更地记录登录、配置、权限和任务控制操作")}${filterBar({ placeholder: "搜索动作或资源 ID…", statuses: [["SUCCESS", "成功"], ["FAILED", "失败"], ["DENIED", "拒绝"]], summary: `共 ${page.total} 条审计事件` })}<section class="panel">${page.items.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>动作 / 操作者</th><th>结果</th><th>资源类型</th><th>资源 ID</th><th>来源 IP</th><th>时间</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>${pagination(page)}` : emptyState("没有匹配的审计记录", "关键管理操作执行后会写入不可修改的审计记录。")}</section>`);
  }

  function auditDetail(id) {
    const item = state.auditRecords?.get(id);
    if (!item) return showToast("记录已变化", "请刷新列表后重试。", "warning");
    const changes = parseJson(item.changes_json || item.changes);
    openDrawer(item.action || "审计详情", "AUDIT EVENT", `<div class="detail-grid">${detailField("执行结果", statusBadge(item.result), true)}${detailField("操作者", `${h(item.actor_type || "system")} · ${h(shortId(item.actor_id))}`, true)}${detailField("资源类型", item.resource_type || "—")}${detailField("资源 ID", `<span class="mono">${h(item.resource_id || "—")}</span>`, true)}${detailField("来源 IP", item.ip_address || "—")}${detailField("发生时间", formatFullDate(item.created_at))}${item.trace_id ? detailField("Trace ID", `<span class="mono">${h(item.trace_id)}</span>`, true, true) : ""}</div><section class="detail-section"><h3 class="detail-section-title">脱敏变更摘要</h3><pre class="code-block">${h(JSON.stringify(changes, null, 2))}</pre></section>`);
  }

  async function renderSettings(id, signal) {
    setPage(pageHeading("系统设置", "查看并调整后台允许修改的非敏感运行参数") + loadingPanel());
    let payload;
    try { payload = unwrap(await api.get("/settings", null, { signal })); }
    catch (error) {
      if (error.status === 404) {
        if (!stillRendering(id)) return;
        setPage(`${pageHeading("系统设置", "查看并调整后台允许修改的非敏感运行参数")}<section class="panel">${emptyState("设置接口尚未启用", "当前版本仍以安全默认值运行。微信连接和用户策略可以在各自页面管理。")}</section>`); return;
      }
      throw error;
    }
    if (!stillRendering(id)) return;
    const settings = payload?.settings || payload?.values || payload || {};
    const capabilities = payload?.capabilities || {};
    const writable = new Set(payload?.writable_fields || payload?.editable_fields || []);
    state.settingsPayload = { settings, writable };
    const entries = Object.entries(settings).filter(([key]) => !["capabilities", "writable_fields", "editable_fields"].includes(key));
    const rows = entries.length ? entries.map(([key, value]) => settingRow(key, value, writable.has(key))).join("") : `<div class="panel-body">${emptyState("没有公开设置", "后台没有返回可安全展示的运行参数。")}</div>`;
    const capRows = Object.entries(capabilities).map(([key, value]) => `<div class="setting-row"><div class="setting-copy"><strong>${h(labelize(key))}</strong><small>${h(key)}</small></div><div>${typeof value === "boolean" ? boolLabel(value) : `<span class="tag">${h(typeof value === "object" ? JSON.stringify(value) : value)}</span>`}</div></div>`).join("");
    setPage(`${pageHeading("系统设置", "查看并调整后台允许修改的非敏感运行参数", writable.size && can("settings.write") ? '<button class="btn btn-primary" data-action="save-settings">保存设置</button>' : '<span class="badge badge-outline">当前只读</span>')}
      <div class="setting-layout"><nav class="setting-nav"><button class="active" type="button">运行参数</button><button type="button" data-action="scroll-setting" data-target="capability-settings">系统能力</button><button type="button" data-action="scroll-setting" data-target="security-notice">安全说明</button></nav><div class="setting-sections">
        <section class="panel setting-section"><div class="panel-header"><div><h3>运行参数</h3><p>只有后端白名单中的字段可以修改</p></div></div><form id="settings-form">${rows}</form></section>
        <section id="capability-settings" class="panel setting-section"><div class="panel-header"><div><h3>系统能力</h3><p>根据当前版本和部署环境检测</p></div></div>${capRows || `<div class="panel-body muted">没有能力信息。</div>`}</section>
        <section id="security-notice" class="panel setting-section"><div class="panel-header"><div><h3>安全边界</h3><p>敏感配置不在本页面回显</p></div></div><div class="panel-body"><div class="form-alert">Bot Secret、DeepSeek API Key、登录令牌和 Cookie 不会通过设置接口返回。需要轮换企业微信 Secret 时，请在“微信连接”页面提交新值；保存后无法读取原值。</div></div></section>
      </div></div>`);
  }

  function labelize(key) {
    const known = { bind_host: "监听地址", bind_port: "管理端口", log_level: "日志级别", log_retention_days: "日志保留天数", task_timeout_seconds: "任务超时（秒）", progress_interval_seconds: "进度提示间隔（秒）", max_page_size: "最大分页数量", database_mode: "数据库模式", remote_access_enabled: "允许远程访问", secure_cookie: "安全 Cookie", session_ttl_minutes: "登录有效期（分钟）" };
    return known[key] || key.replaceAll("_", " ");
  }

  function settingRow(key, value, editable) {
    let control;
    if (!editable) control = typeof value === "boolean" ? boolLabel(value) : `<span class="tag mono">${h(typeof value === "object" ? JSON.stringify(value) : value)}</span>`;
    else if (typeof value === "boolean") control = `<label class="switch"><input data-setting-key="${h(key)}" data-setting-type="boolean" type="checkbox" ${value ? "checked" : ""}><span></span></label>`;
    else if (typeof value === "number") control = `<input class="filter-control" data-setting-key="${h(key)}" data-setting-type="number" type="number" value="${h(value)}">`;
    else control = `<input class="filter-control" data-setting-key="${h(key)}" data-setting-type="string" value="${h(value ?? "")}">`;
    return `<div class="setting-row"><div class="setting-copy"><strong>${h(labelize(key))}</strong><small>${h(key)}${editable ? " · 可修改" : " · 只读"}</small></div><div>${control}</div></div>`;
  }

  async function saveSettings() {
    const form = $("#settings-form"); if (!form) return;
    const changes = {};
    $$('[data-setting-key]', form).forEach((input) => {
      const type = input.dataset.settingType;
      changes[input.dataset.settingKey] = type === "boolean" ? input.checked : type === "number" ? Number(input.value) : input.value;
    });
    if (!Object.keys(changes).length) return showToast("没有可修改字段", "当前部署只提供设置查看能力。", "warning");
    try { await api.patch("/settings", changes); showToast("设置已保存", "新的非敏感运行参数已经生效。", "success"); renderCurrentRoute(); }
    catch (error) { showToast("设置保存失败", error.message, "error"); }
  }

})();
