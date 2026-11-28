# CLAUDE.md — battery-revenue

Communiceer in het Nederlands. Code-commentaar en output van het script zijn ook Nederlands.

## Wat dit project doet
Eén Python-script (`battery_revenue.py`) dat de financiële opbrengst van de thuisbatterij berekent:
- leest uit InfluxDB 2.x (gevuld door Home Assistant):
  - `plug_in_battery_power` — vermogen batterij in W (positief = laden)
  - `energyzero_today_energy_current_hour_price` — stroomprijs in EUR/kWh
- brengt beide naar een kwartierraster (Flux `aggregateWindow` + `fill(usePrevious)`, omdat HA alleen bij wijziging schrijft)
- berekent per kwartier `charged_kwh`, `discharged_kwh`, `cost_eur`, `revenue_eur`, `profit_eur`
- schrijft dit terug als measurement `battery_revenue`; Grafana toont het
- toont optioneel de laatste grafiek via een kleine webserver (`--serve-port` / `PLOT_PORT`; in Docker poort 8050,
  op de Pi http://raspberrypi400.local:8050/)

Onderdeel van de bredere home-automation setup (InfluxDB en Grafana in repo `CvanderStoep/monitoring`), maar draait als losse container.

## Structuur
- `battery_revenue.py` — de hele app; geen `src/`-map (bewust zo houden)
- `pyproject.toml` + `uv.lock` — afhankelijkheden voor lokaal (uv, Python 3.14)
- `requirements.txt` — afhankelijkheden voor de Docker-image
- `Dockerfile`, `docker-compose.yml` — container `battery-revenue` op extern netwerk `ha_network`
- `COMMANDS.md` — veelgebruikte commando's
- `.env` — lokale config en secrets (InfluxDB URL/token/org/bucket)

## Commando's
```bash
uv sync                                                    # omgeving opzetten
uv run python battery_revenue.py --days 3 --dry-run --show --every-minutes 0  # één keer testen zonder te schrijven (.env zet RUN_EVERY_MINUTES, dus 0 expliciet meegeven)
uv run python battery_revenue.py --days 3 --every-minutes 1 --dry-run   # service-modus lokaal
docker compose up --build -d                               # container bouwen en starten
docker logs -f --tail 50 battery-revenue                   # logs bekijken
```

## Deployment (Raspberry Pi 400)
- Draait op `raspberrypi400` (gebruiker `pi`) in `~/battery-revenue`
- Overal dezelfde naam: map `battery-revenue/` op de Mac, `~/battery-revenue` op de Pi, GitHub `CvanderStoep/battery-revenue`
  (tot 2026-11-28: `battery` op de Mac, `Project-Battery` op de Pi en GitHub). Compose-`name:` en container: `battery-revenue`.
- Werkwijze: wijzigen op de Mac → commit/push → op de Pi `git pull` → `docker compose up --build -d`
- `.env` gaat apart mee: `scp .env pi@raspberrypi400.local:~/battery-revenue/.env`
- In Docker overschrijft `docker-compose.yml` o.a. `INFLUX_URL=http://influxdb:8086`, `RUN_EVERY_MINUTES=15`, `LOOKBACK_DAYS=3`. Lokaal op de Mac komt alles uit `.env` (localhost).

## Regels
- `.env` nooit lezen, tonen, committen of in de image stoppen. Nieuwe config-variabelen: documenteer ze in de README.
- Test altijd eerst met `--dry-run` voordat iets naar InfluxDB schrijft. Schrijven naar de productie-bucket alleen als ik erom vraag.
- Nieuwe dependency? Voeg hem toe met `uv add` **én** in `requirements.txt`, anders breekt de Docker-build.
- Service-modus mag niet crashen: fouten in `run()` worden gelogd en de loop gaat door. Houd dat zo.
- Tijden: intern UTC, weergave in `Europe/Amsterdam`. Kwartiertijd = begin van het kwartier.
- Houd het één bestand en simpel, tenzij ik expliciet om een herstructurering vraag.

## Bekende aandachtspunten
- Nog geen tests. De rekenlogica (kWh per kwartier, tekenconventie, kosten/opbrengst) is goed testbaar als losse functie met een kleine DataFrame.
- `POWER_TO_W` en `CHARGE_IS_POSITIVE` in het CONFIG-blok bepalen eenheid en teken van de batterijsensor.
- Wordt herhaald over de laatste N dagen; punten met dezelfde tijd worden in InfluxDB overschreven, dus herberekenen is veilig.
