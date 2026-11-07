"""Noodmeldingen: direct een alert als er iets groots aan lijkt te komen.

De samenvatting komt elk uur. Daartussen kijkt deze module elke 2 minuten of er een uitzonderlijk signaal is:

Crypto
  * koersexplosie : sterke stijging binnen 1 uur samen met een volumepiek (of een extreme stijging)
  * bedrijf_koopt : een bedrijf meldt een zeer grote aankoop van een munt (SEC 8-K)
Aandelen
  * insider_cluster : drie of meer insiders kochten binnen 7 dagen hetzelfde aandeel
  * insider_groot   : een topbestuurder kocht voor een zeer groot bedrag
  * politicus_groot : een politicus meldt een aankoop van meer dan $1 mln
  * koersuitbraak   : een gevolgd aandeel staat vandaag sterk hoger
Beide markten
  * groot_nieuws : positief nieuws met een duidelijke aanleiding (goedkeuring, overname, ETF, ...) dat door
                   minstens drie betrouwbare bronnen tegelijk wordt gemeld
  * score_sprong : de koopscore springt binnen een uur sterk omhoog naar 75+

Elke melding heeft een afkoelperiode per aandeel/munt en er is een daglimiet. Met een API-sleutel kan Claude
een melding eerst beoordelen, zodat hype en losse geruchten geen alarm geven. Een signaal is geen garantie:
het betekent dat er ongewone activiteit is die vaak samengaat met grote bewegingen.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import timedelta

from .config import MARKET_LABELS
from .db import Database, ago, iso, parse_iso, utcnow
from .scoring import SENIOR_TITLES, fmt_num, fmt_pct, fmt_price, fmt_usd

log = logging.getLogger(__name__)

COOLDOWN_HOURS = 6
SAME_ASSET_HOURS = 2
CATALYST_RE = re.compile(
    r"\b(approv(?:e|es|ed|al)|greenlights?|ETFs?|strategic (?:bitcoin |crypto )?reserve|acquir(?:es|ed|ition)|"
    r"buys|bought|balance sheet|legal tender|listing|lists|partnership|partners with|wins (?:lawsuit|case)|"
    r"dismiss(?:es|ed)|settle(?:s|d|ment)|rate cut|beats|raises guidance|record (?:revenue|profit|quarter)|"
    r"takeover|merger|buyout|FDA|breakthrough|goedkeuring|overname)\b",
    re.I,
)

AI_SYSTEM = """Je bewaakt een beleggingsdashboard. Een automatische detector heeft een mogelijk groot koersmoment \
gevonden. Jij beslist of dit een directe noodmelding naar de telefoon van de gebruiker rechtvaardigt.
Stuur alleen door bij iets wat echt uitzonderlijk en relevant is. Niet doorsturen bij hype, promotie, een enkel \
gerucht, een bericht van een minder betrouwbare bron, of een beweging die vooral al achter de rug lijkt.
Baseer je alleen op de aangeleverde gegevens en verzin niets. Antwoord in het Nederlands.
- send: true of false
- title: korte titel van maximaal 60 tekens
- explanation: maximaal 2 zinnen: wat er gebeurt en waarom het belangrijk kan zijn
- risk: 1 zin over het grootste risico (bijvoorbeeld: stijging al grotendeels gebeurd, onbevestigd nieuws)"""

AI_SCHEMA = {
    "type": "object",
    "properties": {
        "send": {"type": "boolean"},
        "title": {"type": "string"},
        "explanation": {"type": "string"},
        "risk": {"type": "string"},
    },
    "required": ["send", "title", "explanation", "risk"],
    "additionalProperties": False,
}


class AlertEngine:
    def __init__(self, cfg, db: Database, hass=None, summarizer=None):
        self.cfg = cfg
        self.db = db
        self.hass = hass
        self.summarizer = summarizer

    @property
    def mult(self) -> float:
        return self.cfg.sensitivity

    # ------------------------------------------------------------------ hoofdlus
    def run(self) -> int:
        if not self.cfg.alerts_enabled:
            return 0
        sent = 0
        checks = []
        if "crypto" in self.cfg.markets:
            checks += [self.check_crypto_spikes, self.check_company_buys]
        if "stocks" in self.cfg.markets:
            checks += [self.check_insider_clusters, self.check_big_politician, self.check_stock_breakouts]
        for market in self.cfg.markets:
            checks += [lambda m=market: self.check_news(m), lambda m=market: self.check_score_jumps(m)]
        for check in checks:
            try:
                for cand in check():
                    sent += self.emit(cand)
            except Exception as exc:  # noqa: BLE001 - één kapotte controle mag de rest niet stoppen
                log.error("Alertcontrole %s mislukt: %s", getattr(check, "__name__", "?"), exc)
        return sent

    # ------------------------------------------------------------------ crypto
    def check_crypto_spikes(self) -> list[dict]:
        last = self.db.query_one("SELECT MAX(ts) ts FROM crypto_snapshots")
        last_ts = parse_iso(last["ts"]) if last else None
        if not last_ts or last_ts < utcnow() - timedelta(minutes=15):
            return []  # geen verse koersen
        ref = self.db.query_one(
            "SELECT MAX(ts) ts FROM crypto_snapshots WHERE ts <= ? AND ts >= ?",
            (iso(last_ts - timedelta(minutes=50)), iso(last_ts - timedelta(minutes=80))),
        )
        ref_rows = {}
        if ref and ref["ts"]:
            ref_rows = {r["coin"]: r for r in self.db.query("SELECT * FROM crypto_snapshots WHERE ts=?", (ref["ts"],))}
        watch = set(self.cfg.crypto_watchlist)
        out = []
        for r in self.db.query("SELECT * FROM crypto_snapshots WHERE ts=?", (last["ts"],)):
            chg = r["chg_1h"]
            if chg is None:
                continue
            rank = r["rank"] or 999
            thr = (4.0 if rank <= 2 else 7.0 if rank <= 20 else 12.0) * self.mult
            prev = ref_rows.get(r["coin"])
            vol_ratio = r["volume"] / prev["volume"] if prev and prev["volume"] and r["volume"] else None
            strong = chg >= thr and vol_ratio is not None and vol_ratio >= 1.06
            extreme = chg >= 2 * thr
            if not (strong or extreme):
                continue
            if rank > 30 and r["coin"] not in watch and not extreme:
                continue  # kleinere munten alleen bij extreme bewegingen
            reasons = [f"{fmt_pct(chg)} in 1 uur (drempel voor deze munt: {thr:.0f}%)"]
            if vol_ratio:
                reasons.append(f"Handelsvolume (24u) {(vol_ratio - 1) * 100:+.0f}% ten opzichte van een uur geleden")
            if r["chg_24h"] is not None:
                reasons.append(f"{fmt_pct(r['chg_24h'])} in 24 uur, marktwaarde-rang #{rank}")
            out.append({
                "market": "crypto", "asset": r["coin"], "kind": "koersexplosie",
                "title": f"{r['coin']} stijgt hard: {fmt_pct(chg)} in 1 uur",
                "message": f"{r['coin']} staat op {fmt_price(r['price'])}. " + "; ".join(reasons) + ".",
                "reasons": reasons, "strength": round(chg / thr, 2),
            })
        return out

    def check_company_buys(self) -> list[dict]:
        out = []
        for t in self.db.query(
            "SELECT * FROM trades WHERE market='crypto' AND actor_type='company' AND tx_type='buy'"
            " AND created_at >= ? AND value_usd >= ?",
            (ago(hours=2), 250e6 * self.mult),
        ):
            out.append({
                "market": "crypto", "asset": t["ticker"], "kind": "bedrijf_koopt",
                "title": f"{t['actor_name']} koopt {t['ticker']} voor {fmt_usd(t['value_usd'])}",
                "message": f"{t['actor_name']} meldt bij de SEC een aankoop van {fmt_num(t['shares'])} {t['ticker']} "
                           f"voor {fmt_usd(t['value_usd'])}.",
                "reasons": [t.get("note") or ""], "strength": round((t["value_usd"] or 0) / (250e6 * self.mult), 2),
            })
        return out

    # ------------------------------------------------------------------ aandelen
    def check_insider_clusters(self) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM trades WHERE market='stocks' AND actor_type='insider' AND tx_type='buy' AND planned=0"
            " AND ticker IS NOT NULL AND filed_at >= ?",
            (ago(days=7),),
        )
        by_t: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_t[r["ticker"]].append(r)
        out, recent = [], ago(hours=2)
        for ticker, trades in by_t.items():
            buyers = sorted({t["actor_name"] for t in trades})
            total = sum(t["value_usd"] or 0 for t in trades)
            newest = max(t["created_at"] for t in trades)
            if newest < recent:
                continue
            if len(buyers) >= 3 and total >= 1e6 * self.mult:
                out.append({
                    "market": "stocks", "asset": ticker, "kind": "insider_cluster",
                    "title": f"{len(buyers)} insiders kochten {ticker} ({fmt_usd(total)})",
                    "message": f"Binnen 7 dagen kochten {len(buyers)} bestuurders/insiders samen voor {fmt_usd(total)} "
                               f"aan {ticker}: {', '.join(buyers[:4])}.",
                    "reasons": [f"{len(buyers)} verschillende kopers", f"Totaal {fmt_usd(total)}"],
                    "strength": round(len(buyers) / 3, 2),
                })
            for t in trades:
                senior = any(s in (t["actor_role"] or "").lower() for s in SENIOR_TITLES)
                if senior and t["created_at"] >= recent and (t["value_usd"] or 0) >= 5e6 * self.mult:
                    out.append({
                        "market": "stocks", "asset": ticker, "kind": "insider_groot",
                        "title": f"{t['actor_role'].split(',')[0]} koopt {ticker} voor {fmt_usd(t['value_usd'])}",
                        "message": f"{t['actor_name']} ({t['actor_role']}) kocht op de open markt voor "
                                   f"{fmt_usd(t['value_usd'])} aan {ticker}.",
                        "reasons": ["Grote open-markt aankoop door een topbestuurder"],
                        "strength": round((t["value_usd"] or 0) / (5e6 * self.mult), 2),
                    })
        return out

    def check_big_politician(self) -> list[dict]:
        out = []
        for t in self.db.query(
            "SELECT * FROM trades WHERE market='stocks' AND actor_type='politician' AND tx_type='buy'"
            " AND created_at >= ? AND value_low >= ? AND ticker IS NOT NULL",
            (ago(hours=2), 1e6 * self.mult),
        ):
            delay = ""
            td, fd = parse_iso(t["tx_date"]), parse_iso(t["filed_at"])
            if td and fd:
                delay = f" De transactie was {(fd - td).days} dagen voor de melding."
            out.append({
                "market": "stocks", "asset": t["ticker"], "kind": "politicus_groot",
                "title": f"{t['actor_name']} kocht {t['ticker']} (>{fmt_usd(t['value_low'] - 1)})",
                "message": f"{t['actor_name']} ({t['actor_role']}) meldt een aankoop van {t['ticker']} tussen "
                           f"{fmt_usd(t['value_low'])} en {fmt_usd(t['value_high'])}.{delay}",
                "reasons": ["Zeer grote aankoop door een politicus", "Politici melden tot 45 dagen later"],
                "strength": round((t["value_low"] or 0) / (1e6 * self.mult), 2),
            })
        return out

    def check_stock_breakouts(self) -> list[dict]:
        thr = 8.0 * self.mult
        out = []
        for r in self.db.query(
            "SELECT m.*, s.score FROM price_meta m JOIN scores_latest s ON s.market=m.market AND s.ticker=m.ticker"
            " WHERE m.market='stocks' AND m.fetched_at >= ? AND m.chg_24h >= ?",
            (ago(minutes=45), thr),
        ):
            out.append({
                "market": "stocks", "asset": r["ticker"], "kind": "koersuitbraak",
                "title": f"{r['ticker']} staat {fmt_pct(r['chg_24h'])} hoger",
                "message": f"{r['ticker']} ({r['name'] or ''}) staat {fmt_pct(r['chg_24h'])} hoger dan het vorige slot "
                           f"(koers {fmt_price(r['last_price'])}); koopscore {r['score']:.0f}.",
                "reasons": [f"Dagwinst boven {thr:.0f}%"], "strength": round(r["chg_24h"] / thr, 2),
            })
        return out

    # ------------------------------------------------------------------ beide markten
    def check_news(self, market: str) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM news WHERE market=? AND is_junk=0 AND published_at >= ? AND tickers != '[]'",
            (market, ago(minutes=45)),
        )
        clusters: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            clusters[r["cluster_id"] or r["id"]].append(r)
        need = max(2, round(3 * self.mult))
        out = []
        for members in clusters.values():
            trusted = {m["domain"] for m in members if (m["trust"] or 0) >= 0.7}
            if len(trusted) < need:
                continue
            avg = sum(m["sentiment"] or 0 for m in members) / len(members)
            if avg < 0.45 or not any(CATALYST_RE.search(m["title"]) for m in members):
                continue
            tick = Counter(t for m in members for t in json.loads(m["tickers"])).most_common(1)[0][0]
            best = max(members, key=lambda m: m["credibility"] or 0)
            out.append({
                "market": market, "asset": tick, "kind": "groot_nieuws",
                "title": f"Groot positief nieuws: {tick}",
                "message": f"{best['title']} (gemeld door {len(trusted)} betrouwbare bronnen, o.a. "
                           f"{', '.join(sorted(trusted)[:3])}).",
                "reasons": [f"{len(trusted)} betrouwbare bronnen binnen 45 minuten", f"Sentiment {avg:+.2f}"],
                "strength": round(len(trusted) / need, 2),
            })
        return out

    def check_score_jumps(self, market: str) -> list[dict]:
        key = f"score_snap_{market}"
        snap = self.db.get_state(key) or {}
        now = utcnow()
        current = {r["ticker"]: r for r in self.db.query("SELECT * FROM scores_latest WHERE market=?", (market,))}
        out = []
        snap_ts = parse_iso(snap.get("ts")) if snap else None
        if snap_ts and timedelta(minutes=45) <= now - snap_ts <= timedelta(hours=3):
            for t, r in current.items():
                before = snap.get("scores", {}).get(t)
                if before is None:
                    continue
                if r["score"] >= 75 and r["score"] - before >= 15 * self.mult:
                    reasons = json.loads(r["reasons"] or "[]")[:3]
                    out.append({
                        "market": market, "asset": t, "kind": "score_sprong",
                        "title": f"Koopscore {t} springt naar {r['score']:.0f}",
                        "message": f"De koopscore van {t} steeg van {before:.0f} naar {r['score']:.0f} "
                                   f"(kans omhoog ~{(r['prob_up'] or 0) * 100:.0f}%). " + " ".join(reasons),
                        "reasons": reasons, "strength": round((r["score"] - before) / 15, 2),
                    })
        if not snap_ts or now - snap_ts >= timedelta(minutes=60):
            self.db.set_state(key, {"ts": iso(now), "scores": {t: r["score"] for t, r in current.items()}})
        return out

    # ------------------------------------------------------------------ versturen
    def ai_check(self, cand: dict) -> dict | None:
        if not (self.summarizer and self.cfg.llm_enabled and self.cfg.ai_check_alerts):
            return None
        news = self.db.query(
            "SELECT title, source, credibility, corroboration, published_at FROM news WHERE market=? AND is_junk=0"
            " AND tickers LIKE ? AND published_at >= ? ORDER BY credibility DESC LIMIT 8",
            (cand["market"], f'%"{cand["asset"]}"%', ago(hours=6)),
        )
        meta = self.db.query_one("SELECT * FROM price_meta WHERE market=? AND ticker=?", (cand["market"], cand["asset"]))
        payload = {
            "markt": MARKET_LABELS[cand["market"]].lower(),
            "signaal": {k: cand[k] for k in ("asset", "kind", "title", "message", "reasons")},
            "koers": {k: (meta or {}).get(k) for k in ("last_price", "chg_1h", "chg_24h", "chg_7d", "rank")},
            "recent_nieuws": news,
        }
        try:
            return self.summarizer.ask_json(AI_SYSTEM, json.dumps(payload, ensure_ascii=False, default=str),
                                            AI_SCHEMA, effort="low", max_tokens=4000)
        except Exception as exc:  # noqa: BLE001 - bij twijfel gewoon versturen
            log.warning("AI-controle van noodmelding mislukt, melding gaat zonder controle door: %s", exc)
            return None

    def emit(self, cand: dict) -> int:
        asset = cand.get("asset")
        if self.db.query_one(
            "SELECT 1 FROM alerts WHERE market=? AND asset IS ? AND (kind=? AND created_at >= ? OR created_at >= ?)",
            (cand["market"], asset, cand["kind"], ago(hours=COOLDOWN_HOURS), ago(hours=SAME_ASSET_HOURS)),
        ):
            return 0  # afkoelperiode
        status, ai_note = "verzonden", None
        sent_today = self.db.query_one(
            "SELECT COUNT(*) n FROM alerts WHERE status='verzonden' AND created_at >= ?", (ago(hours=24),)
        )["n"]
        if sent_today >= self.cfg.max_alerts_per_day:
            status, ai_note = "onderdrukt", "Daglimiet voor noodmeldingen bereikt"
        else:
            verdict = self.ai_check(cand)
            if verdict is not None:
                ai_note = f"{verdict.get('explanation', '')} Risico: {verdict.get('risk', '')}".strip()
                if not verdict.get("send"):
                    status = "onderdrukt"
                elif verdict.get("title"):
                    cand["title"] = verdict["title"][:80]
        now = iso(utcnow())
        self.db.execute(
            "INSERT INTO alerts(market, asset, kind, title, message, reasons, strength, status, ai_note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cand["market"], asset, cand["kind"], cand["title"], cand.get("message"),
             json.dumps(cand.get("reasons", []), ensure_ascii=False), cand.get("strength"), status, ai_note, now),
        )
        alert = self.db.query_one("SELECT * FROM alerts WHERE created_at=? AND kind=? AND asset IS ? ORDER BY id DESC",
                                  (now, cand["kind"], asset))
        log.warning("NOODMELDING (%s) %s: %s", status, cand["market"], cand["title"])
        if status == "verzonden" and self.hass is not None:
            self.hass.push_alert(alert)
        return 1 if status == "verzonden" else 0
