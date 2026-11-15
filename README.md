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