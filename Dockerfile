FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data /app/backend /app/frontend \
    && chown -R app:app /data /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app backend ./backend
COPY --chown=app:app frontend ./frontend

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD-SHELL python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT','8000') + '/health', timeout=3)" || exit 1
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port $(printenv PORT || printf 8000) --no-access-log"]
