# Market Monitor (Home Assistant add-on)

Een dashboard voor je monitor dat volgt wat grote spelers op de aandelenmarkt doen:

- **Transacties van politici** (Amerikaans Congres: Huis en Senaat) en **bedrijfsinsiders** (CEO's, directeuren,
  grootaandeelhouders) uit officiële, openbare bronnen.
- **Nieuws van het hele web** (betrouwbare media, Google News, GDELT en optioneel X), met een **filter dat
  onbetrouwbare bronnen, clickbait en hype eruit haalt**.
- **Elk halfuur een samenvatting** van wat er binnenkwam, optioneel geschreven door Claude (AI).
- Een **kooplijst** met per aandeel een score (0-100), een geschatte **kans op stijging** (~1 maand) en een
  **instapmoment** op basis van de koerstrend.

Het draait als add-on op **Home Assistant OS** (bijvoorbeeld op een Raspberry Pi 4 of 5) en verschijnt als paneel
in de zijbalk. De sensoren en events kun je in je eigen automatiseringen gebruiken, bijvoorbeeld voor een melding op
je telefoon bij een grote aankoop.

![Dashboard (demodata met verzonnen namen)](docs/screenshot.png)

<details><summary>Detailweergave van een aandeel</summary>

![Detail (demodata)](docs/screenshot-detail.png)

</details>

> **Geen beleggingsadvies.** Scores en kansen zijn modelinschattingen op basis van openbare data. Ze kunnen er
> flink naast zitten. Gebruik ze als startpunt voor je eigen onderzoek.

---

## Installatie op je Raspberry Pi

### Optie A: via de add-on store (aanbevolen)

1. Zorg dat deze GitHub-repository **openbaar** is. Home Assistant kan geen privé-repository's ophalen.
2. Ga in Home Assistant naar **Instellingen → Add-ons → Add-on store**.
3. Klik rechtsboven op **⋮ → Repository's** en voeg toe:
   `https://github.com/antoshkalol/Ai-programma-s-`
   Staat de add-on nog alleen op een andere branch dan de standaardbranch? Voeg dan
   `https://github.com/antoshkalol/Ai-programma-s-#claude/determined-wozniak-qsoy8o` toe, of merge eerst naar `main`.
4. Ververs de pagina. Kies **Market Monitor** en klik op **Installeren**. De Pi bouwt de add-on zelf; dat duurt een
   paar minuten.
5. Open het tabblad **Configuratie** en vul minimaal **`sec_user_agent`** in (zie hieronder).
6. Zet **Toon in zijbalk** aan en klik op **Starten**.

### Optie B: lokaal, zonder GitHub

1. Installeer de add-on **Samba share** (of **Advanced SSH & Web Terminal**).
2. Kopieer de map `market_monitor/` uit deze repository naar de gedeelde map **`addons`** op je Pi.
3. Ga naar **Add-on store → ⋮ → Controleren op updates**. De add-on staat dan onder **Lokale add-ons**.
4. Installeer en configureer hem zoals bij optie A, stap 4 t/m 6.

Na het starten verschijnen de eerste transacties en nieuwsberichten binnen een paar minuten. De eerste samenvatting
volgt ongeveer 4 minuten na de start, daarna elk halfuur (op :00 en :30).

---

## Configuratie

| Optie | Uitleg |
|---|---|
| `sec_user_agent` | **Verplicht** voor SEC-data. Vul je naam en e-mailadres in, bijvoorbeeld `Jan Jansen jan@voorbeeld.nl`. De SEC vraagt dit van iedereen die hun data automatisch ophaalt. Het gaat alleen naar sec.gov. |
| `anthropic_api_key` | Optioneel. Een sleutel van [console.anthropic.com](https://console.anthropic.com) voor AI-samenvattingen. Zonder sleutel maakt de add-on zelf een automatische samenvatting (gratis). |
| `anthropic_model` | Standaard `claude-opus-5-5` (beste kwaliteit). `claude-sonnet-5-5` en `claude-haiku-5-5` zijn veel goedkoper; zie de kosten hieronder. |
| `anthropic_effort` | Hoe grondig Claude nadenkt: `low` is sneller en goedkoper, `high` grondiger. Standaard `medium`. |
| `watchlist` | Aandelen die altijd worden beoordeeld, bijvoorbeeld `ASML`, `NVDA`, `AAPL`. |
| `min_trade_value_usd` | Vanaf welk bedrag een transactie "groot" is in de samenvatting (standaard $100.000). |
| `big_trade_alert_usd` | Vanaf dit bedrag vuurt de add-on het event `market_monitor_big_trade` af (standaard $1 mln). |
| `min_news_credibility` | Hoe streng het nieuwsfilter is (0-1, standaard 0,45). |
| `news_queries` | Zoektermen voor Google News en GDELT. |
| `extra_rss_feeds`, `trusted_domains`, `blocked_domains` | Je eigen feeds toevoegen en bronnen extra vertrouwen of blokkeren. |
| `quiver_api_key` | Optioneel, betaald: extra bron voor Congres-transacties, inclusief partij. |
| `x_bearer_token`, `x_accounts` | Optioneel, betaald: posts van belangrijke accounts op X volgen. |

### Wat kost de AI-samenvatting?

Zonder API-sleutel kost de add-on niets. Met een sleutel betaal je Anthropic per gebruik. Een ruwe schatting bij
48 samenvattingen per dag (elk halfuur, ongeveer 6.000 tokens invoer en 2.500 tokens uitvoer per keer):

| Model | Prijs per miljoen tokens (in/uit) | Ongeveer per maand |
|---|---|---|
| `claude-opus-5-5` (standaard) | $4 / $20 | $100 |
| `claude-sonnet-5-5` | $2 / $10 | $55 |
| `claude-haiku-5-5` | $0,10 / $0,50 | $3 |

Het werkelijke bedrag hangt af van hoeveel nieuws er is. Je kunt het verlagen met `claude-haiku-5-5`,
`anthropic_effort: low` of een langer interval (`summary_interval_minutes`).

---

## Databronnen

| Bron | Wat | Vertraging | Kosten |
|---|---|---|---|
| SEC EDGAR (Form 4) | Aan- en verkopen door bestuurders, directeuren en aandeelhouders met meer dan 10% (VS) | Melding binnen 2 werkdagen | Gratis |
| Huis van Afgevaardigden (Clerk) | Transacties van Afgevaardigden (PDF-meldingen) | Tot 45 dagen na de transactie | Gratis |
| Senaat (eFD) | Transacties van Senatoren | Tot 45 dagen na de transactie | Gratis (voorwaarden: niet commercieel) |
| Quiver Quantitative | Congres-transacties incl. partij | Idem | Betaald, optioneel |
| RSS-feeds | o.a. MarketWatch, WSJ, CNBC, BBC, FT, Federal Reserve, SEC, NOS | Minuten | Gratis |
| Google News | Koppen van alle grote media, ook Reuters en Bloomberg | Minuten | Gratis |
| GDELT | Monitort nieuws wereldwijd | Ongeveer 15 minuten | Gratis |
| X API | Posts van accounts die jij kiest | Minuten | Betaald, optioneel |
| Yahoo Finance / Stooq | Dagkoersen (6 maanden) | Ongeveer 15 minuten | Gratis (onofficieel) |

**Belangrijk om te weten:** transacties van politici zie je nooit live; ze hoeven pas tot 45 dagen later gemeld te
worden. Het dashboard toont daarom bij elke transactie de **vertraging** in dagen. Aankopen door insiders worden
veel sneller gemeld. Aankopen door meerdere insiders tegelijk zijn meestal een sterker signaal.

---

## Hoe het werkt

### Het nieuwsfilter

Elk bericht krijgt een **geloofwaardigheidsscore**:

1. **Betrouwbaarheid van de bron.** Een lijst van ruim 60 domeinen (Reuters 0,95, CNBC 0,88, ... onbekende sites 0,35).
   Persberichten (PR Newswire e.d.) tellen als 0,5. Aanpassen kan in `market_monitor/app/market_monitor/data/sources.json`
   of via de opties.
2. **Clickbait-straf.** Hype-taal ("to the moon", "1000%", "get rich", veel hoofdletters of uitroeptekens, ...) verlaagt
   de score.
3. **Bevestiging.** Melden meerdere onafhankelijke bronnen hetzelfde verhaal, dan stijgt de score. Een onbekende bron
   komt zo alleen door het filter als anderen het bevestigen.
4. **AI-controle (optioneel).** Met een API-sleutel markeert Claude elk halfuur ook geruchten, promotie en
   irrelevante berichten.

Weggefilterde berichten worden niet weggegooid. Je kunt ze bekijken met de knop **Weggefilterd** in het nieuwspaneel.

### De koopscore (0-100)

| Signaal | Gewicht | Wat telt |
|---|---|---|
| Insiders | 30% | Open-markt aankopen (geen opties of beloningen), extra gewicht als meerdere tegelijk kopen of als de CEO/CFO koopt. Verkopen tellen minder; vooraf ingeplande (10b5-1) transacties nog minder. |
| Politici | 20% | Aankopen door Congresleden, met een straf voor late melding. |
| Nieuws | 25% | Sentiment van geloofwaardige berichten (48 uur), gewogen naar bron en actualiteit; met AI aangevuld door Claude. |
| Koers | 25% | Trend ten opzichte van het 50-daags gemiddelde, momentum over 20 dagen en RSI. |

50 is neutraal. Daarnaast toont het dashboard het **instapmoment**:

- **Gunstig:** een terugval binnen een stijgende trend.
- **Oververhit:** RSI boven 72. Afwachten op een terugval kan dan verstandig zijn.
- **Dalende trend:** afwachten.

### Kans op stijging

Eerst is dit een **modelschatting** op basis van de score, begrensd tussen 25% en 80%. De add-on houdt bij hoe vaak
eerdere scores uitkwamen: steeg de koers na ongeveer 20 handelsdagen? Zodra er per scoreband minstens 30 metingen zijn
(na grofweg 1 à 2 maanden draaien), wordt de kans **gekalibreerd** op die echte resultaten. Het dashboard laat
zien welke van de twee je ziet.

---

## Home Assistant

### Sensoren

| Entiteit | Status | Belangrijkste attributen |
|---|---|---|
| `sensor.market_monitor_top_pick` | Ticker met de hoogste score | `score`, `kans_omhoog`, `instapmoment`, `redenen`, `top5` |
| `sensor.market_monitor_summary` | Kop van de laatste samenvatting | `kernpunten`, `transacties`, `koopvisie`, `let_op`, `stemming`, `tekst` |
| `sensor.market_monitor_last_big_trade` | Laatste grote transactie | `actor_name`, `ticker`, `tx_type`, `value_usd`, `url` |
| `sensor.market_monitor_status` | `ok` of `fout` | `bronnen_met_fouten` |

### Events

- `market_monitor_big_trade`: bij elke nieuwe transactie boven `big_trade_alert_usd`.
- `market_monitor_summary`: bij elke nieuwe samenvatting.

### Voorbeeld: melding op je telefoon bij een grote transactie

```yaml
alias: Melding bij grote aandelentransactie
triggers:
  - trigger: event
    event_type: market_monitor_big_trade
actions:
  - action: notify.mobile_app_jouw_telefoon
    data:
      title: "Grote transactie: {{ trigger.event.data.ticker }}"
      message: "{{ trigger.event.data.description }}"
      data:
        url: "{{ trigger.event.data.url }}"
```

### Voorbeeld: samenvatting als kaart op je dashboard

```yaml
type: markdown
title: Markt (laatste halfuur)
content: |
  **{{ states('sensor.market_monitor_summary') }}**
  {% for p in state_attr('sensor.market_monitor_summary', 'kernpunten') or [] %}
  - {{ p }}
  {% endfor %}

  Beste kans nu: **{{ states('sensor.market_monitor_top_pick') }}**
  (score {{ state_attr('sensor.market_monitor_top_pick', 'score') }},
  kans omhoog {{ state_attr('sensor.market_monitor_top_pick', 'kans_omhoog') }}%)
```

### Op een aparte monitor

- Via Home Assistant: open het paneel **Market Monitor** en klik op **Volledig scherm**.
- Zonder inloggen: zet in het tabblad **Netwerk** van de add-on een poort open (bijvoorbeeld 8099) en open
  `http://<ip-van-je-pi>:8099` in de browser van de monitor. Let op: iedereen in je thuisnetwerk kan het
  dashboard dan zien. Er staan geen sleutels in, maar het is wel zichtbaar.

---

## Beperkingen

- De parsers zijn gebouwd op de gedocumenteerde formaten van de bronnen en getest met voorbeeldbestanden, niet live
  getest. Werkt een bron niet, dan zie je dat bovenin aan een **✕** (beweeg erover voor de foutmelding). Ook het
  logboek van de add-on toont de fout.
- Papieren (gescande) meldingen van politici zijn niet automatisch leesbaar; ze verschijnen met een link naar de PDF.
- Yahoo Finance heeft geen officiële gratis API en kan zonder aankondiging veranderen; Stooq is de reserve.
- De Senaat-website staat alleen niet-commercieel gebruik van de data toe.

## Ideeën voor later

- Nederlandse/Europese insidertransacties (AFM-register "transacties leidinggevenden").
- Kwartaalposities van grote fondsen (13F, bijvoorbeeld Berkshire Hathaway).
- Backtest op historische data voor een snellere kalibratie.

---

## Ontwikkelen en testen

```bash
cd market_monitor/app
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
python -m pytest -q                            # alle tests
python scripts/demo_seed.py ./devdata          # fictieve demodata
python -m market_monitor --data-dir ./devdata --port 8099 --no-scheduler
# open http://localhost:8099
```

Zonder `--no-scheduler` haalt de app echte data op. Opties zet je in `./devdata/options.json` (zelfde namen als in
de add-on), of via de omgevingsvariabelen `ANTHROPIC_API_KEY` en `MM_SEC_USER_AGENT`.

### Projectstructuur

```
repository.yaml                 Home Assistant add-on repository
market_monitor/                 de add-on
  config.yaml, build.yaml       add-on definitie (opties, ingress, architecturen)
  Dockerfile, run.sh            container
  translations/                 namen en uitleg van de opties (nl/en)
  app/market_monitor/
    sources/                    SEC, Huis, Senaat, Quiver, nieuws, X, koersen
    filtering.py                nieuwsfilter (betrouwbaarheid, clickbait, bevestiging)
    tickers.py, sentiment.py    aandelen herkennen, sentiment
    scoring.py                  koopscore, kans, instapmoment, kalibratie
    summary.py                  halfuur-samenvatting (Claude of automatisch)
    hass.py                     sensoren en events naar Home Assistant
    web.py, static/             dashboard en API
  app/tests/                    tests met voorbeeldbestanden
```
