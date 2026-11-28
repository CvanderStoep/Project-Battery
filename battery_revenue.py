#!/usr/bin/env python3
"""
Opbrengst thuisbatterij per kwartier en per dag.

Twee scenario's, a = marktprijs + energiebelasting, f = terugleverprijs:
  met salderen:    winst = (ontladen_kWh - geladen_kWh) * a
  zonder salderen: winst = (Gi' - Gi) * a - (Ge' - Ge) * f
                   Gi/Ge = werkelijke afname/teruglevering, Gi'/Ge' = zonder batterij

Bronnen (Home Assistant -> InfluxDB 2.x):
  - sensor.plug_in_battery_power                          vermogen batterij (W)
  - sensor.energyzero_today_energy_current_hour_price     prijs EUR/kWh (per uur)

Resultaat wordt teruggeschreven als measurement 'battery_revenue' voor Grafana.

Installeren:  pip install influxdb-client pandas
Draaien:      python battery_revenue.py --days 7 --dry-run
"""
import argparse
import io
import os
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

# Flux-filters per signaal.
# Home Assistant schrijft: tag entity_id (zonder "sensor."), field "value".
FILTER_BATTERY = 'r.entity_id == "plug_in_battery_power" and r._field == "value"'
FILTER_PRICE = 'r.entity_id == "energyzero_today_energy_current_hour_price" and r._field == "value"'
# Zonnepanelen komen niet uit HA maar uit een eigen measurement (in W).
FILTER_SOLAR = 'r._measurement == "solarpanel" and r._field == "power"'
# Netmeting (Tibber Pulse op de P1-poort), beide in W en altijd >= 0:
# Gi = afname van het net, Ge = teruglevering aan het net.
FILTER_GRID_IMPORT = 'r.entity_id == "tibber_pulse_scheepmakersingel_1_power" and r._field == "value"'
FILTER_GRID_EXPORT = 'r.entity_id == "tibber_pulse_scheepmakersingel_1_power_production" and r._field == "value"'
# Laadvermogen auto (laadpaal via HA), in W.
FILTER_CAR = 'r.entity_id == "marielle_van_ooi_charge_power" and r._field == "value"'

POWER_TO_W = 1.0            # 1.0 als de sensor in W meet, 1000 bij kW
CHARGE_IS_POSITIVE = True   # True: positief vermogen = laden, negatief = ontladen

# De prijssensor geeft de marktprijs incl. btw; energiebelasting (ook incl. btw)
# komt erbij (EUR/kWh).
ENERGY_TAX = float(os.getenv("ENERGY_TAX_EUR_KWH", "0.11"))
# Zonder salderen: terugleverprijs f = marktprijs - terugleverkosten (EUR/kWh).
FEED_IN_COST = float(os.getenv("FEED_IN_COST_EUR_KWH", "0.0"))

TIMEZONE = "Europe/Amsterdam"
# --------------------------------------------------------------------------

print(f"Influx: {INFLUX_URL} | org={INFLUX_ORG} | bucket={BUCKET_SOURCE}")


def flux(filter_expr: str, fn: str, days: int) -> str:
    """
    Brengt een signaal naar een vast kwartierraster. Home Assistant schrijft
    alleen bij een wijziging, dus de laatste waarde blijft gelden tot er een
    nieuwe komt.
      - "mean": eerst naar 1 minuut (waarde vasthouden), dan gemiddelde per
        kwartier = tijdgewogen gemiddeld vermogen.
      - "last": laatste waarde per kwartier, vasthouden bij gaten (prijs).
      - "minute": alleen het 1-minuutraster (waarde vasthouden), voor de
        berekening zonder salderen.
    """
    head = f'''
from(bucket: "{BUCKET_SOURCE}")
  |> range(start: -{days + 2}d)
  |> filter(fn: (r) => {filter_expr})
'''
    if fn == "minute":
        body = '''
  |> aggregateWindow(every: 1m, fn: last, createEmpty: true, timeSrc: "_start")
  |> fill(usePrevious: true)
'''
    elif fn == "mean":
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


def energie_per_kwartier(minuut: pd.DataFrame) -> pd.DataFrame:
    """
    Energie per kwartier (kWh), per minuut berekend en dan opgeteld. Per minuut
    omdat laden/ontladen en afname/teruglevering binnen één kwartier allebei
    kunnen voorkomen; eerst per kwartier netto nemen zou dat wegstrepen. Een
    lopend kwartier telt alleen de minuten die al voorbij zijn.

    minuut: kolommen batt_w, gi_w, ge_w (W, één rij per minuut).
    Geeft per kwartier:
      charged_kwh, discharged_kwh            batterij (alleen batt_w nodig)
      gi_kwh, ge_kwh                         net, werkelijk
      gi_zonder_batt_kwh, ge_zonder_batt_kwh net zonder batterij
    De netkolommen tellen alleen minuten waarin batterij én net bekend zijn.
    """
    b = minuut["batt_w"] * POWER_TO_W / 1000 / 60      # kWh per minuut, + = laden
    if not CHARGE_IS_POSITIVE:
        b = -b
    compleet = minuut[["batt_w", "gi_w", "ge_w"]].notna().all(axis=1)
    gi = (minuut["gi_w"] / 1000 / 60).where(compleet)
    ge = (minuut["ge_w"] / 1000 / 60).where(compleet)
    n_zonder = gi - ge - b                              # N' = N + Bd - Bc
    out = pd.DataFrame({
        "charged_kwh": b.clip(lower=0),
        "discharged_kwh": (-b).clip(lower=0),
        "gi_kwh": gi,
        "ge_kwh": ge,
        "gi_zonder_batt_kwh": n_zonder.clip(lower=0),
        "ge_zonder_batt_kwh": (-n_zonder).clip(lower=0),
    })
    return out.resample("15min").sum(min_count=1)


def plot(local: pd.DataFrame, doel) -> None:
    """
    Tijdreeks van zonnepanelen, prijs, batterij en net per kwartier (lokale tijd).
    Trapjes: elke waarde geldt vanaf het begin van zijn kwartier.
    doel: "" = venster tonen, bestandsnaam = opslaan als afbeelding,
    BytesIO = PNG in het geheugen (voor de webserver).
    """
    import matplotlib
    if doel != "":
        matplotlib.use("Agg")           # opslaan werkt ook zonder scherm (Pi/Docker)
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    t = local.index
    batt = local["batt_w"] * POWER_TO_W * (1 if CHARGE_IS_POSITIVE else -1)

    fig, (ax_s, ax_p, ax_b, ax_n, ax_w) = plt.subplots(5, 1, sharex=True, figsize=(12, 13))

    ax_s.step(t, local["solar_w"], where="post", color="goldenrod", linewidth=0.8)
    ax_s.fill_between(t, local["solar_w"], 0, step="post", color="gold", alpha=0.5)
    ax_s.set_ylabel("Zonnepanelen (W)")
    ax_s.set_title("Zonnepanelen, prijs, batterij en net per kwartier; winst per dag")
    ax_s.set_ylim(bottom=0)
    ax_s.grid(alpha=0.3)

    ax_p.step(t, local["price"], where="post", color="tab:blue",
              label=f"incl. energiebelasting (+{ENERGY_TAX:.2f})")
    ax_p.step(t, local["price_market"], where="post", color="tab:blue", alpha=0.4,
              linestyle=":", label="marktprijs")
    ax_p.set_ylabel("Prijs (EUR/kWh)")
    ax_p.legend(loc="upper left")
    ax_p.grid(alpha=0.3)

    ax_b.step(t, batt, where="post", color="0.3", linewidth=0.8)
    # clip i.p.v. where=: anders valt bij trapjes het laatste kwartier van elk blok weg
    ax_b.fill_between(t, batt.clip(lower=0), 0, step="post", color="tab:green",
                      alpha=0.5, label="laden")
    ax_b.fill_between(t, batt.clip(upper=0), 0, step="post", color="tab:orange",
                      alpha=0.5, label="ontladen")
    ax_b.axhline(0, color="0.5", linewidth=0.8)
    ax_b.set_ylabel("Batterijvermogen (W)")
    ax_b.legend(loc="upper right")
    ax_b.grid(alpha=0.3)

    # Net: afname (Gi) boven de nullijn, teruglevering (Ge) eronder.
    ax_n.step(t, local["gi_w"], where="post", color="tab:red", linewidth=0.8)
    ax_n.fill_between(t, local["gi_w"], 0, step="post", color="tab:red",
                      alpha=0.4, label="afname Gi")
    ax_n.step(t, -local["ge_w"], where="post", color="tab:purple", linewidth=0.8)
    ax_n.fill_between(t, -local["ge_w"], 0, step="post", color="tab:purple",
                      alpha=0.4, label="teruglevering Ge")
    # Auto als lijn over de afname heen: laat zien welk deel van Gi de auto is.
    ax_n.step(t, local["car_w"], where="post", color="black", linewidth=1.2,
              linestyle="--", label="laden auto")
    # Per laadsessie van de auto de kosten erboven: kWh per kwartier x prijs a.
    # Sessie = aaneengesloten kwartieren > 100 W; pauzes tot 30 min tellen mee.
    car_w = local["car_w"].fillna(0)
    sessies = []                                # [eerste, laatste] positie
    for i, aan in enumerate(car_w > 100):
        if not aan:
            continue
        if sessies and i - sessies[-1][1] <= 3:
            sessies[-1][1] = i
        else:
            sessies.append([i, i])
    for eerste, laatste in sessies:
        sel = local.iloc[eerste:laatste + 1]
        kwh = sel["car_w"].fillna(0) / 1000 * 0.25
        if kwh.sum() < 0.5:                     # kort piekje, geen echte sessie
            continue
        kosten = (kwh * sel["price"]).sum()
        begin, eind = sel.index[0], sel.index[-1] + pd.Timedelta(minutes=15)
        ax_n.annotate(f"auto €{kosten:.2f}\n{kwh.sum():.1f} kWh",
                      xy=(begin + (eind - begin) / 2, sel["gi_w"].max()),
                      xytext=(0, 5), textcoords="offset points", ha="center", va="bottom",
                      fontsize=8, bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                                            edgecolor="0.6"))
    ax_n.margins(y=0.25)                        # ruimte voor de labels
    ax_n.axhline(0, color="0.5", linewidth=0.8)
    ax_n.set_ylabel("Net (W)")
    ax_n.legend(loc="upper right")
    ax_n.grid(alpha=0.3)

    # Winst per dag (zelfde getallen als de dagtabel in de terminal): per dag
    # twee balken naast elkaar, links met salderen, rechts zonder.
    reeksen = [("profit_saldering_eur", "met salderen", "tab:blue"),
               ("profit_zonder_saldering_eur", "zonder salderen", "tab:orange")]
    winst = local[[k for k, _, _ in reeksen]]
    for _, g in winst.groupby(t.date):
        begin, eind = g.index[0], g.index[-1] + pd.Timedelta(minutes=15)
        if begin.hour == 0 and begin.minute == 0:
            for a in (ax_s, ax_p, ax_b, ax_n, ax_w):
                a.axvline(begin, color="0.4", linewidth=0.8, linestyle=":")
        breedte = (eind - begin) * 0.4
        midden = begin + (eind - begin) / 2
        for i, (kol, _, kleur) in enumerate(reeksen):
            totaal = round(g[kol].sum(), 2) + 0.0      # + 0.0 voorkomt "-0.00"
            x = midden - breedte if i == 0 else midden
            ax_w.bar(x, totaal, width=breedte, align="edge", color=kleur, alpha=0.7)
            if eind - begin < pd.Timedelta(hours=6):   # te smal voor een leesbaar label
                continue
            ax_w.annotate(f"{totaal:+.2f}", xy=(x + breedte / 2, totaal),
                          xytext=(0, 3 if totaal >= 0 else -3), textcoords="offset points",
                          ha="center", va="bottom" if totaal >= 0 else "top", fontsize=8)
    ax_w.axhline(0, color="0.5", linewidth=0.8)
    ax_w.set_ylabel("Winst per dag (EUR)")
    ax_w.legend(handles=[Patch(color=kleur, alpha=0.7,
                               label=f"{label} (totaal {winst[kol].sum():+.2f})")
                         for kol, label, kleur in reeksen],
                loc="upper left", fontsize=9)
    ax_w.margins(y=0.3)                        # ruimte voor de labels
    ax_w.grid(alpha=0.3)

    ax_w.xaxis.set_major_formatter(mdates.DateFormatter("%d-%m %H:%M", tz=t.tz))
    fig.autofmt_xdate()
    fig.tight_layout()

    if doel == "":
        plt.show()
    else:
        fig.savefig(doel, dpi=120)
        if isinstance(doel, str):
            print(f"Grafiek opgeslagen in {doel}")
    plt.close(fig)


# --------------------------------------------------------------------------
# WEBSERVER (optioneel, --serve-port): laatste grafiek in de browser
# --------------------------------------------------------------------------
_laatste_grafiek = None     # (png-bytes, tijdstip); gezet door run(), gelezen door de server


class GrafiekHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        pad = self.path.split("?")[0]
        grafiek = _laatste_grafiek
        if pad == "/plot.png" and grafiek:
            self._stuur(200, "image/png", grafiek[0])
        elif pad in ("/", "/index.html"):
            if grafiek:
                inhoud = (f'<p>Bijgewerkt: {grafiek[1]:%d-%m-%Y %H:%M}</p>'
                          f'<img src="/plot.png?t={int(grafiek[1].timestamp())}" alt="Batterijgrafiek">')
            else:
                inhoud = "<p>Nog geen grafiek: de eerste berekening loopt nog.</p>"
            html = ('<!doctype html><html lang="nl"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<meta http-equiv="refresh" content="60"><title>Thuisbatterij</title>'
                    '<style>body{font-family:sans-serif;margin:16px;background:#fff;color:#222}'
                    'img{max-width:100%;height:auto}</style></head>'
                    f'<body><h1>Thuisbatterij</h1>{inhoud}</body></html>')
            self._stuur(200, "text/html; charset=utf-8", html.encode())
        else:
            self._stuur(404, "text/plain; charset=utf-8", "Niet gevonden".encode())

    def _stuur(self, status: int, soort: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", soort)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):       # geen logregel per verzoek
        pass


def start_webserver(poort: int) -> bool:
    """Start de webserver in een achtergrondthread; False als dat niet lukt."""
    try:
        server = ThreadingHTTPServer(("0.0.0.0", poort), GrafiekHandler)
    except OSError as e:                # bv. poort al in gebruik: rekenen gaat gewoon door
        print(f"Webserver niet gestart op poort {poort}: {e}", flush=True)
        return False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Grafiek via webserver: http://localhost:{poort}/ (op de Pi: "
          f"http://raspberrypi400.local:{poort}/)", flush=True)
    return True


def run(args) -> None:
    """Eén berekening over de laatste `args.days` dagen."""
    global _laatste_grafiek
    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=60_000) as client:
        # Batterij en net per minuut (nodig voor 'zonder salderen');
        # het kwartiergemiddelde volgt daar zelf uit.
        batt_m = query_series(client, flux(FILTER_BATTERY, "minute", args.days), "batt_w")
        gi_m = query_series(client, flux(FILTER_GRID_IMPORT, "minute", args.days), "gi_w")
        ge_m = query_series(client, flux(FILTER_GRID_EXPORT, "minute", args.days), "ge_w")
        price = query_series(client, flux(FILTER_PRICE, "last", args.days), "price_market")
        solar = query_series(client, flux(FILTER_SOLAR, "mean", args.days), "solar_w")
        car = query_series(client, flux(FILTER_CAR, "mean", args.days), "car_w")

        for s in (batt_m, price, solar, gi_m, ge_m, car):
            if s.dropna().empty:
                print(f"  {s.name}: GEEN data")
            else:
                first = s.dropna().index.min().tz_convert(TIMEZONE)
                print(f"  {s.name}: data vanaf {first:%Y-%m-%d %H:%M}")

        minuut = pd.concat([batt_m, gi_m, ge_m], axis=1, sort=True)
        kwartier = minuut.resample("15min").mean()       # tijdgewogen gemiddeld vermogen
        batt, gi, ge = kwartier["batt_w"], kwartier["gi_w"], kwartier["ge_w"]
        energie = energie_per_kwartier(minuut)

        df = pd.concat([batt, price], axis=1, sort=True).dropna()
        if df.empty:
            raise SystemExit(
                "Geen data gevonden: controleer INFLUX_BUCKET en de filters in het CONFIG-blok."
            )
        start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=args.days)
        df = df[df.index >= start].copy()
        # Zon en net alleen ter informatie (grafiek/--show): pas na dropna toevoegen,
        # zodat een gat in die data de berekening niet beïnvloedt.
        for s in (solar, gi, ge, car):
            df[s.name] = s.reindex(df.index)

        df = df.join(energie)

        # Met salderen: één prijs a (marktprijs + energiebelasting) voor alles.
        df["price"] = df["price_market"] + ENERGY_TAX     # a
        df["cost_eur"] = df["charged_kwh"] * df["price"]
        df["revenue_eur"] = df["discharged_kwh"] * df["price"]
        df["profit_eur"] = df["revenue_eur"] - df["cost_eur"]
        df["profit_saldering_eur"] = df["profit_eur"]

        # Zonder salderen: minder afname tegen a, minder teruglevering tegen f.
        # Kwartieren zonder netdata blijven leeg (NaN) en worden niet geschreven.
        df["price_feed_in"] = df["price_market"] - FEED_IN_COST   # f
        df["profit_zonder_saldering_eur"] = (
            (df["gi_zonder_batt_kwh"] - df["gi_kwh"]) * df["price"]
            - (df["ge_zonder_batt_kwh"] - df["ge_kwh"]) * df["price_feed_in"]
        )

        cols = ["charged_kwh", "discharged_kwh", "cost_eur", "revenue_eur", "profit_eur",
                "profit_saldering_eur", "profit_zonder_saldering_eur",
                "gi_kwh", "ge_kwh", "gi_zonder_batt_kwh", "ge_zonder_batt_kwh"]
        local = df.tz_convert(TIMEZONE)

        if args.show:
            with pd.option_context("display.max_rows", None, "display.width", 250):
                print("\nKwartierdata (lokale tijd, tijd = begin van het kwartier):")
                print(local[["solar_w", "gi_w", "ge_w", "car_w", "batt_w", "price_market", "price"]
                            + cols].round(4).to_string())
        dagcols = ["charged_kwh", "discharged_kwh", "gi_kwh", "ge_kwh",
                   "gi_zonder_batt_kwh", "ge_zonder_batt_kwh",
                   "profit_saldering_eur", "profit_zonder_saldering_eur"]
        daily = local.groupby(local.index.date)[dagcols].sum()
        with pd.option_context("display.width", 250):
            print()
            print(daily.round(3).to_string())
        print(f"\nTotaal met salderen:    {daily['profit_saldering_eur'].sum():6.2f} EUR")
        print(f"Totaal zonder salderen: {daily['profit_zonder_saldering_eur'].sum():6.2f} EUR")

        if args.plot is not None:
            plot(local, args.plot)

        if args.serve_port > 0:
            try:
                buf = io.BytesIO()
                plot(local, buf)
                _laatste_grafiek = (buf.getvalue(), pd.Timestamp.now(tz=TIMEZONE))
            except Exception as e:          # mislukte grafiek mag het schrijven niet tegenhouden
                print(f"Grafiek voor webserver mislukt: {e}", flush=True)

        if args.dry_run:
            return

        points = []
        for ts, r in df.iterrows():
            p = Point("battery_revenue").time(ts.to_pydatetime(), WritePrecision.S)
            for c in cols:
                if pd.notna(r[c]):                   # InfluxDB kent geen NaN
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
    ap.add_argument("--plot", nargs="?", const="", default=None, metavar="BESTAND",
                    help="toon een grafiek van prijs en batterijvermogen; "
                         "met BESTAND (bv. plot.png) wordt hij opgeslagen")
    ap.add_argument("--serve-port", type=int, default=int(os.getenv("PLOT_PORT", "0")),
                    metavar="POORT",
                    help="0 = uit; >0 = laatste grafiek tonen via een webserver op deze poort "
                         "(env: PLOT_PORT)")
    args = ap.parse_args()

    if args.plot == "" and args.every_minutes > 0:
        ap.error("--plot zonder bestand blokkeert de service-modus; "
                 "geef --every-minutes 0 mee of een bestandsnaam")
    if args.plot == "" and args.serve_port > 0:
        ap.error("--plot zonder bestand opent een venster; combineer dat niet met --serve-port")

    serveert = args.serve_port > 0 and start_webserver(args.serve_port)

    if args.every_minutes <= 0:
        run(args)
        if serveert and _laatste_grafiek:   # eenmalig: blijf de grafiek serveren
            print("Ctrl+C om te stoppen.", flush=True)
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
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