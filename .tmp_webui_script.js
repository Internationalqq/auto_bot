
    (function bindRebuildSelect() {
      const sel = document.getElementById("rebuildTenderSelect");
      if (!sel) return;
      sel.addEventListener("change", function() {
        applyToolbarDisabled(parseRunning, !!window.__mergeRunLive);
      });
    })();

    let lastNmckJson = "";
    async function parseNmckJustification() {
      const inp = document.getElementById("nmckFileInput");
      const st = document.getElementById("nmckParseStatus");
      const ta = document.getElementById("nmckJsonOut");
      const copyB = document.getElementById("nmckCopyBtn");
      const dlB = document.getElementById("nmckDownloadBtn");
      const prevA = document.getElementById("nmckPreviewLink");
      const f = inp && inp.files && inp.files[0];
      if (!f) { alert("Выберите файл Excel (.xlsx)"); return; }
      if (st) st.textContent = "Загрузка и разбор…";
      lastNmckJson = "";
      if (copyB) copyB.disabled = true;
      if (dlB) dlB.disabled = true;
      if (prevA) { prevA.hidden = true; prevA.href = "#"; }
      if (ta) { ta.hidden = true; ta.value = ""; }
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await fetch("/api/parse-nmck-justification", { method: "POST", body: fd });
        let data = {};
        try { data = await r.json(); } catch (e) {}
        if (!r.ok || !data.ok) {
          if (st) st.textContent = (data && data.message) ? data.message : ("Ошибка " + r.status);
          return;
        }
        const pack = { columns: data.columns, rows: data.rows, meta: data.meta };
        lastNmckJson = JSON.stringify(pack, null, 2);
        const m = data.meta || {};
        if (st) {
          st.textContent = "Готово: " + (m.row_count != null ? m.row_count : "?") + " поз., колонок "
            + (m.column_count != null ? m.column_count : "?") + ", лист «" + (m.sheet || "") + "»";
        }
        if (data.preview_url && prevA) {
          prevA.href = data.preview_url;
          prevA.hidden = false;
        }
        if (ta) { ta.value = lastNmckJson; ta.hidden = false; }
        if (copyB) copyB.disabled = false;
        if (dlB) dlB.disabled = false;
      } catch (e) {
        if (st) st.textContent = "Запрос не выполнен (сеть или сервер).";
      }
    }
    function copyNmckJson() {
      if (!lastNmckJson) return;
      navigator.clipboard.writeText(lastNmckJson).then(function() {
        const st = document.getElementById("nmckParseStatus");
        if (st) st.textContent += " · JSON в буфере обмена";
      }).catch(function() { alert("Не удалось скопировать"); });
    }
    function downloadNmckJson() {
      if (!lastNmckJson) return;
      const blob = new Blob([lastNmckJson], { type: "application/json;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "nmck_prilozhenie_2.json";
      a.click();
      URL.revokeObjectURL(a.href);
    }

    function getRebuildTenderId() {
      const s = document.getElementById("rebuildTenderSelect");
      return s && s.value ? String(s.value).trim() : "";
    }
    function setQuickTenderLinks(tid) {
      const t = String(tid || "").trim();
      const box = document.getElementById("quickTenderCheck");
      const rep = document.getElementById("quickTenderReportLink");
      const eis = document.getElementById("quickTenderEisLink");
      if (!box || !rep || !eis) return;
      if (!t) {
        box.style.display = "none";
        return;
      }
      rep.href = "/merge-report/" + encodeURIComponent(t) + "/";
      eis.href = "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=" + encodeURIComponent(t);
      rep.textContent = "сводка " + t;
      box.style.display = "";
      try { localStorage.setItem("lastTenderCheckId", t); } catch (e) {}
    }
    try {
      const lastTid = localStorage.getItem("lastTenderCheckId") || "";
      if (lastTid) setQuickTenderLinks(lastTid);
    } catch (e) {}
    const TENDER_COUNT = ;

    let parseRunning = false;
    let parseStartMs = null;
    let parsePendingUntil = 0;
    let notifyState = {
      enabled: localStorage.getItem("webPushEnabled") === "1",
      prev: null,
    };

    function formatElapsed(sec) {
      const s = Math.max(0, Math.floor(sec));
      const m = Math.floor(s / 60);
      const h = Math.floor(m / 60);
      if (h > 0) return `${h} ч ${m % 60} мин ${s % 60} с`;
      if (m > 0) return `${m} мин ${s % 60} с`;
      return `${s} с`;
    }

    function updateParseElapsed() {
      if (!parseRunning || parseStartMs == null) return;
      const sec = (Date.now() - parseStartMs) / 1000;
      const el = document.getElementById("parseProgressTime");
      if (el) el.textContent = "Прошло: " + formatElapsed(sec);
    }

    setInterval(updateParseElapsed, 1000);

    function showParseLaunchFeedback() {
      parseRunning = true;
      parseStartMs = Date.now();
      parsePendingUntil = Date.now() + 8000;
      const panel = document.getElementById("parseProgressPanel");
      const label = document.getElementById("parseProgressLabel");
      const bar = document.getElementById("parseBarFill");
      const time = document.getElementById("parseProgressTime");
      const status = document.getElementById("parseStatus");
      const logs = document.getElementById("parseLogs");
      const logCount = document.getElementById("parseProgressLogCount");
      const cmd = document.getElementById("parseCommandLine");
      if (panel) panel.hidden = false;
      if (label) label.textContent = "Запускаем поиск новых закупок…";
      if (bar) {
        bar.classList.add("running");
        bar.style.width = "65%";
      }
      if (time) time.textContent = "Прошло: 0 с";
      if (status) status.textContent = "Статус: передаём задачу серверу";
      if (logs) logs.textContent = "Ожидаем первые сообщения от программы…";
      if (logCount) logCount.textContent = "Поиск запускается.";
      if (cmd) cmd.textContent = "";
      applyToolbarDisabled(true, false);
      window.setTimeout(function() {
        if (panel) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }, 80);
    }

    function showParseLaunchError(message) {
      parseRunning = false;
      parseStartMs = null;
      parsePendingUntil = 0;
      const panel = document.getElementById("parseProgressPanel");
      const label = document.getElementById("parseProgressLabel");
      const bar = document.getElementById("parseBarFill");
      const time = document.getElementById("parseProgressTime");
      const status = document.getElementById("parseStatus");
      if (panel) panel.hidden = false;
      if (label) label.textContent = "Поиск не запустился";
      if (bar) {
        bar.classList.remove("running");
        bar.style.width = "100%";
      }
      if (time) time.textContent = "";
      if (status) status.textContent = message || "Сервер не смог запустить поиск.";
      applyToolbarDisabled(false, !!window.__mergeRunLive);
    }

    function applyToolbarDisabled(parseRun, mergeRun) {
      const busy = parseRun || mergeRun;
      const startBtn = document.getElementById("startBtn");
      const rebuildBtn = document.getElementById("rebuildBtn");
      const rebuildAllBtn = document.getElementById("rebuildAllBtn");
      const genBtn = document.getElementById("genMergeSiteBtn");
      const genMissingBtn = document.getElementById("genMergeMissingBtn");
      const runByLinkBtn = document.getElementById("runByLinkBtn");
      if (startBtn) {
        startBtn.disabled = busy;
        startBtn.textContent = parseRun ? "Ищем закупки…" : "Найти новые закупки";
      }
      if (rebuildBtn) rebuildBtn.disabled = busy || !getRebuildTenderId();
      if (rebuildAllBtn) rebuildAllBtn.disabled = busy || TENDER_COUNT < 1;
      if (genBtn) genBtn.disabled = busy;
      if (genMissingBtn) genMissingBtn.disabled = busy;
      if (runByLinkBtn) runByLinkBtn.disabled = busy;
      document.querySelectorAll(".tender-act-btn").forEach(function(btn) {
        btn.disabled = busy;
      });
    }

    function updatePushButtonUi() {
      const b = document.getElementById("enablePushBtn");
      if (!b) return;
      if (!("Notification" in window)) {
        b.textContent = "Браузер не поддерживает уведомления";
        b.disabled = true;
        return;
      }
      const perm = Notification.permission;
      if (notifyState.enabled && perm === "granted") {
        b.textContent = "Уведомления включены";
        b.disabled = true;
        return;
      }
      b.textContent = "Включить уведомления";
      b.disabled = false;
    }

    async function enableWebPush() {
      if (!("Notification" in window)) {
        alert("Браузер не поддерживает уведомления.");
        return;
      }
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        alert("Разрешение на уведомления не выдано.");
        updatePushButtonUi();
        return;
      }
      notifyState.enabled = true;
      localStorage.setItem("webPushEnabled", "1");
      updatePushButtonUi();
      new Notification("AutoBot", { body: "Уведомления в браузере включены." });
    }

    function safeNotify(title, body) {
      if (!notifyState.enabled) return;
      if (!("Notification" in window)) return;
      if (Notification.permission !== "granted") return;
      try {
        new Notification(title, { body });
      } catch (e) {}
    }

    function handlePushDiff(nextState) {
      const prev = notifyState.prev;
      notifyState.prev = nextState;
      if (!prev) return;

      if (prev.parse_running && !nextState.parse_running) {
        const ok = nextState.parse_exit_code === 0;
        safeNotify(
          ok ? "Поиск закупок завершён" : "Поиск закупок завершён с ошибкой",
          ok ? "Обновите страницу, чтобы увидеть результат." : "Откройте ход работы на странице."
        );
      }

      if (prev.merge_running && !nextState.merge_running) {
        safeNotify("Сравнения цен готовы", nextState.merge_last_summary || "Обработка завершена.");
      }

      if ((nextState.coverage_merge_html || 0) > (prev.coverage_merge_html || 0)) {
        const delta = (nextState.coverage_merge_html || 0) - (prev.coverage_merge_html || 0);
        safeNotify("Появились новые сравнения цен", "Готово новых страниц: " + delta);
      }
    }

    async function refreshPushState() {
      try {
        const r = await fetch("/api/push-state");
        if (!r.ok) return;
        const st = await r.json();
        handlePushDiff(st);
      } catch (e) {}
    }

    async function startParsing() {
      showParseLaunchFeedback();
      try {
        const body = {
          max_pages: parseInt(document.getElementById("optMaxPages").value, 10) || 2,
          max_tenders: parseInt(document.getElementById("optMaxTenders").value, 10) || 15,
          days_back: parseInt(document.getElementById("optDaysBack").value, 10) || 60,
        };
        const r = await fetch("/api/start-parse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) {
          const message = data.message || "Не удалось запустить поиск закупок";
          showParseLaunchError(message);
          alert(message);
          return;
        }
        window.setTimeout(refreshStatus, 200);
      } catch (e) {
        const message = "Не удалось запустить поиск закупок. Проверьте, работает ли сервер.";
        showParseLaunchError(message);
        alert(message);
      }
    }

    async function rebuildReport() {
      const tid = getRebuildTenderId();
      if (!tid) { alert("Выберите закупку."); return; }
      if (!confirm("Повторно извлечь смету для закупки " + tid + " из уже скачанных документов?\\n\\nРыночные цены обновляться не будут.")) return;
      applyToolbarDisabled(true, false);
      try {
        const r = await fetch("/api/rebuild-report", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tender_id: tid }),
        });
        const data = await r.json();
        if (!data.ok) {
          alert(data.message || "Не удалось запустить повторное извлечение сметы");
          refreshStatus();
        }
      } catch (e) {
        alert("Не удалось отправить запрос на повторное извлечение сметы.");
        refreshStatus();
      }
    }

    async function rebuildReportForTender(tid) {
      const t = String(tid || "").trim();
      if (!t) return;
      if (!confirm("Повторно извлечь смету для закупки " + t + " из уже скачанных документов?\\n\\nРыночные цены обновляться не будут.")) return;
      applyToolbarDisabled(true, false);
      try {
        const r = await fetch("/api/rebuild-report", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tender_id: t }),
        });
        const data = await r.json();
        if (!data.ok) {
          alert(data.message || "Не удалось запустить повторное извлечение сметы");
          refreshStatus();
        }
      } catch (e) {
        alert("Не удалось отправить запрос на повторное извлечение сметы.");
        refreshStatus();
      }
    }

    async function rebuildAllReports() {
      if (TENDER_COUNT < 1) { alert("В списке пока нет закупок."); return; }
      if (!confirm(
        "Повторно извлечь сметы для всех " + TENDER_COUNT + " закупок?\\n\\n"
        + "Программа перечитает уже скачанные документы. Рыночные цены обновляться не будут."
      )) return;
      applyToolbarDisabled(true, false);
      try {
        const r = await fetch("/api/rebuild-all-reports", { method: "POST" });
        const data = await r.json();
        if (!data.ok) {
          alert(data.message || "Не удалось запустить повторное извлечение смет");
          refreshStatus();
        }
      } catch (e) {
        alert("Ошибка запроса");
        refreshStatus();
      }
    }

    async function generateMergeSiteAll() {
      if (!confirm("Обновить сравнения цен для всех закупок со сметой?\\n\\nАлиса повторно проверит цены. Процесс может занять несколько часов.")) return;
      try {
        const r = await fetch("/api/generate-merge-site-all", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: "{}",
        });
        let data = {};
        try {
          data = await r.json();
        } catch (e) {
          alert("Сервер вернул не JSON (код " + r.status + "). Проверьте консоль web_ui.py.");
          refreshStatus();
          return;
        }
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
        }
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshStatus();
      refreshCoverage();
    }

    async function generateMergeSiteMissing() {
      if (!confirm("Подготовить сравнения только там, где результата ещё нет или прошлая обработка завершилась с ошибкой?\\n\\nУже готовые страницы будут пропущены.")) return;
      try {
        const r = await fetch("/api/generate-merge-site-missing", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: "{}",
        });
        let data = {};
        try {
          data = await r.json();
        } catch (e) {
          alert("Сервер вернул не JSON (код " + r.status + "). Проверьте консоль web_ui.py.");
          refreshStatus();
          return;
        }
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
        }
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshStatus();
      refreshCoverage();
    }

    async function runFullForTender(tid) {
      const t = String(tid || "").trim();
      if (!t) return;
      if (!confirm("Подготовить сравнение цен для закупки " + t + "?\\n\\nПрограмма проверит документы, найдёт рыночные цены и соберёт готовую страницу.")) return;
      setQuickTenderLinks(t);
      try {
        const r = await fetch("/api/generate-merge-site-one", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ tender_id: t }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
        }
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshStatus();
      refreshCoverage();
    }

    async function rerunAliceForTender(tid) {
      const t = String(tid || "").trim();
      if (!t) return;
      if (!confirm("Начать поиск рыночных цен для закупки " + t + " заново?\\n\\nСохранённый прогресс Алисы будет отброшен.")) return;
      setQuickTenderLinks(t);
      try {
        const r = await fetch("/api/generate-merge-site-one-rerun-alice", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ tender_id: t }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
        }
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshStatus();
      refreshCoverage();
    }

    async function runViabilityOnly(tid) {
      const t = String(tid || "").trim();
      if (!t) return;
      if (!confirm("Обновить вывод о выгодности закупки " + t + " и отправить его в Telegram?")) return;
      try {
        const r = await fetch("/api/tender-viability-refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ tender_id: t }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
          return;
        }
        let msg = data.message || "Готово.";
        if (data.report_url) {
          msg += " | Открыть: " + data.report_url;
        }
        if (data.telegram_sent) {
          msg += " | В Telegram отправлен анализ.";
        }
        alert(msg);
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshCoverage();
    }

    async function runByTenderLink() {
      const inp = document.getElementById("tenderLinkInput");
      const raw = inp && inp.value ? String(inp.value).trim() : "";
      if (!raw) { alert("Вставьте ссылку на закупку с zakupki.gov.ru или её номер."); return; }
      if (!confirm("Проверить эту закупку?\\n\\nПрограмма скачает документы, извлечёт смету и найдёт рыночные цены.")) return;
      try {
        const r = await fetch("/api/generate-merge-site-by-link", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ tender_link: raw }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
        } else if (inp) {
          inp.value = "";
          setQuickTenderLinks(data.tender_id || "");
        }
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshStatus();
      refreshCoverage();
    }

    async function refreshCoverage() {
      const el = document.getElementById("reportCoverageBanner");
      if (!el) return;
      try {
        const r = await fetch("/api/reports-coverage");
        if (!r.ok) return;
        const c = await r.json();
        const nt = c.tender_count ?? 0;
        const mh = c.merge_html_among_tenders ?? 0;
        const miss = c.tenders_missing_merge_html ?? 0;
        const sx = c.svodka_xlsx_count ?? 0;
        const rs_no_est = c.missing_no_estimate ?? 0;
        const rs_no_svodka = c.missing_no_svodka ?? 0;
        const rs_no_html = c.missing_no_html ?? 0;
        let cls = "cov-banner stat-strip cov-ok";
        if (nt === 0) {
          el.className = "cov-banner stat-strip cov-warn";
          el.innerHTML = "В списке пока нет закупок. Нажмите «Найти новые закупки».";
          return;
        }
        if (miss > 0) cls = mh === 0 && sx === 0 ? "cov-banner stat-strip cov-warn" : "cov-banner stat-strip cov-partial";
        el.className = cls;
        let html = "Готовых сравнений цен: <strong>" + mh + "</strong> из " + nt;
        if (miss > 0) {
          html += " · ждут обработки: <strong>" + miss + "</strong>";
          html += "<br/><span style=\\"opacity:.85;font-size:11px\\">Из них: без извлечённой сметы — " + rs_no_est + ", без найденных рыночных цен — " + rs_no_svodka + ", страница результата не собрана — " + rs_no_html + ".</span>";
        }
        el.innerHTML = html;
      } catch (e) {}
    }

    function parseProgressView(pr, parsePending) {
      const lines = Array.isArray(pr.log_tail) ? pr.log_tail : [];
      const isTenderSearch = String(pr.task || "").includes("поиск новых закупок")
        || lines.some(function(line) { return line === "Поиск тендеров..."; });
      if (parsePending) {
        return { title: "Запускаем поиск новых закупок…", detail: "Передаём задачу серверу", percent: 5, indeterminate: true };
      }
      if (!pr.running) {
        if (pr.exit_code === 0) {
          return {
            title: isTenderSearch ? "Поиск закупок завершён" : "Задание завершено",
            detail: isTenderSearch ? "Готово. Обновите страницу, чтобы увидеть новые закупки." : "Готово.",
            percent: 100,
            indeterminate: false,
          };
        }
        if (pr.exit_code !== null && pr.exit_code !== undefined) {
          return { title: isTenderSearch ? "Поиск завершён с ошибкой" : "Задание завершено с ошибкой", detail: "Подробности — в журнале ниже.", percent: 100, indeterminate: false };
        }
        return { title: "Ожидание", detail: "", percent: 0, indeterminate: false };
      }

      let searchChecks = 0;
      let tenderStep = null;
      let filtersDone = false;
      let finalReport = false;
      for (const line of lines) {
        if (line.startsWith("- ") && line.includes(" найдено")) searchChecks += 1;
        if (line.startsWith("Итого после фильтров:")) filtersDone = true;
        if (line.startsWith("Готово. Общий отчет по сметам:")) finalReport = true;
        const match = line.match(/^\[([0-9]+)\/([0-9]+)\] ([0-9]+):/);
        if (match) tenderStep = { current: Number(match[1]), total: Number(match[2]), id: match[3] };
      }

      if (finalReport) {
        return { title: "Завершаем поиск", detail: "Сохраняем итоговый отчёт и список закупок", percent: 98, indeterminate: false };
      }
      if (tenderStep && tenderStep.total > 0) {
        const completedBefore = Math.max(0, tenderStep.current - 1);
        const percent = 48 + Math.round((completedBefore / tenderStep.total) * 47);
        return {
          title: "Скачиваем документы и извлекаем сметы",
          detail: "Закупка " + tenderStep.current + " из " + tenderStep.total + " · № " + tenderStep.id,
          percent: percent,
          indeterminate: false,
        };
      }
      if (filtersDone) {
        return { title: "Формируем список закупок", detail: "Поиск завершён, применяем фильтры и проверяем ранее найденные закупки", percent: 45, indeterminate: true };
      }
      if (searchChecks > 0 || lines.some(function(line) { return line === "Поиск тендеров..."; })) {
        return {
          title: "Ищем закупки на zakupki.gov.ru",
          detail: searchChecks > 0 ? "Проверено поисковых направлений: " + searchChecks : "Получаем первые результаты…",
          percent: Math.min(40, 10 + searchChecks * 5),
          indeterminate: true,
        };
      }
      return { title: pr.task ? "Сейчас: " + pr.task : "Подготавливаем поиск…", detail: "Процесс запущен, ожидаем первые сообщения", percent: 7, indeterminate: true };
    }

    function parseOutcomeSummary(pr, parsePending) {
      const lines = Array.isArray(pr.log_tail) ? pr.log_tail : [];
      const joined = lines.join("\n");
      const foundMatch = joined.match(/Итого после фильтров:\s*([0-9]+)/);
      const addedMatch = joined.match(/Добавлено в систему:\s*([0-9]+)/);
      const resultEl = { text: "Поиск ещё не завершён", cls: "" };
      const issueEl = { text: "Идёт выполнение", cls: "" };
      const nextEl = { text: "Дождаться окончания", cls: "" };

      if (parsePending) {
        return {
          result: { text: "Запускаем поиск", cls: "" },
          issue: { text: "Сервер принимает задачу", cls: "" },
          next: { text: "Подождать несколько секунд", cls: "" },
        };
      }
      if (pr.running) {
        return {
          result: { text: "Идёт поиск закупок", cls: "" },
          issue: { text: "Программа проверяет ЕИС и документы", cls: "" },
          next: { text: "Можно просто оставить вкладку открытой", cls: "" },
        };
      }

      if (joined.includes("ERR_CERT_AUTHORITY_INVALID")) {
        resultEl.text = "Новых закупок не получено";
        resultEl.cls = "bad";
        issueEl.text = "Сайт zakupki.gov.ru отклонён из-за проблемы с сертификатом";
        issueEl.cls = "bad";
        nextEl.text = "Проверить сертификаты/антивирус/VPN и повторить поиск";
        nextEl.cls = "warn";
      } else if (joined.includes("ERR_NETWORK_ACCESS_DENIED")) {
        resultEl.text = "Новых закупок не получено";
        resultEl.cls = "bad";
        issueEl.text = "Нет доступа к zakupki.gov.ru из браузера Playwright";
        issueEl.cls = "bad";
        nextEl.text = "Проверить VPN, прокси, фаервол или блокировку сети";
        nextEl.cls = "warn";
      } else if (pr.exit_code === 0) {
        const found = foundMatch ? Number(foundMatch[1]) : null;
        const added = addedMatch ? Number(addedMatch[1]) : null;
        if (found === 0) {
          resultEl.text = "Подходящих закупок не найдено";
          resultEl.cls = "warn";
          issueEl.text = "По текущим регионам и ключевым словам результат пустой";
          issueEl.cls = "warn";
          nextEl.text = "Расширить параметры поиска или проверить доступ к ЕИС";
        } else {
          resultEl.text = "Поиск завершён";
          resultEl.cls = "ok";
          issueEl.text = "Найдено: " + found + (added !== null ? " · новых в базе: " + added : "");
          issueEl.cls = "ok";
          nextEl.text = "Проверить список закупок ниже";
        }
      } else if (pr.exit_code !== null && pr.exit_code !== undefined) {
        resultEl.text = "Поиск завершился с ошибкой";
        resultEl.cls = "bad";
        issueEl.text = "Подробности скрыты в технических деталях";
        issueEl.cls = "warn";
        nextEl.text = "Открыть детали и посмотреть последнюю ошибку";
      }

      return { result: resultEl, issue: issueEl, next: nextEl };
    }

    async function refreshStatus() {
      let pr = { running: false };
      let mr = { running: false };
      try {
        const rp = await fetch("/api/parse-status");
        if (rp.ok) pr = await rp.json();
      } catch (e) {}
      try {
        const rm = await fetch("/api/merge-site-status");
        if (rm.ok) mr = await rm.json();
      } catch (e) {}
      try {
        if (pr.running) parsePendingUntil = 0;
        const parsePending = !pr.running && Date.now() < parsePendingUntil;
        parseRunning = !!pr.running || parsePending;
        if (pr.running && pr.started_at) {
          const ms = Date.parse(pr.started_at);
          parseStartMs = Number.isNaN(ms) ? null : ms;
        } else if (!parsePending) {
          parseStartMs = null;
        }

        const hasParseHistory = !!(
          (pr.log_tail && pr.log_tail.length)
          || pr.command
          || pr.ended_at
          || pr.exit_code !== null && pr.exit_code !== undefined
        );
        const panel = document.getElementById("parseProgressPanel");
        if (panel) panel.hidden = !(parseRunning || hasParseHistory);

        const progressView = parseProgressView(pr, parsePending);
        const label = document.getElementById("parseProgressLabel");
        if (label) label.textContent = progressView.title;

        const lc = document.getElementById("parseProgressLogCount");
        if (lc && (parseRunning || hasParseHistory)) {
          const n = pr.log_lines_count ?? 0;
          lc.textContent = parsePending
            ? "Поиск запускается."
            : pr.running
            ? "Строк в логе: " + n + " (растёт, пока идёт вывод)."
            : "Строк в логе: " + n + ".";
        } else if (lc) lc.textContent = "";

        const bar = document.getElementById("parseBarFill");
        if (bar) {
          if (parseRunning && progressView.indeterminate) {
            bar.classList.add("running");
          } else {
            bar.classList.remove("running");
          }
          bar.style.width = Math.min(100, Math.max(0, progressView.percent)) + "%";
        }

        const status = document.getElementById("parseStatus");
        const logs = document.getElementById("parseLogs");
        const cmdLine = document.getElementById("parseCommandLine");
        const summary = parseOutcomeSummary(pr, parsePending);
        const resultMain = document.getElementById("parseResultMain");
        const resultIssue = document.getElementById("parseResultIssue");
        const resultNext = document.getElementById("parseResultNext");
        let st = progressView.detail || (parsePending ? "запускается" : pr.running ? "идёт выполнение" : "ожидание");
        if (!pr.running && pr.exit_code !== null && pr.exit_code !== undefined) {
          st += " · код выхода: " + pr.exit_code;
        }
        if (pr.ended_at && !pr.running) st += " · завершено: " + pr.ended_at;
        status.textContent = st;
        if (resultMain) {
          resultMain.textContent = summary.result.text;
          resultMain.className = "parse-summary-value" + (summary.result.cls ? " " + summary.result.cls : "");
        }
        if (resultIssue) {
          resultIssue.textContent = summary.issue.text;
          resultIssue.className = "parse-summary-value" + (summary.issue.cls ? " " + summary.issue.cls : "");
        }
        if (resultNext) {
          resultNext.textContent = summary.next.text;
          resultNext.className = "parse-summary-value" + (summary.next.cls ? " " + summary.next.cls : "");
        }
        if (cmdLine) {
          cmdLine.textContent = pr.running && pr.command ? "Команда: " + pr.command : "";
        }
        if (logs && (!parsePending || pr.log_tail && pr.log_tail.length)) {
          logs.textContent = (pr.log_tail && pr.log_tail.length ? pr.log_tail.join("\\n") : "");
          logs.scrollTop = logs.scrollHeight;
        }

        if (parseRunning) {
          updateParseElapsed();
        } else {
          const endLine = document.getElementById("parseProgressTime");
          if (endLine) {
            if (pr.started_at && pr.ended_at) {
              const ms1 = Date.parse(pr.started_at);
              const ms2 = Date.parse(pr.ended_at);
              if (!Number.isNaN(ms1) && !Number.isNaN(ms2) && ms2 >= ms1) {
                endLine.textContent = "Длительность: " + formatElapsed((ms2 - ms1) / 1000);
              } else {
                endLine.textContent = pr.ended_at ? ("Завершено: " + pr.ended_at) : "";
              }
            } else {
              endLine.textContent = pr.ended_at ? ("Завершено: " + pr.ended_at) : "";
            }
          }
        }

        const mergeRun = !!mr.running;
        const mp = document.getElementById("mergeSitePanel");
        if (mp) mp.hidden = !mergeRun;

        const pct = typeof mr.percent === "number" ? mr.percent : 0;
        const fill = document.getElementById("mergeBarFill");
        const ptext = document.getElementById("mergePercentText");
        const det = document.getElementById("mergeSiteDetail");
        const mlogs = document.getElementById("mergeSiteLogs");
        if (fill) fill.style.width = Math.min(100, Math.max(0, pct)) + "%";
        if (ptext) {
          ptext.textContent = pct + "% · " + (mr.done ?? 0) + " / " + (mr.total ?? 0) + (mr.current_tid ? " · сейчас: " + mr.current_tid : "");
        }
        if (det) {
          det.textContent = mergeRun ? "Ищем рыночные цены и собираем страницы сравнения…" : "";
        }
        if (mlogs) {
          mlogs.textContent = (mr.log_tail && mr.log_tail.length ? mr.log_tail.join("\\n") : "");
          mlogs.scrollTop = mlogs.scrollHeight;
        }

        const mis = document.getElementById("mergeIdleSummary");
        const mreason = document.getElementById("mergeMissingReason");
        if (mis) {
          if (!mergeRun && mr.last_ended_at) {
            mis.textContent = "Последний прогон сводок: " + mr.last_ended_at + " — " + (mr.last_summary || "");
          } else if (mergeRun) {
            mis.textContent = "";
          }
        }
        if (mreason) {
          const reasons = mr.last_reason_counts || {};
          const txt = "Не удалось обработать: без сметы " + (reasons.no_estimate || 0)
            + ", ошибка поиска цен " + (reasons.alice_failed || 0)
            + ", ошибка объединения данных " + (reasons.merge_failed || 0)
            + ", ошибка страницы результата " + (reasons.html_failed || 0);
          mreason.textContent = !mergeRun && mr.last_ended_at ? txt : "";
        }

        window.__mergeRunLive = mergeRun;
        applyToolbarDisabled(parseRunning, mergeRun);
        if (typeof window._wasMergeRun === "undefined") window._wasMergeRun = false;
        if (window._wasMergeRun && !mergeRun) refreshCoverage();
        window._wasMergeRun = mergeRun;
      } catch (e) {}
    }

    setInterval(refreshStatus, 2000);
    setInterval(refreshCoverage, 5000);
    setInterval(refreshPushState, 5000);
    refreshStatus();
    refreshCoverage();
    refreshPushState();
    updatePushButtonUi();
  