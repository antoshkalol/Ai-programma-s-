# Market Monitor

Volgt grote aandelentransacties van Amerikaanse politici en bedrijfsinsiders, filtert het nieuws op
betrouwbaarheid en maakt elk halfuur een samenvatting met een kooplijst. Het dashboard vind je in de zijbalk.

**Geen beleggingsadvies.** Scores en kansen zijn modelinschattingen.

## Snel starten

1. Vul **`sec_user_agent`** in met je naam en e-mailadres, bijvoorbeeld `Jan Jansen jan@voorbeeld.nl`.
   De SEC eist dit; het wordt alleen naar sec.gov gestuurd.
2. Optioneel: vul **`anthropic_api_key`** in voor AI-samenvattingen door Claude. Zonder sleutel krijg je een
   automatische samenvatting (gratis).
3. Pas de **`watchlist`** aan met de aandelen die je wilt volgen.
4. Start de add-on en zet **Toon in zijbalk** aan.

Na een paar minuten verschijnen de eerste gegevens. De eerste samenvatting komt ongeveer 4 minuten na de start,
daarna elk halfuur.

## Kosten van de AI

Elk halfuur één aanvraag bij Anthropic. Ruwe schatting per maand:

- `claude-opus-5-5` (standaard): ~$100
- `claude-sonnet-5-5`: ~$55
- `claude-haiku-5-5`: ~$3

Goedkoper maken kan met een ander model, `anthropic_effort: low` of een groter `summary_interval_minutes`.

## Wat je in Home Assistant krijgt

- `sensor.market_monitor_top_pick`: het aandeel met de hoogste score (attributen: score, kans, instapmoment, redenen).
- `sensor.market_monitor_summary`: de laatste samenvatting (attributen: kernpunten, koopvisie, let_op, tekst).
- `sensor.market_monitor_last_big_trade`: de laatste grote transactie.
- `sensor.market_monitor_status`: `ok` of `fout`.
- Event `market_monitor_big_trade` bij elke transactie boven `big_trade_alert_usd`, en event
  `market_monitor_summary` bij elke nieuwe samenvatting.

Voorbeeld van een melding op je telefoon:

```yaml
triggers:
  - trigger: event
    event_type: market_monitor_big_trade
actions:
  - action: notify.mobile_app_jouw_telefoon
    data:
      title: "Grote transactie: {{ trigger.event.data.ticker }}"
      message: "{{ trigger.event.data.description }}"
```

## Problemen oplossen

- **Bovenin staat een ✕ bij een bron:** beweeg erover voor de foutmelding. Kijk ook in het tabblad **Logboek**.
- **"SEC e-mail ontbreekt":** vul `sec_user_agent` in en herstart de add-on.
- **Geen AI-samenvatting:** controleer de API-sleutel en het tegoed op console.anthropic.com. Bij een fout valt de
  add-on automatisch terug op de automatische samenvatting.
- **Lege kooplijst:** de eerste scores worden ongeveer 3 minuten na de start berekend en daarna elk kwartier.

Meer uitleg over de bronnen, het nieuwsfilter en de score staat in de README van de repository.
