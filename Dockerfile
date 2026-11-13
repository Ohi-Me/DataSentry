# DataSentry: one image, one process, console and API on one port.
#
# Multi-stage so the runtime layer carries no build toolchain. The whole
# dependency set is FastAPI's -- the detection and lineage packages add
# nothing, no numpy, no scipy, no sqlglot -- so this stays small and starts
# instantly, which is the same property that makes `py ds.py demo` work from
# a fresh clone.

# --- build ------------------------------------------------------------------
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install -r requirements.txt

# --- seed -------------------------------------------------------------------
# Run the whole loop once at build time and bake a compact snapshot into the
# image. Free tiers (Render free, Cloud Run, HF Spaces) have an ephemeral disk
# and spin down when idle; re-seeding on every cold start would take ~3 min.
# Restoring a snapshot takes milliseconds and never overwrites a live
# database, so a deployment with a persistent disk keeps its own history.
# Build with --build-arg SEED_DEMO=0 to ship an empty control plane instead.
FROM build AS seed
ARG SEED_DEMO=1
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/build:/build/packages" \
    PYTHONUNBUFFERED=1 \
    DS_DATA_DIR=/tmp/ds \
    DS_DB_PATH=/tmp/ds/datasentry.db \
    DS_WAREHOUSE_PATH=/tmp/ds/warehouse.db \
    DS_LLM_PROVIDER=heuristic \
    NO_COLOR=1
COPY . .
RUN mkdir -p /snapshot && \
    if [ "$SEED_DEMO" = "1" ]; then \
      python ds.py demo --quiet --seed 7 && python ds.py snapshot /snapshot; \
    fi

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app:/app/packages" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DS_HOST=0.0.0.0 \
    DS_PORT=8787 \
    DS_DATA_DIR=/data \
    DS_DB_PATH=/data/datasentry.db \
    DS_WAREHOUSE_PATH=/data/warehouse.db \
    DS_LOG_JSON=1 \
    DS_SNAPSHOT_DIR=/opt/datasentry/snapshot

COPY --from=build /opt/venv /opt/venv
COPY --from=seed /snapshot /opt/datasentry/snapshot

WORKDIR /app
COPY . .

# Not root. The process reads code and writes one directory; it has no reason
# to be able to modify its own source.
RUN useradd --system --create-home --uid 10001 datasentry && \
    mkdir -p /data && \
    chown -R datasentry:datasentry /data /app
USER datasentry

# Declared so `docker run` without -v still warns rather than silently losing
# the control plane on the next `docker rm`.
VOLUME ["/data"]

EXPOSE 8787

# Liveness only -- it does not touch the database, so a health check every
# few seconds costs nothing. /readyz is the one that knows about the data.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT',os.environ.get('DS_PORT','8787'))+'/healthz',timeout=4)"

# `serve` rather than uvicorn directly: it reads .env, resolves the provider,
# prints where things are, and refuses a port that is already taken.
CMD ["python", "ds.py", "serve"]
