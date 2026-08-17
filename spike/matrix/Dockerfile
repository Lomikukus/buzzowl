# Buzzowl Phase 5 spike — Matrix bot image (matrix-nio with E2EE).
# matrix-nio >= 0.26 (2026-07) uses the Rust `vodozemac` crypto backend (wheels on
# PyPI) — libolm is deprecated upstream and NOT needed. Measured in the spike:
# nio 0.26.0 + vodozemac 0.10.0, no `olm` module present.
FROM python:3.12-slim
RUN pip install --no-cache-dir "matrix-nio[e2e]>=0.26.0" aiohttp
WORKDIR /app
COPY federation_bot.py /app/federation_bot.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/federation_bot.py"]
