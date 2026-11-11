# Changelog

## 0.5.0

- **Aanbeveling blijft actueel:**
  - Elke 3 minuten wordt de aanbeveling tegen de actuele koers gehouden, met een live status: in de instapzone,
    niet najagen, doel 1 gehaald, doel 2 gehaald of stop-loss geraakt.
  - Het plan wijzigt direct als het nodig is: stop-loss of doel 2 geraakt, de richting slaat om, of een andere
    kandidaat is duidelijk sterker (30% beter, na minimaal een uur).
  - Bij doel 1 gaat de stop naar de instapprijs.
  - Na 24 uur buiten de instapzone worden instap, stop en doelen bijgewerkt naar de huidige koers.
  - Na een geraakte stop wordt dezelfde kant van dat aandeel of die munt 24 uur niet opnieuw aanbevolen.
  - Popup op je telefoon bij elke echte wijziging (`notify_recommendation_changes`).
  - Eerdere aanbevelingen tonen hun uitkomst ("stop geraakt", "doel 2 gehaald", ...).
  - Na een wijziging wordt de samenvatting ververst; AI-commentaar verschijnt alleen als het bij het huidige plan
    hoort.
- **Aandelenkoersen binnen de gratis limieten:**
  - Tijdens beurstijd (New York) worden volglijst, aanbevolen aandeel en sterkste kandidaten elk kwartier
    ververst, de rest elk uur; daarbuiten één keer na het slot.
  - Twelve Data heeft een dagbudget van 750 verzoeken. Voorheen kon de gratis limiet na een paar uur op zijn,
    waarna koersen en aanbeveling bleven hangen.
- **Grafieken op de telefoon:**
  - Getekend op de echte schermbreedte, met een hogere en compactere telefoonversie en leesbare tekst.
  - Labels staan in de goede volgorde, met verbindingslijntjes als ze moeten uitwijken.
  - De aanbevelingskaart overlapt de rest van de pagina niet meer.
  - Bij het draaien van je telefoon worden de grafieken opnieuw getekend.

## 0.4.0

- **Samenvatting in gewone taal:** elke samenvatting begint met een alinea die uitlegt:
  - wat er speelde in het nieuws;
  - wat de grote kopers de afgelopen week deden;
  - welk aandeel of welke munt er nu het best uitziet om te kopen, of dat afwachten verstandiger is.

  Met en zonder AI-sleutel.
- **Knop "Nu samenvatten":** maakt direct een samenvatting. Na de start wordt er automatisch meteen een gemaakt
  (je hoeft niet meer op het hele uur te wachten).
- **Aanbeveling-tabblad:**
  - Een "In het kort"-blok met die samenvatting bovenaan elke kaart.
  - Is de technische analyse (nog) niet mogelijk, dan staat er toch het beste aandeel volgens de koopscore, met
    de redenen en de reden waarom er geen grafiek is.
- **Aandelenkoersen robuuster:**
  - Nasdaq als extra gratis bron naast Yahoo Finance en Stooq.
  - Optioneel een gratis Twelve Data-sleutel (`stock_price_api_key`).
  - Een bron die "te veel verzoeken" geeft, wordt 30 minuten overgeslagen.
  - De status toont per bron wat er misging.

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
