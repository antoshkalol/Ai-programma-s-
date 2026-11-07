"""Samenvatting per markt (aandelen en crypto) van wat er het afgelopen uur binnenkwam.

Met een Anthropic API-sleutel schrijft Claude de samenvatting (en markeert hij hype/geruchten).
Zonder sleutel maakt de app zelf een automatische samenvatting op basis van de data.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import timedelta

from .config import MARKET_LABELS
from .db import Database, iso, utcnow
from .markets import crypto_proxies
from .scoring import fmt_num, fmt_usd

log = logging.getLogger(__name__)

EFFORT_UNSUPPORTED_PREFIXES = (
    "claude-3", "claude-haiku-4", "claude-sonnet-4-5", "claude-sonnet-4-2", "claude-opus-4-1", "claude-opus-4-2",
)
TICKER_OK = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")

SYSTEM_PROMPT = """Je bent een nuchtere financieel analist. Je schrijft periodiek een korte briefing voor een \
particuliere belegger die een dashboard op een monitor bekijkt. Elke briefing gaat over precies een markt: \
aandelen of crypto (zie "markt"). Je krijgt als JSON: het nieuws en de posts van de afgelopen periode (met \
betrouwbaarheid van de bron en het aantal bronnen dat hetzelfde meldt), nieuw gemelde grote transacties, en de \
huidige top volgens het scoremodel van het dashboard (met koerstrend en instapmoment).

Regels:
- Baseer je uitsluitend op de aangeleverde gegevens. Verzin geen cijfers, koersen, namen of gebeurtenissen.
- Scheid feiten van geruchten. Iets dat alleen door een minder betrouwbare bron of een enkele X-post wordt gemeld, \
noem je onbevestigd.
- unreliable_items: geef de indexnummers ("i") van berichten die hype, clickbait, promotie (pump, presales, \
'next 100x'), ongefundeerde geruchten of irrelevant voor beleggers zijn. Laat de lijst leeg als alles in orde is.
- Aandelen: transacties van politici worden tot 45 dagen later gemeld; zeg dat als je ze noemt. Bij insiders zeggen \
aankopen meer dan verkopen, en 10b5-1-plannen zijn vooraf ingepland en dus minder informatief.
- Crypto: grote on-chain transfers ('walvissen') zeggen niets over de richting; vaak zijn het interne verschuivingen \
van beurzen. Aankopen door bedrijven (uit SEC-meldingen) en door politici/insiders van crypto-ETF's zijn concreter.
- tickers: geef voor elk aandeel of elke munt die duidelijk in het nieuws of de transacties voorkomt een sentiment \
van -1 (zeer negatief) tot 1 (zeer positief), met een reden van maximaal 15 woorden. Gebruik het symbool (bv. NVDA, BTC).
- buy_view: bespreek in 2 tot 4 zinnen welke kandidaten uit de top er nu het interessantst uitzien, gezien score, \
koerstrend en instapmoment, en noem het grootste risico. Geef nooit garanties en formuleer het als inschatting; dit \
is geen beleggingsadvies.
- key_points: 3 tot 7 punten van maximaal 30 woorden, belangrijkste eerst. Was er weinig nieuws, zeg dat dan gewoon.
- headline: een zin die de periode samenvat.
- recommendation_comment: 2 tot 3 zinnen over "aanbeveling" (de technische analyse van het dashboard: long, short of \
afwachten, met instap, stop en doelen). Zeg of het nieuws en de transacties de richting ondersteunen of juist \
tegenspreken, en noem het grootste risico. Herhaal niet alle getallen. Schrijf "geen aanbeveling" als die ontbreekt."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "market_mood": {"type": "string", "enum": ["positief", "neutraal", "negatief", "gemengd"]},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "notable_trades": {"type": "array", "items": {"type": "string"}},
        "tickers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "sentiment": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["ticker", "sentiment", "reason"],
                "additionalProperties": False,
            },
        },
        "watch_out": {"type": "array", "items": {"type": "string"}},
        "unreliable_items": {"type": "array", "items": {"type": "integer"}},
        "buy_view": {"type": "string"},
        "recommendation_comment": {"type": "string"},
    },
    "required": [
        "headline", "market_mood", "key_points", "notable_trades", "tickers", "watch_out", "unreliable_items",
        "buy_view", "recommendation_comment",
    ],
    "additionalProperties": False,
}


def describe_trade(t: dict) -> str:
    what = t.get("ticker") or t.get("company") or "onbekend"
    role = f" ({t['actor_role']})" if t.get("actor_role") else ""
    if t.get("actor_type") == "whale":
        return f"{t['actor_name']} verplaatste {fmt_num(t.get('shares') or 0)} {what} ({fmt_usd(t.get('value_usd'))})"
    action = {"buy": "kocht", "sell": "verkocht"}.get(t["tx_type"], "meldde transactie in")
    if t.get("actor_type") == "politician" and t.get("value_low"):
        amount = (f"{fmt_usd(t['value_low'])}-{fmt_usd(t['value_high'])}" if t.get("value_high")
                  else f"meer dan {fmt_usd(t['value_low'])}")
    else:
        amount = fmt_usd(t.get("value_usd"))
    if t.get("actor_type") == "company" and t.get("shares"):
        amount = f"{fmt_num(t['shares'])} {what}, {amount}"
        return f"{t['actor_name']}{role} {action} {amount}"
    plan = " [10b5-1 plan]" if t.get("planned") else ""
    return f"{t['actor_name']}{role} {action} {what} voor {amount}{plan}"


def recommendation_payload(rec: dict | None) -> dict | None:
    if not rec:
        return None
    return {k: rec.get(k) for k in ("ticker", "label", "action", "conviction", "price", "entry_low", "entry_high",
                                     "stop", "target1", "target2", "rr", "prob", "signals")}


def trades_sql(market: str) -> tuple[str, list]:
    """WHERE-deel voor transacties van een markt. Crypto omvat ook crypto-ETF's/-aandelen van politici/insiders."""
    if market == "crypto":
        proxies = list(crypto_proxies())
        ph = ",".join("?" * len(proxies))
        return f"(market='crypto' OR (market='stocks' AND ticker IN ({ph})))", proxies
    return "market='stocks'", []


class Summarizer:
    def __init__(self, cfg, db: Database, ingestor=None):
        self.cfg = cfg
        self.db = db
        self.ingestor = ingestor
        self._client = None

    # ------------------------------------------------------------------ data
    def gather(self, market: str, start: str, end: str) -> dict:
        rows = self.db.query(
            "SELECT id, source, domain, kind, title, credibility, corroboration, cluster_id, tickers, sentiment,"
            " published_at FROM news WHERE market=? AND is_junk=0 AND kind != 'post' AND published_at >= ?"
            " AND published_at <= ? ORDER BY (credibility * corroboration) DESC LIMIT 150",
            (market, start, end),
        )
        # Eén bericht per verhaal (cluster): het meest geloofwaardige; 'corroboration' telt de bronnen
        news, seen_clusters = [], set()
        for r in rows:
            key = r["cluster_id"] or r["id"]
            if key in seen_clusters:
                continue
            seen_clusters.add(key)
            news.append(r)
        news = news[:60]
        posts = self.db.query(
            "SELECT id, source, author, title, tickers, published_at, credibility FROM news "
            "WHERE market=? AND is_junk=0 AND kind='post' AND published_at >= ? AND published_at <= ? "
            "ORDER BY published_at DESC LIMIT 30",
            (market, start, end),
        )
        filtered = self.db.query_one(
            "SELECT COUNT(*) n FROM news WHERE market=? AND is_junk=1 AND published_at >= ? AND published_at <= ?",
            (market, start, end),
        )["n"]
        where, args = trades_sql(market)
        trades = self.db.query(
            f"SELECT * FROM trades WHERE {where} AND created_at >= ? AND created_at <= ? "
            "AND (tx_type IN ('buy','sell') OR actor_type='whale') ORDER BY COALESCE(value_usd, 0) DESC LIMIT 40",
            (*args, start, end),
        )
        big_trades = [
            t for t in trades
            if (t["value_usd"] or 0) >= self.cfg.min_trade_value_usd or t["actor_type"] in ("politician", "company")
        ]
        scores = self.db.query("SELECT * FROM scores_latest WHERE market=? ORDER BY score DESC LIMIT 8", (market,))
        for s in scores:
            s["reasons"] = json.loads(s["reasons"] or "[]")
        for n in news + posts:
            n["tickers"] = json.loads(n["tickers"] or "[]")
        return {"news": news, "posts": posts, "filtered": filtered, "trades": big_trades or trades[:10],
                "scores": scores, "recommendation": self.db.get_state(f"recommendation_{market}")}

    # ------------------------------------------------------------------ run
    def run(self, now=None) -> int:
        end_dt = now or utcnow()
        start_dt = end_dt - timedelta(minutes=self.cfg.summary_interval_minutes)
        done = 0
        for market in self.cfg.markets:
            self.run_market(market, iso(start_dt), iso(end_dt))
            done += 1
        return done

    def run_market(self, market: str, start: str, end: str) -> dict:
        data = self.gather(market, start, end)
        content, method = None, "automatisch"
        if self.cfg.llm_enabled:
            try:
                content = self.llm_summary(market, data, start, end)
                method = "ai"
            except Exception as exc:  # noqa: BLE001 - dan valt hij terug op de automatische samenvatting
                log.error("AI-samenvatting (%s) mislukt, automatische samenvatting gebruikt: %s", market, exc)
                content = None
        if content is None:
            content = self.extractive_summary(market, data)
        content["stats"] = {
            "news": len(data["news"]),
            "posts": len(data["posts"]),
            "filtered": data["filtered"],
            "trades": len(data["trades"]),
        }
        if method == "ai":
            content["model"] = self.cfg.anthropic_model
        self.db.execute(
            "INSERT INTO summaries(market, period_start, period_end, created_at, method, content) VALUES (?,?,?,?,?,?)",
            (market, start, end, iso(utcnow()), method, json.dumps(content, ensure_ascii=False)),
        )
        return content

    # ------------------------------------------------------------------ AI
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.cfg.anthropic_api_key, timeout=180.0, max_retries=2)
        return self._client

    def ask_json(self, system: str, text: str, schema: dict, effort: str | None = None,
                 max_tokens: int = 16000) -> dict:
        """Eén Claude-aanroep met gestructureerde JSON-uitvoer (gebruikt door samenvatting en noodmelding)."""
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if not self.cfg.anthropic_model.startswith(EFFORT_UNSUPPORTED_PREFIXES):
            output_config["effort"] = effort or self.cfg.anthropic_effort
        kwargs = dict(
            model=self.cfg.anthropic_model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": text}],
            output_config=output_config,
        )
        client = self.client()
        if self.cfg.model_supports_fallback:
            # Bij een (zeldzame) weigering laat de API een ander model het verzoek afmaken
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = client.messages.create(**kwargs)
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"AI stopte met stop_reason={resp.stop_reason}")
        out_text = next((b.text for b in resp.content if b.type == "text"), None)
        if not out_text:
            raise RuntimeError("AI gaf geen tekst terug")
        return json.loads(out_text)

    def build_payload(self, market: str, data: dict, start: str, end: str) -> tuple[str, list[str]]:
        index_ids: list[str] = []
        news = []
        for n in data["news"] + data["posts"]:
            index_ids.append(n["id"])
            news.append(
                {
                    "i": len(index_ids) - 1,
                    "type": "x-post" if n.get("author") else n.get("kind", "news"),
                    "bron": n.get("source"),
                    "betrouwbaarheid": round(n.get("credibility") or 0, 2),
                    "aantal_bronnen": n.get("corroboration", 1),
                    "tickers": n.get("tickers", []),
                    "tijd": n.get("published_at"),
                    "titel": n.get("title"),
                }
            )
        type_names = {"politician": "politicus", "insider": "insider", "company": "bedrijf", "whale": "walvis"}
        trades = [
            {
                "beschrijving": describe_trade(t),
                "type": type_names.get(t["actor_type"], t["actor_type"]),
                "transactiedatum": t.get("tx_date"),
                "gemeld": t.get("filed_at"),
            }
            for t in data["trades"]
        ]
        scores = [
            {
                "ticker": s["ticker"],
                "score": s["score"],
                "kans_omhoog_1mnd": s["prob_up"],
                "instapmoment": s["timing"],
                "koers": s["price"],
                "redenen": s["reasons"][:4],
            }
            for s in data["scores"]
        ]
        lang = "Nederlands" if self.cfg.summary_language.lower().startswith("nl") else "English"
        payload = {
            "markt": MARKET_LABELS[market].lower(),
            "periode": {"van": start, "tot": end},
            "taal": lang,
            "nieuws_en_posts": news,
            "gefilterde_berichten": data["filtered"],
            "nieuwe_transacties": trades,
            "top_scores": scores,
            "aanbeveling": recommendation_payload(data.get("recommendation")),
        }
        text = f"Schrijf de briefing over de {MARKET_LABELS[market].lower()}markt in het {lang}.\n\n" + json.dumps(
            payload, ensure_ascii=False, indent=1
        )
        return text, index_ids

    def llm_summary(self, market: str, data: dict, start: str, end: str) -> dict:
        text, index_ids = self.build_payload(market, data, start, end)
        result = self.ask_json(SYSTEM_PROMPT, text, OUTPUT_SCHEMA)
        self._apply_ai_side_effects(market, result, index_ids)
        result.pop("unreliable_items", None)
        return result

    def _apply_ai_side_effects(self, market: str, result: dict, index_ids: list[str]) -> None:
        now = iso(utcnow())
        rows = []
        for t in result.get("tickers", []):
            tk = str(t.get("ticker", "")).upper().strip().lstrip("$")
            if not TICKER_OK.match(tk):
                continue
            try:
                s = max(-1.0, min(1.0, float(t.get("sentiment", 0))))
            except (TypeError, ValueError):
                continue
            rows.append((market, tk, now, s, str(t.get("reason", ""))[:200]))
        self.db.executemany(
            "INSERT INTO ticker_sentiment(market, ticker, created_at, sentiment, reason) VALUES (?,?,?,?,?)", rows
        )
        bad = [index_ids[i] for i in result.get("unreliable_items", []) if isinstance(i, int) and 0 <= i < len(index_ids)]
        if bad and self.ingestor is not None:
            self.ingestor.mark_unreliable(bad)
        result["filtered_by_ai"] = len(bad)

    # ------------------------------------------------------------------ zonder AI
    def extractive_summary(self, market: str, data: dict) -> dict:
        news = data["news"]
        posts = data["posts"]
        key_points = []
        for n in news[:6]:
            extra = f" ({n['corroboration']} bronnen)" if (n.get("corroboration") or 1) > 1 else ""
            key_points.append(f"{n['title']} - {n['source']}{extra}")
        for p in posts[:2]:
            key_points.append(f"{p['source']}: {p['title'][:160]}")
        if not key_points:
            key_points = ["Rustige periode: geen nieuwe geloofwaardige berichten binnengekomen."]

        sentiments = [n["sentiment"] for n in news if n.get("sentiment") is not None]
        avg = sum(sentiments) / len(sentiments) if sentiments else 0.0
        pos = sum(1 for s in sentiments if s > 0.15)
        neg = sum(1 for s in sentiments if s < -0.15)
        if not sentiments:
            mood = "neutraal"
        elif pos and neg and abs(pos - neg) <= max(1, len(sentiments) // 5):
            mood = "gemengd"
        else:
            mood = "positief" if avg > 0.1 else "negatief" if avg < -0.1 else "neutraal"

        per_ticker: dict[str, list[float]] = defaultdict(list)
        for n in news:
            for t in n["tickers"]:
                per_ticker[t].append(n["sentiment"] or 0)
        tickers = [
            {"ticker": t, "sentiment": round(sum(v) / len(v), 2), "reason": f"{len(v)} bericht(en) in deze periode"}
            for t, v in sorted(per_ticker.items(), key=lambda x: -len(x[1]))[:8]
        ]
        trades = [describe_trade(t) for t in data["trades"][:6]]
        if any(t["actor_type"] == "politician" for t in data["trades"]):
            trades.append("Let op: politici melden transacties tot 45 dagen later.")
        if any(t["actor_type"] == "whale" for t in data["trades"]):
            trades.append("Walvis-transfers zeggen niets over de richting (vaak beurs-intern).")

        top = [s for s in data["scores"] if s["score"] >= 55][:3]
        basis = ("bedrijfsaankopen, nieuws en koerstrend" if market == "crypto"
                 else "insider- en politici-aankopen, nieuws en koerstrend")
        if top:
            parts = [
                f"{s['ticker']} (score {s['score']:.0f}, kans omhoog ~{s['prob_up'] * 100:.0f}%, {s['timing'].lower()})"
                for s in top
            ]
            buy_view = (f"Hoogste scores op dit moment: {'; '.join(parts)}. Dit is een modelinschatting op basis van "
                        f"{basis}; geen beleggingsadvies.")
        else:
            buy_view = "Geen kandidaten met een duidelijk positief totaalsignaal op dit moment."

        watch = []
        if data["filtered"]:
            watch.append(f"{data['filtered']} bericht(en) weggefilterd als onbetrouwbaar of clickbait.")
        for n in news:
            if (n.get("sentiment") or 0) <= -0.5 and n.get("tickers"):
                watch.append(f"Negatief nieuws over {', '.join(n['tickers'][:3])}: {n['title']}")
            if len(watch) >= 4:
                break

        headline = news[0]["title"] if news else "Rustige periode zonder groot nieuws"
        rec = data.get("recommendation")
        if rec:
            sig = "; ".join(s[0].lower() + s[1:] for s in rec.get("signals", [])[:2])
            rr = f" Risico/rendement {str(rec['rr']).replace('.', ',')}." if rec.get("rr") else ""
            comment = f"{rec['label']} {rec['ticker']}: {sig}.{rr}"
        else:
            comment = "Nog geen aanbeveling (te weinig koershistorie)."
        return {
            "recommendation_comment": comment,
            "headline": headline,
            "market_mood": mood,
            "key_points": key_points,
            "notable_trades": trades,
            "tickers": tickers,
            "watch_out": watch,
            "buy_view": buy_view,
        }


def latest_summary(db: Database, market: str) -> dict | None:
    row = db.query_one("SELECT * FROM summaries WHERE market=? ORDER BY id DESC LIMIT 1", (market,))
    if not row:
        return None
    row["content"] = json.loads(row["content"])
    return row
