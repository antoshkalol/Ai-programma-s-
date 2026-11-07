/* Market Monitor dashboard - vanilla JS, geen externe libraries (werkt ook offline op de Pi). */
(function () {
  "use strict";

  const REFRESH_MS = 60 * 1000;
  const state = { actor: "all", type: "all", min: 100000, junk: "0", summaries: [], summaryId: null };

  // ------------------------------------------------------------------ helpers
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    }
    return node;
  }
  const svgNS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs) {
    const n = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    return n;
  }
  const $ = (id) => document.getElementById(id);
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  async function api(path, opts) {
    const r = await fetch("api/" + path, Object.assign({ headers: { Accept: "application/json" } }, opts || {}));
    if (!r.ok) throw new Error(path + " -> " + r.status);
    return r.json();
  }

  const nf1 = new Intl.NumberFormat("nl-NL", { maximumFractionDigits: 1, minimumFractionDigits: 1 });
  const nf0 = new Intl.NumberFormat("nl-NL", { maximumFractionDigits: 0 });
  const nf2 = new Intl.NumberFormat("nl-NL", { maximumFractionDigits: 2, minimumFractionDigits: 2 });

  function fmtUsd(v) {
    if (v === null || v === undefined || isNaN(v)) return "?";
    const a = Math.abs(v);
    if (a >= 1e9) return "$" + nf1.format(v / 1e9) + " mld";
    if (a >= 1e6) return "$" + nf1.format(v / 1e6) + " mln";
    if (a >= 1e3) return "$" + nf0.format(v / 1e3) + "k";
    return "$" + nf0.format(v);
  }
  function fmtAmount(t) {
    if (t.actor_type === "politician" && t.value_low) {
      if (!t.value_high) return "> " + fmtUsd(t.value_low - 1);
      if (t.value_high !== t.value_low) return fmtUsd(t.value_low) + "-" + fmtUsd(t.value_high);
    }
    return fmtUsd(t.value_usd);
  }
  function fmtPrice(v, cur) {
    if (v === null || v === undefined) return "-";
    const sym = !cur || cur === "USD" ? "$" : cur === "EUR" ? "€" : cur + " ";
    return sym + nf2.format(v);
  }
  function parseTime(s) { return s ? new Date(s.length === 10 ? s + "T00:00:00Z" : s) : null; }
  function timeAgo(s) {
    const d = parseTime(s);
    if (!d || isNaN(d)) return "";
    const sec = (Date.now() - d.getTime()) / 1000;
    if (sec < 60) return "zojuist";
    if (sec < 3600) return Math.floor(sec / 60) + " min geleden";
    if (sec < 86400) return Math.floor(sec / 3600) + " uur geleden";
    const days = Math.floor(sec / 86400);
    return days === 1 ? "gisteren" : days + " dagen geleden";
  }
  function fmtDate(s, withTime) {
    const d = parseTime(s);
    if (!d || isNaN(d)) return "";
    const o = withTime ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" } : { day: "numeric", month: "short" };
    return d.toLocaleString("nl-NL", o);
  }
  function fmtTime(s) {
    const d = parseTime(s);
    return d && !isNaN(d) ? d.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" }) : "";
  }
  const ACTION = { buy: "Koop", sell: "Verkoop", other: "Melding" };
  const MOOD = { positief: "pos", negatief: "neg", neutraal: "mid", gemengd: "mid" };
  const JOB_NAMES = {
    sec_form4: "SEC insiders", house: "Huis (VS)", senate: "Senaat (VS)", quiver: "Quiver", rss: "RSS",
    google_news: "Google News", gdelt: "GDELT", x_posts: "X", prices: "Koersen", scoring: "Scores",
    summary: "Samenvatting", calibration: "Kalibratie", cleanup: "Opschonen",
  };

  function grade(c) {
    if (c >= 0.8) return "A";
    if (c >= 0.6) return "B";
    if (c >= 0.45) return "C";
    return "D";
  }
  function timingClass(label) {
    const l = (label || "").toLowerCase();
    if (l.startsWith("gunstig")) return ["good", "✓"];
    if (l.startsWith("stijgende")) return ["good", "↗"];
    if (l.startsWith("oververhit")) return ["warn", "!"];
    if (l.startsWith("dalende")) return ["bad", "↘"];
    return ["neutral", "–"];
  }
  function divBar(value, label) {
    // value in [-1, 1]; 0 = neutraal (midden)
    const v = Math.max(-1, Math.min(1, value || 0));
    const fill = el("span", { class: "fill " + (v >= 0 ? "pos" : "neg") });
    fill.style.width = (Math.abs(v) * 50).toFixed(1) + "%";
    return el("div", { class: "divbar", role: "img", "aria-label": label || "" }, fill, el("span", { class: "mid" }));
  }

  // ------------------------------------------------------------------ tooltip
  const tip = $("tooltip");
  function showTip(x, y, nodes) {
    clear(tip);
    nodes.forEach((n) => tip.appendChild(n));
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let left = x + 14, top = y - r.height - 10;
    if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
    if (top < 8) top = y + 16;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }
  function hideTip() { tip.hidden = true; }

  // ------------------------------------------------------------------ overview
  async function loadOverview() {
    const o = await api("overview");
    const mood = $("mood");
    clear(mood);
    const s = o.summary && o.summary.content;
    if (s && s.market_mood) {
      mood.appendChild(el("span", null, el("span", { class: "dot " + (MOOD[s.market_mood] || "mid") }),
        "Stemming: ", el("strong", { text: s.market_mood })));
    }
    const st = $("stats");
    clear(st);
    st.append(
      el("span", null, "Transacties 24u ", el("strong", { text: nf0.format(o.stats.trades_24h) })),
      el("span", null, "Nieuws 24u ", el("strong", { text: nf0.format(o.stats.news_24h) })),
      el("span", null, "Gefilterd ", el("strong", { text: nf0.format(o.stats.filtered_24h) })),
    );
    const btnJunk = $("btn-junk");
    btnJunk.textContent = "Weggefilterd (" + nf0.format(o.stats.filtered_24h) + ")";

    const h = $("health");
    clear(h);
    const byName = Object.fromEntries(o.jobs.map((j) => [j.name, j]));
    const shown = o.scheduled.filter((n) => !["calibration", "cleanup"].includes(n));
    shown.forEach((name) => {
      const j = byName[name];
      let cls = "idle", ico = "…", title = "Nog niet gedraaid";
      if (j && j.last_error) { cls = "bad"; ico = "✕"; title = j.last_error; }
      else if (j && j.last_ok) { cls = "good"; ico = "✓"; title = "OK, " + timeAgo(j.last_ok) + " (" + j.items + " items)"; }
      h.appendChild(el("span", { class: "status " + cls, title }, el("span", { class: "ico", text: ico }), JOB_NAMES[name] || name));
    });
    const cfg = o.config;
    if (!cfg.sec_user_agent && cfg.enable_sec_form4) {
      h.appendChild(el("span", { class: "status bad", title: "Vul sec_user_agent in bij de add-on opties" },
        el("span", { class: "ico", text: "!" }), "SEC e-mail ontbreekt"));
    }
    $("updated").textContent = "Bijgewerkt " + fmtTime(o.now);
  }

  // ------------------------------------------------------------------ trades
  async function loadTrades() {
    const rows = await api(`trades?actor=${state.actor}&type=${state.type}&min_value=${state.min}&days=30&limit=200`);
    const body = $("trades-body");
    clear(body);
    $("trades-empty").hidden = rows.length > 0;
    rows.forEach((t) => {
      const tickerCell = t.ticker
        ? el("button", { type: "button", class: "ticker-btn", text: t.ticker, onclick: () => openDrawer(t.ticker) })
        : el("span", { text: "-" });
      const tags = [];
      if (t.planned) tags.push(el("span", { class: "tag", title: "Vooraf ingepland via 10b5-1 handelsplan", text: "plan" }));
      if (t.party) tags.push(el("span", { class: "tag", text: t.party }));
      const delay = t.delay_days;
      body.appendChild(
        el("tr", null,
          el("td", { title: fmtDate(t.filed_at, true) }, el("span", { text: fmtDate(t.filed_at) })),
          el("td", null, el("span", { class: "who", text: t.actor_name }), ...tags,
            el("span", { class: "role", text: " " + (t.actor_role || "") })),
          el("td", null, tickerCell, el("span", { class: "company", title: t.company || "", text: t.company || t.note || "" })),
          el("td", null, el("span", { class: "act " + t.tx_type, text: ACTION[t.tx_type] || t.tx_type }),
            el("span", { class: "m-amount", text: fmtAmount(t) })),
          el("td", { class: "num" }, t.url ? el("a", { href: t.url, target: "_blank", rel: "noopener", text: fmtAmount(t) }) : fmtAmount(t)),
          el("td", { class: "num" + (delay > 20 ? " late" : "") }, delay === null || delay === undefined ? "-" : delay + " d"),
        ),
      );
    });
  }

  // ------------------------------------------------------------------ news
  async function loadNews() {
    const rows = await api(`news?junk=${state.junk}&limit=150`);
    const list = $("news-list");
    clear(list);
    $("news-empty").hidden = rows.length > 0;
    rows.forEach((n) => {
      const c = n.credibility || 0;
      const meter = el("span", { class: "meter", title: "Geloofwaardigheid " + Math.round(c * 100) + "%" }, el("span"));
      meter.firstChild.style.width = Math.round(c * 100) + "%";
      const meta = [
        el("span", { text: n.source || n.domain }),
        el("span", { title: fmtDate(n.published_at, true), text: timeAgo(n.published_at) }),
      ];
      if (n.corroboration > 1) meta.push(el("span", { text: n.corroboration + " bronnen" }));
      if (n.kind === "press_release") meta.push(el("span", { class: "tag", text: "persbericht" }));
      if (n.kind === "post") meta.push(el("span", { class: "tag", text: "X-post" }));
      n.tickers.slice(0, 5).forEach((t) =>
        meta.push(el("button", { type: "button", class: "chip", text: t, onclick: () => openDrawer(t) })));
      if (n.is_junk && n.junk_reason) meta.push(el("span", { class: "junk-reason", text: n.junk_reason }));
      list.appendChild(
        el("li", null,
          el("div", { class: "cred" }, el("span", { class: "cred-grade", text: grade(c) }), meter),
          el("div", null,
            el("div", { class: "news-title" }, n.url ? el("a", { href: n.url, target: "_blank", rel: "noopener", text: n.title }) : n.title),
            el("div", { class: "news-meta" }, meta)),
        ),
      );
    });
  }

  // ------------------------------------------------------------------ summary
  async function loadSummaries() {
    const rows = await api("summaries?limit=24");
    state.summaries = rows;
    const sel = $("summary-select");
    const keep = state.summaryId;
    clear(sel);
    rows.forEach((s, i) => {
      sel.appendChild(el("option", { value: String(s.id), text: (i === 0 ? "Laatste: " : "") + fmtTime(s.period_start) + "-" + fmtTime(s.period_end) + " (" + fmtDate(s.period_end) + ")" }));
    });
    if (keep && rows.some((r) => r.id === keep) && keep !== (rows[0] && rows[0].id)) sel.value = String(keep);
    else state.summaryId = rows[0] ? rows[0].id : null;
    renderSummary();
  }

  function renderSummary() {
    const body = $("summary-body");
    clear(body);
    const s = state.summaries.find((x) => x.id === state.summaryId) || state.summaries[0];
    if (!s) {
      body.appendChild(el("p", { class: "empty", text: "De eerste samenvatting verschijnt binnen een paar minuten na het starten, daarna elk halfuur." }));
      $("summary-period").textContent = "";
      return;
    }
    const c = s.content;
    $("summary-period").textContent = fmtTime(s.period_start) + "-" + fmtTime(s.period_end);
    const wrap = el("div", { class: "summary" });
    wrap.appendChild(el("div", { class: "summary-headline", text: c.headline || "" }));
    const meta = el("div", { class: "summary-meta" },
      el("span", null, el("span", { class: "dot " + (MOOD[c.market_mood] || "mid") }), "Stemming: " + (c.market_mood || "-")),
      el("span", { text: s.method === "ai" ? "AI-samenvatting" + (c.model ? " (" + c.model + ")" : "") : "Automatische samenvatting" }));
    if (c.stats) meta.appendChild(el("span", { text: `${c.stats.news} berichten · ${c.stats.posts} posts · ${c.stats.filtered} gefilterd · ${c.stats.trades} transacties` }));
    wrap.appendChild(meta);

    const section = (title, items) => {
      if (!items || !items.length) return;
      wrap.appendChild(el("h3", { text: title }));
      wrap.appendChild(el("ul", null, items.map((t) => el("li", { text: t }))));
    };
    section("Kernpunten", c.key_points);
    section("Opvallende transacties", c.notable_trades);
    if (c.tickers && c.tickers.length) {
      wrap.appendChild(el("h3", { text: "Sentiment per aandeel" }));
      wrap.appendChild(el("div", { class: "tick-sent" }, c.tickers.slice(0, 12).map((t) => {
        const cls = t.sentiment > 0.15 ? "pos" : t.sentiment < -0.15 ? "neg" : "mid";
        return el("span", { title: t.reason || "" }, el("span", { class: "dot " + cls }),
          el("strong", { text: t.ticker }), (t.sentiment >= 0 ? "+" : "") + nf2.format(t.sentiment));
      })));
    }
    if (c.buy_view) {
      wrap.appendChild(el("h3", { text: "Koopvisie" }));
      wrap.appendChild(el("div", { class: "buy-view", text: c.buy_view }));
    }
    section("Let op", c.watch_out);
    body.appendChild(wrap);
  }

  // ------------------------------------------------------------------ scores
  async function loadScores() {
    const rows = await api("scores?limit=25");
    const list = $("scores-list");
    clear(list);
    $("scores-empty").hidden = rows.length > 0;
    rows.forEach((s, i) => {
      const [tcls, tico] = timingClass(s.timing);
      const chg = s.change_pct;
      const price = el("div", { class: "s-price" }, fmtPrice(s.price, s.currency) + " ",
        chg === null || chg === undefined ? "" : el("span", { class: chg >= 0 ? "chg-pos" : "chg-neg", text: (chg >= 0 ? "+" : "") + nf1.format(chg) + "%" }));
      const li = el("li", { tabindex: "0", role: "button", "aria-label": `${s.ticker}: score ${Math.round(s.score)}, kans omhoog ${Math.round((s.prob_up || 0) * 100)} procent` },
        el("span", { class: "rank", text: String(i + 1) }),
        el("div", null, el("div", { class: "s-ticker", text: s.ticker }), el("div", { class: "s-name", text: s.name || "" }), price),
        el("div", { class: "score-cell" }, el("span", { class: "score-num", text: nf0.format(s.score) }), divBar((s.score - 50) / 50, "score " + s.score)),
        el("div", { class: "prob", title: s.prob_source === "gekalibreerd" ? "Gekalibreerd op eerdere resultaten" : "Modelschatting (nog niet gekalibreerd)" },
          Math.round((s.prob_up || 0) * 100) + "%", el("small", { text: s.prob_source === "gekalibreerd" ? "gekalibreerd" : "model" })),
        el("span", { class: "timing " + tcls }, el("span", { class: "ico", text: tico }), s.timing || ""),
      );
      li.addEventListener("click", () => openDrawer(s.ticker));
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDrawer(s.ticker); } });
      li.addEventListener("pointermove", (e) => {
        const r = (s.reasons || []).slice(0, 4);
        if (!r.length) return;
        showTip(e.clientX, e.clientY, r.map((t) => el("div", { class: "row", text: "• " + t })));
      });
      li.addEventListener("pointerleave", hideTip);
      list.appendChild(li);
    });
  }

  // ------------------------------------------------------------------ chart
  function sma(values, n) {
    return values.map((_, i) => {
      if (i < n - 1) return null;
      let s = 0;
      for (let k = i - n + 1; k <= i; k++) s += values[k];
      return s / n;
    });
  }
  function niceTicks(min, max, count) {
    const span = max - min || 1;
    const step0 = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
    const start = Math.floor(min / step) * step;
    const ticks = [];
    for (let v = start; v <= max + step * 0.5; v += step) ticks.push(+v.toFixed(6));
    return ticks;
  }

  function priceChart(prices, currency) {
    const W = 520, H = 220, m = { l: 48, r: 56, t: 10, b: 24 };
    const closes = prices.map((p) => p.close);
    const avg = sma(closes, 50);
    const all = closes.concat(avg.filter((v) => v !== null));
    let lo = Math.min(...all), hi = Math.max(...all);
    const pad = (hi - lo) * 0.08 || 1;
    lo -= pad; hi += pad;
    const ticks = niceTicks(lo, hi, 4).filter((t) => t >= lo && t <= hi);
    const x = (i) => m.l + (i / Math.max(1, prices.length - 1)) * (W - m.l - m.r);
    const y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Koersgrafiek met 50-daags gemiddelde" });

    ticks.forEach((t) => {
      root.appendChild(svg("line", { x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), stroke: "var(--grid)", "stroke-width": 1 }));
      const lab = svg("text", { x: m.l - 6, y: y(t) + 4, "text-anchor": "end", class: "axis-label" });
      lab.textContent = nf0.format(t);
      root.appendChild(lab);
    });
    root.appendChild(svg("line", { x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1 }));
    // maand-labels
    let lastMonth = null;
    prices.forEach((p, i) => {
      const mo = p.date.slice(0, 7);
      if (mo !== lastMonth) {
        if (lastMonth !== null && x(i) < W - m.r - 20) {
          const lab = svg("text", { x: x(i), y: H - 6, "text-anchor": "middle", class: "axis-label" });
          lab.textContent = new Date(p.date + "T00:00:00Z").toLocaleDateString("nl-NL", { month: "short" });
          root.appendChild(lab);
        }
        lastMonth = mo;
      }
    });

    const pathOf = (vals) => vals.map((v, i) => (v === null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`))
      .reduce((acc, pt) => { if (pt === null) { acc.pen = "M"; return acc; } acc.d += acc.pen + pt + " "; acc.pen = "L"; return acc; }, { d: "", pen: "M" }).d;
    const area = `M${x(0)},${H - m.b} ` + closes.map((v, i) => `L${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ") + ` L${x(closes.length - 1)},${H - m.b} Z`;
    root.appendChild(svg("path", { d: area, fill: "var(--series-1)", "fill-opacity": "0.10", stroke: "none" }));
    root.appendChild(svg("path", { d: pathOf(avg), fill: "none", stroke: "var(--series-2)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    root.appendChild(svg("path", { d: pathOf(closes), fill: "none", stroke: "var(--series-1)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));

    // eindpunt + labels (selectief: alleen het laatste punt)
    const li = closes.length - 1;
    root.appendChild(svg("circle", { cx: x(li), cy: y(closes[li]), r: 4, fill: "var(--series-1)", stroke: "var(--surface)", "stroke-width": 2 }));
    const endLabels = [{ v: closes[li], t: nf0.format(closes[li]) }];
    if (avg[li] !== null) endLabels.push({ v: avg[li], t: "gem. " + nf0.format(avg[li]) });
    if (endLabels.length === 2 && Math.abs(y(endLabels[0].v) - y(endLabels[1].v)) < 13) endLabels.pop();
    endLabels.forEach((e) => {
      const t = svg("text", { x: x(li) + 8, y: y(e.v) + 4, class: "end-label" });
      t.textContent = e.t;
      root.appendChild(t);
    });

    // crosshair + tooltip
    const cross = svg("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" });
    const hoverDot = svg("circle", { r: 4, fill: "var(--series-1)", stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" });
    root.appendChild(cross);
    root.appendChild(hoverDot);
    const hit = svg("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent", tabindex: "0" });
    const at = (i, cx, cy) => {
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      hoverDot.setAttribute("cx", x(i)); hoverDot.setAttribute("cy", y(closes[i])); hoverDot.setAttribute("visibility", "visible");
      const rows = [
        el("div", { class: "muted", text: new Date(prices[i].date + "T00:00:00Z").toLocaleDateString("nl-NL", { day: "numeric", month: "short", year: "numeric" }) }),
        el("div", { class: "row" }, el("span", { class: "key", style: "background: var(--series-1)" }), el("strong", { text: fmtPrice(closes[i], currency) }), el("span", { class: "muted", text: "koers" })),
      ];
      if (avg[i] !== null) rows.push(el("div", { class: "row" }, el("span", { class: "key", style: "background: var(--series-2)" }), el("strong", { text: fmtPrice(avg[i], currency) }), el("span", { class: "muted", text: "50-daags gem." })));
      showTip(cx, cy, rows);
    };
    let focusIdx = li;
    hit.addEventListener("pointermove", (e) => {
      const r = root.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * W;
      const i = Math.max(0, Math.min(li, Math.round(((px - m.l) / (W - m.l - m.r)) * li)));
      focusIdx = i;
      at(i, e.clientX, e.clientY);
    });
    const leave = () => { cross.setAttribute("visibility", "hidden"); hoverDot.setAttribute("visibility", "hidden"); hideTip(); };
    hit.addEventListener("pointerleave", leave);
    hit.addEventListener("blur", leave);
    hit.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      focusIdx = Math.max(0, Math.min(li, focusIdx + (e.key === "ArrowRight" ? 1 : -1)));
      const r = root.getBoundingClientRect();
      at(focusIdx, r.left + (x(focusIdx) / W) * r.width, r.top + (y(closes[focusIdx]) / H) * r.height);
    });
    root.appendChild(hit);
    return root;
  }

  // ------------------------------------------------------------------ drawer
  async function openDrawer(ticker) {
    const d = $("drawer");
    $("d-title").textContent = ticker;
    $("d-name").textContent = "Laden...";
    const body = $("d-body");
    clear(body);
    d.classList.add("open");
    d.setAttribute("aria-hidden", "false");
    $("drawer-backdrop").hidden = false;
    d.focus();
    let data;
    try { data = await api("ticker/" + encodeURIComponent(ticker)); } catch (e) { $("d-name").textContent = "Kon gegevens niet laden"; return; }
    const meta = data.meta || {};
    $("d-name").textContent = (meta.name || "") + (meta.last_price ? " · " + fmtPrice(meta.last_price, meta.currency) : "");
    const sc = data.score;
    if (sc) {
      const [tcls, tico] = timingClass(sc.timing);
      body.appendChild(el("div", { class: "kpis" },
        el("div", { class: "kpi" }, el("div", { class: "label", text: "Score" }), el("div", { class: "value", text: nf0.format(sc.score) })),
        el("div", { class: "kpi" }, el("div", { class: "label", text: "Kans omhoog (~1 mnd)" }), el("div", { class: "value", text: Math.round(sc.prob_up * 100) + "%" })),
        el("div", { class: "kpi" }, el("div", { class: "label", text: "Instapmoment" }), el("div", { class: "timing " + tcls, style: "font-size:13px;margin-top:6px" }, el("span", { class: "ico", text: tico }), sc.timing)),
      ));
      body.appendChild(el("h3", { text: "Signalen (links negatief, rechts positief)" }));
      const names = { insider: "Insiders", politician: "Politici", news: "Nieuws", momentum: "Koers" };
      Object.entries(names).forEach(([k, label]) => {
        const v = sc.components[k] || 0;
        body.appendChild(el("div", { class: "comp" }, el("span", { text: label }), divBar(v, label + " " + v), el("span", { class: "v", text: (v >= 0 ? "+" : "") + nf2.format(v) })));
      });
      if (sc.reasons && sc.reasons.length) {
        body.appendChild(el("h3", { text: "Waarom" }));
        body.appendChild(el("ul", { class: "plain" }, sc.reasons.map((r) => el("li", { text: r }))));
      }
    }
    if (data.prices && data.prices.length > 25) {
      body.appendChild(el("h3", { text: "Koers (6 maanden)" }));
      body.appendChild(el("div", { class: "legend" },
        el("span", null, el("span", { class: "key", style: "background: var(--series-1)" }), "Slotkoers"),
        el("span", null, el("span", { class: "key", style: "background: var(--series-2)" }), "50-daags gemiddelde")));
      body.appendChild(el("div", { class: "chart-wrap" }, priceChart(data.prices, meta.currency)));
      const tableBox = el("div", { hidden: true });
      const last = data.prices.slice(-15).reverse();
      tableBox.appendChild(el("table", { class: "mini" },
        el("thead", null, el("tr", null, el("th", { text: "Datum" }), el("th", { class: "num", text: "Slotkoers" }))),
        el("tbody", null, last.map((p) => el("tr", null, el("td", { text: p.date }), el("td", { class: "num", text: fmtPrice(p.close, meta.currency) }))))));
      const toggle = el("button", { type: "button", class: "linkbtn", text: "Toon als tabel" });
      toggle.addEventListener("click", () => { tableBox.hidden = !tableBox.hidden; toggle.textContent = tableBox.hidden ? "Toon als tabel" : "Verberg tabel"; });
      body.appendChild(toggle);
      body.appendChild(tableBox);
    } else {
      body.appendChild(el("p", { class: "empty", text: "Nog geen koersdata voor dit aandeel." }));
    }
    body.appendChild(el("h3", { text: "Transacties (90 dagen)" }));
    if (data.trades.length) {
      body.appendChild(el("ul", { class: "plain" }, data.trades.map((t) => el("li", null,
        el("span", { class: "act " + t.tx_type, text: ACTION[t.tx_type] }), " ",
        el("strong", { text: t.actor_name }), el("span", { class: "role", text: " " + (t.actor_role || "") + " · " }),
        t.url ? el("a", { href: t.url, target: "_blank", rel: "noopener", text: fmtAmount(t) }) : fmtAmount(t),
        el("span", { class: "role", text: " · gemeld " + fmtDate(t.filed_at) + (t.delay_days !== null && t.delay_days !== undefined ? " (" + t.delay_days + " d na transactie)" : "") })))));
    } else body.appendChild(el("p", { class: "empty", text: "Geen transacties gevonden." }));
    body.appendChild(el("h3", { text: "Nieuws (7 dagen)" }));
    if (data.news.length) {
      body.appendChild(el("ul", { class: "plain" }, data.news.map((n) => el("li", null,
        el("strong", { text: grade(n.credibility || 0) + " " }),
        n.url ? el("a", { href: n.url, target: "_blank", rel: "noopener", text: n.title }) : n.title,
        el("span", { class: "role", text: " · " + (n.source || "") + " · " + timeAgo(n.published_at) })))));
    } else body.appendChild(el("p", { class: "empty", text: "Geen recente berichten." }));
  }
  function closeDrawer() {
    const d = $("drawer");
    d.classList.remove("open");
    d.setAttribute("aria-hidden", "true");
    $("drawer-backdrop").hidden = true;
    hideTip();
  }

  // ------------------------------------------------------------------ events
  document.querySelectorAll(".seg").forEach((seg) => {
    seg.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      state[seg.dataset.filter] = b.dataset.v;
      if (seg.dataset.filter === "junk") loadNews().catch(console.error);
      else loadTrades().catch(console.error);
    });
  });
  $("f-min").addEventListener("change", (e) => { state.min = +e.target.value; loadTrades().catch(console.error); });
  $("summary-select").addEventListener("change", (e) => { state.summaryId = +e.target.value; renderSummary(); });
  $("d-close").addEventListener("click", closeDrawer);
  $("drawer-backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  $("btn-full").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => {});
  });
  $("btn-refresh").addEventListener("click", async () => {
    const btn = $("btn-refresh");
    btn.disabled = true;
    btn.textContent = "Bezig...";
    const jobs = ["sec_form4", "rss", "google_news", "gdelt", "x_posts", "quiver", "prices", "scoring"];
    await Promise.all(jobs.map((j) => fetch("api/run/" + j, { method: "POST" }).catch(() => null)));
    setTimeout(() => { refreshAll(); btn.disabled = false; btn.textContent = "Verversen"; }, 8000);
  });

  // Thema: automatisch -> donker -> licht
  const themes = ["auto", "dark", "light"];
  function applyTheme(t) {
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    $("btn-theme").textContent = "Thema: " + ({ auto: "auto", dark: "donker", light: "licht" })[t];
  }
  let theme = "auto";
  try { theme = localStorage.getItem("mm-theme") || "auto"; } catch (e) { /* geen opslag beschikbaar */ }
  applyTheme(theme);
  $("btn-theme").addEventListener("click", () => {
    theme = themes[(themes.indexOf(theme) + 1) % themes.length];
    try { localStorage.setItem("mm-theme", theme); } catch (e) { /* negeren */ }
    applyTheme(theme);
  });

  function refreshAll() {
    return Promise.allSettled([loadOverview(), loadTrades(), loadNews(), loadSummaries(), loadScores()]).then((res) => {
      res.forEach((r) => { if (r.status === "rejected") console.error(r.reason); });
    });
  }
  refreshAll();
  setInterval(refreshAll, REFRESH_MS);
})();
