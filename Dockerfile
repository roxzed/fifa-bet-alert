# ============================================================
# Multi-stage Dockerfile for FIFA Bet Alert System
# Production-ready: healthcheck, watchdog, non-root user
# ============================================================

# ── Stage 1: Builder ─────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime ─────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL maintainer="Plini"
LABEL description="FIFA Bet Alert - eSoccer betting signal system"

WORKDIR /app

# Copy pre-built Python packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /bin/false appuser \
    && mkdir -p /app/data /app/logs \
    && chown -R appuser:appuser /app

# Copy application code
COPY --chown=appuser:appuser . .

USER appuser

# Healthcheck: verifica se o main loop esta respondendo
# Checa se o arquivo heartbeat foi atualizado nos ultimos 5 minutos
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import os, time; h='/app/data/heartbeat'; assert os.path.exists(h) and time.time() - os.path.getmtime(h) < 300, 'stale heartbeat'" || exit 1

CMD ["python", "-m", "src.main"]
