/* Market Monitor - tabblad Trading: instellingen, paper-tests, live handelen, resultaten en vergelijken. */
(function () {
  "use strict";

  const S = { data: null, selected: null, detail: null, dirty: false, logFilter: "all", busy: false };
  const $ = (id) => document.getElementById(id);
  const svgNS = "http://www.w3.org/2000/svg";

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    }
    return node;
  }
  function svg(tag, attrs) {
    const n = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  async function api(path, opts) {
    const o = Object.assign({ headers: { Accept: "application/json" } }, opts || {});
    if (o.json !== undefined) {
      o.method = o.method || "POST";
      o.headers["Content-Type"] = "application/json";
      o.body = JSON.stringify(o.json);
      delete o.json;
    }
    const r = await fetch("api/" + path, o);
    let body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    if (!r.ok) {
      const err = new Error((body && (body.message || JSON.stringify(body.errors))) || (path + " -> " + r.status));
      err.body = body;
      throw err;
    }
    return body;
  }

  const eurF = new Intl.NumberFormat("nl-NL", { style: "currency", currency: "EUR", maximumFractionDigits: 2 });
  const nf2 = new Intl.NumberFormat("nl-NL", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
  const nf1 = new Intl.NumberFormat("nl-NL", { maximumFractionDigits: 1 });
  const eur = (v) => (v === null || v === undefined || isNaN(v)) ? "-" : eurF.format(v);
  const pct = (v) => (v === null || v === undefined || isNaN(v)) ? "-" : (v > 0 ? "+" : "") + nf2.format(v) + "%";
  const px = (v) => {
    if (v === null || v === undefined || isNaN(v)) return "-";
    const a = Math.abs(v);
    const d = a >= 100 ? 2 : a >= 1 ? 3 : 6;
    return "$" + new Intl.NumberFormat("nl-NL", { maximumFractionDigits: d }).format(v);
  };
  const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
  function when(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    return d.toLocaleString("nl-NL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  function dur(h) {
    if (h === null || h === undefined) return "-";
    if (h < 1) return Math.round(h * 60) + " min";
    if (h < 48) return nf1.format(h) + " uur";
    return nf1.format(h / 24) + " dagen";
  }
  function toast(msg, good) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast " + (good ? "good" : "bad");
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.hidden = true; }, 6000);
  }
  const MARKET = { stocks: "Aandelen", crypto: "Crypto" };
  const ACTOR = { ai: "AI", regels: "regels", risico: "risico", systeem: "systeem" };

  function modeBadge(r) {
    if (r.mode === "paper") return el("span", { class: "badge paper", text: "PAPER" });
    if (r.dry_run) return el("span", { class: "badge dry", text: "LIVE · PROEFDRAAIEN" });
    return el("span", { class: "badge live", text: "LIVE" });
  }
  function statusText(r) {
    return ({ running: "loopt", stopped: "gestopt", finished: "klaar", halted: "noodrem" })[r.status] || r.status;
  }

  // ------------------------------------------------------------------ verversen
  async function refresh() {
    if (S.busy) return;
    S.busy = true;
    try {
      const d = await api("trading");
      S.data = d;
      renderTop(d);
      renderActive(d.runs.filter((r) => r.status === "running"));
      renderLive(d);
      renderCompare(d.runs);
      if (!S.dirty) renderSettings(d.schema, d.settings);
      renderRunSelect(d.runs);
      if (S.selected) await loadDetail(S.selected);
      else { clear($("trade-detail-body")); $("trade-detail-body").appendChild(el("p", { class: "empty", text: "Nog geen tests. Start hierboven een paper-test." })); }
    } catch (e) {
      console.error(e);
    } finally {
      S.busy = false;
    }
  }

  function renderTop(d) {
    const s = d.settings;
    let line;
    if (!d.ai_available) line = "Claude: geen API-sleutel - het model handelt alleen op de regels";
    else if (!s.ai_enabled) line = "Claude: uitgezet in de instellingen - alleen regels";
    else line = "Claude beslist mee: " + s.ai_model + " (" + s.ai_mode + "), max. " + s.ai_max_calls_per_day + " aanroepen per run per dag";
    $("trade-ai-line").textContent = line;
  }

  function renderActive(runs) {
    const box = $("trade-runs-active");
    clear(box);
    if (!runs.length) {
      box.appendChild(el("p", { class: "empty", text: "Er loopt nu geen test of live-run." }));
      return;
    }
    runs.forEach((r) => {
      const st = r.stats || {};
      const left = r.ends_at ? Math.max(0, (new Date(r.ends_at) - Date.now()) / 3.6e6) : null;
      const card = el("article", { class: "run-card " + r.mode + (r.dry_run ? " dry" : ""), tabindex: "0" },
        el("div", { class: "run-card-head" }, modeBadge(r), el("strong", { text: r.name })),
        el("div", { class: "run-card-figs" },
          el("div", null, el("span", { class: "label", text: "Vermogen" }), el("span", { class: "value", text: eur(st.equity_eur) })),
          el("div", null, el("span", { class: "label", text: "Resultaat" }), el("span", { class: "value " + cls(st.return_pct), text: pct(st.return_pct) })),
          el("div", null, el("span", { class: "label", text: "Open" }), el("span", { class: "value", text: String(st.open_positions || 0) })),
          el("div", null, el("span", { class: "label", text: left !== null ? "Nog" : "Loopt" }), el("span", { class: "value", text: left !== null ? dur(left) : dur(st.hours) }))),
        r.paused ? el("p", { class: "run-warn", text: "Gepauzeerd: " + r.paused }) : null,
        el("div", { class: "run-card-actions" },
          el("button", { type: "button", text: "Details", onclick: () => select(r.id) }),
          r.mode === "paper"
            ? el("button", { type: "button", text: "Stop test", onclick: () => stopRun(r, true) })
            : [el("button", { type: "button", class: "danger", text: "Stop en sluit alles", onclick: () => stopRun(r, true) }),
               el("button", { type: "button", text: "Stop, laat posities staan", onclick: () => stopRun(r, false) })]));
      card.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === card) select(r.id); });
      box.appendChild(card);
    });
  }

  function renderLive(d) {
    const ul = $("live-checks");
    clear(ul);
    d.live_checks.forEach((c) => ul.appendChild(el("li", { class: c.ok ? "ok" : "bad" },
      el("span", { class: "ico", "aria-hidden": "true", text: c.ok ? "✓" : "✕" }), c.text)));
    const s = d.settings;
    const dry = s.live_dry_run;
    $("live-mode-line").textContent = (dry
      ? "Proefdraaien staat aan: je saldo wordt gelezen, maar er gaan geen echte orders uit. Zet 'Proefdraaien' uit in de instellingen om echt te handelen."
      : "Proefdraaien staat UIT: er worden echte orders geplaatst.") +
      " Trading 212-omgeving: " + s.trading212_env + ". Live kan alleen long: de API's van Bitvavo en Trading 212 ondersteunen geen short.";
    $("live-confirm-wrap").hidden = dry;
    $("live-confirm").placeholder = d.confirm_text;
    $("live-start").textContent = dry ? "Start live (proefdraaien)" : "Start live met echt geld";
    $("live-start").disabled = !d.live_checks.every((c) => c.ok);
  }

  function renderRunSelect(runs) {
    const sel = $("trade-run-select");
    clear(sel);
    runs.forEach((r) => sel.appendChild(el("option", { value: r.id, text: (r.status === "running" ? "● " : "") + r.name + " (" + statusText(r) + ")" })));
    if (!runs.length) { S.selected = null; return; }
    if (!S.selected || !runs.some((r) => r.id === S.selected)) {
      const running = runs.find((r) => r.status === "running");
      S.selected = (running || runs[0]).id;
    }
    sel.value = String(S.selected);
  }

  function select(id) {
    S.selected = id;
    $("trade-run-select").value = String(id);
    loadDetail(id).then(() => $("trade-detail").scrollIntoView({ behavior: "smooth", block: "start" }));
  }

  // ------------------------------------------------------------------ acties
  async function stopRun(r, close) {
    const what = close ? (r.mode === "paper" ? "Test stoppen en alle posities afrekenen?" : "Live-run stoppen en ALLE posities verkopen?")
      : "Live-run stoppen? De open posities blijven bij de broker staan en worden niet meer bewaakt.";
    if (!window.confirm(what)) return;
    try {
      await api("trading/runs/" + r.id + "/stop", { json: { close } });
      toast("Gestopt: " + r.name, true);
    } catch (e) { toast("Stoppen mislukt: " + e.message); }
    refresh();
  }

  $("paper-start").addEventListener("click", async () => {
    if (S.dirty && !window.confirm("Je hebt niet-opgeslagen instellingen. De test gebruikt de opgeslagen instellingen. Toch starten?")) return;
    const btn = $("paper-start");
    btn.disabled = true;
    try {
      const r = await api("trading/runs", { json: { mode: "paper", name: $("paper-name").value, hours: +$("paper-hours").value } });
      toast("Paper-test gestart: " + r.run.name, true);
      $("paper-name").value = "";
      S.selected = r.run.id;
    } catch (e) { toast(e.message); }
    btn.disabled = false;
    refresh();
  });

  $("live-start").addEventListener("click", async () => {
    const s = S.data.settings;
    const msg = s.live_dry_run
      ? "Live-run starten in proefdraai-modus (geen echte orders)?"
      : "LET OP: het model gaat zelfstandig handelen met ECHT GELD (max. " + eur(s.budget_crypto_eur) + " crypto en " + eur(s.budget_stocks_eur) + " aandelen). Doorgaan?";
    if (!window.confirm(msg)) return;
    try {
      const r = await api("trading/runs", { json: { mode: "live", confirm: $("live-confirm").value } });
      toast("Live-run gestart: " + r.run.name, true);
      S.selected = r.run.id;
      $("live-confirm").value = "";
    } catch (e) { toast(e.message); }
    refresh();
  });

  $("live-test").addEventListener("click", async () => {
    const ul = $("live-test-results");
    clear(ul);
    ul.appendChild(el("li", { text: "Bezig met testen..." }));
    try {
      const r = await api("trading/check", { method: "POST" });
      clear(ul);
      r.results.forEach((x) => ul.appendChild(el("li", { class: x.ok ? "ok" : "bad" },
        el("span", { class: "ico", "aria-hidden": "true", text: x.ok ? "✓" : "✕" }), x.broker + ": " + x.message)));
    } catch (e) { clear(ul); ul.appendChild(el("li", { class: "bad", text: e.message })); }
  });

  $("trade-refresh").addEventListener("click", refresh);
  $("trade-run-select").addEventListener("change", (e) => { S.selected = +e.target.value; loadDetail(S.selected); });

  // ------------------------------------------------------------------ details van een run
  async function loadDetail(id) {
    try {
      const d = await api("trading/runs/" + id);
      S.detail = d;
      renderDetail(d);
    } catch (e) { console.error(e); }
  }

  function tile(label, value, klass, title) {
    return el("div", { class: "plan-item", title: title || null },
      el("div", { class: "label", text: label }), el("div", { class: "value " + (klass || ""), text: value }));
  }

  function renderDetail(d) {
    const body = $("trade-detail-body");
    clear(body);
    const st = d.stats || {};
    $("trade-detail-sub").textContent = d.name;
    body.appendChild(el("div", { class: "detail-head" }, modeBadge(d),
      el("span", { class: "run-status " + d.status, text: statusText(d) }),
      el("span", { class: "sub", text: "gestart " + when(d.created_at) + (d.ends_at ? " · einde " + when(d.ends_at) : "") + (d.ended_at ? " · gestopt " + when(d.ended_at) : "") })));
    if (d.halt_reason && d.status !== "running") body.appendChild(el("p", { class: d.status === "halted" ? "run-warn" : "sub", text: d.halt_reason }));
    if (d.paused) body.appendChild(el("p", { class: "run-warn", text: "Gepauzeerd: " + d.paused }));

    body.appendChild(el("div", { class: "plan-grid" },
      tile("Resultaat", pct(st.return_pct) + " (" + eur(st.pnl_eur) + ")", cls(st.return_pct)),
      tile("Vermogen", eur(st.equity_eur), "", "Startkapitaal " + eur(st.start_eur)),
      tile("Grootste terugval", st.max_drawdown_pct ? "-" + nf2.format(st.max_drawdown_pct) + "%" : "0%"),
      tile("Trades (afgerond)", String(st.trades || 0) + (st.open_positions ? " + " + st.open_positions + " open" : "")),
      tile("Win-rate", st.win_rate === null || st.win_rate === undefined ? "-" : nf1.format(st.win_rate) + "%"),
      tile("Winstfactor", st.profit_factor ? nf2.format(st.profit_factor) : "-", "", "Totale winst gedeeld door totaal verlies (boven 1 = winstgevend)"),
      tile("Kosten broker", eur(st.fees_eur)),
      tile("AI-kosten", st.ai_calls ? "$" + nf2.format(st.ai_cost_usd) + " (" + st.ai_calls + "x)" : "-")));
    const mk = Object.entries(st.markets || {});
    if (mk.length > 1) {
      body.appendChild(el("p", { class: "sub" }, mk.map(([m, v]) => MARKET[m] + ": " + pct(v.return_pct) + " (" + eur(v.equity) + ")").join(" · ")));
    }

    body.appendChild(el("h3", { text: "Vermogen" }));
    const chart = el("div", { class: "chart-wrap", id: "equity-chart" });
    body.appendChild(chart);
    drawEquity(chart, d.equity, st.start_eur);

    const views = Object.entries(d.market_view || {}).filter(([, v]) => v);
    if (views.length) {
      body.appendChild(el("h3", { text: "Marktvisie van Claude" }));
      views.forEach(([m, v]) => body.appendChild(el("div", { class: "rec-comment ai-view" }, el("strong", { text: MARKET[m] + ": " }), v)));
    }

    body.appendChild(el("h3", { text: "Open posities" }));
    body.appendChild(d.positions.length ? positionsTable(d.positions) : el("p", { class: "empty", text: "Geen open posities." }));

    body.appendChild(el("h3", { text: "Afgeronde trades" }));
    body.appendChild(d.closed.length ? closedTable(d.closed) : el("p", { class: "empty", text: "Nog geen afgeronde trades." }));

    const logHead = el("div", { class: "log-head" }, el("h3", { text: "Logboek: wat het model deed en waarom" }));
    const seg = el("div", { class: "seg", role: "group", "aria-label": "Filter logboek" });
    [["all", "Alles"], ["trades", "Trades"], ["ai", "AI"], ["rejected", "Afgewezen"]].forEach(([v, label]) => {
      seg.appendChild(el("button", { type: "button", class: S.logFilter === v ? "on" : null, text: label, onclick: () => { S.logFilter = v; renderDetail(S.detail); } }));
    });
    logHead.appendChild(seg);
    body.appendChild(logHead);
    body.appendChild(decisionLog(d.decisions));

    const det = el("details", { class: "run-settings" }, el("summary", { text: "Instellingen van deze run" }));
    const ul = el("ul", { class: "kv" });
    const labels = {};
    (S.data ? S.data.schema.fields : []).forEach((f) => { labels[f.key] = f; });
    Object.entries(d.settings).forEach(([k, v]) => {
      const f = labels[k];
      if (!f) return;
      const cur = S.data ? S.data.settings[k] : v;
      const diff = JSON.stringify(cur) !== JSON.stringify(v);
      ul.appendChild(el("li", { class: diff ? "diff" : null, title: diff ? "Anders dan je huidige instellingen" : null },
        el("span", { text: f.label }), el("strong", { text: fmtSetting(f, v) })));
    });
    det.appendChild(ul);
    body.appendChild(det);

    const actions = el("div", { class: "form-row" },
      el("button", { type: "button", text: "Instellingen van deze run overnemen", onclick: async () => {
        await api("trading/settings/from/" + d.id, { method: "POST" });
        S.dirty = false;
        toast("Instellingen overgenomen", true);
        refresh();
      } }));
    if (d.status !== "running") {
      actions.appendChild(el("button", { type: "button", class: "danger", text: "Verwijderen", onclick: async () => {
        if (!window.confirm("Deze run en alle gegevens ervan verwijderen?")) return;
        await api("trading/runs/" + d.id + "/delete", { method: "POST" });
        S.selected = null;
        refresh();
      } }));
    }
    body.appendChild(actions);
  }

  function fmtSetting(f, v) {
    if (f.kind === "bool") return v ? "aan" : "uit";
    if (f.kind === "list") return v.length ? v.join(", ") : "(alles)";
    if (f.kind === "eur") return eur(v);
    if (f.kind === "pct") return nf2.format(v) + "%";
    return String(v) + (f.unit && f.unit !== "€" && f.unit !== "%" ? " " + f.unit : "");
  }

  function positionsTable(rows) {
    const tb = el("tbody");
    rows.forEach((p) => {
      tb.appendChild(el("tr", { title: p.reason_open || "" },
        el("td", null, el("strong", { text: p.ticker }), el("span", { class: "sub", text: " " + MARKET[p.market] })),
        el("td", null, el("span", { class: "side " + p.side, text: p.side.toUpperCase() })),
        el("td", { class: "num hide-m", text: px(p.entry_price) }),
        el("td", { class: "num", text: px(p.price) }),
        el("td", { class: "num " + cls(p.upnl_pct), text: pct(p.upnl_pct) }),
        el("td", { class: "num hide-m", text: eur(p.value_eur) }),
        el("td", { class: "num", text: px(p.stop) }),
        el("td", { class: "num hide-m", text: px(p.target1) + (p.t1_done ? " ✓" : "") + " / " + px(p.target2) }),
        el("td", { class: "hide-m", text: p.source === "ai" ? "AI" : "regels" })));
    });
    return el("div", { class: "table-wrap" }, el("table", { class: "tbl" },
      el("thead", null, el("tr", null, ["Wat", "Kant", "Instap", "Koers", "Resultaat", "Waarde", "Stop", "Doel 1 / 2", "Door"].map((h, i) => el("th", { class: (i >= 2 && i <= 7 ? "num" : "") + ([2, 5, 7, 8].includes(i) ? " hide-m" : ""), text: h })))),
      tb));
  }

  function closedTable(rows) {
    const tb = el("tbody");
    rows.forEach((c) => {
      tb.appendChild(el("tr", { title: c.reason_open || "" },
        el("td", null, el("strong", { text: c.ticker }), el("span", { class: "sub", text: " " + MARKET[c.market] })),
        el("td", null, el("span", { class: "side " + c.side, text: c.side.toUpperCase() })),
        el("td", { class: "num hide-m", text: px(c.entry_price) }),
        el("td", { class: "num hide-m", text: px(c.exit_price) }),
        el("td", { class: "num " + cls(c.pnl_eur), text: eur(c.pnl_eur) + " (" + pct(c.pnl_pct) + ")" }),
        el("td", { class: "sub hide-m", text: when(c.opened_at) + " \u2192 " + when(c.closed_at) }),
        el("td", { class: "reason", text: c.reason_close || "" })));
    });
    return el("div", { class: "table-wrap" }, el("table", { class: "tbl" },
      el("thead", null, el("tr", null, ["Wat", "Kant", "Instap", "Uit", "Resultaat", "Wanneer", "Reden"].map((h, i) => el("th", { class: (i >= 2 && i <= 4 ? "num" : "") + ([2, 3, 5].includes(i) ? " hide-m" : ""), text: h })))),
      tb));
  }

  function decisionLog(rows) {
    const f = S.logFilter;
    const shown = rows.filter((r) => {
      if (f === "trades") return r.outcome === "uitgevoerd";
      if (f === "ai") return r.actor === "ai";
      if (f === "rejected") return r.outcome === "afgewezen";
      return true;
    });
    if (!shown.length) return el("p", { class: "empty", text: "Nog niets in het logboek." });
    const ul = el("ul", { class: "log" });
    shown.forEach((r) => {
      const extra = r.details && r.details.kosten_usd !== undefined
        ? el("span", { class: "sub", text: " (" + (r.details.model || "") + ", " + (r.details.tokens_in || 0) + "+" + (r.details.tokens_out || 0) + " tokens, $" + nf2.format(r.details.kosten_usd) + ")" })
        : null;
      ul.appendChild(el("li", { class: "o-" + r.outcome },
        el("div", { class: "log-meta" },
          el("span", { class: "sub", text: when(r.ts) }),
          r.market ? el("span", { class: "chip " + r.market, text: MARKET[r.market] }) : null,
          r.ticker ? el("strong", { text: r.ticker }) : null,
          el("span", { class: "actor " + r.actor, text: ACTOR[r.actor] || r.actor }),
          el("span", { class: "outcome", text: r.outcome })),
        el("div", { class: "log-text" }, r.reason, extra)));
    });
    return ul;
  }

  // ------------------------------------------------------------------ vermogensgrafiek
  function drawEquity(box, rows, start) {
    clear(box);
    if (!rows || rows.length < 2) {
      box.appendChild(el("p", { class: "empty", text: "De grafiek verschijnt na een paar minuten." }));
      return;
    }
    const W = Math.max(280, Math.round(box.clientWidth || 600));
    const H = W < 500 ? 170 : 200;
    const m = { l: 8, r: 70, t: 12, b: 22 };
    const xs = rows.map((r) => new Date(r.ts).getTime());
    const ys = rows.map((r) => r.equity);
    let lo = Math.min(start, ...ys), hi = Math.max(start, ...ys);
    if (hi - lo < start * 0.002) { lo -= start * 0.002; hi += start * 0.002; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;
    const x = (t) => m.l + (t - xs[0]) / Math.max(1, xs[xs.length - 1] - xs[0]) * (W - m.l - m.r);
    const y = (v) => m.t + (hi - v) / (hi - lo) * (H - m.t - m.b);
    const s = svg("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", "aria-label": "Vermogen van " + eur(ys[0]) + " naar " + eur(ys[ys.length - 1]) });
    s.appendChild(svg("line", { x1: m.l, x2: W - m.r, y1: y(start), y2: y(start), stroke: "var(--axis)", "stroke-dasharray": "4 4", "stroke-width": 1 }));
    const startLbl = svg("text", { x: W - m.r + 6, y: y(start) + 4, class: "axis-label" });
    startLbl.textContent = "start";
    const last = ys[ys.length - 1];
    const color = last >= start ? "var(--pos)" : "var(--neg)";
    const d = rows.map((r, i) => (i ? "L" : "M") + x(xs[i]).toFixed(1) + " " + y(r.equity).toFixed(1)).join(" ");
    const area = d + " L" + x(xs[xs.length - 1]).toFixed(1) + " " + y(start).toFixed(1) + " L" + x(xs[0]).toFixed(1) + " " + y(start).toFixed(1) + " Z";
    s.appendChild(svg("path", { d: area, fill: color, opacity: 0.1 }));
    s.appendChild(svg("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round" }));
    const endY = y(last);
    s.appendChild(svg("circle", { cx: x(xs[xs.length - 1]), cy: endY, r: 3.5, fill: color }));
    const endLbl = svg("text", { x: W - m.r + 6, y: Math.abs(endY - y(start)) < 14 ? endY + (endY < y(start) ? -8 : 16) : endY + 4, class: "lvl-label strong" });
    endLbl.textContent = eur(last);
    s.appendChild(startLbl);
    s.appendChild(endLbl);
    [[0, "start"], [xs.length - 1, "end"]].forEach(([i, a]) => {
      const t = svg("text", { x: a === "start" ? m.l : W - m.r, y: H - 6, class: "axis-label", "text-anchor": a === "start" ? "start" : "end" });
      t.textContent = when(rows[i].ts);
      s.appendChild(t);
    });
    // Hover: koers op een tijdstip
    const cross = svg("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" });
    const dot = svg("circle", { r: 3.5, fill: color, visibility: "hidden" });
    s.appendChild(cross);
    s.appendChild(dot);
    const tip = $("tooltip");
    const hit = svg("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent", tabindex: "0" });
    function show(i, cx, cy) {
      const X = x(xs[i]);
      cross.setAttribute("x1", X); cross.setAttribute("x2", X); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", X); dot.setAttribute("cy", y(ys[i])); dot.setAttribute("visibility", "visible");
      tip.textContent = when(rows[i].ts) + ": " + eur(ys[i]) + " (" + pct((ys[i] / start - 1) * 100) + ")";
      tip.hidden = false;
      tip.style.left = Math.min(window.innerWidth - 220, cx + 12) + "px";
      tip.style.top = (cy - 36) + "px";
    }
    function nearest(clientX) {
      const r = s.getBoundingClientRect();
      const X = (clientX - r.left) / r.width * W;
      const t = xs[0] + (X - m.l) / (W - m.l - m.r) * (xs[xs.length - 1] - xs[0]);
      let best = 0;
      xs.forEach((v, i) => { if (Math.abs(v - t) < Math.abs(xs[best] - t)) best = i; });
      return best;
    }
    hit.addEventListener("pointermove", (e) => show(nearest(e.clientX), e.clientX, e.clientY));
    hit.addEventListener("pointerleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
    s.appendChild(hit);
    box.appendChild(s);
  }

  // ------------------------------------------------------------------ vergelijken
  function renderCompare(runs) {
    const box = $("trade-compare-body");
    clear(box);
    if (!runs.length) { box.appendChild(el("p", { class: "empty", text: "Nog geen tests om te vergelijken." })); return; }
    const best = runs.filter((r) => r.mode === "paper" && r.stats && r.stats.trades >= 3)
      .sort((a, b) => b.stats.return_pct - a.stats.return_pct)[0];
    const tb = el("tbody");
    runs.forEach((r) => {
      const st = r.stats || {};
      const k = r.key_settings || {};
      const tr = el("tr", { tabindex: "0", class: (r.id === S.selected ? "sel " : "") + (best && r.id === best.id ? "best" : ""), onclick: () => select(r.id) },
        el("td", null, el("strong", { text: r.name }), " ", modeBadge(r)),
        el("td", { text: statusText(r) }),
        el("td", { class: "num", text: dur(st.hours) }),
        el("td", { class: "num " + cls(st.return_pct), text: pct(st.return_pct) }),
        el("td", { class: "num", text: st.max_drawdown_pct ? "-" + nf2.format(st.max_drawdown_pct) + "%" : "0%" }),
        el("td", { class: "num", text: String(st.trades || 0) }),
        el("td", { class: "num", text: st.win_rate === null || st.win_rate === undefined ? "-" : nf1.format(st.win_rate) + "%" }),
        el("td", { class: "num", text: st.profit_factor ? nf2.format(st.profit_factor) : "-" }),
        el("td", { class: "num", text: eur(st.fees_eur) }),
        el("td", { class: "num", text: st.ai_calls ? "$" + nf2.format(st.ai_cost_usd) : "-" }),
        el("td", { class: "sub", text: [k.trade_style, "risico " + nf2.format(k.risk_per_trade_pct || 0) + "%", "min. " + k.min_conviction, "R/R " + k.min_rr, k.allow_short ? "short" : "geen short", k.ai_enabled ? "AI " + (k.ai_model || "").replace("claude-", "") + " " + k.ai_mode : "geen AI", "TA/kopers/nieuws/X " + [k.weight_ta, k.weight_flow, k.weight_news, k.weight_x].join("/")].join(" · ") }));
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") select(r.id); });
      tb.appendChild(tr);
    });
    box.appendChild(el("table", { class: "tbl compare" },
      el("thead", null, el("tr", null, ["Run", "Status", "Duur", "Resultaat", "Terugval", "Trades", "Win-rate", "Winstfactor", "Kosten", "AI", "Instellingen"].map((h, i) => el("th", { class: i >= 2 && i <= 9 ? "num" : null, text: h })))),
      tb));
    if (best) box.appendChild(el("p", { class: "sub", text: "★ Beste paper-test (minimaal 3 trades): " + best.name + ". Een paar uur zegt weinig; vergelijk liever tests van minimaal een paar dagen." }));
  }

  // ------------------------------------------------------------------ instellingen
  function renderSettings(schema, values) {
    const form = $("settings-form");
    clear(form);
    $("settings-msg").textContent = "";
    schema.groups.forEach((g) => {
      const fs = el("fieldset", { class: "set-group" }, el("legend", { text: g.label }));
      const grid = el("div", { class: "set-grid" });
      schema.fields.filter((f) => f.group === g.key).forEach((f) => grid.appendChild(fieldInput(f, values[f.key])));
      fs.appendChild(grid);
      if (g.key === "weights") fs.appendChild(el("p", { class: "sub", id: "weights-share" }));
      form.appendChild(fs);
    });
    updateShares();
  }

  function fieldInput(f, v) {
    const id = "set-" + f.key;
    let input;
    if (f.kind === "bool") {
      input = el("input", { type: "checkbox", id, name: f.key });
      input.checked = !!v;
    } else if (f.kind === "choice") {
      input = el("select", { id, name: f.key }, f.options.map((o) => el("option", { value: o, text: o })));
      input.value = v;
    } else if (f.kind === "list") {
      input = el("input", { type: "text", id, name: f.key, value: (v || []).join(", "), placeholder: "leeg = alles" });
    } else {
      const step = f.kind === "int" ? 1 : f.kind === "eur" ? 10 : (f.max !== null && f.max <= 2 ? 0.01 : 0.1);
      input = el("input", { type: "number", id, name: f.key, value: v, step, min: f.min, max: f.max, inputmode: f.kind === "int" ? "numeric" : "decimal" });
    }
    input.addEventListener("input", () => { S.dirty = true; wrap.classList.add("changed"); $("settings-msg").textContent = "Niet opgeslagen wijzigingen"; updateShares(); });
    input.addEventListener("change", () => { S.dirty = true; wrap.classList.add("changed"); updateShares(); });
    const range = (f.min !== null && f.max !== null && f.kind !== "bool" && f.kind !== "choice" && f.kind !== "list")
      ? " (" + f.min + " - " + f.max + (f.unit ? " " + f.unit : "") + ")" : "";
    const wrap = el("div", { class: "field" + (f.kind === "bool" ? " bool" : "") },
      f.kind === "bool"
        ? el("label", { for: id }, input, el("span", { text: f.label }))
        : el("label", { for: id, text: f.label + (f.unit && f.kind !== "pct" && f.kind !== "eur" ? " (" + f.unit + ")" : f.kind === "pct" ? " (%)" : f.kind === "eur" ? " (€)" : "") }),
      f.kind === "bool" ? null : input,
      el("small", { text: f.help + range }),
      el("span", { class: "field-err", id: id + "-err", role: "alert" }));
    return wrap;
  }

  function readForm() {
    const out = {};
    S.data.schema.fields.forEach((f) => {
      const n = $("set-" + f.key);
      if (!n) return;
      if (f.kind === "bool") out[f.key] = n.checked;
      else if (f.kind === "list") out[f.key] = n.value;
      else if (f.kind === "choice") out[f.key] = n.value;
      else out[f.key] = n.value === "" ? null : n.value;
    });
    return out;
  }

  function updateShares() {
    const p = $("weights-share");
    if (!p) return;
    const keys = [["weight_ta", "techniek"], ["weight_flow", "kopers"], ["weight_news", "nieuws"], ["weight_x", "X"]];
    const vals = keys.map(([k]) => +(($("set-" + k) || {}).value || 0));
    const tot = vals.reduce((a, b) => a + b, 0) || 1;
    p.textContent = "Verhouding: " + keys.map(([, l], i) => l + " " + Math.round(vals[i] / tot * 100) + "%").join(" · ") +
      ". Onderdelen zonder data (bv. geen X-token) tellen niet mee; de techniek telt altijd.";
  }

  $("settings-save").addEventListener("click", async () => {
    document.querySelectorAll(".field-err").forEach((n) => { n.textContent = ""; });
    try {
      await api("trading/settings", { json: readForm() });
      S.dirty = false;
      $("settings-msg").textContent = "Opgeslagen. Nieuwe paper-tests en de volgende live-run gebruiken deze instellingen.";
      toast("Instellingen opgeslagen", true);
      refresh();
    } catch (e) {
      const errs = (e.body && e.body.errors) || {};
      Object.entries(errs).forEach(([k, msg]) => { const n = $("set-" + k + "-err"); if (n) n.textContent = msg; });
      $("settings-msg").textContent = "Niet opgeslagen: controleer de rood gemarkeerde velden.";
      const first = Object.keys(errs)[0];
      if (first && $("set-" + first)) $("set-" + first).focus();
    }
  });
  $("settings-reset").addEventListener("click", async () => {
    if (!window.confirm("Alle instellingen terugzetten naar de standaardwaarden?")) return;
    await api("trading/settings/reset", { method: "POST" });
    S.dirty = false;
    refresh();
  });

  let resizeT = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeT);
    resizeT = setTimeout(() => {
      if (document.body.dataset.view === "trade" && S.detail) drawEquity($("equity-chart"), S.detail.equity, (S.detail.stats || {}).start_eur);
    }, 200);
  });

  window.MMTrading = { refresh };
  if (document.body.dataset.view === "trade") refresh();
})();
