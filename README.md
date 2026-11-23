# Battery Revenue

Python-script om de financiële opbrengst van een thuisbatterij te berekenen op basis van:
- het batterijvermogen (laad/ontlaadvermogen)
- de actuele elektriciteitsprijs
- de tijdsperiode die je wilt analyseren

Het script leest data uit InfluxDB, berekent per kwartier de kosten/opbrengst en schrijft de resultaten weer terug als een measurement genaamd `battery_revenue`.

Belangrijk:
- lokaal staat de configuratie in `.env`
- die file mag niet in git terechtkomen
- de projectroot bevat de echte app; er is geen extra `src`-map nodig

Gebruik lokaal:

```bash
python battery_revenue.py --days 7 --dry-run --show
```

Gebruik in Docker:

```bash
docker compose up --build -d
```

Configuratie:
- `.env` bevat de standaardwaarden voor InfluxDB en de loopinstellingen
- in `docker-compose.yml` worden sommige waarden overschreven zodat de container correct communiceert met de juiste service in Docker
- het effect is dat dezelfde settings zowel lokaal op de Mac als in Docker kunnen werken
- `ENERGY_TAX_EUR_KWH` (standaard `0.11`): energiebelasting in EUR/kWh, incl. btw, die bij de marktprijs (al incl. btw) van de prijssensor wordt opgeteld. Kosten, opbrengst en winst rekenen met marktprijs + energiebelasting.
- `FEED_IN_COST_EUR_KWH` (standaard `0.0`): terugleverkosten in EUR/kWh. Zonder salderen is de terugleverprijs f = marktprijs − deze kosten.

Met en zonder salderen (velden in measurement `battery_revenue`):
- `profit_saldering_eur`: (ontladen − geladen) × a, met a = marktprijs + energiebelasting. Gelijk aan het bestaande `profit_eur`.
- `profit_zonder_saldering_eur`: (Gi′ − Gi) × a − (Ge′ − Ge) × f. Gi/Ge is de werkelijke afname/teruglevering, Gi′/Ge′ wat het net zonder batterij had gedaan.
- `gi_kwh`, `ge_kwh`, `gi_zonder_batt_kwh`, `ge_zonder_batt_kwh`: de energie per kwartier achter die berekening. Deze wordt per minuut berekend en per kwartier opgeteld, omdat afname en teruglevering binnen één kwartier allebei kunnen voorkomen.

Voorbeeld van een run zonder schrijven naar InfluxDB:

```bash
python battery_revenue.py --days 3 --dry-run --show
```

Voor continue service-modus:

```bash
python battery_revenue.py --days 3 --every-minutes 1
```

Alles gecombineerd:

python battery_revenue.py --days 3 --every-minutes 1 --show --dry-run