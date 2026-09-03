(function () {
  "use strict";

  const API_BASE = String(window.__ADMIN_API_BASE__ || "/api/admin/v1").replace(/\/$/, "");
  const SESSION_KEYS = {
    token: "wecom.admin.access_token",
    csrf: "wecom.admin.csrf_token",
    eventSequence: "wecom.admin.event_sequence",
  };

  class ApiError extends Error {
    constructor(message, options) {
      super(message || "请求失败");
      this.name = "ApiError";
      this.status = options?.status || 0;
      this.code = options?.code || "REQUEST_FAILED";
      this.requestId = options?.requestId || "";
      this.details = options?.details;
      this.isNetworkError = Boolean(options?.isNetworkError);
    }
  }

  function storageGet(key) {
    try { return sessionStorage.getItem(key) || ""; } catch (_) { return ""; }
  }

  function storageSet(key, value) {
    try {
      if (value) sessionStorage.setItem(key, value);
      else sessionStorage.removeItem(key);
    } catch (_) { /* Storage can be disabled without breaking cookie auth. */ }
  }

  function uuid() {
    if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }

  function queryString(query) {
    const params = new URLSearchParams();
    Object.entries(query || {}).forEach(([key, value]) => {
      if (value === undefined || value === null || value === "") return;
      if (Array.isArray(value)) value.forEach((item) => params.append(key, item));
      else params.set(key, String(value));
    });
    const text = params.toString();
    return text ? `?${text}` : "";
  }

  function extractError(payload, response) {
    const detail = payload?.detail;
    if (detail && typeof detail === "object") {
      return {
        message: detail.message || detail.msg || "请求未能完成",
        code: detail.code || `HTTP_${response.status}`,
        requestId: detail.request_id || response.headers.get("X-Request-ID") || "",
        details: detail,
      };
    }
    if (typeof detail === "string") {
      return {
        message: detail,
        code: `HTTP_${response.status}`,
        requestId: response.headers.get("X-Request-ID") || "",
        details: payload,
      };
    }
    if (Array.isArray(detail)) {
      const message = detail.map((item) => item.msg || item.message).filter(Boolean).join("；");
      return {
        message: message || "提交的数据不符合要求",
        code: "VALIDATION_ERROR",
        requestId: response.headers.get("X-Request-ID") || "",
        details: detail,
      };
    }
    return {
      message: payload?.message || `请求失败（HTTP ${response.status}）`,
      code: payload?.code || `HTTP_${response.status}`,
      requestId: payload?.request_id || response.headers.get("X-Request-ID") || "",
      details: payload,
    };
  }

  class AdminApiClient {
    constructor(baseUrl) {
      this.baseUrl = baseUrl;
      this.csrfToken = storageGet(SESSION_KEYS.csrf);
      this.accessToken = storageGet(SESSION_KEYS.token);
    }

    setAuth(data) {
      if (data && Object.prototype.hasOwnProperty.call(data, "csrf_token")) this.csrfToken = data.csrf_token || "";
      if (data && Object.prototype.hasOwnProperty.call(data, "access_token")) this.accessToken = data.access_token || "";
      else if (data?.token_type === "cookie") this.accessToken = "";
      storageSet(SESSION_KEYS.csrf, this.csrfToken);
      storageSet(SESSION_KEYS.token, this.accessToken);
    }

    clearAuth() {
      this.csrfToken = "";
      this.accessToken = "";
      storageSet(SESSION_KEYS.csrf, "");
      storageSet(SESSION_KEYS.token, "");
    }

    async request(path, options) {
      const opts = options || {};
      const method = String(opts.method || "GET").toUpperCase();
      const headers = new Headers(opts.headers || {});
      const isMutation = !["GET", "HEAD", "OPTIONS"].includes(method);
      headers.set("Accept", "application/json");
      headers.set("X-Request-ID", opts.requestId || uuid());
      if (this.accessToken) headers.set("Authorization", `Bearer ${this.accessToken}`);
      else if (isMutation && this.csrfToken) headers.set("X-CSRF-Token", this.csrfToken);
      if (isMutation && opts.idempotent !== false) headers.set("Idempotency-Key", opts.idempotencyKey || uuid());

      let body = opts.body;
      if (body !== undefined && body !== null && !(body instanceof FormData) && typeof body !== "string") {
        headers.set("Content-Type", "application/json");
        body = JSON.stringify(body);
      }

      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), opts.timeout || 20000);
      if (opts.signal) {
        if (opts.signal.aborted) controller.abort();
        else opts.signal.addEventListener("abort", () => controller.abort(), { once: true });
      }

      let response;
      try {
        response = await fetch(`${this.baseUrl}${path}${queryString(opts.query)}`, {
          method,
          headers,
          body,
          credentials: "same-origin",
          signal: controller.signal,
          cache: method === "GET" ? "no-store" : "default",
        });
      } catch (error) {
        clearTimeout(timeout);
        const timedOut = error?.name === "AbortError";
        throw new ApiError(timedOut ? "请求超时，请检查后台服务状态" : "无法连接后台服务", {
          status: 0,
          code: timedOut ? "REQUEST_TIMEOUT" : "NETWORK_ERROR",
          isNetworkError: true,
          details: error,
        });
      } finally {
        clearTimeout(timeout);
      }

      let payload = null;
      if (response.status !== 204) {
        const contentType = response.headers.get("content-type") || "";
        try {
          payload = contentType.includes("application/json") ? await response.json() : await response.text();
        } catch (_) {
          payload = null;
        }
      }

      if (!response.ok) {
        const info = extractError(payload, response);
        const error = new ApiError(info.message, { status: response.status, ...info });
        if ((response.status === 401 || (response.status === 403 && info.code === "CSRF_INVALID")) && opts.authEvent !== false) {
          this.clearAuth();
          window.dispatchEvent(new CustomEvent("admin:unauthorized", { detail: error }));
        }
        throw error;
      }

      return payload;
    }

    get(path, query, options) { return this.request(path, { ...(options || {}), query }); }
    post(path, body, options) { return this.request(path, { ...(options || {}), method: "POST", body }); }
    patch(path, body, options) { return this.request(path, { ...(options || {}), method: "PATCH", body }); }
    delete(path, options) { return this.request(path, { ...(options || {}), method: "DELETE" }); }
  }

  class AdminEventStream {
    constructor(apiClient) {
      this.api = apiClient;
      this.source = null;
      this.retryTimer = null;
      this.retries = 0;
      this.closed = true;
      this.listeners = new Set();
      this.lastSequence = Number(storageGet(SESSION_KEYS.eventSequence) || 0);
    }

    subscribe(listener) {
      this.listeners.add(listener);
      return () => this.listeners.delete(listener);
    }

    emit(type, detail) {
      this.listeners.forEach((listener) => {
        try { listener(type, detail); } catch (error) { console.error("Event listener failed", error); }
      });
    }

    connect() {
      this.close(false);
      this.closed = false;
      if (!("EventSource" in window) || this.api.accessToken) {
        this.emit("unsupported", { reason: this.api.accessToken ? "token-auth" : "browser" });
        return;
      }
      const after = Number.isFinite(this.lastSequence) ? this.lastSequence : 0;
      const url = `${this.api.baseUrl}/events/stream?after=${encodeURIComponent(after)}`;
      const source = new EventSource(url, { withCredentials: true });
      this.source = source;
      source.onopen = () => {
        this.retries = 0;
        this.emit("connected", {});
      };
      source.onmessage = (event) => this.handleEvent(event);
      [
        "message.received", "message.outbound",
        "task.started", "task.completed", "task.failed", "task.cancelled", "task.timeout", "task.progress",
        "tool.started", "tool.completed", "tool.failed",
        "artifact.created", "artifact.delivery.started", "artifact.delivery.succeeded", "artifact.delivery.failed",
        "connection.created", "connection.updated", "connection.activated", "connection.status_changed", "connection.connecting", "connection.authenticated", "connection.online", "connection.heartbeat", "connection.degraded", "connection.reconnecting", "connection.disconnected", "connection.failed",
        "node.heartbeat", "node.online", "node.offline", "service.heartbeat", "service.healthy", "service.unhealthy", "service.health_changed",
        "alert.created", "system.error", "agent.notification", "log.created", "log.python",
      ].forEach((name) => {
        source.addEventListener(name, (event) => this.handleEvent(event));
      });
      source.onerror = () => {
        source.close();
        if (this.source === source) this.source = null;
        this.emit("disconnected", { retries: this.retries });
        if (!this.closed) this.scheduleReconnect();
      };
    }

    handleEvent(event) {
      let data;
      try { data = JSON.parse(event.data); } catch (_) { return; }
      const sequence = Number(data?.seq || event.lastEventId || 0);
      if (sequence > this.lastSequence) {
        this.lastSequence = sequence;
        storageSet(SESSION_KEYS.eventSequence, String(sequence));
      }
      this.emit("event", data);
    }

    scheduleReconnect() {
      clearTimeout(this.retryTimer);
      const delay = Math.min(30000, 1000 * (2 ** Math.min(this.retries++, 5))) + Math.floor(Math.random() * 400);
      this.retryTimer = setTimeout(() => this.connect(), delay);
    }

    close(markClosed = true) {
      if (markClosed) this.closed = true;
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
      if (this.source) this.source.close();
      this.source = null;
    }
  }

  function asPage(payload) {
    if (Array.isArray(payload)) return { items: payload, page: 1, page_size: payload.length, total: payload.length };
    const root = payload?.data && !Array.isArray(payload.data) ? payload.data : payload;
    const items = root?.items || root?.results || root?.data || [];
    return {
      items: Array.isArray(items) ? items : [],
      page: Number(root?.page || 1),
      page_size: Number(root?.page_size || root?.pageSize || Math.max(items?.length || 0, 20)),
      total: Number(root?.total ?? root?.count ?? items?.length ?? 0),
    };
  }

  function unwrap(payload) {
    return payload?.data !== undefined ? payload.data : payload;
  }

  const api = new AdminApiClient(API_BASE);
  window.AdminConsole = Object.freeze({
    api,
    ApiError,
    AdminEventStream,
    asPage,
    unwrap,
    queryString,
    uuid,
    API_BASE,
  });
})();
