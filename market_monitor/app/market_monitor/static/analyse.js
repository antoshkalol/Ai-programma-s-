/* Market Monitor - tabblad Analyse: kies een aandeel of munt, Claude (standaard Opus 5.5) schrijft een uitgebreide
   technische analyse met aanbeveling, kans en scenario's. Gebruikt de hulpfuncties uit app.js (window.MM). */
(function () {
  "use strict";
  const MM = window.MM;
  if (!MM) return;
  const { el, clear, fmtPrice, fmtDate, nf0, nf1, nf2 } = MM;
  const $ = (id) => document.getElementById(id);
  const MARKET = { stocks: "Aandelen", crypto: "Crypto" };
  const ACTION = {
    kopen: { cls: "long", text: "KOPEN ▲" }, short: { cls: "short", text: "SHORT ▼" },
    afwachten: { cls: "neutral", text: "AFWACHTEN" }, verkopen: { cls: "sell", text: "VERKOPEN" },
  };
  const S = { meta: null, poll: null, started: 0, current: null, tickers: {} };
  const pct = (v) => (v === null || v === undefined || isNaN(v) ? "-" : (v >= 0 ? "+" : "") + nf1.format(v) + "%");
  const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

  async function req(path, body) {
    const opts = { headers: { Accept: "application/json" } };
    if (body !== undefined) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    const r = await fetch("api/" + path, opts);
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) throw new Error((data && data.message) || path + " -> " + r.status);
    return data;
  }

  // ------------------------------------------------------------------ formulier
  async function fillTickers() {
    const m = $("an-market").value;
    if (!S.tickers[m]) {
      try { S.tickers[m] = await req("tickers?market=" + m); } catch (e) { S.tickers[m] = []; }
    }
    const dl = $("an-tickers");
    clear(dl);
    S.tickers[m].forEach((t) => dl.appendChild(el("option", { value: t.ticker, label: (t.name || "") + (t.last_price ? " · " + fmtPrice(t.last_price) : "") })));
  }
  $("an-market").addEventListener("change", fillTickers);

  function helpText() {
    const model = $("an-model").value;
    const ai = S.meta && S.meta.ai_available;
    const cost = { "claude-opus-5-5": "ongeveer $0,10-0,30", "claude-sonnet-5-5": "ongeveer $0,05-0,15", "claude-haiku-5-5": "minder dan $0,01" }[model] || "";
    $("an-help").textContent = ai
      ? "Kosten per analyse met " + (S.meta.models[model] || model) + ": " + cost + " (hogere diepgang = grondiger en duurder). Een analyse van dezelfde ticker binnen 30 minuten wordt hergebruikt; met 'Opnieuw analyseren' maak je een nieuwe. Elke ticker kan, ook als het dashboard hem niet volgt."
      : "Er is geen AI-sleutel ingevuld (anthropic_api_key): je krijgt alleen de eigen technische berekening, zonder de analyse door Claude.";
  }
  $("an-model").addEventListener("change", helpText);

  $("analyse-form").addEventListener("submit", (e) => { e.preventDefault(); start(false); });

  async function start(force, market, ticker) {
    market = market || $("an-market").value;
    ticker = (ticker || $("an-ticker").value || "").trim().toUpperCase();
    if (!ticker) { MM.toast("Vul een aandeel of munt in"); $("an-ticker").focus(); return; }
    $("an-go").disabled = true;
    try {
      const r = await req("analysis", { market, ticker, model: $("an-model").value, effort: $("an-effort").value, force });
      const a = r.analysis;
      S.current = a.id;
      if (a.status === "running") track(a.id);
      else { showProgress(null); render(a, r.analysis.cached); }
    } catch (err) {
      MM.toast(err.message);
    }
    $("an-go").disabled = false;
  }

  function showProgress(a) {
    const box = $("an-progress");
    if (!a) { box.hidden = true; clear(box); return; }
    box.hidden = false;
    clear(box);
    const secs = Math.round((Date.now() - S.started) / 1000);
    box.append(el("span", { class: "spinner", "aria-hidden": "true" }),
      el("span", null, el("strong", { text: a.ticker + ": " }), (a.step || "Bezig") + " · " + secs + " s"));
  }

  function track(id) {
    clearTimeout(S.poll);
    S.started = Date.now();
    const tick = async () => {
      let a;
      try { a = await req("analysis/" + id); } catch (e) { S.poll = setTimeout(tick, 4000); return; }
      if (a.status === "running") {
        showProgress(a);
        S.poll = setTimeout(tick, 2000);
        return;
      }
      showProgress(null);
      render(a, false);
      loadHistory();
    };
    tick();
  }

  // ------------------------------------------------------------------ resultaat
  function tile(label, value, sub, klass) {
    return el("div", { class: "plan-item" }, el("div", { class: "label", text: label }), el("div", { class: "value " + (klass || ""), text: value }), sub ? el("div", { class: "sub", text: sub }) : null);
  }
  function section(title, ...children) {
    return el("div", { class: "an-section" }, el("h3", { text: title }), ...children);
  }
  function list(items) {
    return items && items.length ? el("ul", { class: "plain small" }, items.map((x) => el("li", { text: x }))) : el("p", { class: "sub", text: "Niets bijzonders." });
  }

  function chartRec(a) {
    // Koersgrafiek van de eigen berekening, met de niveaus uit de aanbeveling
    const L = a.local, rec = (a.ai || {}).recommendation;
    const plan = Object.assign({}, L.plan);
    if (rec) {
      plan.direction = rec.action === "kopen" ? "long" : rec.action === "short" ? "short" : "neutral";
      ["entry_low", "entry_high", "stop", "target1", "target2"].forEach((k) => { plan[k] = rec[k] || null; });
      if (plan.direction === "neutral" || !plan.stop) plan.direction = "neutral";
    }
    const chart = Object.assign({}, L.chart);
    if (plan.direction !== "neutral") {
      const h = L.plan.horizon_days || 20;
      chart.scenario = [[0, L.price], [Math.round(h / 2), plan.target1], [h, plan.target2]];
    } else chart.scenario = [];
    return { ticker: L.ticker, currency: L.currency, plan, chart };
  }

  function render(a, cached) {
    const box = $("analyse-result");
    box.hidden = false;
    clear(box);
    if (a.status === "error") {
      box.append(el("div", { class: "panel-head" }, el("h2", { text: a.ticker + ": analyse mislukt" })),
        el("div", { class: "panel-body" }, el("p", { class: "run-warn", text: a.error || "Onbekende fout" }),
          el("button", { type: "button", text: "Opnieuw proberen", onclick: () => start(true, a.market, a.ticker) })));
      box.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const L = a.local, ai = a.ai, rec = ai ? ai.recommendation : null;
    const act = rec ? ACTION[rec.action] || ACTION.afwachten : null;
    const head = el("div", { class: "panel-head" },
      el("h2", null, el("span", { class: "market-chip", style: "border-left-color: var(" + (a.market === "crypto" ? "--series-2" : "--series-1") + ")", text: MARKET[a.market] }),
        "Analyse van " + a.ticker, el("span", { class: "sub", text: " " + fmtDate(a.created_at, true) + (ai ? " · " + a.model_label + (a.cost_usd ? " · $" + nf2.format(a.cost_usd) : "") : " · eigen berekening") + (cached ? " · hergebruikt" : "") })),
      el("div", { class: "filters" },
        el("button", { type: "button", text: "Opnieuw analyseren", onclick: () => start(true, a.market, a.ticker) }),
        el("button", { type: "button", text: "Grafiek & details", onclick: () => { MM.switchMarket(a.market); MM.openDrawer(a.ticker); } })));
    const body = el("div", { class: "panel-body" });
    box.append(head, body);

    body.appendChild(el("div", { class: "rec-top" },
      el("div", { class: "rec-asset" },
        el("span", { class: "rec-ticker", text: a.ticker }),
        el("span", { class: "rec-name", text: L.name || "" }),
        el("span", { class: "rec-price", text: fmtPrice(L.price, L.currency) }),
        L.chg_24h !== null && L.chg_24h !== undefined ? el("span", { class: L.chg_24h >= 0 ? "chg-pos" : "chg-neg", text: pct(L.chg_24h) + " 24u" }) : null),
      act ? el("div", { class: "dir-wrap" }, el("span", { class: "dir-badge " + act.cls, text: act.text })) : null));
    if (a.error) body.appendChild(el("p", { class: "run-warn", text: a.error }));

    if (rec) {
      const hit = (L.hit_rates || {})[rec.action === "short" ? "short" : "long"];
      const gauge = el("div", { class: "an-gauge" },
        el("div", { class: "an-gauge-num", text: rec.probability_target1 + "%" }),
        el("div", { class: "an-gauge-lbl", text: rec.action === "kopen" || rec.action === "short" ? "kans dat doel 1 eerder wordt geraakt dan de stop" : "kans (schatting van Claude)" }),
        el("span", { class: "meter wide" }, el("span")));
      gauge.querySelector(".meter > span").style.width = rec.probability_target1 + "%";
      body.appendChild(el("div", { class: "an-hero " + act.cls },
        el("div", { class: "an-hero-main" },
          el("div", { class: "an-headline", text: ai.headline }),
          el("div", { class: "sub", text: "Termijn: " + rec.timeframe + " · zekerheid: " + rec.confidence }),
          el("p", { class: "an-reason", text: rec.reasoning })),
        gauge));
      const checks = [];
      if (hit && hit.win_rate !== null) checks.push("Historisch (afgelopen jaar, zelfde afstanden, 20 dagen): " + nf0.format(hit.win_rate) + "% eerst het doel (" + hit.wins + " van " + (hit.wins + hit.losses) + " keer)");
      if (L.prob_up) checks.push("Koopscore-model: kans op stijging (~1 maand) " + Math.round(L.prob_up * 100) + "%");
      if (checks.length) body.appendChild(el("p", { class: "an-checks sub", text: "Ter controle: " + checks.join(" · ") }));
      if (rec.stop && rec.target1) {
        body.appendChild(el("div", { class: "plan-grid" },
          tile("Instapzone", fmtPrice(rec.entry_low, L.currency) + " - " + fmtPrice(rec.entry_high, L.currency), pct(((rec.entry_low + rec.entry_high) / 2 / L.price - 1) * 100) + " t.o.v. nu"),
          tile("Stop-loss", fmtPrice(rec.stop, L.currency), pct((rec.stop / L.price - 1) * 100), "neg"),
          tile("Doel 1", fmtPrice(rec.target1, L.currency), pct((rec.target1 / L.price - 1) * 100), "pos"),
          tile("Doel 2", fmtPrice(rec.target2, L.currency), pct((rec.target2 / L.price - 1) * 100), "pos"),
          tile("Risico / rendement", rec.rr ? "1 : " + nf1.format(rec.rr) : "-", "tot doel 1"),
          tile("Risico tot de stop", rec.risk_pct ? nf1.format(rec.risk_pct) + "%" : "-", "vanaf het midden van de zone")));
      }
      if (rec.action === "kopen" && window.MMAdvice) {
        body.appendChild(el("div", { class: "form-row" }, el("button", { type: "button", class: "primary", text: "Ik heb dit gekocht",
          onclick: () => window.MMAdvice.buyForm({ market: a.market, ticker: a.ticker, price: L.price, source: "analyse", levels: { stop: rec.stop, target1: rec.target1, target2: rec.target2, atr: L.plan.atr } }) })));
      }
      if (ai.scenarios && ai.scenarios.length) {
        const sc = el("div", { class: "an-scen" });
        ai.scenarios.forEach((s) => {
          const bar = el("span", { class: "an-scen-bar " + s.name }, el("span"));
          bar.firstChild.style.width = s.probability + "%";
          sc.appendChild(el("div", { class: "an-scen-row" },
            el("div", { class: "an-scen-head" }, el("strong", { text: s.name.charAt(0).toUpperCase() + s.name.slice(1) }),
              el("span", { class: "an-scen-p", text: s.probability + "%" }),
              el("span", { class: "sub", text: "doel " + fmtPrice(s.target, L.currency) + " (" + pct((s.target / L.price - 1) * 100) + ")" })),
            bar, el("div", { class: "an-scen-desc", text: s.description })));
        });
        body.appendChild(section("Scenario's", sc));
      }
      body.appendChild(el("div", { class: "summary-overview an-summary" }, el("strong", { text: "In gewone taal: " }), ai.summary));
    } else {
      const p = L.plan;
      body.appendChild(el("div", { class: "an-hero neutral" }, el("div", { class: "an-hero-main" },
        el("div", { class: "an-headline", text: "Eigen berekening: " + p.label + " (overtuiging " + p.conviction + ")" }),
        el("p", { class: "an-reason", text: p.action || "" }))));
    }

    // Grafiek
    const cr = chartRec(a);
    body.appendChild(el("h3", { text: "Koersgrafiek met technische analyse" + (rec ? " en de niveaus uit de aanbeveling" : "") }));
    const cbox = el("div", { class: "ta-chart" });
    body.appendChild(cbox);
    const rbox = el("div", { class: "ta-chart" });
    body.appendChild(rbox);
    const mbox = el("div", { class: "ta-chart" });
    body.appendChild(mbox);
    requestAnimationFrame(() => {
      const w = cbox.clientWidth || 760;
      cbox.appendChild(MM.taChart(cr, a.market, w));
      rbox.appendChild(MM.rsiChart(cr, w));
      if (cr.chart.macd && cr.chart.macd.length) mbox.appendChild(MM.macdChart(cr, w));
    });

    if (ai) {
      const t = ai.trend || {};
      const cols = el("div", { class: "an-cols" },
        el("div", null,
          section("Trend", el("div", { class: "kv-row" }, el("span", { text: "Kort (dagen-weken)" }), el("strong", { text: t.short || "-" })),
            el("div", { class: "kv-row" }, el("span", { text: "Middellang (weken-maanden)" }), el("strong", { text: t.medium || "-" })),
            el("div", { class: "kv-row" }, el("span", { text: "Lang (maanden-jaar)" }), el("strong", { text: t.long || "-" }))),
          section("Momentum", el("p", { text: ai.momentum })),
          section("Beweeglijkheid", el("p", { text: ai.volatility })),
          section("Patronen", list(ai.patterns))),
        el("div", null,
          section("Steun en weerstand", ai.levels && ai.levels.length ? el("table", { class: "tbl" }, el("tbody", null,
            ai.levels.slice().sort((x, y) => y.price - x.price).map((lv) => el("tr", null,
              el("td", null, el("span", { class: "sig " + (lv.type === "steun" ? "positief" : "negatief"), text: lv.type })),
              el("td", { class: "num", text: fmtPrice(lv.price, L.currency) }),
              el("td", { class: "num sub", text: pct((lv.price / L.price - 1) * 100) }),
              el("td", { class: "sub", text: lv.strength + (lv.note ? " · " + lv.note : "") }))))) : el("p", { class: "sub", text: "-" })),
          section("Indicatoren (uitleg door Claude)", ai.indicators && ai.indicators.length ? el("ul", { class: "plain small" }, ai.indicators.map((i) =>
            el("li", null, el("span", { class: "sig " + i.signal, text: i.signal }), " ", el("strong", { text: i.name + ": " }), i.reading))) : el("p", { class: "sub", text: "-" }))));
      body.appendChild(cols);
      body.appendChild(el("div", { class: "an-cols" },
        el("div", null, section("Wat de koers kan bewegen (nieuws, kopers)", list(ai.catalysts)), section("Om op te letten", list(ai.watch))),
        el("div", null, section("Risico's", list(ai.risks)), section("Wanneer klopt de analyse niet meer?", el("p", { class: "an-invalid", text: ai.invalidation })))));
    }

    const facts = L.returns || {};
    body.appendChild(section("Cijfers",
      el("div", { class: "plan-grid" },
        tile("1 dag", pct(facts["1d"]), "", cls(facts["1d"])), tile("5 dagen", pct(facts["5d"]), "", cls(facts["5d"])),
        tile("20 dagen", pct(facts["20d"]), "", cls(facts["20d"])), tile("60 dagen", pct(facts["60d"]), "", cls(facts["60d"])),
        tile("120 dagen", pct(facts["120d"]), "", cls(facts["120d"])), tile("1 jaar", pct(facts["250d"]), "", cls(facts["250d"])),
        tile("Beweeglijkheid", nf1.format(L.volatility_annual_pct) + "% per jaar", "gem. dagbeweging " + nf2.format(L.atr_pct) + "%"),
        tile("Jaar hoog / laag", fmtPrice(L.year_high, L.currency) + " / " + fmtPrice(L.year_low, L.currency), L.days + " dagen historie"))));
    if (L.plan.indicators && L.plan.indicators.length) {
      const det = el("details", { class: "review-ta" }, el("summary", { text: "Scorekaart van de eigen berekening (" + L.plan.indicators.length + " indicatoren)" }));
      det.appendChild(MM.taScorecard(L.plan));
      body.appendChild(det);
    }
    body.appendChild(el("p", { class: "rec-note", text: "Technische analyse op dagkoersen, geschreven door een AI-model op basis van de gegevens van het dashboard. Kansen zijn schattingen; de historische trefkans laat zien hoe vaak vergelijkbare afstanden in het afgelopen jaar eerst het doel of eerst de stop raakten. Geen beleggingsadvies: gebruik altijd een stop-loss." }));
    box.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ------------------------------------------------------------------ geschiedenis
  async function loadHistory() {
    let d;
    try { d = await req("analysis"); } catch (e) { return; }
    const first = !S.meta;
    S.meta = d;
    if (first) {
      const ms = $("an-model"), ef = $("an-effort");
      clear(ms); clear(ef);
      Object.entries(d.models).forEach(([k, v]) => ms.appendChild(el("option", { value: k, text: v + (k === "claude-opus-5-5" ? " (aanbevolen)" : "") })));
      Object.entries(d.efforts).forEach(([k, v]) => ef.appendChild(el("option", { value: k, text: v })));
      ms.value = "claude-opus-5-5";
      ef.value = "high";
      helpText();
      fillTickers();
    }
    const box = $("an-history");
    clear(box);
    if (!d.recent.length) { box.appendChild(el("p", { class: "empty", text: "Nog geen analyses." })); return; }
    const ul = el("ul", { class: "plain hist-list" });
    d.recent.forEach((r) => {
      const act = ACTION[r.action] || null;
      const btn = el("button", { type: "button", class: "hist-item" },
        el("span", { class: "hist-date", text: fmtDate(r.created_at, true) }),
        el("strong", { text: r.ticker }),
        el("span", { class: "sub", text: MARKET[r.market] }),
        act ? el("span", { class: "sig " + (r.action === "kopen" ? "positief" : r.action === "short" || r.action === "verkopen" ? "negatief" : "neutraal"), text: r.action }) : el("span", { class: "sub", text: r.status === "error" ? "mislukt" : r.status === "running" ? "bezig..." : (r.action || "") }),
        r.probability ? el("span", { class: "hist-status", text: r.probability + "% kans" }) : null,
        el("span", { class: "hist-status", text: r.model + (r.cost_usd ? " · $" + nf2.format(r.cost_usd) : "") }),
        el("span", { class: "hist-go", "aria-hidden": "true", text: "Bekijk ›" }));
      btn.addEventListener("click", async () => {
        const a = await req("analysis/" + r.id);
        if (a.status === "running") { S.current = a.id; track(a.id); } else render(a, false);
      });
      ul.appendChild(el("li", null, btn));
    });
    box.appendChild(ul);
  }

  async function refresh() { await loadHistory(); }

  // Vanuit het detailvenster van een aandeel of munt
  function analyse(market, ticker) {
    MM.switchView("analyse");
    $("an-market").value = market;
    $("an-ticker").value = ticker;
    const go = () => start(false, market, ticker);
    if (S.meta) go(); else loadHistory().then(go);
  }

  window.MMAnalyse = { refresh, analyse };
  if (document.body.dataset.view === "analyse") refresh();
})();
