# The ChatReceipt API, with the Chromium it uses to print PDF and PNG records.
# Built by Render from render.yaml (see docs/deployment.md); also runs anywhere Docker does.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    BROWSER_CHANNEL=""

WORKDIR /app

COPY requirements.txt .
# Chromium and the system libraries it needs come from Playwright, at the version in requirements.txt.
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY scripts ./scripts

# Run as an ordinary user. var/ holds the engine's local storage, which is temporary on free hosts.
RUN useradd --create-home --uid 10001 chatreceipt \
    && mkdir -p /app/var \
    && chown -R chatreceipt /app/var
USER chatreceipt

EXPOSE 8000
# Hosts such as Render set $PORT. Migrations are not run here: apply them on purpose with
# `python -m app.db.migrate` (docs/deployment.md).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-server-header"]
