# Dockerfile for Class Action Daily.
#
# Used for two deployment modes:
#   1. Polling worker (v1):    docker run ... python -m server.worker all
#   2. Webhook server (later): docker run ... (uses default CMD)
#
# The CMD below is for mode 2. For mode 1 (scheduled batch), override the
# command at `fly machine run` or in your cron entry.

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt

COPY server/    /app/server/
COPY migrations/ /app/migrations/
COPY law360_feeds.json /app/law360_feeds.json

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Default CMD is for the webhook server (mode 2). For mode 1 (polling),
# pass `python -m server.worker all` as the command when starting the
# container.
EXPOSE 8080
CMD ["uvicorn", "server.webhook_server:app", "--host", "0.0.0.0", "--port", "8080"]
