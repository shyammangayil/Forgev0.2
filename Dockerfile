# FORGE Dockerfile — Multi-stage build
# Stage 1: builder
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt pyproject.toml README.md ./
COPY forge/ ./forge/

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir -e .

# Stage 2: runtime (non-root)
FROM python:3.12-slim AS runtime

# Security: run as non-root
RUN groupadd -r forge && useradd -r -g forge -d /home/forge -m forge

WORKDIR /home/forge/app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/forge /usr/local/bin/forge
COPY --from=builder /usr/local/bin/forge-server /usr/local/bin/forge-server

COPY --chown=forge:forge forge/ ./forge/
COPY --chown=forge:forge .env.example ./.env.example

# Create runtime directories
RUN mkdir -p TASKS workspace logs && chown -R forge:forge /home/forge/app

USER forge

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/status')" || exit 1

EXPOSE 8080

ENV FORGE_API_HOST=0.0.0.0
ENV FORGE_API_PORT=8080
ENV LOG_FORMAT=json
ENV FORGE_TASK_DIR=/home/forge/app/TASKS
ENV FORGE_LOG_DIR=/home/forge/app/logs

CMD ["forge-server"]
