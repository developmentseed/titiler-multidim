# syntax=docker/dockerfile:1.7

# Pin both to versions you have verified exist (and ideally to digests:
#   FROM image:tag@sha256:...) for fully reproducible builds.
ARG GDAL_TAG=ubuntu-small-3.11.4
ARG UV_VERSION=0.12.20

# ============================================================
# uv binary stage
# ============================================================
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv


# ============================================================
# Builder
# ============================================================
FROM ghcr.io/osgeo/gdal:${GDAL_TAG} AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-dev \
        python3-venv \
        build-essential \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first (cached unless lockfile/pyproject change)
COPY pyproject.toml uv.lock README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
        --frozen \
        --no-dev \
        --extra server \
        --no-install-project

# Application
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
        --frozen \
        --no-dev \
        --extra server \
        --no-editable


# ============================================================
# Runtime
# ============================================================
FROM ghcr.io/osgeo/gdal:${GDAL_TAG} AS runtime

LABEL org.opencontainers.image.title="titiler-multidim" \
      org.opencontainers.image.source="https://github.com/developmentseed/titiler-multidim"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd \
        --uid 10001 \
        --no-create-home \
        --shell /usr/sbin/nologin \
        titiler

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    WEB_CONCURRENCY=2 \
    FORWARDED_ALLOW_IPS='*' \
    GDAL_CACHEMAX=200 \
    VSI_CACHE=TRUE \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES \
    GDAL_HTTP_MULTIPLEX=YES \
    GDAL_HTTP_VERSION=2

USER 10001

WORKDIR /tmp

EXPOSE 8000

# Uses the venv's Python, so curl isn't needed in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "-c", "import os,urllib.request as u; u.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/healthz\", timeout=4)"]

ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["sh", "-c", "exec uvicorn titiler.multidim.main:app --host ${HOST} --port ${PORT} --workers ${WEB_CONCURRENCY} --proxy-headers --timeout-keep-alive 75"]
