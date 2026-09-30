ARG PYTHON_VERSION=3.12
ARG GDAL_TAG=ubuntu-small-3.11.4
ARG UV_VERSION=0.12.20

# ---------- Builder stage ----------
FROM ghcr.io/osgeo/gdal:${GDAL_TAG} AS builder

ARG PYTHON_VERSION
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-pip \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast resolver/installer used by the project)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
WORKDIR /app

# Copy only dependency-defining files first for better layer caching
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src

# Install the project + its "server" extra (adds uvicorn) into a venv,
# without the heavier dev/deployment/notebooks dependency groups.
RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install ".[server]"

# ---------- Runtime stage ----------
FROM ghcr.io/osgeo/gdal:${GDAL_TAG} AS runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home titiler

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    WEB_CONCURRENCY=4 \
    FORWARDED_ALLOW_IPS='*' \
    GDAL_CACHEMAX=200 \
    VSI_CACHE=TRUE \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES \
    GDAL_HTTP_MULTIPLEX=YES \
    GDAL_HTTP_VERSION=2

USER titiler
WORKDIR /home/titiler

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/healthz" || exit 1

CMD ["sh", "-c", "uvicorn titiler.multidim.main:app --host ${HOST} --port ${PORT}"]