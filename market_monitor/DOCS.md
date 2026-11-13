# Market Monitor

Volgt grote transacties op de aandelen- en cryptomarkt, filtert het nieuws op betrouwbaarheid, maakt elk uur
een samenvatting met een kooplijst en stuurt direct een noodmelding als er iets groots aan lijkt te komen. Het
dashboard vind je in de zijbalk, met aparte tabbladen voor **Aandelen** en **Crypto**.

**Geen beleggingsadvies.** Scores, kansen en noodmeldingen zijn modelinschattingen; een signaal is geen garantie.

## Snel starten

1. Vul **`sec_user_agent`** in met je naam en e-mailadres, bijvoorbeeld `Jan Jansen jan@voorbeeld.nl`.
   De SEC eist dit; het wordt alleen naar sec.gov gestuurd.
2. Installeer de **Home Assistant-app** op je telefoon, log in en sta meldingen toe. De add-on vindt je telefoon
   automatisch. Klik na het starten in het dashboard op **Testmelding** om het te controleren.
3. Optioneel: vul **`anthropic_api_key`** in voor AI-samenvattingen door Claude en een AI-controle van
   noodmeldingen. Zonder sleutel krijg je een automatische samenvatting (gratis).
4. Pas **`stock_watchlist`** en **`crypto_watchlist`** aan met wat je wilt volgen.
5. Start de add-on en zet **Toon in zijbalk** aan.

## Meldingen (popups op je telefoon)

- **Elk uur:** één popup met de kop en de aanbeveling van beide markten. Een nieuw uurbericht vervangt het vorige.
  Uitzetten met `notify_summaries: false`; 's nachts stil met bijvoorbeeld `quiet_hours: 23-7`.
- **Noodmelding:** direct en met hoge prioriteit, bijvoorbeeld bij:
  - een koersexplosie met volumepiek;
  - een zeer grote crypto-aankoop door een bedrijf;
  - drie insiders die samen hetzelfde aandeel kopen;
  - groot positief nieuws dat door drie betrouwbare bronnen wordt gemeld;
  - een plotselinge sprong in de koopscore.
  
  Met `alert_critical: true` komt de melding ook door stil/niet storen heen.
- **Minder of meer meldingen:** zet `alert_sensitivity` op `laag` (alleen heel uitzonderlijk) of `hoog`, en/of
  verlaag `max_alerts_per_day`.

## Aanbeveling (long/short)

Het tabblad **Aanbeveling** toont per markt de beste kandidaat:
- **Richting:** LONG, SHORT of AFWACHTEN.
- **Plan:** instapzone, stop-loss, twee koersdoelen, risico/rendement en de kans.
- **Grafiek:** met technische analyse (trend, steun/weerstand, Bollinger, RSI) en de verwachte bandbreedte voor
  de komende ~20 dagen.

Het plan wordt elke 3 minuten gecontroleerd en past zich aan als het nodig is:
- bij doel 1 gaat de stop naar de instapprijs;
- bij stop-loss of doel 2 komt er een nieuwe aanbeveling;
- als de richting omslaat of een andere kandidaat duidelijk sterker wordt, komt er een nieuw plan.

Je krijgt dan een popup (uitzetten met `notify_recommendation_changes: false`).

Shorten is riskant; gebruik altijd een stop-loss.

## Zelfstandig handelen (tabblad Trading)

Het model kan zelf kopen en verkopen binnen een budget dat jij kiest:
- **crypto** via **Bitvavo**;
- **aandelen** via **Trading 212**.

Het volgt de techniek, het nieuws, de posts op X en de grote kopers. Claude beslist mee, met een onderbouwing in
het logboek. De harde grenzen bewaakt de code: risico per trade, maximale positie, daglimiet en noodrem.

1. **Instellen:** kies budgetten en risico op het tabblad Trading en klik op **Opslaan**.
2. **Oefenen:** start een of meer **paper-tests** (nepgeld, echte koersen) en vergelijk ze. Neem de instellingen
   van de beste over.
3. **Sleutels:** vul `bitvavo_api_key`/`bitvavo_api_secret` en `trading212_api_key`/`trading212_api_secret` in.
   Geef ze alleen de rechten bekijken en handelen, **nooit opnemen**, en zet een IP-whitelist aan.
4. **Live:** zet `allow_live_trading` aan.
   - Begin met **Proefdraaien**: je saldo wordt gelezen, maar er worden geen echte orders verstuurd.
   - Daarna met een klein bedrag. Om te starten typ je `IK BEGRIJP HET RISICO`.

**Live alleen long:** via de API's van Bitvavo en Trading 212 kun je niet short gaan. Een short-signaal sluit live
alleen je longpositie. Shorten kun je wel simuleren in paper trading.

**Claude als beslisser** vereist `anthropic_api_key` (console.anthropic.com). Kosten per run per dag, bij
maximaal 100 beslissingen:
- `claude-opus-5-5`: ongeveer $9;
- `claude-sonnet-5-5`: ongeveer $4,50;
- `claude-haiku-5-5`: ongeveer $0,25.

Het dashboard toont de werkelijke kosten per run. Zonder sleutel handelt het model alleen op de regels.

**Geen garantie:** ook met een slim model kun je geld verliezen. Een test van een paar uur zegt weinig; laat de
beste instellingen eerst dagen tot weken op paper lopen.

## Kosten van de AI

Voor de samenvattingen: ongeveer 48 aanvragen per dag (elk uur één per markt). Het handelen komt daar bovenop
(zie hierboven). Ruwe schatting per maand voor de samenvattingen:

- `claude-opus-5-5` (standaard): ~$100
- `claude-sonnet-5-5`: ~$55
- `claude-haiku-5-5`: ~$3

## Wat je in Home Assistant krijgt

- **Beste kans per markt:** `sensor.market_monitor_stocks_top_pick` en `sensor.market_monitor_crypto_top_pick`.
- **Laatste samenvatting per markt:** `sensor.market_monitor_stocks_summary` en `sensor.market_monitor_crypto_summary`.
- **Laatste grote transactie per markt:** `sensor.market_monitor_stocks_last_big_trade` en
  `sensor.market_monitor_crypto_last_big_trade`.
- **Aanbeveling per markt:** `sensor.market_monitor_stocks_recommendation` en
  `sensor.market_monitor_crypto_recommendation`.
- **Laatste noodmelding:** `sensor.market_monitor_alert`. Status van de add-on: `sensor.market_monitor_status`.
- **Handelen:** `sensor.market_monitor_trading` (vermogen van de live-run, resultaten van alle runs).
- **Events:** `market_monitor_alert`, `market_monitor_summary` en `market_monitor_big_trade`, voor eigen
  automatiseringen (bijvoorbeeld een lamp laten knipperen).

## Problemen oplossen

- **Bovenin staat een ✕ bij een bron:** beweeg erover voor de foutmelding. Kijk ook in het tabblad **Logboek**.
- **"SEC e-mail ontbreekt":** vul `sec_user_agent` in en herstart de add-on.
- **Geen popup op je telefoon:** klik op **Testmelding** en lees de melding onderin het dashboard. Controleer
  daarnaast de meldingsinstellingen van de Home Assistant-app op je telefoon:
  - iPhone: stijl *Banners*;
  - Android: kanaal *Market Monitor* op *Pop-up op scherm*.
- **Geen aandelen-aanbeveling of ✕ bij "Koersen":** Yahoo Finance weigert soms verzoeken. Maak een gratis
  sleutel aan op twelvedata.com, vul die in bij `stock_price_api_key` en herstart de add-on. Op het tabblad
  Aanbeveling staat welke koersbron wat terugstuurde.
- **Nog geen samenvatting:** klik in het samenvattingspaneel op **Nu samenvatten**.
- **"CoinGecko: limiet bereikt":** maak een gratis demo-sleutel aan op coingecko.com en vul `coingecko_api_key` in.
- **Handelen: "Verbinding testen" mislukt:** controleer de sleutel en het geheim, de rechten (bekijken + handelen)
  en de IP-whitelist bij de broker. Bij Trading 212 moet de sleutel uit de gekozen omgeving komen (demo of live).
- **Handelen: geen trades:** kijk in het logboek bij *Afgewezen*. Daar staat per aandeel of munt waarom niet. Vaak
  is de reden een te lage overtuiging, risico/rendement onder de grens, een te oude koers, een gesloten beurs of
  een bereikte daglimiet.
- **Geen AI-samenvatting:** controleer de API-sleutel en het tegoed. Bij een fout valt de add-on automatisch terug
  op de automatische samenvatting.

Meer uitleg over bronnen, filter, score en detectoren staat in de README van de repository.
