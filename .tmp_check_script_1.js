
    let estimateMarketRenderFresh = true;
    let estimateMarketReloadPending = false;
    let estimateCrmDrawerTimer = null;
    const estimateCrmPrefill = {"created_at": "24.07.2026 12:14", "estimate_id": "4db7a218224c4642", "estimate_title": "LSR_po_Metodike_2020_RIM", "original_filename": "LSR_po_Metodike_2020_RIM.xlsx", "project": {"address": "\u0410\u0434\u0440\u0435\u0441 \u0443\u0442\u043e\u0447\u043d\u0438\u0442\u044c \u043f\u043e \u0441\u043c\u0435\u0442\u0435", "budget": 86773378.08000003, "client_name": "\u041e\u0431\u044a\u0435\u043a\u0442 \u043f\u043e \u0441\u043c\u0435\u0442\u0435", "contract_no": "ESTIMATE-4db7a218224c4642", "description": "\u0418\u043c\u043f\u043e\u0440\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u043e \u0438\u0437 auto_bot \u043f\u043e \u043e\u0442\u0434\u0435\u043b\u044c\u043d\u043e\u0439 \u0441\u043c\u0435\u0442\u0435.\n\u0421\u043c\u0435\u0442\u0430: LSR_po_Metodike_2020_RIM\n\u0424\u0430\u0439\u043b: LSR_po_Metodike_2020_RIM.xlsx\n\u0414\u0430\u0442\u0430 \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0438: 24.07.2026 12:14\n\u0421\u0442\u0440\u043e\u043a \u0432 \u0441\u043c\u0435\u0442\u0435: 1097\n\u0421\u043e\u0441\u0442\u0430\u0432: \u0442\u043e\u0432\u0430\u0440\u044b: 153, \u043f\u0440\u043e\u0447\u0435\u0435: 244, \u0440\u0430\u0431\u043e\u0442\u044b: 359, \u043c\u0430\u0442\u0435\u0440\u0438\u0430\u043b\u044b: 330, \u0443\u0441\u043b\u0443\u0433\u0438: 11", "region": "", "title": "LSR_po_Metodike_2020_RIM"}, "row_count": 1097, "total_sum": 86773378.08000003, "total_sum_fmt": "86 773 378,08 \u20bd"};

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

    window.openEstimateCrmDrawer = function() {
      const drawer = document.getElementById("estimateCrmDrawer");
      if (!drawer) return;
      if (estimateCrmDrawerTimer) {
        clearTimeout(estimateCrmDrawerTimer);
        estimateCrmDrawerTimer = null;
      }
      fillEstimateCrmForm(estimateCrmPrefill);
      setEstimateCrmStatus("На основе сметы «" + (estimateCrmPrefill.estimate_title || "") + "». Поля можно поправить перед созданием объекта.", "");
      drawer.hidden = false;
      requestAnimationFrame(() => drawer.classList.add("is-open"));
      document.body.style.overflow = "hidden";
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
    };

    window.submitEstimateCrmForm = async function(event) {
      event.preventDefault();
      const submitBtn = document.getElementById("estimateCrmSubmitBtn");
      if (submitBtn) submitBtn.disabled = true;
      const payload = {
        title: getEstimateCrmFieldValue("estimateCrmTitle"),
        client_name: getEstimateCrmFieldValue("estimateCrmClient"),
        address: getEstimateCrmFieldValue("estimateCrmAddress"),
        region: getEstimateCrmFieldValue("estimateCrmRegion"),
        contract_no: getEstimateCrmFieldValue("estimateCrmContractNo"),
        budget: getEstimateCrmFieldValue("estimateCrmBudget"),
        description: getEstimateCrmFieldValue("estimateCrmDescription"),
      };
      setEstimateCrmStatus("Создаю объект в CRM…", "");
      try {
        const resp = await fetch("/api/estimates/4db7a218224c4642/export-to-crm", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify(payload),
        });
        let data = {};
        try { data = await resp.json(); } catch (e) {}
        if (!resp.ok || !data.ok) {
          setEstimateCrmStatus(data.message || ("Не удалось создать объект (HTTP " + resp.status + ")."), "error");
          return;
        }
        if (data.already_exists) {
          setEstimateCrmStatus("Этот объект уже есть в CRM: #" + data.project_id + ".", "success");
          if (data.project_url) window.open(data.project_url, "_blank", "noopener,noreferrer");
          return;
        }
        const summary = data.summary || {};
        setEstimateCrmStatus("Готово: объект #" + data.project_id + " создан. Материалов отправлено: " + (data.materials_sent || 0) + ".", "success");
        if (data.project_url) window.open(data.project_url, "_blank", "noopener,noreferrer");
      } catch (e) {
        setEstimateCrmStatus("Не удалось отправить данные в CRM: " + e, "error");
      } finally {
        if (submitBtn) submitBtn.disabled = false;
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

    async function deleteEstimate(btn) {
      const title = String("LSR_po_Metodike_2020_RIM");
      const ok = confirm(`Удалить смету "${title}"?

Будут удалены карточка сметы, её строки и все сохранённые файлы рынка по этой смете.`);
      if (!ok) return;
      const initialHtml = btn ? btn.innerHTML : "";
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = "...";
      }
      try {
        const resp = await fetch("/api/estimates/4db7a218224c4642/delete", {
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
      buttons.forEach((btn) => btn.classList.toggle("is-active", btn === activeBtn));
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
      try {
        const resp = await fetch("/api/estimates/4db7a218224c4642/market-status");
        if (!resp.ok) return;
        const data = await resp.json();
        const main = document.getElementById("marketStatusMain");
        const detail = document.getElementById("marketStatusDetail");
        const logs = document.getElementById("marketLogs");
        const startBtn = document.getElementById("marketStartBtn");
        const mergedBtn = document.getElementById("marketMergedBtn");
        const rawBtn = document.getElementById("marketRawBtn");
        const compareBtn = document.querySelector('[data-estimate-view-btn="compare"]');
        const sourcesBtn = document.querySelector('[data-estimate-view-btn="sources"]');
        if (startBtn) {
          startBtn.dataset.running = data.running ? "1" : "0";
          startBtn.disabled = startBtn.dataset.busy === "1";
          startBtn.textContent = data.running ? "Остановить поиск" : "Найти цены";
          startBtn.classList.toggle("is-stop", !!data.running);
        }
        if (mergedBtn) mergedBtn.hidden = !data.has_merged;
        if (rawBtn) rawBtn.hidden = !data.has_raw;
        if (!data.running && !estimateMarketRenderFresh && (data.has_merged || data.has_raw) && !estimateMarketReloadPending) {
          estimateMarketRenderFresh = true;
          estimateMarketReloadPending = true;
          if (compareBtn && data.has_merged) compareBtn.disabled = false;
          if (sourcesBtn && data.has_raw) sourcesBtn.disabled = false;
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
          } else {
            main.textContent = "Пока поиск рынка не запускался.";
          }
        }
        if (detail) {
          const bits = [];
          if (data.stage) bits.push(data.stage);
          if (data.detail) bits.push(data.detail);
          if (data.city) bits.push("город: " + data.city);
          detail.textContent = bits.join(" · ");
        }
        if (logs) {
          const arr = Array.isArray(data.log_tail) ? data.log_tail : [];
          logs.textContent = arr.length ? arr.join("\n") : "—";
          logs.scrollTop = logs.scrollHeight;
        }
      } catch (e) {}
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
      if (btn) {
        btn.dataset.busy = "1";
        btn.disabled = true;
      }
      try {
        const resp = await fetch("/api/estimates/4db7a218224c4642/market-start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ city, selected_types: selectedTypes })
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
          alert(data.message || "Не удалось запустить поиск рынка");
        }
      } catch (e) {
        alert("Не удалось запустить поиск рынка");
      } finally {
        if (btn) btn.dataset.busy = "0";
        refreshEstimateMarketStatus();
      }
    }

    async function stopEstimateMarket() {
      const btn = document.getElementById("marketStartBtn");
      if (btn) {
        btn.dataset.busy = "1";
        btn.disabled = true;
      }
      try {
        const resp = await fetch("/api/estimates/4db7a218224c4642/market-stop", { method: "POST" });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
          alert(data.message || "Не удалось остановить поиск");
        }
      } catch (e) {
        alert("Не удалось остановить поиск");
      } finally {
        if (btn) btn.dataset.busy = "0";
        refreshEstimateMarketStatus();
      }
    }

    setEstimateTableView("estimate");
    refreshEstimateMarketStatus();
    setInterval(refreshEstimateMarketStatus, 3000);
  