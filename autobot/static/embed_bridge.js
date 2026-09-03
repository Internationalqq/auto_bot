(function () {
  "use strict";

  var embedded = window.parent !== window;
  if (!embedded) {
    window.AutoBotCrmBridge = { embedded: false, available: false };
    return;
  }

  var configuredOriginNode = document.querySelector('meta[name="autobot-parent-origin"]');
  var trustedParentOrigin = normalizeWebOrigin(configuredOriginNode && configuredOriginNode.content);
  var referrerOrigin = normalizeWebOrigin(document.referrer);
  if (referrerOrigin === window.location.origin) referrerOrigin = "";
  var originMismatch = Boolean(trustedParentOrigin && referrerOrigin && trustedParentOrigin !== referrerOrigin);
  var scrollTargetOrigin = trustedParentOrigin || referrerOrigin || "*";
  var pending = Object.create(null);
  var lastScrolled = null;
  var scheduled = false;

  function normalizeWebOrigin(value) {
    try {
      var parsed = new URL(String(value || ""), window.location.href);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "";
      return parsed.origin === "null" ? "" : parsed.origin;
    } catch (error) {
      return "";
    }
  }

  function postTrusted(message) {
    if (!trustedParentOrigin) {
      throw new Error("Для AutoBot не настроен адрес PM.bi.");
    }
    if (originMismatch) {
      throw new Error("Адрес PM.bi не совпадает с настройкой AutoBot.");
    }
    window.parent.postMessage(message, trustedParentOrigin);
  }

  function newRequestId() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return "autobot-" + window.crypto.randomUUID();
      }
    } catch (error) {}
    return "autobot-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 14);
  }

  function requestFromParent(requestType, resultType, fields, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var requestId = newRequestId();
      var timeout = window.setTimeout(function () {
        delete pending[requestId];
        reject(new Error("PM.bi не подтвердила результат вовремя. Откройте объект и проверьте сметы перед повтором."));
      }, Math.max(3000, Number(timeoutMs || 20000)));
      pending[requestId] = {
        resultType: resultType,
        resolve: resolve,
        reject: reject,
        timeout: timeout,
      };
      try {
        postTrusted(Object.assign({ type: requestType, requestId: requestId }, fields || {}));
      } catch (error) {
        window.clearTimeout(timeout);
        delete pending[requestId];
        reject(error);
      }
    });
  }

  function handleParentMessage(event) {
    if (event.source !== window.parent) return;
    if (!trustedParentOrigin || originMismatch || event.origin !== trustedParentOrigin) return;

    var data = event.data;
    if (!data || typeof data !== "object" || Array.isArray(data)) return;
    var requestId = typeof data.requestId === "string" ? data.requestId : "";
    var waiting = requestId ? pending[requestId] : null;
    if (!waiting || data.type !== waiting.resultType) return;

    window.clearTimeout(waiting.timeout);
    delete pending[requestId];
    if (data.ok !== true) {
      waiting.reject(new Error(String(data.message || "PM.bi вернула некорректный ответ.")));
      return;
    }
    waiting.resolve(data);
  }

  function isValidImportPayload(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
    if (Array.isArray(payload.items) && payload.items.length > 0) return true;
    return Array.isArray(payload.estimates) && payload.estimates.length > 0 && payload.estimates.every(function (estimate) {
      return estimate && typeof estimate === "object" && !Array.isArray(estimate)
        && estimate.source && typeof estimate.source === "object" && !Array.isArray(estimate.source)
        && Array.isArray(estimate.items) && estimate.items.length > 0;
    });
  }

  function importEstimatePayload(projectId, payload) {
    var normalizedProjectId = Number(projectId);
    if (!Number.isInteger(normalizedProjectId) || normalizedProjectId <= 0) {
      return Promise.reject(new Error("Выберите объект PM.bi."));
    }
    if (!isValidImportPayload(payload)) {
      return Promise.reject(new Error("AutoBot не подготовил данные сметы."));
    }
    return requestFromParent(
      "autobot:crm-estimate-import",
      "autobot:crm-estimate-import-result",
      { projectId: normalizedProjectId, payload: payload },
      120000
    );
  }

  function publishScrollState() {
    scheduled = false;
    var root = document.scrollingElement || document.documentElement;
    var scrollTop = Math.max(Number(window.scrollY || 0), Number(root && root.scrollTop || 0));
    var scrolled = scrollTop > 24;
    if (scrolled === lastScrolled) return;
    lastScrolled = scrolled;
    window.parent.postMessage({ type: "autobot:scroll", scrolled: scrolled }, scrollTargetOrigin);
  }

  function schedulePublish() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(publishScrollState);
  }

  window.addEventListener("message", handleParentMessage);
  window.addEventListener("scroll", schedulePublish, { passive: true });
  window.addEventListener("pageshow", schedulePublish);
  document.addEventListener("DOMContentLoaded", schedulePublish, { once: true });

  window.AutoBotCrmBridge = {
    embedded: true,
    available: Boolean(trustedParentOrigin && !originMismatch),
    originMismatch: originMismatch,
    parentOrigin: trustedParentOrigin,
    requestProjects: function () {
      return requestFromParent(
        "autobot:crm-projects-request",
        "autobot:crm-projects-result",
        {},
        15000
      );
    },
    importEstimate: importEstimatePayload,
    importEstimates: importEstimatePayload,
    setModalOpen: function (open) {
      try {
        postTrusted({ type: "autobot:feature-modal", open: open === true });
        return true;
      } catch (error) {
        return false;
      }
    },
    navigate: function (href) {
      var safeHref = String(href || "");
      if (!safeHref.startsWith("/app/projects")) return false;
      try {
        postTrusted({ type: "pmbi:navigate", href: safeHref });
        return true;
      } catch (error) {
        return false;
      }
    },
  };

  schedulePublish();
})();
