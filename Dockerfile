FROM python:3.12-slim

# WeasyPrint runtime deps (Cairo, Pango, GDK-Pixbuf, fonts).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libcairo2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libffi8 \
        fonts-dejavu-core \
        shared-mime-info \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/
# Historical GCP Console cost-table CSVs (months before BigQuery export existed).
# The gcp collector reads these for those months (config gcp.source=auto|csv).
COPY gcp-billing/ ./gcp-billing/

# Secrets/config are injected at runtime (Azure Key Vault via Workload Identity,
# or docker-compose env_file / K8s for local) — never baked into the image.

# APP_ENV is intentionally NOT defaulted to "production" here: that flag turns on
# HTTPS-only session cookies and is set explicitly by the production runtime
# (docker-compose.prod.yml / k8s) so the plain-http dev stack keeps working.
ENV CONFIG_PATH=/app/config/config.yaml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEB_CONCURRENCY=2 \
    FORWARDED_ALLOW_IPS="127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

# Default: run the web app with multiple workers. For the scheduled/ad-hoc CLI,
# override the command:  python -m src.main --month 2026-05
CMD ["sh", "-c", "uvicorn src.web.app:app --host 0.0.0.0 --port 8080 --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS}"]
