/* Market Monitor - advies voor je eigen geld: budgetplan, mijn posities (verkoopadvies) en terugkijken op
   eerdere aanbevelingen ("kan ik nu nog kopen?"). Gebruikt de hulpfuncties uit app.js (window.MM). */
(function () {
  "use strict";
  const MM = window.MM;
  if (!MM) return;
  const { el, clear, fmtPrice, fmtDate, nf0, nf1, nf2 } = MM;
  const $ = (id) => document.getElementById(id);
  const MARKET = { stocks: "Aandelen", crypto: "Crypto" };
  const eurF = new Intl.NumberFormat("nl-NL", { style: "currency", currency: "EUR", maximumFractionDigits: 2 });
  const z = (v) => (Math.abs(v) < 0.005 ? 0 : v);     // geen "-0,00"
  const eur = (v) => (v === null || v === undefined || isNaN(v) ? "-" : eurF.format(z(v)));
  const eur0 = (v) => (v === null || v === undefined || isNaN(v) ? "-" : new Intl.NumberFormat("nl-NL", { style: "currency", currency: "EUR", maximumFractionDigits: Math.abs(v) >= 100 ? 0 : 2 }).format(v));
  const pct = (v) => (v === null || v === undefined || isNaN(v) ? "-" : (z(v) >= 0 ? "+" : "") + nf1.format(Math.abs(v) < 0.05 ? 0 : v) + "%");
  const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
  const S = { budget: null, holdings: null, busy: false, tickers: {} };

  async function req(path, body) {
    const opts = { headers: { Accept: "application/json" } };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await fetch("api/" + path, opts);
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) throw new Error((data && data.message) || path + " -> " + r.status);
    return data;
  }
  const num = (v) => {
    if (v === null || v === undefined || v === "") return null;
    const n = parseFloat(String(v).replace(",", "."));
    return isNaN(n) ? null : n;
  };

  // ------------------------------------------------------------------ venster (modal)
  const dlg = $("modal");
  function openModal(title, nodes) {
    $("modal-title").textContent = title;
    const body = $("modal-body");
    clear(body);
    [].concat(nodes).forEach((n) => n && body.appendChild(n));
    if (typeof dlg.showModal === "function") { if (!dlg.open) dlg.showModal(); } else dlg.setAttribute("open", "");
    body.scrollTop = 0;
  }
  function closeModal() { if (typeof dlg.close === "function") dlg.close(); else dlg.removeAttribute("open"); }
  $("modal-close").addEventListener("click", closeModal);
  dlg.addEventListener("click", (e) => { if (e.target === dlg) closeModal(); });

  // ------------------------------------------------------------------ koop-nu-oordeel
  function verdictBox(v, buy) {
    const yes = v.buy_now === true, no = v.buy_now === false;
    const box = el("div", { class: "verdict " + (yes ? "yes" : no ? "no" : "maybe") },
      el("div", { class: "verdict-head" },
        el("span", { class: "verdict-q", text: "Kan ik nu kopen?" }),
        el("span", { class: "verdict-a", text: yes ? "JA" : no ? "NEE" : "?" })),
      el("div", { class: "verdict-title", text: v.title }),
      el("div", { class: "verdict-text", text: v.text }),
      v.fresh ? el("div", { class: "verdict-fresh", text: v.fresh }) : null);
    if (buy) {
      box.appendChild(el("div", { class: "verdict-actions" },
        el("button", { type: "button", class: yes ? "primary" : null, text: "Ik heb dit gekocht", onclick: () => buyForm(buy) })));
    }
    return box;
  }

  // ------------------------------------------------------------------ gekocht invullen
  async function tickerList(market) {
    if (!S.tickers[market]) {
      try { S.tickers[market] = await req("tickers?market=" + market); } catch (e) { S.tickers[market] = []; }
    }
    return S.tickers[market];
  }

  function buyForm(pref) {
    pref = pref || {};
    const manual = !pref.ticker;
    const f = el("form", { class: "buy-form", novalidate: true });
    const market = el("select", { id: "bf-market", disabled: !manual },
      el("option", { value: "stocks", text: "Aandelen" }), el("option", { value: "crypto", text: "Crypto" }));
    market.value = pref.market || "stocks";
    const ticker = el("input", { type: "text", id: "bf-ticker", list: "bf-tickers", value: pref.ticker || "", required: true, autocomplete: "off", placeholder: "bv. NVDA of BTC", disabled: !manual });
    const dl = el("datalist", { id: "bf-tickers" });
    const amount = el("input", { type: "number", id: "bf-amount", min: "1", step: "any", inputmode: "decimal", value: pref.amount_eur ? Math.round(pref.amount_eur) : "", required: true, placeholder: "bv. 500" });
    const price = el("input", { type: "number", id: "bf-price", min: "0", step: "any", inputmode: "decimal", value: pref.price ? +(+pref.price).toPrecision(6) : "", placeholder: "koers bij aankoop" });
    const lv = pref.levels || {};
    const stop = el("input", { type: "number", step: "any", id: "bf-stop", value: lv.stop ? +(+lv.stop).toPrecision(6) : "" });
    const t1 = el("input", { type: "number", step: "any", id: "bf-t1", value: lv.target1 ? +(+lv.target1).toPrecision(6) : "" });
    const t2 = el("input", { type: "number", step: "any", id: "bf-t2", value: lv.target2 ? +(+lv.target2).toPrecision(6) : "" });
    const msg = el("p", { class: "form-msg", role: "alert" });
    async function fillList() {
      clear(dl);
      (await tickerList(market.value)).forEach((t) => dl.appendChild(el("option", { value: t.ticker, label: (t.name || "") + (t.last_price ? " · " + fmtPrice(t.last_price) : "") })));
    }
    market.addEventListener("change", fillList);
    ticker.addEventListener("change", async () => {
      const t = (await tickerList(market.value)).find((x) => x.ticker === ticker.value.toUpperCase());
      if (t && t.last_price && !price.value) price.value = +(+t.last_price).toPrecision(6);
    });
    if (manual) fillList();
    f.append(
      pref.ticker ? el("p", { class: "buy-what" }, el("strong", { text: pref.ticker }), " · " + MARKET[pref.market] + (pref.price ? " · koers nu " + fmtPrice(pref.price) : "")) : null,
      el("div", { class: "form-grid" },
        manual ? el("label", null, "Markt", market) : null,
        manual ? el("label", null, "Aandeel of munt", ticker, dl) : null,
        el("label", null, "Bedrag dat je hebt ingelegd (€)", amount),
        el("label", null, "Aankoopkoers ($, zoals in het dashboard)", price)),
      el("details", { class: "buy-levels", open: !!(lv.stop) },
        el("summary", { text: "Verkoopniveaus (stop-loss en doelen)" }),
        el("p", { class: "sub", text: "Overgenomen uit het advies. Leeg laten = automatisch op basis van de technische analyse. Je kunt ze later aanpassen." }),
        el("div", { class: "form-grid" }, el("label", null, "Stop-loss ($)", stop), el("label", null, "Doel 1 ($)", t1), el("label", null, "Doel 2 ($)", t2))),
      el("p", { class: "sub", text: "Je krijgt een melding als je moet verkopen: bij de stop-loss, bij doel 1 (deels verkopen), bij doel 2 of de trailing stop, en als de analyse omslaat." }),
      msg,
      el("div", { class: "form-row" }, el("button", { type: "submit", class: "primary", text: "Opslaan" }), el("button", { type: "button", text: "Annuleren", onclick: closeModal })));
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      msg.textContent = "";
      const body = {
        market: market.value, ticker: (ticker.value || pref.ticker || "").toUpperCase(), amount_eur: num(amount.value),
        price: num(price.value), rec_id: pref.rec_id || null, source: pref.source || "handmatig",
        stop: num(stop.value), target1: num(t1.value), target2: num(t2.value),
      };
      if (!body.ticker) { msg.textContent = "Kies een aandeel of munt."; ticker.focus(); return; }
      if (!body.amount_eur) { msg.textContent = "Vul het bedrag in."; amount.focus(); return; }
      try {
        const r = await req("holdings", body);
        closeModal();
        MM.toast(r.holding.ticker + " toegevoegd aan Mijn posities", true);
        loadHoldings();
      } catch (err) { msg.textContent = err.message; }
    });
    openModal(manual ? "Positie toevoegen" : "Ik heb " + pref.ticker + " gekocht", f);
    setTimeout(() => (manual ? ticker : amount).focus(), 50);
  }

  // ------------------------------------------------------------------ terugkijken op een aanbeveling
  function row(label, value, klass) {
    return el("div", { class: "kv-row" }, el("span", { text: label }), el("strong", { class: klass || null, text: value }));
  }
  function hitText(d) { return d ? "ja, op " + fmtDate(d) : "nee"; }

  async function openReview(id) {
    openModal("Aanbeveling", el("p", { class: "sub", text: "Laden..." }));
    let rv;
    try { rv = await req("recommendation/review/" + id); } catch (e) { openModal("Aanbeveling", el("p", { class: "form-msg", text: e.message })); return; }
    const t = rv.then, n = rv.now, cur = rv.currency;
    const dir = MM.DIR[t.direction] || MM.DIR.neutral;
    const buy = t.direction === "long" ? { market: rv.market, ticker: rv.ticker, price: n.price, rec_id: rv.id, source: "aanbeveling", levels: { stop: t.stop, target1: t.target1, target2: t.target2 } } : null;
    const nodes = [];
    nodes.push(el("div", { class: "review-top" },
      el("span", { class: "dir-badge " + dir.cls, text: dir.text }),
      el("span", { class: "rec-ticker", text: rv.ticker }),
      el("span", { class: "rec-name", text: rv.name || "" }),
      el("span", { class: "sub", text: MARKET[rv.market] + " · aanbevolen " + fmtDate(rv.created_at, true) + " (" + nf0.format(rv.age_days) + " dagen geleden)" })));
    nodes.push(verdictBox(rv.verdict, buy));
    const then = el("div", { class: "review-col" }, el("h3", { text: "Het advies toen" }),
      row("Advies", (t.label || "AFWACHTEN") + (t.action ? " · " + t.action : "")),
      row("Koers toen", fmtPrice(rv.issued_price, cur)));
    if (t.direction !== "neutral" && t.entry_low) {
      then.append(row("Instapzone", fmtPrice(t.entry_low, cur) + " - " + fmtPrice(t.entry_high, cur)),
        row("Stop-loss", fmtPrice(t.stop, cur)),
        row("Doel 1 / doel 2", fmtPrice(t.target1, cur) + " / " + fmtPrice(t.target2, cur)),
        row("Risico / rendement", t.rr ? "1 : " + nf1.format(t.rr) : "-"),
        row("Overtuiging · kans", (t.conviction || 0) + " · " + Math.round((t.prob || 0) * 100) + "%"));
    }
    if (t.change_reason) then.append(row("Waarom toen", t.change_reason));
    if (rv.outcome) then.append(row("Afgesloten", rv.outcome + (rv.closed_at ? " (" + fmtDate(rv.closed_at) + ")" : "")));
    if (t.signals && t.signals.length) then.append(el("ul", { class: "plain small" }, t.signals.map((x) => el("li", { text: x }))));
    const now = el("div", { class: "review-col" }, el("h3", { text: "Wat er sindsdien gebeurde" }),
      row("Koers nu", fmtPrice(n.price, cur) + (n.change_pct !== null ? " (" + pct(n.change_pct) + ")" : ""), cls(n.change_pct)));
    if (t.direction !== "neutral") {
      now.append(row("Resultaat als je het volgde", pct(n.followed_pct), cls(n.followed_pct)),
        row("Stop-loss geraakt", hitText(n.hits && n.hits.stop), n.hits && n.hits.stop ? "neg" : ""),
        row("Doel 1 gehaald", hitText(n.hits && n.hits.target1), n.hits && n.hits.target1 ? "pos" : ""),
        row("Doel 2 gehaald", hitText(n.hits && n.hits.target2), n.hits && n.hits.target2 ? "pos" : ""));
      if (n.status) now.append(row("Status van het plan", n.status.text));
    }
    if (rv.fresh) {
      const f = rv.fresh;
      now.append(el("h3", { text: "De analyse van nu" }),
        row("Richting", (f.label || "") + " · overtuiging " + f.conviction),
        f.direction !== "neutral" ? row("Nieuwe instapzone", fmtPrice(f.entry_low, cur) + " - " + fmtPrice(f.entry_high, cur)) : null,
        f.direction !== "neutral" ? row("Nieuwe stop / doel 1", fmtPrice(f.stop, cur) + " / " + fmtPrice(f.target1, cur)) : null);
    }
    nodes.push(el("div", { class: "review-cols" }, then, now));
    if (rv.chart) {
      nodes.push(el("h3", { text: "Grafiek van nu, met de niveaus van het advies van toen" }));
      const box = el("div", { class: "ta-chart" });
      nodes.push(box);
      requestAnimationFrame(() => {
        const w = box.clientWidth || 700;
        box.appendChild(MM.taChart(rv.chart, rv.market, w));
      });
    }
    if (rv.fresh && rv.fresh.indicators && rv.fresh.indicators.length) {
      const det = el("details", { class: "review-ta" }, el("summary", { text: "Technische analyse van nu in detail" }));
      det.appendChild(MM.taScorecard(rv.fresh));
      nodes.push(det);
    }
    nodes.push(el("p", { class: "rec-note", text: "Modelinschatting, geen beleggingsadvies. Gebruik altijd een stop-loss." }));
    openModal(rv.ticker + " · " + fmtDate(rv.created_at, true), nodes);
  }

  // ------------------------------------------------------------------ mijn posities
  const ADVICE = {
    hold: { cls: "hold", text: "Houden" }, partial: { cls: "sell", text: "Deels verkopen" }, sell: { cls: "sell", text: "Verkopen" },
    run: { cls: "run", text: "Laten lopen" }, warn: { cls: "warn", text: "Let op" }, unknown: { cls: "hold", text: "Geen koers" },
    closed: { cls: "hold", text: "Verkocht" },
  };

  function levelBar(h) {
    const L = h.live;
    if (!L.price) return null;
    const pts = [["Stop", L.stop, "stop"], ["Aankoop", h.entry_price, "entry"], ["Doel 1", h.target1, "t1"], ["Doel 2", h.target2, "t2"]];
    const lo = Math.min(L.stop, L.price, h.entry_price), hi = Math.max(h.target2, L.price);
    const pos = (v) => Math.max(0, Math.min(100, (v - lo) / (hi - lo || 1) * 100));
    const bar = el("div", { class: "lvl-bar", role: "img", "aria-label": "Koers " + fmtPrice(L.price) + " tussen stop " + fmtPrice(L.stop) + " en doel 2 " + fmtPrice(h.target2) });
    bar.appendChild(el("div", { class: "lvl-track" }));
    const fill = el("div", { class: "lvl-fill " + (L.price >= h.entry_price ? "pos" : "neg") });
    fill.style.left = Math.min(pos(h.entry_price), pos(L.price)) + "%";
    fill.style.width = Math.abs(pos(L.price) - pos(h.entry_price)) + "%";
    bar.appendChild(fill);
    pts.forEach(([label, v, k]) => {
      const m = el("div", { class: "lvl-mark " + k, title: label + " " + fmtPrice(v) });
      m.style.left = pos(v) + "%";
      bar.appendChild(m);
    });
    const now = el("div", { class: "lvl-now", title: "Nu " + fmtPrice(L.price) });
    now.style.left = pos(L.price) + "%";
    bar.appendChild(now);
    const legend = el("div", { class: "lvl-legend" },
      pts.map(([label, v, k]) => el("span", { class: k }, el("i"), label + " " + fmtPrice(v))));
    return el("div", { class: "lvl-wrap" }, bar, legend);
  }

  function holdingCard(h) {
    const L = h.live || {};
    const a = ADVICE[L.code] || ADVICE.hold;
    const closed = h.status === "closed";
    const card = el("article", { class: "holding " + a.cls + (closed ? " closed" : "") },
      el("div", { class: "holding-head" },
        el("div", null, el("strong", { class: "holding-ticker", text: h.ticker }), el("span", { class: "sub", text: " " + (h.name || "") + " · " + MARKET[h.market] })),
        el("span", { class: "advice-badge " + a.cls, text: a.text })),
      el("div", { class: "holding-figs" },
        el("div", null, el("span", { class: "label", text: "Ingelegd" }), el("span", { class: "value", text: eur(h.amount_eur) })),
        el("div", null, el("span", { class: "label", text: closed ? "Opbrengst" : "Waarde nu" }), el("span", { class: "value", text: eur(L.value_eur) })),
        el("div", null, el("span", { class: "label", text: "Resultaat" }), el("span", { class: "value " + cls(L.pnl_eur), text: eur(L.pnl_eur) + " (" + pct(L.pnl_pct) + ")" })),
        el("div", null, el("span", { class: "label", text: "Aankoop → nu" }), el("span", { class: "value", text: fmtPrice(h.entry_price) + " → " + fmtPrice(L.price) }))),
      el("div", { class: "holding-advice" }, el("strong", { text: L.title || "" }), el("span", { text: " " + (L.text || "") })),
      closed ? null : levelBar(h),
      el("div", { class: "holding-meta sub", text: "Gekocht " + fmtDate(h.bought_at) + (h.source ? " · via " + h.source : "") + (L.price_at ? (L.price_at.length === 10 ? " · slotkoers van " + fmtDate(L.price_at) : " · koers van " + MM.fmtTime(L.price_at)) : "") + (h.note ? " · " + h.note : "") }));
    const actions = el("div", { class: "form-row holding-actions" });
    if (!closed) {
      actions.append(
        el("button", { type: "button", text: "Verkocht", onclick: () => sellForm(h) }),
        el("button", { type: "button", text: "Aanpassen", onclick: () => editForm(h) }),
        el("button", { type: "button", text: "Grafiek", onclick: () => { MM.switchMarket(h.market); MM.openDrawer(h.ticker); } }));
    }
    actions.append(el("button", { type: "button", class: "danger", text: "Verwijderen", onclick: async () => {
      if (!window.confirm(h.ticker + " uit je posities verwijderen?")) return;
      await req("holdings/" + h.id + "/delete", {});
      loadHoldings();
    } }));
    card.appendChild(actions);
    return card;
  }

  function sellForm(h) {
    const price = el("input", { type: "number", step: "any", inputmode: "decimal", value: h.live && h.live.price ? +(+h.live.price).toPrecision(6) : "" });
    const msg = el("p", { class: "form-msg", role: "alert" });
    const f = el("form", { class: "buy-form" }, el("label", null, "Verkoopkoers ($)", price), msg,
      el("div", { class: "form-row" }, el("button", { type: "submit", class: "primary", text: "Verkocht" }), el("button", { type: "button", text: "Annuleren", onclick: closeModal })));
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { await req("holdings/" + h.id + "/close", { price: num(price.value) }); closeModal(); MM.toast(h.ticker + " verkocht", true); loadHoldings(); } catch (err) { msg.textContent = err.message; }
    });
    openModal(h.ticker + " verkocht", f);
  }

  function editForm(h) {
    const mk = (v) => el("input", { type: "number", step: "any", inputmode: "decimal", value: v ? +(+v).toPrecision(6) : "" });
    const fields = { amount_eur: mk(h.amount_eur), entry_price: mk(h.entry_price), stop: mk(h.stop), target1: mk(h.target1), target2: mk(h.target2) };
    const note = el("input", { type: "text", value: h.note || "", maxlength: "300" });
    const msg = el("p", { class: "form-msg", role: "alert" });
    const f = el("form", { class: "buy-form" },
      el("div", { class: "form-grid" },
        el("label", null, "Ingelegd (€)", fields.amount_eur), el("label", null, "Aankoopkoers ($)", fields.entry_price),
        el("label", null, "Stop-loss ($)", fields.stop), el("label", null, "Doel 1 ($)", fields.target1), el("label", null, "Doel 2 ($)", fields.target2),
        el("label", null, "Notitie", note)),
      msg, el("div", { class: "form-row" }, el("button", { type: "submit", class: "primary", text: "Opslaan" }), el("button", { type: "button", text: "Annuleren", onclick: closeModal })));
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { note: note.value };
      Object.entries(fields).forEach(([k, inp]) => { body[k] = num(inp.value); });
      try { await req("holdings/" + h.id, body); closeModal(); loadHoldings(); } catch (err) { msg.textContent = err.message; }
    });
    openModal(h.ticker + " aanpassen", f);
  }

  async function loadHoldings() {
    const box = $("holdings-body");
    let d;
    try { d = await req("holdings"); } catch (e) { return; }
    S.holdings = d;
    clear(box);
    if (!d.open.length) {
      box.appendChild(el("p", { class: "empty", text: "Nog geen posities. Klik bij een aanbeveling of in je budgetplan op 'Ik heb dit gekocht', of voeg er hier een toe. Je krijgt dan een melding wanneer je moet verkopen." }));
    } else {
      const t = d.totals;
      box.appendChild(el("div", { class: "plan-grid holdings-totals" },
        el("div", { class: "plan-item" }, el("div", { class: "label", text: "Ingelegd" }), el("div", { class: "value", text: eur(t.invested_eur) })),
        el("div", { class: "plan-item" }, el("div", { class: "label", text: "Waarde nu" }), el("div", { class: "value", text: eur(t.value_eur) })),
        el("div", { class: "plan-item" }, el("div", { class: "label", text: "Resultaat" }), el("div", { class: "value " + cls(t.pnl_eur), text: eur(t.pnl_eur) + " (" + pct(t.pnl_pct) + ")" })),
        el("div", { class: "plan-item" }, el("div", { class: "label", text: "Actie nodig" }), el("div", { class: "value", text: String(d.open.filter((h) => ["sell", "partial", "warn"].includes((h.live || {}).code)).length) }))));
      const list = el("div", { class: "holdings" });
      d.open.sort((a, b) => ({ sell: 0, partial: 1, warn: 2, run: 3, hold: 4 }[(a.live || {}).code] ?? 5) - ({ sell: 0, partial: 1, warn: 2, run: 3, hold: 4 }[(b.live || {}).code] ?? 5));
      d.open.forEach((h) => list.appendChild(holdingCard(h)));
      box.appendChild(list);
      if (!d.notify) box.appendChild(el("p", { class: "run-warn", text: "Meldingen staan uit (optie notify_holdings). Zet die aan om een melding te krijgen als je moet verkopen." }));
    }
    if (d.closed.length) {
      const det = el("details", { class: "closed-list" }, el("summary", { text: "Verkochte posities (" + d.closed.length + ")" }));
      const list = el("div", { class: "holdings" });
      d.closed.forEach((h) => list.appendChild(holdingCard(h)));
      det.appendChild(list);
      box.appendChild(det);
    }
  }
  $("holding-add").addEventListener("click", () => buyForm({}));

  // ------------------------------------------------------------------ budgetplan
  function profileHelp(profiles, key) {
    const p = profiles && profiles[key];
    if (!p) return "";
    return p.label + ": maximaal " + p.max_positions + " posities, hooguit " + nf1.format(p.risk_pct) + "% van je budget risico per positie, " +
      "maximaal " + p.max_pos_pct + "% per positie en " + p.max_crypto_pct + "% in crypto, minimaal " + p.min_cash_pct + "% in kas.";
  }

  function tile(label, value, sub, klass) {
    return el("div", { class: "plan-item" }, el("div", { class: "label", text: label }), el("div", { class: "value " + (klass || ""), text: value }), sub ? el("div", { class: "sub", text: sub }) : null);
  }

  function planItemCard(it, ai) {
    const verdict = ai && ai.items ? ai.items.find((x) => x.ticker === it.ticker) : null;
    const meter = el("span", { class: "meter" }, el("span"));
    meter.firstChild.style.width = it.certainty + "%";
    const card = el("article", { class: "plan-card " + (it.when === "nu" ? "now" : "wait") },
      el("div", { class: "plan-card-head" },
        el("div", null,
          el("span", { class: "market-chip", style: "border-left-color: var(" + (it.market === "crypto" ? "--series-2" : "--series-1") + ")", text: MARKET[it.market] }),
          el("strong", { class: "plan-ticker", text: it.ticker }), el("span", { class: "sub", text: " " + (it.name || "") })),
        el("span", { class: "action-badge " + (it.when === "nu" ? "now" : "wait"), text: it.action })),
      el("div", { class: "plan-card-amount" },
        el("span", { class: "big", text: eur0(it.amount_eur) }),
        el("span", { class: "sub", text: nf1.format(it.weight_pct) + "% van je budget · ~" + new Intl.NumberFormat("nl-NL", { maximumSignificantDigits: 4 }).format(it.qty) + (it.market === "crypto" ? " " + it.ticker : " stuks") })),
      el("div", { class: "plan-grid compact" },
        tile("Instapzone", fmtPrice(it.entry_low) + " - " + fmtPrice(it.entry_high), "koers nu " + fmtPrice(it.price)),
        tile("Stop-loss", fmtPrice(it.stop), pct(-it.risk_pct) + " · max. " + eur(it.max_loss_eur), "neg"),
        tile("Doel 1", fmtPrice(it.target1), pct(it.reward1_pct) + " · " + eur(it.profit_t1_eur), "pos"),
        tile("Doel 2", fmtPrice(it.target2), pct(it.reward2_pct) + " · " + eur(it.profit_t2_eur), "pos")),
      el("div", { class: "certainty" },
        el("span", { class: "label", text: "Zekerheid" }), meter,
        el("strong", { text: it.certainty + " (" + it.certainty_label + ")" }),
        el("span", { class: "sub", text: "overtuiging " + it.conviction + " · kans op stijging ~" + Math.round(it.prob * 100) + "% · risico/rendement 1 : " + nf1.format(it.rr) + " · verwachte waarde " + pct(it.ev_pct) })),
      el("p", { class: "plan-timing" }, el("strong", { text: "Wanneer: " }), it.timing),
      el("p", { class: "plan-sell" }, el("strong", { text: "Wanneer verkopen: " }), it.sell_plan),
      el("details", { class: "plan-why" }, el("summary", { text: "Onderbouwing" }),
        el("ul", { class: "plain small" }, it.signals.concat(it.reasons).map((x) => el("li", { text: x }))),
        it.news && it.news.length ? el("div", null, el("div", { class: "label", text: "Nieuws" }), el("ul", { class: "plain small" }, it.news.map((x) => el("li", { text: x })))) : null),
      verdict ? el("div", { class: "ai-verdict " + verdict.verdict }, el("strong", { text: "Claude: " + verdict.verdict + ". " }), verdict.reason) : null,
      el("div", { class: "form-row" },
        el("button", { type: "button", class: it.when === "nu" ? "primary" : null, text: "Ik heb dit gekocht", onclick: () => buyForm({ market: it.market, ticker: it.ticker, price: it.price, amount_eur: it.amount_eur, source: "budgetplan", levels: { stop: it.stop, target1: it.target1, target2: it.target2, atr: it.atr } }) }),
        el("button", { type: "button", text: "Grafiek", onclick: () => { MM.switchMarket(it.market); MM.openDrawer(it.ticker); } })));
    return card;
  }

  function renderPlan(plan, ai) {
    const box = $("budget-result");
    clear(box);
    if (!plan) {
      box.appendChild(el("p", { class: "empty", text: "Vul je budget in en klik op 'Bereken plan'. Het programma kiest dan de beste aandelen en munten, met hoeveel je instapt, wanneer, hoe zeker het model is en wanneer je verkoopt." }));
      return;
    }
    if (!plan.ok) { box.appendChild(el("p", { class: "empty", text: plan.message })); return; }
    const t = plan.totals;
    box.appendChild(el("div", { class: "plan-grid" },
      tile("Budget", eur0(t.budget), plan.profile_label),
      tile("Beleggen", eur0(t.invested), plan.items.length + " positie" + (plan.items.length === 1 ? "" : "s")),
      tile("In kas houden", eur0(t.cash), t.budget ? nf0.format(t.cash / t.budget * 100) + "% van je budget" : ""),
      tile("Zekerheid (gewogen)", t.certainty !== null ? t.certainty + " (" + t.certainty_label + ")" : "-", "modelinschatting"),
      tile("Winst bij doel 1", eur0(t.profit_t1), "als alles doel 1 haalt", "pos"),
      tile("Winst volgens plan", eur0(t.profit_t2), "helft bij doel 1, rest bij doel 2", "pos"),
      tile("Maximaal verlies", eur0(-t.max_loss), "als alle stops geraakt worden", "neg"),
      tile("Verwachte waarde", eur0(t.ev_eur), "kans x winst - kans x verlies - kosten", cls(t.ev_eur))));
    box.appendChild(el("div", { class: "summary-overview budget-analysis" }, el("strong", { text: "Analyse: " }), plan.analysis));
    if (ai) box.appendChild(el("div", { class: "rec-comment ai-overall" }, el("strong", { text: "Beoordeling door Claude (" + ai.model + "): " }), ai.overall));
    const tools = el("div", { class: "form-row budget-tools" },
      el("span", { class: "sub", text: "Berekend " + MM.fmtTime(plan.created_at) + " met de actuele koersen." }));
    if (S.budget && S.budget.ai_available && plan.items.length) {
      const b = el("button", { type: "button", text: "Laat Claude het plan beoordelen" });
      b.addEventListener("click", async () => {
        b.disabled = true;
        b.textContent = "Claude denkt na...";
        try { const r = await req("budget/ai", {}); renderPlan(plan, r.review); } catch (e) { MM.toast(e.message); b.disabled = false; b.textContent = "Laat Claude het plan beoordelen"; }
      });
      tools.appendChild(b);
    }
    box.appendChild(tools);
    const list = el("div", { class: "plan-cards" });
    plan.items.forEach((it) => list.appendChild(planItemCard(it, ai)));
    box.appendChild(list);
    if (plan.rejected && plan.rejected.length) {
      const det = el("details", { class: "plan-rejected" }, el("summary", { text: "Niet gekozen, en waarom (" + plan.rejected.length + ")" }));
      det.appendChild(el("ul", { class: "plain small" }, plan.rejected.map((r) => el("li", null, el("strong", { text: r.ticker }), " (" + MARKET[r.market] + ", zekerheid " + r.certainty + "): " + r.why_not))));
      box.appendChild(det);
    }
    box.appendChild(el("p", { class: "rec-note", text: "Alleen kopen (geen short). Bedragen zijn gebaseerd op risico: bij de stop-loss verlies je per positie hooguit het risico van je profiel. Zekerheid en kansen zijn modelinschattingen op basis van technische analyse, koopscore en nieuws: geen garantie en geen beleggingsadvies." }));
  }

  async function loadBudget() {
    let d;
    try { d = await req("budget"); } catch (e) { return; }
    S.budget = d;
    const st = d.settings;
    if (document.activeElement !== $("budget-amount") && st.amount) $("budget-amount").value = st.amount;
    $("budget-profile").value = st.profile;
    $("budget-markets").value = st.markets;
    $("budget-alerts").checked = st.alerts !== false;
    $("budget-profile-help").textContent = profileHelp(d.profiles, st.profile);
    renderPlan(d.plan);
  }
  $("budget-profile").addEventListener("change", () => { $("budget-profile-help").textContent = profileHelp(S.budget && S.budget.profiles, $("budget-profile").value); });
  $("budget-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("budget-go");
    btn.disabled = true;
    try {
      const r = await req("budget", { amount: num($("budget-amount").value), profile: $("budget-profile").value, markets: $("budget-markets").value, alerts: $("budget-alerts").checked });
      renderPlan(r.plan);
    } catch (err) { MM.toast(err.message); }
    btn.disabled = false;
  });

  async function refresh() {
    if (S.busy) return;
    S.busy = true;
    // Posities elke keer verversen; het budgetplan alleen als het ouder is dan 10 minuten en je er niet in bezig bent
    const busyInPlan = document.querySelector("#budget-result details[open]") || $("modal").open;
    const jobs = [loadHoldings()];
    if (!S.budgetAt || (Date.now() - S.budgetAt > 10 * 60 * 1000 && !busyInPlan)) {
      S.budgetAt = Date.now();
      jobs.push(loadBudget());
    }
    try { await Promise.all(jobs); } finally { S.busy = false; }
  }

  window.MMAdvice = { refresh, verdictBox, openReview, buyForm };
  if (document.body.dataset.view === "rec") refresh();
})();
