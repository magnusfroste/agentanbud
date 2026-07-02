FROM python:3.12-slim

# cron for scheduled scraper runs
RUN apt-get update && apt-get install -y --no-install-recommends \
        cron ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/
COPY scraper/ /app/scraper/
COPY web/ /app/web/

# Cron entry — runs the orchestrator once daily.
# Time is interpreted in the container's local timezone.
ENV CRON_SCHEDULE="0 6 * * *"
RUN printf 'PATH=/usr/local/bin:/usr/bin:/bin\n%s root cd /app && python -m scraper.orchestrator >> /var/log/agentanbud.log 2>&1\n' \
        "$CRON_SCHEDULE" > /etc/cron.d/agentanbud && \
    chmod 0644 /etc/cron.d/agentanbud && \
    touch /var/log/agentanbud.log

# /data is the persistent volume — SQLite + sync logs live here
RUN mkdir -p /data

EXPOSE 8080

# Start cron in background, then FastAPI in foreground.
CMD ["sh", "-c", "cron && uvicorn app.main:get_app --factory --host 0.0.0.0 --port 8080"]
