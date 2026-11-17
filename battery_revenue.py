#!/usr/bin/env python3
"""
Opbrengst thuisbatterij per kwartier en per dag.

Alleen de energiestroom in/uit de batterij telt, gewaardeerd tegen de prijs:
    laden    (kost)      = geladen_kWh   * prijs
    ontladen (opbrengst) = ontladen_kWh  * prijs
    winst                = opbrengst - kosten

Bronnen (Home Assistant -> InfluxDB 2.x):
  - sensor.plug_in_battery_power                          vermogen batterij (W)
  - sensor.energyzero_today_energy_current_hour_price     prijs EUR/kWh (per uur)

Resultaat wordt teruggeschreven als measurement 'battery_revenue' voor Grafana.

Installeren:  pip install influxdb-client pandas
Draaien:      python battery_revenue.py --days 7 --dry-run
"""
import argparse
import os
import time
import warnings

import pandas as pd
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.warnings import MissingPivotFunction
from influxdb_client.client.write_api import SYNCHRONOUS

warnings.simplefilter("ignore", MissingPivotFunction)

# Lees .env automatisch als python-dotenv geïnstalleerd is (handig op de Mac).
# Bestaande omgevingsvariabelen winnen, dus in Docker blijven de compose-waarden gelden.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
INFLUX_URL = os.getenv("INFLUX_URL", "")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "home")

BUCKET_SOURCE = os.getenv("INFLUX_BUCKET", "localdatadb2")
BUCKET_TARGET = os.getenv("INFLUX_BUCKET_TARGET", BUCKET_SOURCE)

# Home Assistant schrijft: tag entity_id (zonder "sensor."), field "value"
ENTITY_BATTERY = "plug_in_battery_power"
ENTITY_PRICE = "energyzero_today_energy_current_hour_price"
FIELD = "value"

POWER_TO_W = 1.0            # 1.0 als de sensor in W meet, 1000 bij kW
CHARGE_IS_POSITIVE = True   # True: positief vermogen = laden, negatief = ontladen

TIMEZONE = "Europe/Amsterdam"
# --------------------------------------------------------------------------

print(f"Influx: {INFLUX_URL} | org={INFLUX_ORG} | bucket={BUCKET_SOURCE}")


def flux(entity: str, fn: str, days: int) -> str:
    """
    Brengt een signaal naar een vast kwartierraster. Home Assistant schrijft
    alleen bij een wijziging, dus de laatste waarde blijft gelden tot er een
    nieuwe komt.
      - "mean": eerst naar 1 minuut (waarde vasthouden), dan gemiddelde per
        kwartier = tijdgewogen gemiddeld vermogen.
      - "last": laatste waarde per kwartier, vasthouden bij gaten (prijs).
    """
    head = f'''
from(bucket: "{BUCKET_SOURCE}")
  |> range(start: -{days + 2}d)
  |> filter(fn: (r) => r.entity_id == "{entity}" and r._field == "{FIELD}")
'''
    if fn == "mean":
        body = '''
  |> aggregateWindow(every: 1m, fn: last, createEmpty: true, timeSrc: "_start")
  |> fill(usePrevious: true)
  |> aggregateWindow(every: 15m, fn: mean, createEmpty: false, timeSrc: "_start")
'''
    else:
        body = '''
  |> aggregateWindow(every: 15m, fn: last, createEmpty: true, timeSrc: "_start")
  |> fill(usePrevious: true)
'''
    return head + body + '  |> keep(columns: ["_time", "_value"])\n'


def query_series(client: InfluxDBClient, query: str, name: str) -> pd.Series:
    df = client.query_api().query_data_frame(query, org=INFLUX_ORG)
    if isinstance(df, list):
        df = pd.concat(df) if df else pd.DataFrame()
    if df.empty:
        return pd.Series(name=name, dtype=float)
    s = df.set_index("_time")["_value"].astype(float)
    s.index = pd.to_datetime(s.index, utc=True)
    s = s[~s.index.duplicated(keep="last")]
    s.name = name
    return s


def run(args) -> None:
    """Eén berekening over de laatste `args.days` dagen."""
    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=60_000) as client:
        batt = query_series(client, flux(ENTITY_BATTERY, "mean", args.days), "batt_w")
        price = query_series(client, flux(ENTITY_PRICE, "last", args.days), "price")

        for s in (batt, price):
            if s.empty:
                print(f"  {s.name}: GEEN data")
            else:
                first = s.dropna().index.min().tz_convert(TIMEZONE)
                print(f"  {s.name}: data vanaf {first:%Y-%m-%d %H:%M}")

        df = pd.concat([batt, price], axis=1, sort=True).dropna()
        if df.empty:
            raise SystemExit(
                "Geen data gevonden: controleer INFLUX_BUCKET en de entity_id's in het CONFIG-blok."
            )
        start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=args.days)
        df = df[df.index >= start].copy()

        kwh = df["batt_w"] * POWER_TO_W / 1000 * 0.25     # kWh per kwartier, met teken
        if not CHARGE_IS_POSITIVE:
            kwh = -kwh
        df["charged_kwh"] = kwh.clip(lower=0)
        df["discharged_kwh"] = (-kwh).clip(lower=0)

        df["cost_eur"] = df["charged_kwh"] * df["price"]
        df["revenue_eur"] = df["discharged_kwh"] * df["price"]
        df["profit_eur"] = df["revenue_eur"] - df["cost_eur"]

        cols = ["charged_kwh", "discharged_kwh", "cost_eur", "revenue_eur", "profit_eur"]
        local = df.tz_convert(TIMEZONE)

        if args.show:
            with pd.option_context("display.max_rows", None, "display.width", 200):
                print("\nKwartierdata (lokale tijd, tijd = begin van het kwartier):")
                print(local[["batt_w", "price"] + cols].round(4).to_string())
        daily = local.groupby(local.index.date)[cols].sum()
        print()
        print(daily.round(3).to_string())
        print(f"\nTotaal over periode: {daily['profit_eur'].sum():.2f} EUR")

        if args.dry_run:
            return

        points = []
        for ts, r in df.iterrows():
            p = Point("battery_revenue").time(ts.to_pydatetime(), WritePrecision.S)
            for c in cols:
                p = p.field(c, float(r[c]))
            points.append(p)
        client.write_api(write_options=SYNCHRONOUS).write(
            bucket=BUCKET_TARGET, org=INFLUX_ORG, record=points
        )
        print(f"{len(points)} kwartierpunten geschreven naar '{BUCKET_TARGET}'.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=int(os.getenv("LOOKBACK_DAYS", "7")),
                    help="aantal dagen terug (env: LOOKBACK_DAYS)")
    ap.add_argument("--dry-run", action="store_true", help="niet naar InfluxDB schrijven")
    ap.add_argument("--show", action="store_true", help="print alle kwartierwaarden (ter controle)")
    ap.add_argument("--every-minutes", type=int, default=int(os.getenv("RUN_EVERY_MINUTES", "0")),
                    help="0 = één keer draaien; >0 = blijven draaien en elke N minuten herhalen "
                         "(env: RUN_EVERY_MINUTES)")
    args = ap.parse_args()

    if args.every_minutes <= 0:
        run(args)
        return

    print(f"Service-modus: elke {args.every_minutes} min, laatste {args.days} dagen.", flush=True)
    while True:
        try:
            run(args)
        except (Exception, SystemExit) as e:   # een fout mag de container niet stoppen
            print(f"Fout: {e}", flush=True)
        time.sleep(args.every_minutes * 60)


if __name__ == "__main__":
    main()