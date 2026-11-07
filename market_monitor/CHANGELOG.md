# Changelog

## 0.3.0

- **Aanbeveling met technische analyse:** nieuw tabblad met per markt de beste kandidaat en de vraag of je moet
  longen, shorten of afwachten.
  - Plan met instapzone, stop-loss, twee doelen, risico/rendement (minimaal 1 : 1,2) en kans.
  - Koersgrafiek met SMA 20/50, Bollinger-band, trendkanaal, steun/weerstand, instap/stop/doelen, een
    verwachtingskegel (±1σ) en een RSI-strook.
  - Bewaart eerdere aanbevelingen met hun resultaat.
  - AI-commentaar in het uurbericht.
- **Popup-meldingen op je telefoon:**
  - Vindt telefoons met de Home Assistant-app automatisch.
  - Uurbericht en noodmeldingen verschijnen als popup; tikken opent het dashboard.
  - Een nieuw uurbericht vervangt het vorige.
  - Stille uren (`quiet_hours`).
  - Knop **Testmelding** in het dashboard.
- **Uurbericht en sensor:** het uurbericht bevat de aanbeveling per markt. Nieuwe sensor
  `sensor.market_monitor_<markt>_recommendation`.
- **RSI:** overal dezelfde (Wilder) berekening.

## 0.2.0

- **Crypto toegevoegd**, met een duidelijke splitsing: tabbladen Aandelen en Crypto, elk met eigen transacties,
  nieuws, samenvatting en kooplijst.
  - Koersen en volumes van de top 100 via CoinGecko.
  - Bedrijfsaankopen van crypto via SEC 8-K-meldingen.
  - Grote bitcoin-transfers.
  - Politici en insiders die crypto-ETF's of -aandelen kopen.
  - Crypto-nieuwsfeeds en extra filters tegen presales en "next 100x"-promotie.
- **Samenvatting elk uur** (was elk halfuur), optioneel als één pushbericht voor beide markten (`notify_service`).
- **Noodmeldingen:** direct een alert met hoge prioriteit bij een koersexplosie, groot nieuws, insiders die samen
  kopen, zeer grote aankopen of een score-sprong. Met afkoelperiode, daglimiet, instelbare gevoeligheid en
  optionele AI-controle tegen hype. Op het dashboard verschijnt een rode balk, eventueel met geluid.
- **Wissel-modus** voor een wandmonitor: automatisch wisselen tussen aandelen en crypto.
- **Opties hernoemd** (`stock_watchlist`, `stock_news_queries`, `stock_x_accounts`); de oude namen werken nog.
- **Database:** wordt bij de update automatisch opnieuw opgebouwd (alleen openbare data).

## 0.1.0

Eerste versie.

- Transacties van insiders (SEC Form 4) en Amerikaanse Congresleden (Huis, Senaat, optioneel Quiver).
- Nieuws uit RSS-feeds, Google News, GDELT en optioneel X, met een filter op betrouwbaarheid, clickbait en bevestiging.
- Samenvatting elk halfuur: door Claude (met API-sleutel) of automatisch.
- Kooplijst met score, kans op stijging (zelf-kalibrerend) en instapmoment.
- Dashboard via Home Assistant Ingress, sensoren en events voor automatiseringen.
