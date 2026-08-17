FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.server.txt .
RUN pip install --no-cache-dir -r requirements.server.txt

# Optional in-Docker live transcription (browser mic → faster-whisper, CPU
# int8 — no PyTorch/whisperx; post-pass degrades to a lite pass without
# diarization). Enable with: INSTALL_TRANSCRIBE=1 docker compose build server
ARG INSTALL_TRANSCRIBE=0
RUN if [ "$INSTALL_TRANSCRIBE" = "1" ]; then \
      pip install --no-cache-dir faster-whisper>=1.0 ; \
    fi

COPY . .

RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
