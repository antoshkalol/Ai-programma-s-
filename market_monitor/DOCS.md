# Market Monitor

Volgt grote transacties op de aandelen- en cryptomarkt, filtert het nieuws op betrouwbaarheid, maakt elk uur
een samenvatting met een kooplijst en stuurt direct een noodmelding als er iets groots aan lijkt te komen. Het
dashboard vind je in de zijbalk, met aparte tabbladen voor **Aandelen** en **Crypto**.

**Geen beleggingsadvies.** Scores, kansen en noodmeldingen zijn modelinschattingen; een signaal is geen garantie.

## Snel starten

1. Vul **`sec_user_agent`** in met je naam en e-mailadres, bijvoorbeeld `Jan Jansen jan@voorbeeld.nl`.
   De SEC eist dit; het wordt alleen naar sec.gov gestuurd.
2. Vul **`notify_service`** in om berichten op je telefoon te krijgen, bijvoorbeeld `mobile_app_iphone_van_jan`.
   De naam vind je bij **Ontwikkelhulpmiddelen → Acties** door `notify.` te typen.
3. Optioneel: vul **`anthropic_api_key`** in voor AI-samenvattingen door Claude en een AI-controle van
   noodmeldingen. Zonder sleutel krijg je een automatische samenvatting (gratis).
4. Pas **`stock_watchlist`** en **`crypto_watchlist`** aan met wat je wilt volgen.
5. Start de add-on en zet **Toon in zijbalk** aan.

## Meldingen

- **Elk uur:** één gewoon pushbericht met de kop van beide markten (uitzetten met `notify_summaries: false`).
- **Noodmelding:** direct en met hoge prioriteit, bijvoorbeeld bij:
  - een koersexplosie met volumepiek;
  - een zeer grote crypto-aankoop door een bedrijf;
  - drie insiders die samen hetzelfde aandeel kopen;
  - groot positief nieuws dat door drie betrouwbare bronnen wordt gemeld;
  - een plotselinge sprong in de koopscore.
  
  Met `alert_critical: true` komt de melding ook door stil/niet storen heen.
- **Minder of meer meldingen:** zet `alert_sensitivity` op `laag` (alleen heel uitzonderlijk) of `hoog`, en/of
  verlaag `max_alerts_per_day`.

## Kosten van de AI

Ongeveer 48 aanvragen per dag (elk uur één per markt). Ruwe schatting per maand:

- `claude-opus-5-5` (standaard): ~$100
- `claude-sonnet-5-5`: ~$55
- `claude-haiku-5-5`: ~$3

## Wat je in Home Assistant krijgt

- **Beste kans per markt:** `sensor.market_monitor_stocks_top_pick` en `sensor.market_monitor_crypto_top_pick`.
- **Laatste samenvatting per markt:** `sensor.market_monitor_stocks_summary` en `sensor.market_monitor_crypto_summary`.
- **Laatste grote transactie per markt:** `sensor.market_monitor_stocks_last_big_trade` en
  `sensor.market_monitor_crypto_last_big_trade`.
- **Laatste noodmelding:** `sensor.market_monitor_alert`. Status van de add-on: `sensor.market_monitor_status`.
- **Events:** `market_monitor_alert`, `market_monitor_summary` en `market_monitor_big_trade`, voor eigen
  automatiseringen (bijvoorbeeld een lamp laten knipperen).

## Problemen oplossen

- **Bovenin staat een ✕ bij een bron:** beweeg erover voor de foutmelding. Kijk ook in het tabblad **Logboek**.
- **"SEC e-mail ontbreekt":** vul `sec_user_agent` in en herstart de add-on.
- **Geen pushberichten:** controleer de naam van `notify_service`. Test hem eerst zelf via
  Ontwikkelhulpmiddelen → Acties.
- **"CoinGecko: limiet bereikt":** maak een gratis demo-sleutel aan op coingecko.com en vul `coingecko_api_key` in.
- **Geen AI-samenvatting:** controleer de API-sleutel en het tegoed. Bij een fout valt de add-on automatisch terug
  op de automatische samenvatting.

Meer uitleg over bronnen, filter, score en detectoren staat in de README van de repository.
