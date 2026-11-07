/* Market Monitor dashboard - vanilla JS, geen externe libraries (werkt ook offline op de Pi). */
(function () {
  "use strict";

  const REFRESH_MS = 60 * 1000;
  const ALERT_POLL_MS = 20 * 1000;
  const ROTATE_MS = 45 * 1000;
  const TEXT = {
    stocks: {
      label: "Aandelen", trades: "Grote transacties", tradesSub: "politici en insiders", asset: "Aandeel",
      news: "Nieuws", summary: "Samenvatting", scores: "Kooplijst", min: 100000,
      actors: [["all", "Alles"], ["politician", "Politici"], ["insider", "Insiders"]],
      refreshJobs: ["sec_form4", "rss_stocks", "google_news_stocks", "gdelt_stocks", "x_stocks", "quiver", "prices_stocks", "scoring_stocks"],
    },
    crypto: {
      label: "Crypto", trades: "Grote transacties", tradesSub: "bedrijven, walvissen en crypto-ETF's", asset: "Munt",
      news: "Nieuws", summary: "Samenvatting", scores: "Kooplijst", min: 0,
      actors: [["all", "Alles"], ["company", "Bedrijven"], ["whale", "Walvissen"], ["proxy", "Politici/insiders"]],
      refreshJobs: ["crypto_markets", "crypto_treasury", "btc_whales", "rss_crypto", "google_news_crypto", "gdelt_crypto", "x_crypto", "crypto_history", "scoring_crypto"],
    },
  };
  function store(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* geen opslag */ } }
  function recall(key, fallback) { try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; } }
  const hashMarket = (location.hash || "").replace("#", "");
  const state = {
    market: TEXT[hashMarket] ? hashMarket : (TEXT[recall("mm-market", "")] ? recall("mm-market", "") : "stocks"),
    actor: "all", type: "all", min: 100000, junk: "0", summaries: [], summaryId: null,
    markets: ["stocks", "crypto"], sound: recall("mm-sound", "off") === "on",
    rotate: hashMarket === "rotate" || recall("mm-rotate", "off") === "on",
    view: ["aanbeveling", "rec"].includes(hashMarket) ? "rec" : null,
    lastAlertId: +recall("mm-last-alert", "0"), alerts: [],
  };
  state.min = TEXT[state.market].min;
  state.view = state.view || state.market;
  const q = (path) => path + (path.includes("?") ? "&" : "?") + "market=" + state.market;

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
    if (Math.abs(v) > 0 && Math.abs(v) < 1) {
      // kleine crypto-koersen (bv. 0,00001234) met genoeg significante cijfers
      return sym + new Intl.NumberFormat("nl-NL", { maximumSignificantDigits: 4 }).format(v);
    }
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
  const actionLabel = (t) => (t.actor_type === "whale" ? "Transfer" : ACTION[t.tx_type] || t.tx_type);
  const MOOD = { positief: "pos", negatief: "neg", neutraal: "mid", gemengd: "mid" };
  const JOB_NAMES = {
    sec_form4: "SEC insiders", house: "Huis (VS)", senate: "Senaat (VS)", quiver: "Quiver",
    rss_stocks: "RSS", google_news_stocks: "Google News", gdelt_stocks: "GDELT", x_stocks: "X",
    prices_stocks: "Koersen", scoring_stocks: "Scores",
    crypto_markets: "CoinGecko", crypto_history: "Koershistorie", crypto_treasury: "Bedrijfsaankopen (SEC)",
    btc_whales: "BTC-walvissen", rss_crypto: "RSS", google_news_crypto: "Google News", gdelt_crypto: "GDELT",
    x_crypto: "X", scoring_crypto: "Scores",
    summary: "Samenvatting", alerts: "Noodmeldingen", calibration: "Kalibratie", cleanup: "Opschonen",
  };
  const KIND_NAMES = {
    koersexplosie: "Koersexplosie", bedrijf_koopt: "Bedrijf koopt", insider_cluster: "Insiders kopen samen",
    insider_groot: "Grote insider-aankoop", politicus_groot: "Grote aankoop politicus", koersuitbraak: "Koersuitbraak",
    groot_nieuws: "Groot nieuws", score_sprong: "Score-sprong",
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
    const o = await api(q("overview"));
    state.markets = o.markets.map((m) => m.key);
    document.querySelectorAll("#market-tabs button").forEach((b) => { b.hidden = b.dataset.market !== "rec" && !state.markets.includes(b.dataset.market); });
    if (!state.markets.includes(state.market) && state.markets.length) { switchMarket(state.markets[0]); return; }
    renderRecLink(o.recommendation);
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
    const shown = o.scheduled.filter((n) => !["calibration", "cleanup"].includes(n)
      && [state.market, "all"].includes(o.job_markets[n]));
    shown.forEach((name) => {
      const j = byName[name];
      let cls = "idle", ico = "…", title = "Nog niet gedraaid";
      if (j && j.last_error) { cls = "bad"; ico = "✕"; title = j.last_error; }
      else if (j && j.last_ok) { cls = "good"; ico = "✓"; title = "OK, " + timeAgo(j.last_ok) + " (" + j.items + " items)"; }
      h.appendChild(el("span", { class: "status " + cls, title }, el("span", { class: "ico", text: ico }), JOB_NAMES[name] || name));
    });
    const cfg = o.config;
    if (!cfg.sec_user_agent && (cfg.enable_sec_form4 || cfg.enable_crypto_treasury)) {
      h.appendChild(el("span", { class: "status bad", title: "Vul sec_user_agent in bij de add-on opties" },
        el("span", { class: "ico", text: "!" }), "SEC e-mail ontbreekt"));
    }
    $("updated").textContent = "Bijgewerkt " + fmtTime(o.now);
  }

  // ------------------------------------------------------------------ trades
  async function loadTrades() {
    const rows = await api(q(`trades?actor=${state.actor}&type=${state.type}&min_value=${state.min}&days=30&limit=200`));
    const body = $("trades-body");
    clear(body);
    $("trades-empty").hidden = rows.length > 0;
    rows.forEach((t) => {
      // In het crypto-tabblad openen ETF-/aandeeltransacties de onderliggende munt
      const target = state.market === "crypto" && t.proxy_for && t.proxy_for !== "crypto" ? t.proxy_for : t.ticker;
      const tickerCell = t.ticker
        ? el("button", { type: "button", class: "ticker-btn", text: t.ticker, onclick: () => openDrawer(target) })
        : el("span", { text: "-" });
      const tags = [];
      if (t.planned) tags.push(el("span", { class: "tag", title: "Vooraf ingepland via 10b5-1 handelsplan", text: "plan" }));
      if (t.party) tags.push(el("span", { class: "tag", text: t.party }));
      if (state.market === "crypto" && t.proxy_for) {
        tags.push(el("span", { class: "tag", title: "Aandeel/ETF dat crypto volgt", text: "volgt " + t.proxy_for }));
      }
      if (t.actor_type === "whale" && t.shares) {
        tags.push(el("span", { class: "tag", text: nf0.format(t.shares) + " " + (t.ticker || "") }));
      }
      const delay = t.delay_days;
      body.appendChild(
        el("tr", null,
          el("td", { title: fmtDate(t.filed_at, true) }, el("span", { text: fmtDate(t.filed_at) })),
          el("td", null, el("span", { class: "who", text: t.actor_name }), ...tags,
            el("span", { class: "role", text: " " + (t.actor_role || "") })),
          el("td", null, tickerCell, el("span", { class: "company", title: t.company || "", text: t.company || t.note || "" })),
          el("td", null, el("span", { class: "act " + t.tx_type, text: actionLabel(t) }),
            el("span", { class: "m-amount", text: fmtAmount(t) })),
          el("td", { class: "num" }, t.url ? el("a", { href: t.url, target: "_blank", rel: "noopener", text: fmtAmount(t) }) : fmtAmount(t)),
          el("td", { class: "num" + (delay > 20 ? " late" : "") }, delay === null || delay === undefined ? "-" : delay + " d"),
        ),
      );
    });
  }

  // ------------------------------------------------------------------ news
  async function loadNews() {
    const rows = await api(q(`news?junk=${state.junk}&limit=150`));
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
    const rows = await api(q("summaries?limit=24"));
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
      wrap.appendChild(el("h3", { text: state.market === "crypto" ? "Sentiment per munt" : "Sentiment per aandeel" }));
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
    const log = state.alerts.filter((a) => a.market === state.market);
    if (log.length) {
      wrap.appendChild(el("h3", { text: "Noodmeldingen (24 uur)" }));
      wrap.appendChild(el("ul", { class: "alerts-log" }, log.slice(0, 8).map((a) => el("li", { title: a.ai_note || "" },
        el("span", { class: "st", text: a.status === "verzonden" ? "verzonden" : "onderdrukt" }),
        fmtTime(a.created_at) + " " + a.title))));
    }
    body.appendChild(wrap);
  }

  // ------------------------------------------------------------------ scores
  async function loadScores() {
    const rows = await api(q("scores?limit=25"));
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
    try { data = await api(q("ticker/" + encodeURIComponent(ticker))); } catch (e) { $("d-name").textContent = "Kon gegevens niet laden"; return; }
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
      const names = state.market === "crypto"
        ? { institutional: "Instituties", news: "Nieuws", momentum: "Koers" }
        : { insider: "Insiders", politician: "Politici", news: "Nieuws", momentum: "Koers" };
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
        el("span", { class: "act " + t.tx_type, text: actionLabel(t) }), " ",
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

  // ------------------------------------------------------------------ aanbeveling
  const fmtPct = (v) => (v === null || v === undefined || isNaN(v) ? "" : (v >= 0 ? "+" : "") + nf1.format(v) + "%");
  const DIR = {
    long: { cls: "long", text: "LONG ▲" },
    short: { cls: "short", text: "SHORT ▼" },
    neutral: { cls: "neutral", text: "AFWACHTEN" },
  };
  function renderRecLink(rec) {
    const b = $("rec-link");
    if (!rec) { b.hidden = true; return; }
    clear(b);
    const cls = rec.direction === "long" ? "pos" : rec.direction === "short" ? "neg" : "mid";
    b.append(el("span", { class: "dot " + cls }), "Aanbeveling: " + (rec.direction === "neutral" ? "afwachten" : rec.label + " " + rec.ticker) + " ›");
    b.hidden = false;
  }
  $("rec-link").addEventListener("click", () => switchView("rec"));

  async function loadRecommendations() {
    const markets = state.markets.length ? state.markets : ["stocks", "crypto"];
    ["stocks", "crypto"].forEach((m) => { $("rec-" + m).hidden = !markets.includes(m); });
    await Promise.all(markets.map(async (m) => renderRecCard(m, await api("recommendation?market=" + m))));
  }

  function planItem(label, value, sub, keyColor) {
    return el("div", { class: "plan-item" },
      el("div", { class: "label" }, keyColor ? el("span", { class: "key", style: "background:" + keyColor }) : null, label),
      el("div", { class: "value", text: value }),
      sub ? el("div", { class: "sub", text: sub }) : null);
  }

  function renderRecCard(market, d) {
    const card = $("rec-" + market);
    clear(card);
    const t = TEXT[market];
    const head = el("div", { class: "panel-head" },
      el("h2", null, el("span", { class: "market-chip", style: "border-left-color: var(" + (market === "crypto" ? "--series-2" : "--series-1") + ")", text: t.label }),
        "Aanbeveling ", el("span", { class: "sub", text: "technische analyse + koopscore" })));
    card.appendChild(head);
    const body = el("div", { class: "panel-body" });
    card.appendChild(body);
    const rec = d.recommendation;
    if (!rec) {
      body.appendChild(el("p", { class: "empty", text: "Nog geen aanbeveling: er is minstens 60 dagen koershistorie en een koopscore nodig. Dit verschijnt na de eerste rondes." }));
      return;
    }
    const p = rec.plan, cur = rec.currency, price = p.price;
    const rel = (v) => (v ? fmtPct((v / price - 1) * 100) : "");
    const dir = DIR[p.direction] || DIR.neutral;
    const convMeter = el("span", { class: "meter" }, el("span"));
    convMeter.firstChild.style.width = p.conviction + "%";
    const openBtn = el("button", { type: "button", class: "linkbtn", text: "Details" });
    openBtn.addEventListener("click", () => { switchMarket(market); openDrawer(rec.ticker); });
    body.appendChild(el("div", { class: "rec-top" },
      el("div", { class: "rec-asset" },
        el("span", { class: "rec-ticker", text: rec.ticker }),
        el("span", { class: "rec-name", text: rec.name || "" }),
        el("span", { class: "rec-price", text: fmtPrice(price, cur) }),
        rec.chg_24h !== null && rec.chg_24h !== undefined ? el("span", { class: rec.chg_24h >= 0 ? "chg-pos" : "chg-neg", text: fmtPct(rec.chg_24h) + " 24u" }) : null,
        openBtn),
      el("div", { class: "dir-wrap" },
        el("span", { class: "dir-badge " + dir.cls, text: dir.text }),
        el("span", { class: "conv" }, "Overtuiging " + p.conviction + "%", convMeter))));
    body.appendChild(el("div", { class: "rec-action", text: p.action + (rec.since ? " · sinds " + fmtDate(rec.since, true) : "") }));

    const grid = el("div", { class: "plan-grid" });
    if (p.direction !== "neutral") {
      grid.append(
        planItem("Instapzone", fmtPrice(p.entry_low, cur) + " - " + fmtPrice(p.entry_high, cur), rel((p.entry_low + p.entry_high) / 2) + " t.o.v. nu", "var(--text-2)"),
        planItem("Stop-loss", fmtPrice(p.stop, cur), rel(p.stop) + (p.risk_pct ? " · risico " + nf1.format(p.risk_pct) + "%" : ""), "var(--critical)"),
        planItem("Doel 1", fmtPrice(p.target1, cur), rel(p.target1), "var(--good)"),
        planItem("Doel 2", fmtPrice(p.target2, cur), rel(p.target2), "var(--good)"),
        planItem("Risico / rendement", p.rr ? "1 : " + nf1.format(p.rr) : "-", "tot doel 1"),
        planItem(p.direction === "long" ? "Kans op stijging" : "Kans op daling", Math.round(p.prob * 100) + "%", rec.prob_source === "gekalibreerd" ? "gekalibreerd" : "modelschatting"),
        planItem("Horizon", "~" + p.horizon_days + (market === "stocks" ? " koersdagen" : " dagen"), "verwachting t/m " + fmtDate(rec.chart.future_dates[rec.chart.future_dates.length - 1])),
        planItem("Koopscore", nf0.format(rec.score), rec.timing || ""),
      );
    } else {
      grid.append(
        planItem("Steun", p.support ? fmtPrice(p.support, cur) : "-", rel(p.support)),
        planItem("Weerstand", p.resistance ? fmtPrice(p.resistance, cur) : "-", rel(p.resistance)),
        planItem("Verwachte bandbreedte", fmtPrice(p.expected.low, cur) + " - " + fmtPrice(p.expected.high, cur), "±1σ over ~" + p.horizon_days + " dagen"),
        planItem("Koopscore", nf0.format(rec.score), rec.timing || ""),
      );
    }
    body.appendChild(grid);

    body.appendChild(el("div", { class: "ta-legend" },
      el("span", null, el("span", { class: "key", style: "background: var(--series-1)" }), "Koers"),
      el("span", null, el("span", { class: "key", style: "background: var(--series-3)" }), "SMA 20"),
      el("span", null, el("span", { class: "key", style: "background: var(--series-2)" }), "SMA 50"),
      el("span", null, el("span", { class: "key band", style: "background: color-mix(in srgb, var(--muted) 22%, transparent)" }), "Bollinger-band"),
      el("span", null, el("span", { class: "key dash", style: "color: var(--muted)" }), "Trendkanaal"),
      el("span", null, el("span", { class: "key", style: "background: var(--axis)" }), "Steun / weerstand"),
      p.direction !== "neutral" ? el("span", null, el("span", { class: "key dash", style: "color: var(" + (p.direction === "long" ? "--pos" : "--neg") + ")" }), "Scenario") : null,
      el("span", null, el("span", { class: "key band", style: "background: color-mix(in srgb, var(" + (p.direction === "short" ? "--neg" : "--pos") + ") 18%, transparent)" }), "Verwachting ±1σ")));
    body.appendChild(el("div", { class: "ta-chart" }, taChart(rec, market)));
    body.appendChild(el("div", { class: "ta-chart" }, rsiChart(rec)));

    const cols = el("div", { class: "rec-cols" });
    cols.appendChild(el("div", null, el("h3", { text: "Technische signalen" }),
      el("ul", { class: "plain" }, p.signals.map((x) => el("li", { text: x })))));
    const right = el("div", null);
    if (rec.reasons && rec.reasons.length) {
      right.append(el("h3", { text: "Waarom (koopscore)" }), el("ul", { class: "plain" }, rec.reasons.slice(0, 5).map((x) => el("li", { text: x }))));
    }
    if (d.ai_comment) {
      right.append(el("h3", { text: d.comment_method === "ai" ? "Commentaar (AI)" : "Commentaar" }), el("div", { class: "rec-comment", text: d.ai_comment }));
    }
    cols.appendChild(right);
    body.appendChild(cols);

    if (d.history && d.history.length) {
      body.appendChild(el("h3", { text: "Eerdere aanbevelingen" }));
      body.appendChild(el("ul", { class: "plain" }, d.history.map((h) => {
        const dd = DIR[h.direction] || DIR.neutral;
        const ret = h.return_pct;
        return el("li", null,
          fmtDate(h.created_at, true) + " · ",
          el("strong", { text: dd.text.split(" ")[0] + " " + h.ticker }),
          " · " + h.status,
          ret !== null && ret !== undefined ? el("span", { class: "hist-ret " + (ret >= 0 ? "pos" : "neg"), text: " " + fmtPct(ret) + (h.direction === "short" ? " (short)" : "") }) : null);
      })));
    }
    body.appendChild(el("p", { class: "rec-note", text: "Schets op basis van technische analyse en het scoremodel, geen beleggingsadvies. Short gaan = inzetten op een daling (via CFD's, futures of short-ETF's); je verlies kan groter zijn dan je inleg. Gebruik altijd een stop-loss en beleg niet met geld dat je niet kunt missen." }));
  }

  function axisFmt(v, hi) {
    if (hi < 1) return new Intl.NumberFormat("nl-NL", { maximumSignificantDigits: 3 }).format(v);
    if (hi < 10) return nf2.format(v);
    return nf0.format(v);
  }

  function taChart(rec, market) {
    const c = rec.chart, p = rec.plan, cur = rec.currency;
    const n = c.close.length, h = c.future_dates.length, total = n - 1 + h;
    const W = 760, H = 340, m = { l: 52, r: 96, t: 12, b: 24 };
    const x = (i) => m.l + (i / total) * (W - m.l - m.r);
    const ok = (v) => v !== null && v !== undefined && !isNaN(v);
    const vals = c.close.concat(c.bb_upper.filter(ok), c.bb_lower.filter(ok), c.cone_low, c.cone_high);
    [p.stop, p.target1, p.target2, p.entry_low, p.entry_high].forEach((v) => { if (ok(v)) vals.push(v); });
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.06 || hi * 0.05;
    lo -= pad; hi += pad;
    const y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Koersgrafiek ${rec.ticker} met technische analyse en verwachting` });
    const add = (tag, attrs) => { const e = svg(tag, attrs); root.appendChild(e); return e; };
    const txt = (xx, yy, str, attrs) => { const e = add("text", Object.assign({ x: xx, y: yy }, attrs || {})); e.textContent = str; return e; };

    // toekomst-achtergrond
    add("rect", { x: x(n - 1), y: m.t, width: x(total) - x(n - 1), height: H - m.t - m.b, fill: "var(--surface-2)", opacity: 0.6 });
    txt(x(n - 1) + 6, m.t + 12, "verwachting", { class: "axis-label" });
    // raster
    niceTicks(lo, hi, 5).filter((v) => v >= lo && v <= hi).forEach((v) => {
      add("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 });
      txt(m.l - 6, y(v) + 4, axisFmt(v, hi), { class: "axis-label", "text-anchor": "end" });
    });
    add("line", { x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1 });
    const allDates = c.dates.concat(c.future_dates);
    let lastMonth = null;
    allDates.forEach((dt, i) => {
      const mo = dt.slice(0, 7);
      if (mo !== lastMonth) {
        if (lastMonth !== null) txt(x(i), H - 6, new Date(dt + "T00:00:00Z").toLocaleDateString("nl-NL", { month: "short" }), { class: "axis-label", "text-anchor": "middle" });
        lastMonth = mo;
      }
    });
    const line = (pts, attrs) => {
      const d = pts.filter((pt) => ok(pt[1])).map((pt, k) => (k ? "L" : "M") + x(pt[0]).toFixed(1) + "," + y(pt[1]).toFixed(1)).join(" ");
      return add("path", Object.assign({ d, fill: "none", "stroke-linejoin": "round", "stroke-linecap": "round" }, attrs));
    };
    // Bollinger-band
    const bbIdx = c.bb_upper.map((v, i) => i).filter((i) => ok(c.bb_upper[i]));
    if (bbIdx.length) {
      const d = bbIdx.map((i, k) => (k ? "L" : "M") + x(i) + "," + y(c.bb_upper[i])).join(" ") + " " +
        bbIdx.slice().reverse().map((i) => "L" + x(i) + "," + y(c.bb_lower[i])).join(" ") + " Z";
      add("path", { d, fill: "var(--muted)", "fill-opacity": 0.14, stroke: "none" });
    }
    // trendkanaal
    if (c.channel.length) {
      line(c.channel.map((r) => [r[0], r[1]]), { stroke: "var(--muted)", "stroke-width": 1.5, "stroke-dasharray": "5 4" });
      line(c.channel.map((r) => [r[0], r[2]]), { stroke: "var(--muted)", "stroke-width": 1, "stroke-dasharray": "2 4" });
      line(c.channel.map((r) => [r[0], r[3]]), { stroke: "var(--muted)", "stroke-width": 1, "stroke-dasharray": "2 4" });
    }
    const labels = [];
    // steun / weerstand
    c.levels.forEach((lv) => {
      if (lv.price < lo || lv.price > hi) return;
      add("line", { x1: m.l, x2: W - m.r, y1: y(lv.price), y2: y(lv.price), stroke: "var(--axis)", "stroke-width": 1 });
      labels.push({ v: lv.price, text: (lv.kind === "steun" ? "Steun " : "Weerst. ") + axisFmt(lv.price, hi), prio: 3 });
    });
    // plan: instapzone, stop, doelen (vanaf 'nu' naar rechts)
    const nowX = x(n - 1);
    if (p.direction !== "neutral") {
      const top = Math.max(p.entry_low, p.entry_high), bot = Math.min(p.entry_low, p.entry_high);
      add("rect", { x: nowX, y: y(top), width: x(total) - nowX, height: Math.max(2, y(bot) - y(top)), fill: "var(--text-2)", "fill-opacity": 0.16 });
      labels.push({ v: (top + bot) / 2, text: "Instap " + axisFmt((top + bot) / 2, hi), prio: 1 });
      add("line", { x1: nowX, x2: x(total), y1: y(p.stop), y2: y(p.stop), stroke: "var(--critical)", "stroke-width": 2 });
      labels.push({ v: p.stop, text: "Stop " + axisFmt(p.stop, hi), prio: 0, color: "var(--critical)" });
      [["Doel 1", p.target1], ["Doel 2", p.target2]].forEach(([lab, v]) => {
        add("line", { x1: nowX, x2: x(total), y1: y(v), y2: y(v), stroke: "var(--good)", "stroke-width": 2 });
        labels.push({ v, text: lab + " " + axisFmt(v, hi), prio: 0, color: "var(--good)" });
      });
    }
    // verwachtingskegel (±1σ)
    const coneColor = p.direction === "short" ? "var(--neg)" : "var(--pos)";
    const up = [[n - 1, p.price]].concat(c.cone_high.map((v, k) => [n + k, v]));
    const down = c.cone_low.map((v, k) => [n + k, v]).reverse().concat([[n - 1, p.price]]);
    add("path", { d: up.concat(down).map((pt, k) => (k ? "L" : "M") + x(pt[0]).toFixed(1) + "," + y(pt[1]).toFixed(1)).join(" ") + " Z", fill: coneColor, "fill-opacity": 0.14, stroke: "none" });
    line([[n - 1, p.price]].concat(c.cone_mid.map((v, k) => [n + k, v])), { stroke: coneColor, "stroke-width": 1, "stroke-opacity": 0.6 });
    // scenario
    if (c.scenario.length) {
      line(c.scenario.map((sp) => [n - 1 + sp[0], sp[1]]), { stroke: coneColor, "stroke-width": 2, "stroke-dasharray": "6 5" });
      const last = c.scenario[c.scenario.length - 1];
      add("circle", { cx: x(n - 1 + last[0]), cy: y(last[1]), r: 4, fill: coneColor, stroke: "var(--surface)", "stroke-width": 2 });
    }
    // koers + gemiddelden
    const closePts = c.close.map((v, i) => [i, v]);
    add("path", { d: "M" + x(0) + "," + (H - m.b) + " " + closePts.map((pt) => "L" + x(pt[0]).toFixed(1) + "," + y(pt[1]).toFixed(1)).join(" ") + " L" + x(n - 1) + "," + (H - m.b) + " Z", fill: "var(--series-1)", "fill-opacity": 0.08, stroke: "none" });
    line(c.sma50.map((v, i) => [i, v]), { stroke: "var(--series-2)", "stroke-width": 2 });
    line(c.sma20.map((v, i) => [i, v]), { stroke: "var(--series-3)", "stroke-width": 2 });
    line(closePts, { stroke: "var(--series-1)", "stroke-width": 2 });
    add("line", { x1: nowX, x2: nowX, y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1 });
    txt(nowX - 4, m.t + 12, "nu", { class: "axis-label", "text-anchor": "end" });
    add("circle", { cx: nowX, cy: y(p.price), r: 4, fill: "var(--series-1)", stroke: "var(--surface)", "stroke-width": 2 });
    labels.push({ v: p.price, text: axisFmt(p.price, hi), prio: 0, strong: true });

    // labels rechts, zonder overlap (belangrijkste eerst)
    const placed = [];
    labels.sort((a, b) => a.prio - b.prio).forEach((lb) => {
      const yy = y(lb.v);
      if (yy < m.t || yy > H - m.b || placed.some((py) => Math.abs(py - yy) < 12)) return;
      placed.push(yy);
      if (lb.color) add("line", { x1: W - m.r + 3, x2: W - m.r + 9, y1: yy, y2: yy, stroke: lb.color, "stroke-width": 2 });
      txt(W - m.r + 12, yy + 4, lb.text, { class: "lvl-label" + (lb.strong || lb.color ? " strong" : "") });
    });

    // crosshair + tooltip op de historie
    const cross = add("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" });
    const dot = add("circle", { r: 4, fill: "var(--series-1)", stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" });
    const hit = add("rect", { x: m.l, y: m.t, width: nowX - m.l, height: H - m.t - m.b, fill: "transparent", tabindex: "0" });
    let idx = n - 1;
    const show = (i, cx, cy) => {
      idx = i;
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(c.close[i])); dot.setAttribute("visibility", "visible");
      const row = (color, val, lab) => el("div", { class: "row" }, el("span", { class: "key", style: "background:" + color }), el("strong", { text: val }), el("span", { class: "muted", text: lab }));
      const rows = [el("div", { class: "muted", text: new Date(c.dates[i] + "T00:00:00Z").toLocaleDateString("nl-NL", { day: "numeric", month: "short", year: "numeric" }) }),
        row("var(--series-1)", fmtPrice(c.close[i], cur), "koers")];
      if (ok(c.sma20[i])) rows.push(row("var(--series-3)", fmtPrice(c.sma20[i], cur), "SMA 20"));
      if (ok(c.sma50[i])) rows.push(row("var(--series-2)", fmtPrice(c.sma50[i], cur), "SMA 50"));
      if (ok(c.rsi[i])) rows.push(el("div", { class: "row" }, el("strong", { text: nf0.format(c.rsi[i]) }), el("span", { class: "muted", text: "RSI" })));
      showTip(cx, cy, rows);
    };
    hit.addEventListener("pointermove", (e) => {
      const r = root.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * W;
      const i = Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / (W - m.l - m.r)) * total)));
      show(i, e.clientX, e.clientY);
    });
    const leave = () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); };
    hit.addEventListener("pointerleave", leave);
    hit.addEventListener("blur", leave);
    hit.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const i = Math.max(0, Math.min(n - 1, idx + (e.key === "ArrowRight" ? 1 : -1)));
      const r = root.getBoundingClientRect();
      show(i, r.left + (x(i) / W) * r.width, r.top + (y(c.close[i]) / H) * r.height);
    });
    return root;
  }

  function rsiChart(rec) {
    const c = rec.chart, n = c.close.length, h = c.future_dates.length, total = n - 1 + h;
    const W = 760, H = 86, m = { l: 52, r: 96, t: 8, b: 14 };
    const x = (i) => m.l + (i / total) * (W - m.l - m.r);
    const y = (v) => m.t + (1 - v / 100) * (H - m.t - m.b);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "RSI (14 dagen)" });
    const add = (tag, attrs) => { const e = svg(tag, attrs); root.appendChild(e); return e; };
    add("rect", { x: m.l, y: y(70), width: x(n - 1) - m.l, height: y(30) - y(70), fill: "var(--muted)", "fill-opacity": 0.08 });
    [30, 70].forEach((v) => {
      add("line", { x1: m.l, x2: x(n - 1), y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 });
      const t = add("text", { x: m.l - 6, y: y(v) + 4, class: "axis-label", "text-anchor": "end" });
      t.textContent = String(v);
    });
    const pts = c.rsi.map((v, i) => [i, v]).filter((pt) => pt[1] !== null && pt[1] !== undefined);
    add("path", { d: pts.map((pt, k) => (k ? "L" : "M") + x(pt[0]).toFixed(1) + "," + y(pt[1]).toFixed(1)).join(" "), fill: "none", stroke: "var(--series-1)", "stroke-width": 2, "stroke-linejoin": "round" });
    const last = pts[pts.length - 1];
    if (last) {
      add("circle", { cx: x(last[0]), cy: y(last[1]), r: 4, fill: "var(--series-1)", stroke: "var(--surface)", "stroke-width": 2 });
      const t = add("text", { x: W - m.r + 12, y: y(last[1]) + 4, class: "lvl-label strong" });
      t.textContent = "RSI " + nf0.format(last[1]);
    }
    return root;
  }

  // ------------------------------------------------------------------ testmelding
  let toastTimer = null;
  function toast(msg, good) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast " + (good ? "good" : "bad");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 7000);
  }
  $("btn-test").addEventListener("click", async () => {
    const btn = $("btn-test");
    btn.disabled = true;
    try {
      const r = await (await fetch("api/notify/test", { method: "POST" })).json();
      toast(r.message, r.ok);
    } catch (e) {
      toast("Testmelding mislukt: " + e.message, false);
    }
    btn.disabled = false;
  });

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
    const jobs = TEXT[state.market].refreshJobs.concat(["alerts"]);
    await Promise.all(jobs.map((j) => fetch("api/run/" + j, { method: "POST" }).catch(() => null)));
    setTimeout(() => { refreshAll(); btn.disabled = false; btn.textContent = "Verversen"; }, 8000);
  });

  // ------------------------------------------------------------------ markt wisselen
  function renderActorSeg() {
    const seg = $("seg-actor");
    clear(seg);
    TEXT[state.market].actors.forEach(([v, label]) => {
      seg.appendChild(el("button", { type: "button", "data-v": v, class: v === state.actor ? "on" : null, text: label }));
    });
  }
  function applyMarketTexts() {
    const t = TEXT[state.market];
    document.body.dataset.market = state.market;
    document.querySelectorAll("#market-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.market === state.view)));
    document.body.dataset.view = state.view;
    $("rec-view").hidden = state.view !== "rec";
    document.querySelector("main.grid").hidden = state.view === "rec";
    document.querySelectorAll("[data-market-chip]").forEach((c) => { c.textContent = t.label; });
    $("t-trades").textContent = t.trades;
    $("t-trades-sub").textContent = t.tradesSub;
    $("t-news").textContent = t.news;
    $("t-summary").textContent = t.summary;
    $("t-scores").textContent = t.scores;
    $("th-asset").textContent = t.asset;
    $("f-min").value = String(state.min);
    document.title = "Market Monitor - " + t.label;
    renderActorSeg();
  }
  function switchView(v) {
    if (v === state.view) return;
    if (v === "rec") {
      state.view = "rec";
      if (!state.rotate) history.replaceState(null, "", "#aanbeveling");
      closeDrawer();
      applyMarketTexts();
      document.title = "Market Monitor - Aanbeveling";
      refreshAll();
      return;
    }
    switchMarket(v);
  }
  function switchMarket(m) {
    if (!TEXT[m] || (m === state.market && state.view === m)) return;
    state.view = m;
    state.market = m;
    state.actor = "all";
    state.min = TEXT[m].min;
    state.summaryId = null;
    store("mm-market", m);
    if (!state.rotate) history.replaceState(null, "", "#" + m);
    closeDrawer();
    applyMarketTexts();
    refreshAll();
  }
  document.querySelectorAll("#market-tabs button").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.market)));

  let rotateTimer = null;
  function applyRotate() {
    $("btn-rotate").textContent = "Wissel: " + (state.rotate ? "aan" : "uit");
    clearInterval(rotateTimer);
    if (state.rotate) {
      rotateTimer = setInterval(() => {
        if ($("drawer").classList.contains("open")) return;
        const views = state.markets.concat(["rec"]);
        const i = views.indexOf(state.view);
        switchView(views[(i + 1) % views.length]);
      }, ROTATE_MS);
    }
  }
  $("btn-rotate").addEventListener("click", () => { state.rotate = !state.rotate; store("mm-rotate", state.rotate ? "on" : "off"); applyRotate(); });

  // ------------------------------------------------------------------ noodmeldingen
  let audioCtx = null;
  function beep() {
    if (!state.sound) return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      [0, 0.35, 0.7].forEach((t) => {
        const o = audioCtx.createOscillator(), g = audioCtx.createGain();
        o.type = "square"; o.frequency.value = 880;
        g.gain.setValueAtTime(0.0001, audioCtx.currentTime + t);
        g.gain.exponentialRampToValueAtTime(0.25, audioCtx.currentTime + t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + t + 0.25);
        o.connect(g).connect(audioCtx.destination);
        o.start(audioCtx.currentTime + t); o.stop(audioCtx.currentTime + t + 0.3);
      });
    } catch (e) { /* geen audio beschikbaar */ }
  }
  function applySound() { $("btn-sound").textContent = "Geluid: " + (state.sound ? "aan" : "uit"); }
  $("btn-sound").addEventListener("click", () => {
    state.sound = !state.sound;
    store("mm-sound", state.sound ? "on" : "off");
    applySound();
    if (state.sound) beep(); // ontgrendelt audio in de browser en laat horen hoe het klinkt
  });

  async function loadAlerts() {
    const rows = await api("alerts?hours=24");
    state.alerts = rows;
    const active = rows.filter((a) => a.status === "verzonden" && !a.acknowledged
      && Date.now() - parseTime(a.created_at).getTime() < 12 * 3600 * 1000);
    const bar = $("alertbar");
    const list = $("alert-list");
    clear(list);
    bar.hidden = active.length === 0;
    $("alert-count").textContent = active.length > 1 ? active.length + " actief" : "";
    active.forEach((a) => {
      const label = TEXT[a.market] ? TEXT[a.market].label : a.market;
      const view = el("button", { type: "button", text: "Bekijk" });
      view.addEventListener("click", () => { if (a.market !== state.market) switchMarket(a.market); if (a.asset) openDrawer(a.asset); });
      const ack = el("button", { type: "button", text: "Gezien" });
      ack.addEventListener("click", async () => { await fetch("api/alerts/" + a.id + "/ack", { method: "POST" }); loadAlerts(); });
      list.appendChild(el("li", null,
        el("div", { class: "alert-title" }, el("span", { class: "market-chip", style: "border-left-color: var(" + (a.market === "crypto" ? "--series-2" : "--series-1") + ")", text: label }),
          a.title, el("span", { class: "alert-meta", text: (KIND_NAMES[a.kind] || a.kind) + " · " + timeAgo(a.created_at) })),
        el("div", { class: "alert-msg", text: a.message || "" }),
        a.ai_note ? el("div", { class: "alert-ai", text: "AI: " + a.ai_note }) : null,
        el("div", { class: "alert-actions" }, view, ack)));
    });
    const newest = active.reduce((m, a) => Math.max(m, a.id), 0);
    if (newest > state.lastAlertId) {
      state.lastAlertId = newest;
      store("mm-last-alert", String(newest));
      bar.classList.remove("flash"); void bar.offsetWidth; bar.classList.add("flash");
      beep();
    }
  }

  // Thema: automatisch -> donker -> licht
  const themes = ["auto", "dark", "light"];
  function applyTheme(t) {
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    $("btn-theme").textContent = "Thema: " + ({ auto: "auto", dark: "donker", light: "licht" })[t];
  }
  let theme = recall("mm-theme", "auto");
  applyTheme(theme);
  $("btn-theme").addEventListener("click", () => {
    theme = themes[(themes.indexOf(theme) + 1) % themes.length];
    store("mm-theme", theme);
    applyTheme(theme);
  });

  function refreshAll() {
    const jobs = state.view === "rec"
      ? [loadOverview(), loadAlerts(), loadRecommendations()]
      : [loadOverview(), loadTrades(), loadNews(), loadAlerts().then(loadSummaries), loadScores()];
    return Promise.allSettled(jobs).then((res) => {
      res.forEach((r) => { if (r.status === "rejected") console.error(r.reason); });
    });
  }
  applyMarketTexts();
  applySound();
  applyRotate();
  refreshAll();
  setInterval(refreshAll, REFRESH_MS);
  setInterval(() => loadAlerts().catch(console.error), ALERT_POLL_MS);
})();
