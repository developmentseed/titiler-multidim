ARG PYTHON_VERSION=3.12

# Stage 1: Build dependencies

# Download the OpenTelemetry Layer with AppSignals Support:
FROM public.ecr.aws/lambda/python:${PYTHON_VERSION} as otel-builder
RUN dnf install -y unzip wget && \
  wget https://github.com/aws-observability/aws-otel-python-instrumentation/releases/latest/download/layer.zip -O /tmp/layer.zip && \
  mkdir -p /opt-builder && \
  unzip /tmp/layer.zip -d /opt-builder/

# install python dependencies
FROM public.ecr.aws/lambda/python:${PYTHON_VERSION} AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN dnf install -y gcc-c++ && dnf clean all

WORKDIR /build

COPY README.md uv.lock .python-version pyproject.toml ./
COPY src/titiler/ ./src/titiler/

COPY infrastructure/aws/lambda/otel-requirements.txt ./

# Install dependencies to temporary directory with Lambda-specific optimizations
# Install OTEL packages to /opt/python (where otel-instrument wrapper expects them)
RUN uv pip install \
  --target /opt-python \
  --no-cache-dir \
  --disable-pip-version-check \
  -r otel-requirements.txt

# Install app packages (excluding OTEL packages since they're already installed)
RUN uv export --locked --no-editable --no-dev --extra lambda --format requirements.txt -o requirements.txt && \
  uv pip install \
  --compile-bytecode \
  --no-binary pydantic \
  --target /deps \
  --no-cache-dir \
  --disable-pip-version-check \
  -r requirements.txt


# Aggressive cleanup to minimize size and optimize for Lambda container
# Clean up app dependencies in /deps
WORKDIR /deps
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN <<EOF
# Convert .pyc files and remove source .py files for faster cold starts
find . -type f -name '*.pyc' | while read -r f; do n="$(echo "$f" | sed 's/__pycache__\///' | sed 's/.cpython-[0-9]*//')"; cp "$f" "$n"; done
find . -type d -a -name '__pycache__' -print0 | xargs -0 rm -rf
find . -type f -a -name '*.py' -print0 | xargs -0 rm -f
# Remove unnecessary files for Lambda runtime
find . -type d -a -name 'tests' -print0 | xargs -0 rm -rf
find . -type d -a -name 'test' -print0 | xargs -0 rm -rf
rm -rf numpy/doc/ bin/ geos_license Misc/
# Remove unnecessary locale and documentation files
find . -name '*.mo' -delete
find . -name '*.po' -delete
find . -name 'LICENSE*' -delete
find . -name 'README*' -delete
find . -name '*.md' -delete
# Strip debug symbols from shared libraries (preserve numpy.libs)
find . -type f -name '*.so*' -not -path "*/numpy.libs/*" -exec strip --strip-unneeded {} \; 2>/dev/null || true
# Create a manifest file for debugging
du -sh . > /tmp/package_size.txt
EOF

# Clean up OTEL dependencies in /opt-python (lighter cleanup to preserve CLI tools)
WORKDIR /opt-python
RUN <<EOF
# Remove test files and docs
find . -type d -a -name 'tests' -print0 | xargs -0 rm -rf
find . -type d -a -name 'test' -print0 | xargs -0 rm -rf
# Keep bin/ directory for opentelemetry-instrument CLI
find . -name '*.mo' -delete
find . -name '*.po' -delete
find . -name 'LICENSE*' -delete
find . -name 'README*' -delete
find . -name '*.md' -delete
EOF

# Stage 3: Final runtime stage - minimal Lambda image optimized for container runtime
FROM public.ecr.aws/lambda/python:${PYTHON_VERSION}

# Set Lambda-specific environment variables for optimal performance
# PYTHONPATH includes both /opt/python (OTEL packages) and LAMBDA_RUNTIME_DIR (app packages)
ENV PYTHONUNBUFFERED=1 \
  PYTHONDONTWRITEBYTECODE=1

# Copy OTEL code
COPY --from=otel-builder /opt-builder/ /opt/
COPY infrastructure/aws/lambda/collector-config.yaml /opt/collector-config/config.yaml

# Copy dependencies from builder stage
# OTEL packages to /opt/python (includes opentelemetry-instrument CLI)
COPY --from=builder /opt-python /opt/python

# App packages to LAMBDA_RUNTIME_DIR
COPY --from=builder /deps ${LAMBDA_RUNTIME_DIR}/

# Copy required system library
COPY --from=builder /usr/lib64/libexpat.so.1 ${LAMBDA_RUNTIME_DIR}/

# Copy application handler and OpenTelemetry configuration
COPY infrastructure/aws/lambda/handler.py ${LAMBDA_RUNTIME_DIR}/

# Ensure handler is executable and optimize permissions
RUN <<EOF
chmod 644 "${LAMBDA_RUNTIME_DIR}"/handler.py
chmod -R 755 /opt/
# Pre-compile the handler for faster cold starts
python -c "import py_compile; py_compile.compile('${LAMBDA_RUNTIME_DIR}/handler.py', doraise=True)"
# Create cache directories with proper permissions
mkdir -p /tmp/.cache && chmod 777 /tmp/.cache
EOF

CMD ["handler.lambda_handler"]
