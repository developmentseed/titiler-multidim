# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.12
ARG GDAL_VERSION=3.12.1

# ── Stage 0: capture the unmodified Lambda runtime libs ───────────────────────
# Used to pin libcurl to the exact version present in the Lambda execution
# environment, so GDAL is built against — and bundled with — that same version.
FROM --platform=linux/amd64 public.ecr.aws/lambda/python:${PYTHON_VERSION} AS lambda-base

# ── Stage 1: build minimal GDAL from source ───────────────────────────────────
FROM --platform=linux/amd64 public.ecr.aws/amazonlinux/amazonlinux:2023 AS gdal-builder

ARG GDAL_VERSION

RUN dnf install -y \
    gcc gcc-c++ make cmake \
    tar gzip \
    proj-devel \
    geos-devel \
    sqlite-devel \
    libcurl-devel \
    libjpeg-turbo-devel \
    libwebp-devel \
    libzstd-devel \
    openjpeg2-devel \
    libtiff-devel \
    && dnf clean all

# Pin libcurl to the Lambda runtime version. dnf may install a newer libcurl
# above; overwriting the .so files here ensures GDAL links against the same
# version that will be present in the Lambda execution environment.
COPY --from=lambda-base /usr/lib64/libcurl.so* /usr/lib64/

RUN curl -fsSL "https://github.com/OSGeo/gdal/releases/download/v${GDAL_VERSION}/gdal-${GDAL_VERSION}.tar.gz" \
    | tar -xz -C /tmp

RUN cmake \
    -S /tmp/gdal-${GDAL_VERSION} \
    -B /tmp/gdal-build \
    -DCMAKE_INSTALL_PREFIX=/opt/gdal \
    -DCMAKE_BUILD_TYPE=Release \
    -DGDAL_BUILD_OPTIONAL_DRIVERS=OFF \
    -DOGR_BUILD_OPTIONAL_DRIVERS=OFF \
    -DGDAL_ENABLE_DRIVER_GTiff=ON \
    -DGDAL_ENABLE_DRIVER_MEM=ON \
    -DGDAL_ENABLE_DRIVER_VRT=ON \
    -DGDAL_ENABLE_DRIVER_PNG=ON \
    -DGDAL_ENABLE_DRIVER_JPEG=ON \
    -DGDAL_USE_CURL=ON \
    -DGDAL_USE_PROJ=ON \
    -DGDAL_USE_GEOS=ON \
    -DGDAL_USE_TIFF_INTERNAL=ON \
    -DGDAL_USE_GEOTIFF_INTERNAL=ON \
    -DGDAL_USE_JPEG=ON \
    -DGDAL_USE_WEBP=ON \
    -DGDAL_USE_ZSTD=ON \
    -DGDAL_USE_OPENJPEG=ON \
    -DGDAL_USE_DEFLATE=OFF \
    -DGDAL_USE_HDF5=OFF \
    -DGDAL_USE_NETCDF=OFF \
    -DGDAL_USE_BLOSC=OFF \
    -DBUILD_TESTING=OFF \
    -DBUILD_APPS=OFF \
    && cmake --build /tmp/gdal-build -j$(nproc) \
    && cmake --install /tmp/gdal-build


# ── Stage 2: build Python deps in full AL2023 image ───────────────────────────
FROM --platform=linux/amd64 public.ecr.aws/amazonlinux/amazonlinux:2023 AS python-builder

ARG PYTHON_VERSION

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY --from=gdal-builder /opt/gdal /opt/gdal

RUN dnf install -y \
    python${PYTHON_VERSION} python${PYTHON_VERSION}-devel python${PYTHON_VERSION}-pip \
    gcc gcc-c++ gcc-gfortran \
    patchelf \
    proj proj-devel \
    geos geos-devel \
    sqlite sqlite-devel \
    libcurl-devel \
    libjpeg-turbo libjpeg-turbo-devel \
    libwebp libwebp-devel \
    libzstd libzstd-devel \
    openjpeg2 openjpeg2-devel \
    libtiff libtiff-devel \
    libpng libpng-devel \
    && dnf clean all

WORKDIR /tmp

# ── Step 1: install third-party deps (cached until lockfile changes) ──────────
# --no-emit-project excludes the titiler-multidim package itself so that changes
# to the project source don't bust this layer.
COPY uv.lock .python-version pyproject.toml ./

# Build pydantic, rasterio, numpy, pillow, pyproj, and shapely from source.
# GDAL_CONFIG points rasterio's build at our minimal GDAL.
# NPY_BLAS/LAPACK_ORDER="" disables BLAS detection: numpy uses its bundled lapack_lite
# instead of OpenBLAS, saving ~42MB at the cost of linalg performance (acceptable for Lambda).
# h5py stays on its manylinux wheel because Amazon Linux 2023 does not ship hdf5-devel in the
# default repos exposed to these container images.
RUN uv export --locked --no-editable --no-dev --extra lambda \
    --no-emit-project --format requirements.txt -o requirements.txt \
    && GDAL_CONFIG=/opt/gdal/bin/gdal-config \
       NPY_BLAS_ORDER="" \
       NPY_LAPACK_ORDER="" \
       uv pip install \
           --python /usr/bin/python${PYTHON_VERSION} \
           --compile-bytecode \
           --no-binary pydantic,rasterio,numpy,pillow,pyproj,shapely \
           --target /asset \
           -r requirements.txt

# ── Step 2: install project source (busted only by source changes) ────────────
# --no-deps skips re-installing deps already present in /asset from Step 1.
COPY README.md uv.lock .python-version pyproject.toml ./
COPY src/titiler/ ./src/titiler/

RUN uv pip install \
    --python /usr/bin/python${PYTHON_VERSION} \
    --no-deps \
    --compile-bytecode \
    --target /asset \
    .

# Bundle PROJ data directory so pyproj can find CRS definitions at runtime.
# Source-built pyproj doesn't include data; it searches pyproj/proj_dir/share/proj/
# which mirrors the layout of the manylinux wheel.
# Only proj.db (CRS definitions) and proj.ini are needed — the hundreds of datum
# grid .tif files are for high-precision geodetic transforms we don't use.
RUN <<'EOF'
set -e
mkdir -p /asset/pyproj/proj_dir/share/proj
cp /usr/share/proj/proj.db /usr/share/proj/proj.ini /asset/pyproj/proj_dir/share/proj/
EOF

# Bundle libcurl from the unmodified Lambda runtime image so the version bundled
# here matches exactly what GDAL was compiled against in the gdal-builder stage.
COPY --from=lambda-base /usr/lib64/libcurl.so.4 /asset/libcurl.so.4

# Assemble all native library dependencies and fix their load paths:
# - Extension RPATHs → $ORIGIN/../shared.libs (rasterio, PIL, pyproj, shapely)
# - shared.libs: libgdal + all non-Lambda-runtime deps, RPATH → $ORIGIN so
#   libgdal finds libproj/libgeos/etc. within the directory at runtime
# - h5py keeps its bundled manylinux HDF5 libs in h5py.libs
# - libexpat: required by libgdal at runtime
# - libgfortran/libquadmath: Fortran runtime for numpy's bundled lapack_lite
#   (no OpenBLAS needed — numpy uses lapack_lite instead, saving ~42MB)
RUN <<'EOF'
set -e
for d in /asset/rasterio /asset/PIL /asset/pyproj /asset/shapely; do
    if [ -d "$d" ]; then
        find "$d" -name '*.so*' -not -type l | xargs -r -I{} patchelf --set-rpath '$ORIGIN/../shared.libs' {}
    fi
done

mkdir -p /asset/shared.libs
# Copy exactly one file per runtime SONAME to avoid shipping duplicate .so symlink
# chains. Lambda zip staging can dereference symlinks, which turns the symlink
# chain into tens of megabytes of duplicated shared libraries and pushes the
# unzipped asset over Lambda's 250 MiB limit.
cp -L /opt/gdal/lib64/libgdal.so.38 /asset/shared.libs/libgdal.so.38
cp -L /usr/lib64/libproj.so.25 /asset/shared.libs/libproj.so.25
cp -L /usr/lib64/libgeos.so.3.13.0 /asset/shared.libs/libgeos.so.3.13.0
cp -L /usr/lib64/libgeos_c.so.1 /asset/shared.libs/libgeos_c.so.1
cp -L /usr/lib64/libsqlite3.so.0 /asset/shared.libs/libsqlite3.so.0
cp -L /usr/lib64/libtiff.so.5 /asset/shared.libs/libtiff.so.5
cp -L /usr/lib64/libjpeg.so.62 /asset/shared.libs/libjpeg.so.62
cp -L /usr/lib64/libwebp.so.7 /asset/shared.libs/libwebp.so.7
cp -L /usr/lib64/libzstd.so.1 /asset/shared.libs/libzstd.so.1
cp -L /usr/lib64/libopenjp2.so.7 /asset/shared.libs/libopenjp2.so.7
cp -L /usr/lib64/libpng16.so.16 /asset/shared.libs/libpng16.so.16
cp -L /usr/lib64/libjbig.so.2.1 /asset/shared.libs/libjbig.so.2.1
find /asset/shared.libs -name '*.so*' -not -type l | xargs -I{} patchelf --set-rpath '$ORIGIN' {}

cp /usr/lib64/libexpat.so.1 /asset/
find /usr/lib64 -maxdepth 1 \( -name 'libgfortran.so.5' -o -name 'libquadmath.so.0' \) \
    -exec cp -L {} /asset/ \;
EOF

# Reduce package size: promote .pyc files alongside their .py sources, then
# remove source files, caches, test directories, docs, and debug symbols.
RUN <<'EOF'
set -e
cd /asset
find . -type f -name '*.pyc' | while read f; do
    n=$(echo $f | sed 's/__pycache__\///' | sed 's/.cpython-[0-9]*//')
    cp $f $n
done
find . -type d -name '__pycache__' -print0 | xargs -0 rm -rf
find . -type f -name '*.py'        -print0 | xargs -0 rm -f
find . -type d -name 'tests'       -print0 | xargs -0 rm -rf
rm -rdf numpy/doc/ bin geos_license Misc boto3* botocore*
find . -type f -name '*.so*' -exec strip --strip-unneeded {} \;
EOF

# ── Stage 3: assemble final Lambda image ──────────────────────────────────────
FROM --platform=linux/amd64 public.ecr.aws/lambda/python:${PYTHON_VERSION}

# Bundle GDAL data files (schemas, SRS definitions) needed by the GDAL drivers
COPY --from=python-builder /asset /asset
COPY --from=gdal-builder /opt/gdal/share/gdal /asset/gdal_data

COPY infrastructure/aws/lambda/handler.py /asset/handler.py

CMD ["echo", "hello world"]
