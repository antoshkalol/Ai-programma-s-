"""Alle instelbare variabelen van de handelsmodule, met standaardwaarde, grenzen en uitleg.

De instellingen staan in de database (niet in de add-on opties), zodat je ze in het dashboard kunt aanpassen
zonder herstart. Een paper-test of live-run krijgt bij de start een kopie van de instellingen: zo kun je tests met
verschillende instellingen naast elkaar vergelijken.
"""

from __future__ import annotations

from dataclasses import dataclass

GROUPS = [
    ("budget", "Budget"),
    ("risk", "Risico en limieten"),
    ("strategy", "Strategie: instappen en uitstappen"),
    ("weights", "Signaalgewichten (waar het model op let)"),
    ("ai", "AI-beslisser (Claude)"),
    ("costs", "Kosten (voor paper trading)"),
    ("universe", "Wat mag er verhandeld worden"),
    ("live", "Live handelen"),
]

AI_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]


@dataclass(frozen=True)
class Spec:
    key: str
    kind: str               # eur | pct | int | float | bool | choice | list
    default: object
    group: str
    label: str
    help: str
    lo: float | None = None
    hi: float | None = None
    options: tuple = ()
    unit: str = ""


SPECS: list[Spec] = [
    # ------------------------------------------------------------------ budget
    Spec("budget_stocks_eur", "eur", 1000.0, "budget", "Budget aandelen (Trading 212)",
         "Hoeveel euro het model maximaal in aandelen mag zetten. 0 = geen aandelen verhandelen.", 0, 1_000_000,
         unit="€"),
    Spec("budget_crypto_eur", "eur", 1000.0, "budget", "Budget crypto (Bitvavo)",
         "Hoeveel euro het model maximaal in crypto mag zetten. 0 = geen crypto verhandelen.", 0, 1_000_000, unit="€"),
    Spec("reinvest_profits", "bool", True, "budget", "Winst herinvesteren",
         "Aan: het budget groeit mee met de winst (en krimpt bij verlies). Uit: nooit meer dan het budget inzetten."),
    # ------------------------------------------------------------------ risico
    Spec("risk_per_trade_pct", "pct", 1.0, "risk", "Risico per trade",
         "Hoeveel procent van het budget je maximaal verliest als de stop-loss geraakt wordt. Dit bepaalt de "
         "positiegrootte: een verre stop geeft een kleinere positie.", 0.1, 10, unit="%"),
    Spec("max_position_pct", "pct", 25.0, "risk", "Maximale positie",
         "Een positie is nooit groter dan dit percentage van het budget.", 1, 100, unit="%"),
    Spec("max_open_positions", "int", 5, "risk", "Maximaal aantal open posities",
         "Per markt.", 1, 30),
    Spec("max_exposure_pct", "pct", 100.0, "risk", "Maximale blootstelling (marge)",
         "Totaal van alle posities (long + short) als percentage van het budget. Boven 100% is met geleend geld "
         "(hefboom) en kan alleen in paper trading; live is het maximaal 100%.", 10, 300, unit="%"),
    Spec("max_daily_loss_pct", "pct", 4.0, "risk", "Maximaal verlies per dag",
         "Bij dit verlies op één dag opent het model die dag geen nieuwe posities meer.", 0.5, 50, unit="%"),
    Spec("max_drawdown_pct", "pct", 15.0, "risk", "Noodrem: maximale terugval",
         "Daalt het vermogen zoveel procent onder de hoogste stand, dan stopt de run (noodrem).", 1, 90, unit="%"),
    Spec("close_on_halt", "bool", True, "risk", "Posities sluiten bij noodrem",
         "Sluit alle posities als de noodrem of de daglimiet ingrijpt."),
    Spec("max_trades_per_day", "int", 12, "risk", "Maximaal aantal nieuwe trades per dag", "Per markt.", 1, 200),
    Spec("min_order_eur", "eur", 10.0, "risk", "Minimale ordergrootte",
         "Kleinere orders worden overgeslagen (Bitvavo vereist minimaal €5).", 1, 10_000, unit="€"),
    # ------------------------------------------------------------------ strategie
    Spec("trade_style", "choice", "swing", "strategy", "Handelsstijl",
         "swing: posities van dagen tot weken, stops op basis van de dagbeweging. kort: posities van uren tot een "
         "paar dagen, krappere stops en doelen (0,4x) en meer gewicht voor de koersbeweging van het laatste uur. "
         "Kort geeft in een paper-test van een paar uur meer afgeronde trades, maar ook meer kosten.",
         options=("swing", "kort")),
    Spec("decision_minutes", "int", 15, "strategy", "Beslissen elke",
         "Hoe vaak het model zoekt naar nieuwe trades. Stops en doelen worden elke minuut gecontroleerd.", 3, 240,
         unit="min"),
    Spec("news_trigger", "bool", True, "strategy", "Direct beslissen bij groot nieuws",
         "Bij een noodmelding of nieuws dat door meerdere betrouwbare bronnen wordt gemeld, beslist het model meteen "
         "in plaats van te wachten."),
    Spec("min_conviction", "int", 35, "strategy", "Minimale overtuiging om in te stappen",
         "0-100. Hoger = minder, maar sterkere trades.", 5, 95),
    Spec("min_rr", "float", 1.5, "strategy", "Minimaal risico/rendement",
         "Het eerste koersdoel moet minstens zoveel keer verder liggen dan de stop-loss.", 0.5, 6),
    Spec("allow_short", "bool", True, "strategy", "Shorten toestaan",
         "Inzetten op een daling. Alleen in paper trading: de API's van Bitvavo en Trading 212 ondersteunen geen "
         "short. Live sluit een short-signaal alleen je longpositie."),
    Spec("require_ta_confirmation", "bool", True, "strategy", "Techniek moet bevestigen",
         "Alleen instappen als de technische analyse niet de andere kant op wijst."),
    Spec("stop_atr_mult", "float", 1.5, "strategy", "Stop-loss afstand (x dagbeweging)",
         "Minimale afstand van de stop in gemiddelde dagbewegingen (ATR). Groter = ruimere stop, kleinere positie.",
         0.5, 6),
    Spec("take_profit_fraction", "pct", 50.0, "strategy", "Winst nemen bij doel 1",
         "Welk deel van de positie bij doel 1 verkocht wordt. De rest loopt door tot doel 2 of de trailing stop.",
         0, 100, unit="%"),
    Spec("breakeven_after_t1", "bool", True, "strategy", "Stop naar instapprijs na doel 1",
         "Na doel 1 kan de trade geen verlies meer worden."),
    Spec("trailing_atr_mult", "float", 2.0, "strategy", "Trailing stop (x dagbeweging)",
         "Na doel 1 schuift de stop mee met de koers op deze afstand. 0 = uit.", 0, 10),
    Spec("exit_on_flip", "int", 40, "strategy", "Uitstappen als het signaal omslaat",
         "Sluit een positie als het signaal met deze overtuiging de andere kant op wijst. 0 = uit.", 0, 100),
    Spec("max_hold_hours", "int", 0, "strategy", "Maximale looptijd",
         "Sluit een positie na zoveel uur. 0 = geen limiet.", 0, 24 * 90, unit="uur"),
    Spec("cooldown_after_stop_hours", "int", 12, "strategy", "Pauze na stop-loss",
         "Zoveel uur niet opnieuw dezelfde kant van dat aandeel of die munt.", 0, 240, unit="uur"),
    # ------------------------------------------------------------------ gewichten
    Spec("weight_ta", "int", 40, "weights", "Technische analyse",
         "Trend, momentum (MACD, RSI), steun en weerstand.", 0, 100),
    Spec("weight_flow", "int", 25, "weights", "Grote kopers",
         "Insiders, politici, bedrijven die crypto kopen.", 0, 100),
    Spec("weight_news", "int", 20, "weights", "Nieuws",
         "Sentiment van betrouwbare nieuwsberichten (en de AI-analyse daarvan).", 0, 100),
    Spec("weight_x", "int", 15, "weights", "Posts op X",
         "Sentiment van posts van de gevolgde accounts op X (vereist een X-token).", 0, 100),
    # ------------------------------------------------------------------ AI
    Spec("ai_enabled", "bool", True, "ai", "Claude laat de beslissingen controleren",
         "Claude leest het nieuws, de X-posts en de techniek en beslist welke trades doorgaan. Vereist "
         "'anthropic_api_key'. Zonder sleutel handelt het model alleen op de regels."),
    Spec("ai_mode", "choice", "vrij", "ai", "Vrijheid van de AI",
         "vrij: Claude mag ook zelf trades kiezen uit de kandidaten. filter: Claude mag alleen voorstellen van de "
         "regels goedkeuren, verkleinen of afwijzen. Risicolimieten gelden altijd.", options=("vrij", "filter")),
    Spec("ai_model", "choice", "claude-opus-5-5", "ai", "Model",
         "Opus 5.5 is het slimst, Sonnet 5.5 goedkoper, Haiku 5.5 het goedkoopst.", options=tuple(AI_MODELS)),
    Spec("ai_effort", "choice", "medium", "ai", "Denkniveau",
         "Hoger = grondiger, maar trager en duurder.", options=("low", "medium", "high")),
    Spec("ai_min_interval_minutes", "int", 15, "ai", "Claude maximaal elke",
         "Minimale tijd tussen twee AI-beslissingen per run (bij groot nieuws mag het eerder).", 3, 240, unit="min"),
    Spec("ai_max_calls_per_day", "int", 100, "ai", "Maximaal aantal AI-aanroepen per dag",
         "Per run. Daarna gaat het model verder op de regels. Bewaakt je kosten.", 1, 1000),
    # ------------------------------------------------------------------ kosten
    Spec("fee_pct_crypto", "pct", 0.25, "costs", "Kosten crypto per order",
         "Bitvavo rekent 0,15-0,25% per order.", 0, 2, unit="%"),
    Spec("fee_pct_stocks", "pct", 0.15, "costs", "Kosten aandelen per order",
         "Trading 212 rekent geen commissie, maar wel 0,15% wisselkoerskosten op dollaraandelen.", 0, 2, unit="%"),
    Spec("slippage_pct", "pct", 0.10, "costs", "Slippage",
         "Hoeveel slechter de uitvoering gemiddeld is dan de laatste koers.", 0, 2, unit="%"),
    Spec("short_fee_pct_day", "pct", 0.03, "costs", "Leenkosten short per dag", "Alleen paper.", 0, 1, unit="%"),
    # ------------------------------------------------------------------ universum
    Spec("universe_size", "int", 15, "universe", "Aantal kandidaten per markt",
         "Hoeveel aandelen/munten met het sterkste signaal het model per keer bekijkt.", 3, 40),
    Spec("allowed_tickers", "list", [], "universe", "Alleen deze tickers",
         "Komma-gescheiden, bv. AAPL, NVDA, BTC. Leeg = alles wat het dashboard volgt."),
    Spec("blocked_tickers", "list", [], "universe", "Nooit deze tickers", "Komma-gescheiden."),
    Spec("stocks_market_hours_only", "bool", True, "universe", "Aandelen alleen tijdens beurstijd",
         "Handelen in Amerikaanse aandelen alleen tussen 15:30 en 22:00 (Nederlandse tijd)."),
    Spec("max_price_age_minutes", "int", 30, "universe", "Maximale ouderdom van de koers",
         "Niet handelen op een koers die ouder is dan dit.", 5, 240, unit="min"),
    # ------------------------------------------------------------------ live
    Spec("live_dry_run", "bool", True, "live", "Proefdraaien (geen echte orders)",
         "Live-run die wel je saldo leest bij de broker, maar orders alleen in het logboek zet. Zet pas uit als "
         "alles werkt."),
    Spec("trading212_env", "choice", "demo", "live", "Trading 212-omgeving",
         "demo: oefenaccount met nepgeld bij Trading 212. live: echt geld. Gebruik de API-sleutel van die omgeving.",
         options=("demo", "live")),
    Spec("bitvavo_exchange_stop", "bool", True, "live", "Stop-loss ook bij Bitvavo plaatsen",
         "Zet bij elke cryptopositie een stop-loss-order op de beurs, zodat je ook beschermd bent als de Pi uitvalt."),
    Spec("bitvavo_operator_id", "int", 1001, "live", "Bitvavo operatorId",
         "Verplicht nummer voor orders via de API (kies zelf een getal, bv. 1001).", 1, 2**31 - 1),
    Spec("notify_trades", "choice", "live", "live", "Popup bij elke trade",
         "live: alleen bij echte trades. alle: ook bij paper trades. uit: geen popups (noodrem altijd wel).",
         options=("live", "alle", "uit")),
]

BY_KEY = {s.key: s for s in SPECS}
DEFAULTS = {s.key: (list(s.default) if isinstance(s.default, list) else s.default) for s in SPECS}
STATE_KEY = "trading_settings"


def _coerce(spec: Spec, value):
    if spec.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "ja", "aan")
        return bool(value)
    if spec.kind == "list":
        if isinstance(value, str):
            items = value.replace(";", ",").split(",")
        elif isinstance(value, (list, tuple)):
            items = value
        else:
            raise ValueError("verwacht een lijst")
        return [str(v).strip().upper().lstrip("$") for v in items if str(v).strip()]
    if spec.kind == "choice":
        v = str(value).strip()
        if v not in spec.options:
            raise ValueError(f"kies uit {', '.join(spec.options)}")
        return v
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
    num = float(value)
    if num != num:  # NaN
        raise ValueError("geen getal")
    if spec.kind == "int":
        num = int(round(num))
    if spec.lo is not None and num < spec.lo:
        raise ValueError(f"minimaal {spec.lo:g}")
    if spec.hi is not None and num > spec.hi:
        raise ValueError(f"maximaal {spec.hi:g}")
    return num


def validate(values: dict, base: dict | None = None) -> tuple[dict, dict]:
    """Controleert (een deel van) de instellingen. Geeft (volledige instellingen, fouten per sleutel)."""
    out = dict(DEFAULTS)
    if base:
        out.update({k: v for k, v in base.items() if k in BY_KEY})
    errors: dict[str, str] = {}
    for key, value in (values or {}).items():
        spec = BY_KEY.get(key)
        if spec is None:
            continue
        try:
            out[key] = _coerce(spec, value)
        except (TypeError, ValueError) as exc:
            errors[key] = str(exc) or "ongeldige waarde"
    if not errors and sum(out[k] for k in ("weight_ta", "weight_flow", "weight_news", "weight_x")) <= 0:
        errors["weight_ta"] = "minstens één gewicht moet groter dan 0 zijn"
    return out, errors


def load(db) -> dict:
    stored = db.get_state(STATE_KEY) or {}
    out, _ = validate({}, stored)
    return out


def save(db, values: dict) -> tuple[dict, dict]:
    current = load(db)
    new, errors = validate(values, current)
    if not errors:
        db.set_state(STATE_KEY, new)
    return new, errors


def schema() -> dict:
    """Voor het instellingenformulier in het dashboard."""
    return {
        "groups": [{"key": k, "label": label} for k, label in GROUPS],
        "fields": [{"key": s.key, "kind": s.kind, "default": DEFAULTS[s.key], "group": s.group, "label": s.label,
                    "help": s.help, "min": s.lo, "max": s.hi, "options": list(s.options), "unit": s.unit}
                   for s in SPECS],
    }


def diff(a: dict, b: dict) -> dict:
    """Instellingen die verschillen (voor het vergelijken van tests)."""
    return {k: {"a": a.get(k), "b": b.get(k)} for k in BY_KEY if a.get(k) != b.get(k)}
