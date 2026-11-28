docker logs -f --tail 50 battery-revenue
git pull
scp .env pi@raspberrypi400.local:~/battery-revenue/.env
docker compose up --build -d
python battery_revenue.py --days 3 --every-minutes 1 --show --dry-run
uv run python battery_revenue.py --days 3 --dry-run --plot --every-minutes 0   # eenmalige dry-run met grafiek (--plot plot.png slaat hem op)
uv run python battery_revenue.py --days 3 --dry-run --serve-port 8050 --every-minutes 1   # grafiek in de browser op http://localhost:8050/
python battery_revenue.py --every-minutes 15 --days 3   # standaard run in Docker op de Pi (schrijft naar InfluxDB)
ssh pi@raspberrypi400.local 'cd ~/battery-revenue && git pull && docker compose up --build -d && docker logs -f --tail 50 battery-revenue'   # Pi bijwerken vanaf de Mac (Ctrl+C stopt alleen het meekijken)