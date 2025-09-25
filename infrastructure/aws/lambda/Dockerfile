ARG PYTHON_VERSION=3.12

# Build stage - includes all build tools and dependencies
FROM public.ecr.aws/lambda/python:${PYTHON_VERSION} AS builder

# Copy uv for faster dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install system dependencies needed for compilation
RUN dnf install -y gcc-c++ && dnf clean all

# Set working directory for build
WORKDIR /build

# Copy dependency files first for better caching
COPY README.md uv.lock .python-version pyproject.toml ./
COPY src/titiler/ ./src/titiler/

# Install dependencies to temporary directory with Lambda-specific optimizations
RUN uv export --locked --no-editable --no-dev --extra lambda --format requirements.txt -o requirements.txt && \
  uv pip install \
  --compile-bytecode \
  --no-binary pydantic \
  --target /deps \
  --no-cache-dir \
  --disable-pip-version-check \
  -r requirements.txt

# Aggressive cleanup to minimize size and optimize for Lambda container
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

# Final runtime stage - minimal Lambda image optimized for container runtime
FROM public.ecr.aws/lambda/python:${PYTHON_VERSION}

# Set Lambda-specific environment variables for optimal performance
ENV PYTHONPATH=${LAMBDA_RUNTIME_DIR} \
  PYTHONUNBUFFERED=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  AWS_LWA_ENABLE_COMPRESSION=true

# Copy only the cleaned dependencies from builder stage
COPY --from=builder /deps ${LAMBDA_RUNTIME_DIR}/

# Copy required system library
COPY --from=builder /usr/lib64/libexpat.so.1 ${LAMBDA_RUNTIME_DIR}/

# Copy package size manifest for debugging
COPY --from=builder /tmp/package_size.txt /tmp/

# Copy application handler
COPY infrastructure/aws/lambda/handler.py ${LAMBDA_RUNTIME_DIR}/

# Ensure handler is executable and optimize permissions
RUN <<EOF
chmod 644 "${LAMBDA_RUNTIME_DIR}"/handler.py
# Pre-compile the handler for faster cold starts
python -c "import py_compile; py_compile.compile('${LAMBDA_RUNTIME_DIR}/handler.py', doraise=True)"
# Create cache directories with proper permissions
mkdir -p /tmp/.cache && chmod 777 /tmp/.cache
EOF

# Set the Lambda handler
CMD ["handler.lambda_handler"]
