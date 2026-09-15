function safeSourceUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return ["https:", "http:"].includes(url.protocol) ? url.href : "#";
  } catch (_) { return "#"; }
}

function money(v) {
  const num = Number(v || 0);
  if (!Number.isFinite(num) || num <= 0) return "цена не указана";
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(num) + " ₽";
}

function fillExample() {
  const q = document.getElementById("researchQueries");
  if (q) q.value = "Кабель ВВГнг 3х2,5 | м\nУкладка тротуарной плитки | м2";
}

function renderResearch(data) {
  const root = document.getElementById("researchResults");
  const status = document.getElementById("researchStatus");
  if (!root || !status) return;
  root.replaceChildren();
  const items = Array.isArray(data.results) ? data.results : [];
  status.textContent = data.message || (items.length ? "Готово." : "Ничего не найдено.");
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Нет результатов.";
    root.appendChild(empty);
    return;
  }
  for (const item of items) {
    const card = document.createElement("div");
    card.className = "result-card";
    const h = document.createElement("h3");
    h.textContent = String(item.query || "");
    const meta = document.createElement("div");
    meta.className = "meta";
    const prices = Array.isArray(item.offers) ? item.offers.filter(x => x.verified).map(x => Number(x.price || 0)).filter(x => Number.isFinite(x) && x > 0) : [];
    const verifiedCount = Array.isArray(item.offers) ? item.offers.filter(x => x.verified).length : 0;
    const candidateCount = Array.isArray(item.offers) ? item.offers.filter(x => !x.verified).length : 0;
    const cityText = item.region ? (" · город: " + item.region) : "";
    meta.textContent = (item.position_label ? item.position_label + (item.unit ? " · ед.: " + item.unit : "") + " · " : "") + "проверено: " + verifiedCount + " · кандидатов: " + candidateCount + cityText + (prices.length ? (" · диапазон: " + money(Math.min(...prices)) + " — " + money(Math.max(...prices))) : "");
    card.appendChild(h);
    card.appendChild(meta);
    if (item.strategy || item.warning) {
      const strategy = document.createElement("div");
      strategy.className = "strategy-note";
      strategy.textContent = [item.strategy, item.warning].filter(Boolean).join(" · ");
      card.appendChild(strategy);
    }
    const offersWrap = document.createElement("div");
    offersWrap.className = "offers";
    const offers = Array.isArray(item.offers) ? item.offers : [];
    if (!offers.length) {
      const empty = document.createElement("div");
      empty.className = "offer";
      empty.textContent = item.errors || "Ничего не найдено.";
      offersWrap.appendChild(empty);
    } else {
      for (const offer of offers) {
        const box = document.createElement("div");
        box.className = "offer " + (offer.verified ? "is-verified" : "is-candidate");
        const top = document.createElement("div");
        top.className = "offer-top";
        const source = document.createElement("span");
        source.className = "offer-source";
        source.textContent = (offer.verified ? "✓ Проверен · " : "? Кандидат · ") + String(offer.source || "Источник");
        const price = document.createElement("span");
        price.className = "offer-price" + (offer.verified ? "" : " is-candidate");
        price.textContent = offer.verified ? money(offer.price) : "не принято в расчёт";
        top.appendChild(source);
        top.appendChild(price);
        const link = document.createElement("a");
        link.href = safeSourceUrl(offer.url);
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = String(offer.title || offer.url || "Открыть источник");
        box.appendChild(top);
        box.appendChild(link);
        if (offer.snippet) {
          const sn = document.createElement("div");
          sn.className = "offer-snippet";
          sn.textContent = String(offer.snippet);
          box.appendChild(sn);
        }
        const verification = document.createElement("div");
        verification.className = "verification";
        const adapter = offer.adapter ? (" · адаптер: " + offer.adapter) : "";
        const unit = offer.matched_unit ? (" · единица: " + offer.matched_unit) : "";
        verification.textContent = (offer.verified ? "Цена подтверждена" : "Источник отклонён") + " · " + String(offer.reason || "Нет доказательства на прямой странице") + adapter + unit;
        box.appendChild(verification);
        offersWrap.appendChild(box);
      }
    }
    if (item.errors && offers.length) {
      const warn = document.createElement("div");
      warn.className = "meta";
      warn.textContent = "Ограничения поиска: " + item.errors;
      card.appendChild(warn);
    }
    card.appendChild(offersWrap);
    root.appendChild(card);
  }
}

async function runResearch() {
  const btn = document.getElementById("researchRunBtn");
  if (btn?.disabled) return;
  const status = document.getElementById("researchStatus");
  const queries = document.getElementById("researchQueries");
  const city = document.getElementById("researchCity");
  if (!queries) return;
  if (!String(queries.value || "").trim()) {
    if (status) status.textContent = "Добавьте хотя бы одну позицию для поиска.";
    queries.focus();
    return;
  }
  if (btn) btn.disabled = true;
  document.body.classList.add("research-is-running");
  const buttonLabel = btn ? btn.querySelector("[data-research-button-label]") : null;
  if (buttonLabel) buttonLabel.textContent = "Ищем предложения…";
  if (status) status.textContent = "Ищу кандидатов и проверяю цены на прямых страницах…";
  try {
    const resp = await fetch("/research/items", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        queries: String(queries.value || ""),
        city: city ? String(city.value || "").trim() : ""
      })
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      if (status) status.textContent = data.message || "Не удалось выполнить поиск.";
      return;
    }
    renderResearch(data);
  } catch (e) {
    if (status) status.textContent = "Не удалось выполнить поиск.";
  } finally {
    if (btn) btn.disabled = false;
    document.body.classList.remove("research-is-running");
    if (buttonLabel) buttonLabel.textContent = "Найти цены";
  }
}

document.getElementById("researchQueries")?.addEventListener("keydown", function(event) {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    runResearch();
  }
});
