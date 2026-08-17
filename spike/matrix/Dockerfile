# Buzzowl Phase 5 spike — Matrix bot image (matrix-nio with E2EE).
# matrix-nio[e2e] pulls python-olm, which builds against the libolm C library
# (deprecated upstream, still the crypto backend nio uses — see the spike report).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libolm-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "matrix-nio[e2e]" aiohttp
WORKDIR /app
COPY federation_bot.py /app/federation_bot.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/federation_bot.py"]
