FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY battery_revenue.py .

# Geen root in de container
RUN useradd --create-home appuser
USER appuser

CMD ["python", "battery_revenue.py"]
