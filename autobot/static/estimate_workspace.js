const estimatePage = JSON.parse(document.getElementById("estimatePageConfig").textContent);
const estimateMarketRenderRevision = estimatePage.marketRevision;
let estimateMarketStatusPending = false;
let estimateMarketRunId = null;
const estimateMarketStorageKey = 'autobot:market-start:' + estimatePage.estimateId;
let estimateMarketPendingStart = null;
try {
  const saved = JSON.parse(window.sessionStorage.getItem(estimateMarketStorageKey) || 'null');
  if (saved && /^[a-f0-9]{32}$/.test(saved.id || '') && typeof saved.fingerprint === 'string') estimateMarketPendingStart = saved;
} catch (_) {}
function rememberEstimateMarketStart(value) {
  estimateMarketPendingStart = value;
  try {
    if (value) window.sessionStorage.setItem(estimateMarketStorageKey, JSON.stringify(value));
    else window.sessionStorage.removeItem(estimateMarketStorageKey);
  } catch (_) {}
}
    let estimateMarketReloadPending = false;
    let estimateCrmDrawerTimer = null;
    let estimateCrmProjectsLoaded = false;
    const estimateCrmPrefill = estimatePage.crmPrefill;
    const estimateCrmBridge = window.AutoBotCrmBridge || { embedded: false, available: false };
    const estimateCrmEmbedded = Boolean(estimateCrmBridge.embedded);
    const estimateCrmLegacyAllowed = Boolean(estimatePage.legacyCrmAllowed);
    const estimateImportCapability = estimatePage.importCapability;

    function setEstimateCrmStatus(message, tone) {
      const box = document.getElementById("estimateCrmStatus");
      if (!box) return;
      box.textContent = message || "";
      box.classList.toggle("is-error", tone === "error");
      box.classList.toggle("is-success", tone === "success");
    }

    function fillEstimateCrmForm(data) {
      const project = data && data.project ? data.project : {};
      const budget = project.budget == null ? "" : String(project.budget);
      const map = {
        estimateCrmTitle: project.title || "",
        estimateCrmClient: project.client_name || "",
        estimateCrmAddress: project.address || "",
        estimateCrmRegion: project.region || "",
        estimateCrmContractNo: project.contract_no || "",
        estimateCrmBudget: budget,
        estimateCrmDescription: project.description || "",
      };
      Object.entries(map).forEach(([id, value]) => {
        const node = document.getElementById(id);
        if (node) node.value = value;
      });
    }

    function getEstimateCrmFieldValue(id) {
      const node = document.getElementById(id);
      return node ? (node.value || "") : "";
    }

    function syncEstimateCrmMode() {
      const select = document.getElementById("estimateCrmProject");
      const submit = document.getElementById("estimateCrmSubmitBtn");
      const hint = document.getElementById("estimateCrmProjectHint");
      const newProjectFields = document.getElementById("estimateCrmNewProjectFields");
      const titleInput = document.getElementById("estimateCrmTitle");
      const adding = Boolean(select && select.value);
      const canCreate = !estimateCrmEmbedded && estimateCrmLegacyAllowed;
      if (newProjectFields) newProjectFields.hidden = !canCreate || adding;
      if (titleInput) titleInput.required = canCreate && !adding;
      if (estimateCrmEmbedded && select && select.options.length) select.options[0].textContent = "Выберите объект";
      if (submit) {
        submit.textContent = !canCreate || adding ? "Добавить смету" : "Создать объект";
        submit.disabled = !adding && !canCreate;
      }
      if (hint && estimateCrmProjectsLoaded) {
        if (estimateCrmEmbedded) {
          hint.textContent = adding
            ? "Смета будет добавлена к выбранному объекту с вашими правами PM.bi."
            : "Выберите объект, в который нужно добавить смету.";
        } else {
          hint.textContent = adding
            ? "Смета будет добавлена отдельным файлом к выбранному объекту."
            : "Будет создан новый объект с данными из этой сметы.";
        }
      }
    }

    async function loadEstimateCrmProjects() {
      if (estimateCrmProjectsLoaded) return;
      const select = document.getElementById("estimateCrmProject");
      const hint = document.getElementById("estimateCrmProjectHint");
      if (!select) return;
      try {
        let data = {};
        if (estimateCrmEmbedded) {
          if (!estimateCrmBridge.available) {
            throw new Error(estimateCrmBridge.originMismatch
              ? "Адрес PM.bi не совпадает с настройкой AutoBot."
              : "На сервере AutoBot не настроен адрес PM.bi.");
          }
          data = await estimateCrmBridge.requestProjects();
        } else {
          if (!estimateCrmLegacyAllowed) {
            throw new Error("Откройте AutoBot внутри PM.bi, чтобы выбрать доступный объект.");
          }
          const response = await fetch("/api/tenders/crm/projects", { headers: { "Accept": "application/json" }, cache: "no-store" });
          data = await response.json().catch(function() { return {}; });
          if (!response.ok || !data.ok) throw new Error(data.message || ("HTTP " + response.status));
        }
        const projects = Array.isArray(data.projects) ? data.projects : [];
        projects.forEach(function(project) {
          const projectId = Number(project && project.id);
          if (!Number.isInteger(projectId) || projectId <= 0) return;
          const option = document.createElement("option");
          option.value = String(projectId);
          const contractNo = project.contract_no || project.contractNo || "";
          option.textContent = "#" + projectId + " · " + (project.title || "Без названия") + (contractNo ? " · " + contractNo : "");
          select.appendChild(option);
        });
        estimateCrmProjectsLoaded = true;
        if (hint) {
          hint.textContent = projects.length
            ? (estimateCrmEmbedded ? "Выберите доступный вам объект." : "Выберите объект или создайте новый.")
            : (estimateCrmEmbedded ? "У вас пока нет доступных объектов." : "Доступных объектов пока нет — будет создан новый.");
        }
        syncEstimateCrmMode();
      } catch (error) {
        if (hint) hint.textContent = "Не удалось загрузить объекты: " + (error.message || error);
      }
    }

    function navigateEstimateCrmProject(url) {
      if (!url) return;
      if (estimateCrmEmbedded) {
        if (!estimateCrmBridge.navigate(url)) {
          setEstimateCrmStatus("Смета добавлена. Откройте объект в PM.bi.", "success");
        }
        return;
      }
      window.location.href = url;
    }

    window.openEstimateCrmDrawer = function() {
      const drawer = document.getElementById("estimateCrmDrawer");
      if (!drawer) return;
      if (estimateCrmDrawerTimer) {
        clearTimeout(estimateCrmDrawerTimer);
        estimateCrmDrawerTimer = null;
      }
      fillEstimateCrmForm(estimateCrmPrefill);
      setEstimateCrmStatus(
        estimateCrmEmbedded
          ? "Выберите объект PM.bi для сметы «" + (estimateCrmPrefill.estimate_title || "") + "»."
          : (estimateCrmLegacyAllowed
            ? "На основе сметы «" + (estimateCrmPrefill.estimate_title || "") + "»."
            : "Для добавления сметы откройте AutoBot внутри PM.bi."),
        estimateCrmEmbedded || estimateCrmLegacyAllowed ? "" : "error"
      );
      drawer.hidden = false;
      requestAnimationFrame(() => {
        drawer.classList.add("is-open");
        const close = document.getElementById("estimateCrmCloseBtn");
        if (close) close.focus();
      });
      document.body.style.overflow = "hidden";
      syncEstimateCrmMode();
      loadEstimateCrmProjects();
    };

    window.closeEstimateCrmDrawer = function() {
      const drawer = document.getElementById("estimateCrmDrawer");
      if (!drawer) return;
      drawer.classList.remove("is-open");
      if (estimateCrmDrawerTimer) clearTimeout(estimateCrmDrawerTimer);
      estimateCrmDrawerTimer = setTimeout(() => {
        drawer.hidden = true;
        estimateCrmDrawerTimer = null;
      }, 320);
      document.body.style.overflow = "";
      const opener = document.getElementById("estimateCrmOpenBtn");
      if (opener) opener.focus();
    };

    window.submitEstimateCrmForm = async function(event) {
      event.preventDefault();
      const submitBtn = document.getElementById("estimateCrmSubmitBtn");
      const payload = {
        project_id: getEstimateCrmFieldValue("estimateCrmProject") || null,
        title: getEstimateCrmFieldValue("estimateCrmTitle"),
        client_name: getEstimateCrmFieldValue("estimateCrmClient"),
        address: getEstimateCrmFieldValue("estimateCrmAddress"),
        region: getEstimateCrmFieldValue("estimateCrmRegion"),
        contract_no: getEstimateCrmFieldValue("estimateCrmContractNo"),
        budget: getEstimateCrmFieldValue("estimateCrmBudget"),
        description: getEstimateCrmFieldValue("estimateCrmDescription"),
      };
      const addingToExisting = Boolean(payload.project_id);
      if (estimateCrmEmbedded && !addingToExisting) {
        setEstimateCrmStatus("Выберите объект PM.bi.", "error");
        return;
      }
      if (!estimateCrmEmbedded && !estimateCrmLegacyAllowed) {
        setEstimateCrmStatus("Откройте AutoBot внутри PM.bi — так импорт выполнится с вашими правами.", "error");
        return;
      }
      if (submitBtn) submitBtn.disabled = true;
      setEstimateCrmStatus(addingToExisting ? "Добавляю смету в выбранный объект…" : "Создаю объект в CRM…", "");
      try {
        if (estimateCrmEmbedded) {
          setEstimateCrmStatus("Подготавливаю позиции сметы…", "");
          const payloadResponse = await fetch(("/api/estimates/" + encodeURIComponent(estimatePage.estimateId) + "/crm-import-payload"), {
            method: "GET",
            headers: {
              "Accept": "application/json",
              "X-AutoBot-Estimate-Capability": estimateImportCapability,
            },
            cache: "no-store",
            credentials: "same-origin",
          });
          const prepared = await payloadResponse.json().catch(function() { return {}; });
          if (!payloadResponse.ok || !prepared.ok) {
            throw new Error(prepared.message || ("Не удалось подготовить смету (HTTP " + payloadResponse.status + ")."));
          }
          const importPayload = {
            items: Array.isArray(prepared.items) ? prepared.items : [],
            source: prepared.source || {},
            sourceLabel: prepared.sourceLabel || prepared.label || "Смета",
            sourceReference: prepared.sourceReference || prepared.reference || "",
            replace_source: prepared.replace_source !== false,
          };
          setEstimateCrmStatus("Передаю смету в PM.bi с вашими правами…", "");
          const bridgeResponse = await estimateCrmBridge.importEstimate(payload.project_id, importPayload);
          const data = bridgeResponse.result && typeof bridgeResponse.result === "object"
            ? bridgeResponse.result
            : bridgeResponse;
          const projectId = Number(data.project_id || data.projectId || payload.project_id);
          const materialsSent = Number(data.materials_sent || data.materialsSent || data.imported || importPayload.items.length);
          const projectUrl = data.project_url || data.projectUrl || (projectId > 0 ? "/app/projects?openProject=" + projectId + "&tab=schedule" : "");
          setEstimateCrmStatus("Готово: смета добавлена в объект #" + projectId + ". Строк обработано: " + materialsSent + ".", "success");
          if (projectUrl) window.setTimeout(function() { navigateEstimateCrmProject(projectUrl); }, 350);
          return;
        }
        const resp = await fetch(("/api/estimates/" + encodeURIComponent(estimatePage.estimateId) + "/export-to-crm"), {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify(payload),
        });
        let data = {};
        try { data = await resp.json(); } catch (e) {}
        if (!resp.ok || !data.ok) {
          setEstimateCrmStatus(data.message || ("Не удалось добавить смету (HTTP " + resp.status + ")."), "error");
          return;
        }
        if (data.added_to_existing) {
          setEstimateCrmStatus("Готово: смета добавлена в объект #" + data.project_id + ". Строк обновлено: " + (data.materials_sent || 0) + ".", "success");
          if (data.project_url) navigateEstimateCrmProject(data.project_url);
          return;
        }
        const summary = data.summary || {};
        setEstimateCrmStatus("Готово: объект #" + data.project_id + " создан. Материалов отправлено: " + (data.materials_sent || 0) + ".", "success");
        if (data.project_url) navigateEstimateCrmProject(data.project_url);
      } catch (e) {
        setEstimateCrmStatus("Не удалось отправить данные в CRM: " + e, "error");
      } finally {
        if (submitBtn) syncEstimateCrmMode();
      }
    };

    const estimateCrmOpenBtn = document.getElementById("estimateCrmOpenBtn");
    if (estimateCrmOpenBtn) estimateCrmOpenBtn.addEventListener("click", window.openEstimateCrmDrawer);
    const estimateCrmBackdrop = document.getElementById("estimateCrmBackdrop");
    if (estimateCrmBackdrop) estimateCrmBackdrop.addEventListener("click", window.closeEstimateCrmDrawer);
    const estimateCrmCloseBtn = document.getElementById("estimateCrmCloseBtn");
    if (estimateCrmCloseBtn) estimateCrmCloseBtn.addEventListener("click", window.closeEstimateCrmDrawer);
    const estimateCrmCancelBtn = document.getElementById("estimateCrmCancelBtn");
    if (estimateCrmCancelBtn) estimateCrmCancelBtn.addEventListener("click", window.closeEstimateCrmDrawer);
    const estimateCrmForm = document.getElementById("estimateCrmForm");
    if (estimateCrmForm) estimateCrmForm.addEventListener("submit", window.submitEstimateCrmForm);
    const estimateCrmProject = document.getElementById("estimateCrmProject");
    if (estimateCrmProject) estimateCrmProject.addEventListener("change", syncEstimateCrmMode);
    const estimateCrmDrawer = document.getElementById("estimateCrmDrawer");
    if (estimateCrmDrawer) estimateCrmDrawer.addEventListener("keydown", function(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        window.closeEstimateCrmDrawer();
      } else if (event.key === "Tab") {
        const nodes = Array.from(estimateCrmDrawer.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]'))
          .filter((node) => node.getClientRects().length);
        const target = event.shiftKey ? nodes[nodes.length - 1] : nodes[0];
        if (nodes.length && document.activeElement === (event.shiftKey ? nodes[0] : nodes[nodes.length - 1])) {
          event.preventDefault();
          target.focus();
        }
      }
    });

    async function deleteEstimate(btn) {
      const title = String(estimatePage.title);
      const ok = confirm(`Удалить смету "${title}"?

Будут удалены карточка сметы, её строки и все сохранённые файлы рынка по этой смете.`);
      if (!ok) return;
      const initialHtml = btn ? btn.innerHTML : "";
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = "...";
      }
      try {
        const resp = await fetch(("/api/estimates/" + encodeURIComponent(estimatePage.estimateId) + "/delete"), {
          method: "POST",
          headers: { "Accept": "application/json" },
        });
        let data = {};
        try { data = await resp.json(); } catch (e) {}
        if (!resp.ok || !data.ok) {
          alert(data.message || ("Не удалось удалить смету (HTTP " + resp.status + ")."));
          if (btn) {
            btn.disabled = false;
            btn.innerHTML = initialHtml;
          }
          return;
        }
        window.location.href = "/estimates";
      } catch (e) {
        alert("Не удалось удалить смету: " + e);
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = initialHtml;
        }
      }
    }

    function setEstimateTableView(viewKey) {
      const buttons = Array.from(document.querySelectorAll("[data-estimate-view-btn]"));
      const panels = Array.from(document.querySelectorAll("[data-estimate-view-panel]"));
      const activeBtn = buttons.find((btn) => btn.getAttribute("data-estimate-view-btn") === viewKey && !btn.disabled) || buttons.find((btn) => !btn.disabled);
      const nextKey = activeBtn ? activeBtn.getAttribute("data-estimate-view-btn") : "estimate";
      buttons.forEach((btn) => {
        btn.classList.toggle("is-active", btn === activeBtn);
        btn.setAttribute("aria-selected", String(btn === activeBtn));
        btn.tabIndex = btn === activeBtn ? 0 : -1;
      });
      panels.forEach((panel) => {
        panel.hidden = panel.getAttribute("data-estimate-view-panel") !== nextKey;
      });
      const hiddenInput = document.getElementById("estimateTableViewInput");
      if (hiddenInput) hiddenInput.value = nextKey;
      const downloadBtn = document.getElementById("activeTableDownloadBtn");
      if (downloadBtn && activeBtn) {
        downloadBtn.href = activeBtn.getAttribute("data-download-href") || "#";
        const label = activeBtn.getAttribute("data-download-label") || "";
        downloadBtn.textContent = label ? ("Скачать Excel: " + label) : "Скачать Excel";
      }
    }

    document.querySelectorAll("[data-estimate-view-btn]").forEach((btn) => {
      btn.addEventListener("click", function() {
        if (btn.disabled) return;
        setEstimateTableView(btn.getAttribute("data-estimate-view-btn") || "estimate");
      });
    });

    async function refreshEstimateMarketStatus() {
      if (estimateMarketStatusPending) return;
      estimateMarketStatusPending = true;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const resp = await fetch(("/api/estimates/" + encodeURIComponent(estimatePage.estimateId) + "/market-status"), { signal: controller.signal });
        if (!resp.ok) throw new Error("status_unavailable");
        const data = await resp.json();
        if (data.ok === false) throw new Error('status_unavailable');
        estimateMarketRunId = data.run_id || null;
        if (estimateMarketPendingStart?.id === estimateMarketRunId) rememberEstimateMarketStart(null);
        const main = document.getElementById("marketStatusMain");
        const detail = document.getElementById("marketStatusDetail");
        const logs = document.getElementById("marketLogs");
        const settings = document.getElementById("estimateMarketSettings");
        const statusKey = String(data.run_id || "saved") + (data.running ? ":running" : data.error ? ":error" : ":idle");
        if (settings) {
          if (settings.dataset.statusKey !== statusKey && (data.running || data.error)) settings.open = true;
          settings.dataset.statusKey = statusKey;
        }
        const startBtn = document.getElementById("marketStartBtn");
        if (startBtn) {
          startBtn.dataset.running = data.running ? "1" : "0";
          startBtn.disabled = startBtn.dataset.busy === "1";
          startBtn.textContent = data.running ? "Остановить поиск" : "Найти цены";
          startBtn.classList.toggle("is-stop", !!data.running);
        }
        if (!data.running && (data.has_merged || data.has_raw) && data.market_revision
            && data.market_revision !== estimateMarketRenderRevision && !estimateMarketReloadPending) {
          estimateMarketReloadPending = true;
          const nextUrl = new URL(window.location.href);
          nextUrl.searchParams.set("table_view", data.has_merged ? "compare" : "sources");
          window.location.replace(nextUrl.toString());
          return;
        }
        if (main) {
          if (data.running) {
            main.textContent = "Идёт поиск цен: " + (data.done || 0) + " / " + (data.total || 0);
          } else if (data.result_ok) {
            main.textContent = "Поиск рынка завершён.";
          } else if (data.error) {
            main.textContent = "Поиск завершился с ошибкой.";
          } else if (data.canceled) {
            main.textContent = "Поиск остановлен. Уже найденные цены сохранены.";
          } else if (data.has_merged || data.has_raw) {
            main.textContent = "Сохранённые цены доступны во вкладках.";
          } else {
            main.textContent = "Поиск ещё не запускался.";
          }
        }
        if (detail) {
          const bits = [];
          if (data.stage) bits.push(data.stage);
          if (data.detail) bits.push(data.detail);
          if (data.error && data.error !== data.detail) bits.push(data.error);
          if (data.city) bits.push("город: " + data.city);
          detail.textContent = bits.join(" · ");
        }
        if (logs) {
          const arr = Array.isArray(data.log_tail) ? data.log_tail : [];
          logs.textContent = arr.length ? arr.join("\n") : "—";
          logs.scrollTop = logs.scrollHeight;
          const logDetails = document.getElementById("marketLogDetails");
          if (logDetails) logDetails.hidden = !arr.length;
        }
      } catch (e) {
        const main = document.getElementById("marketStatusMain");
        if (main) main.textContent = "Не удалось обновить статус. Пробуем снова…";
        const btn = document.getElementById("marketStartBtn");
        if (btn) btn.disabled = true;
      } finally {
        clearTimeout(timeout);
        estimateMarketStatusPending = false;
      }
    }

    async function toggleEstimateMarket() {
      const btn = document.getElementById("marketStartBtn");
      const isRunning = btn && btn.dataset.running === "1";
      if (isRunning) {
        await stopEstimateMarket();
      } else {
        await startEstimateMarket();
      }
    }

    async function startEstimateMarket() {
      const cityInput = document.getElementById("marketCityInput");
      const city = cityInput ? String(cityInput.value || "").trim() : "";
      const selectedTypes = Array.from(document.querySelectorAll('input[name="types"]:checked')).map(x => String(x.value || ""));
      const btn = document.getElementById("marketStartBtn");
      if (btn?.dataset.busy === '1') return;
      const fingerprint = JSON.stringify([city, [...selectedTypes].sort()]);
      if (!estimateMarketPendingStart || estimateMarketPendingStart.fingerprint !== fingerprint) {
        rememberEstimateMarketStart({id: window.crypto.randomUUID().replaceAll('-', ''), fingerprint});
      }
      const operationId = estimateMarketPendingStart.id;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 20000);
      if (btn) {
        btn.dataset.busy = "1";
        btn.disabled = true;
      }
      try {
        const resp = await fetch(("/api/estimates/" + encodeURIComponent(estimatePage.estimateId) + "/market-start"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ city, selected_types: selectedTypes, operation_id: operationId }),
          signal: controller.signal
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
          alert(data.message || "Не удалось запустить поиск рынка");
          if ([400, 409].includes(resp.status)) rememberEstimateMarketStart(null);
        } else if (data.run_id === operationId) {
          rememberEstimateMarketStart(null);
        }
      } catch (e) {
        alert("Ответ о запуске не получен. Проверяем очередь; повтор использует тот же запуск.");
      } finally {
        clearTimeout(timeout);
        if (btn) btn.dataset.busy = "0";
        refreshEstimateMarketStatus();
      }
    }

    async function stopEstimateMarket() {
      const btn = document.getElementById("marketStartBtn");
      if (btn?.dataset.busy === '1') return;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 20000);
      if (btn) {
        btn.dataset.busy = "1";
        btn.disabled = true;
      }
      try {
        const resp = await fetch(("/api/estimates/" + encodeURIComponent(estimatePage.estimateId) + "/market-stop"), {
          method: "POST", headers: {'Content-Type':'application/json'},
          body: JSON.stringify({run_id:estimateMarketRunId}), signal:controller.signal });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
          alert(data.message || "Не удалось остановить поиск");
        }
      } catch (e) {
        alert("Не удалось остановить поиск");
      } finally {
        clearTimeout(timeout);
        if (btn) btn.dataset.busy = "0";
        refreshEstimateMarketStatus();
      }
    }

    document.querySelectorAll("[data-estimate-view-btn]").forEach((btn) => {
      btn.addEventListener("keydown", (event) => {
        const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
        if (!keys.includes(event.key)) return;
        const enabled = Array.from(document.querySelectorAll("[data-estimate-view-btn]")).filter((item) => !item.disabled);
        const current = enabled.indexOf(btn);
        const index = event.key === "Home" ? 0 : event.key === "End" ? enabled.length - 1
          : (current + (event.key === "ArrowRight" ? 1 : -1) + enabled.length) % enabled.length;
        if (!enabled[index]) return;
        event.preventDefault();
        setEstimateTableView(enabled[index].getAttribute("data-estimate-view-btn"));
        enabled[index].focus();
      });
    });
    setEstimateTableView(estimatePage.activeTableView);
    refreshEstimateMarketStatus();
    setInterval(refreshEstimateMarketStatus, 3000);
