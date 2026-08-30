FROM python:3.11-slim

WORKDIR /app

# Deterministic log ordering under docker compose (no stdout buffering delay).
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.server.txt .
RUN pip install --no-cache-dir -r requirements.server.txt

COPY . .

RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
