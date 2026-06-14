FROM python:3.12-slim-bookworm

# Chromium + Xvfb are needed only for domain discovery (search engines block
# plain HTTP). undetected-chromedriver drives the system Chromium.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium chromium-driver xvfb \
        fonts-liberation ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    DATA_DIR=/app/data

VOLUME /app/data
EXPOSE 8585

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD curl -fs "http://localhost:${PORT:-8585}/api/health" || exit 1

ENTRYPOINT ["./entry