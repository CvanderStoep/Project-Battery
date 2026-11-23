docker logs -f --tail 50 battery-revenue
git pull
scp .env pi@raspberrypi400.local:~/Project-Battery/.env
docker compose up --build -d
python battery_revenue.py --days 3 --every-minutes 1 --show --dry-run