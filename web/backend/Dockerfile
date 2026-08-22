# Remnawave Admin Web Panel - Backend Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (postgresql-client for pg_dump/psql used by backup service)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY web/backend/requirements.txt /app/web/backend/requirements.txt
COPY requirements.txt /app/requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN pip install --no-cache-dir -r /app/web/backend/requirements.txt

# Copy application code
COPY shared/ /app/shared/
COPY web/backend/ /app/web/backend/

# Agellar production plugins are baked into the immutable backend image.
# Keeping them out of a mutable bind mount makes the image tag a complete
# rollback unit. RWA_DEV_PLUGINS_PREFER also keeps an uploaded wheel with the
# same plugin id from silently replacing an audited local implementation.
COPY plugins-src/rwa_incident_hub/ /app/rwa_incident_hub/
COPY plugins-src/rwa_local_block_radar/ /app/rwa_local_block_radar/
COPY plugins-src/rwa_retention_radar/ /app/rwa_retention_radar/
COPY plugins-src/rwa_smart_support/ /app/rwa_smart_support/
COPY plugins-src/rwa_live_flow/ /app/rwa_live_flow/
ENV RWA_DEV_PLUGINS="rwa_smart_support.plugin:manifest,rwa_retention_radar.plugin:manifest,rwa_local_block_radar.plugin:manifest,rwa_incident_hub.plugin:manifest,rwa_live_flow:manifest"
ENV RWA_DEV_PLUGINS_PREFER="true"

# Copy VERSION file from repo (updated at release time)
COPY VERSION /app/VERSION
# Optional: override version at build time via --build-arg
ARG APP_VERSION=""
RUN if [ -n "${APP_VERSION}" ] && [ "${APP_VERSION}" != "unknown" ]; then echo "${APP_VERSION}" > /app/VERSION; fi

# Copy Alembic migrations (auto-run on startup)
COPY alembic/ /app/alembic/
COPY alembic.ini /app/alembic.ini

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /app/logs /app/geoip /app/plugins /app/plugins/.installed

# Expose port
EXPOSE 8081

# Run the application (--no-access-log to avoid duplicate logging, format configured in main.py)
CMD ["python", "-m", "uvicorn", "web.backend.main:app", "--host", "0.0.0.0", "--port", "8081", "--no-access-log", "--log-level", "warning", "--ws", "websockets", "--ws-per-message-deflate", "true"]
