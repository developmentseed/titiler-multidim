import os
import pytest
import docker
import time
from minio import Minio
from unittest.mock import patch
from titiler.multidim.reader import guess_opener, identify_storage_backend
import xarray as xr
from pathlib import Path

# Minio configuration
MINIO_HOST = "127.0.0.1"
MINIO_PORT = "9000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"
MINIO_BUCKET_NAME = "test-bucket"

FIXTURE_EXPECTATIONS = {
    "icechunk_native": {
        "format": "icechunk",
        "path": "./tests/fixtures/icechunk_native",
    },
    "icechunk_virtual_accessible": {
        "format": "icechunk",
        "path": "./tests/fixtures/icechunk_virtual_accessible",
    },
    "zarr_store_v2.zarr": {
        "format": "zarr",
        "path": "./tests/fixtures/zarr_store_v2.zarr",
    },
    "zarr_store_v3.zarr": {
        "format": "zarr",
        "path": "./tests/fixtures/zarr_store_v3.zarr",
    },
    "unconsolidated.zarr": {
        "format": "zarr",
        "path": "./tests/fixtures/unconsolidated.zarr",
    },
    "pyramid.zarr": {"format": "zarr", "path": "./tests/fixtures/pyramid.zarr"},
    "testfile.nc": {"format": "h5netcdf", "path": "./tests/fixtures/testfile.nc"},
}


@pytest.fixture(scope="session")
def minio_server():
    """Starts a Minio server in a Docker container for testing."""
    try:
        client = docker.from_env()
    except docker.errors.DockerException as e:
        raise RuntimeError(
            "Docker is not running or not configured correctly. Please ensure Docker is running to execute S3 tests."
        ) from e
    container_name = "test-minio-server"

    # Stop and remove any existing container with the same name
    try:
        existing_container = client.containers.get(container_name)
        existing_container.stop()
        existing_container.remove()
    except docker.errors.NotFound:
        pass

    container = client.containers.run(
        "minio/minio",
        "server /data --console-address :9001",
        name=container_name,
        ports={f"{MINIO_PORT}/tcp": MINIO_PORT},
        environment={
            "MINIO_ROOT_USER": MINIO_ACCESS_KEY,
            "MINIO_ROOT_PASSWORD": MINIO_SECRET_KEY,
        },
        detach=True,
        remove=True,
    )
    print(f"Minio container '{container_name}' started.")

    # Wait for Minio to be ready
    minio_client = Minio(
        f"{MINIO_HOST}:{MINIO_PORT}",
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=False,
    )
    for i in range(30):
        try:
            minio_client.bucket_exists(MINIO_BUCKET_NAME)
            print("Minio server is ready.")
            break
        except Exception as e:
            print(f"Waiting for Minio... Attempt {i + 1}/30. Error: {e}")
            time.sleep(2)
    else:
        container.stop()
        container.remove()
        raise RuntimeError("Minio server did not start in time.")

    yield minio_client

    print(f"Stopping Minio container '{container_name}'.")
    container.stop()
    print(f"Minio container '{container_name}' stopped and removed.")


@pytest.fixture(scope="session", autouse=True)
def configure_minio_env():
    """Configure AWS-like environment variables for MinIO for all tests."""
    os.environ.update(
        {
            "AWS_ACCESS_KEY_ID": MINIO_ACCESS_KEY,
            "AWS_SECRET_ACCESS_KEY": MINIO_SECRET_KEY,
            "AWS_DEFAULT_REGION": "us-west-2",
            "AWS_ENDPOINT_URL": f"http://{MINIO_HOST}:{MINIO_PORT}",
        }
    )
    yield
    # Optional teardown (cleanup) if needed


@pytest.fixture(scope="session")
def s3_fixtures(minio_server):
    """Upload all local test fixtures to Minio once, return dict of S3 paths."""
    minio_client = minio_server
    bucket_name = "test-bucket"
    base_prefix = "test_fixtures"

    if not minio_client.bucket_exists(bucket_name):
        minio_client.make_bucket(bucket_name)

    local_fixtures = {
        name: fixture["path"] for name, fixture in FIXTURE_EXPECTATIONS.items()
    }

    s3_paths = {}

    for name, local_path in local_fixtures.items():
        prefix = f"{base_prefix}/{Path(local_path).name}/"
        if Path(local_path).is_dir():
            for root, _, files in os.walk(local_path):
                for f in files:
                    src = Path(root) / f
                    rel = src.relative_to(local_path)
                    object_name = os.path.join(prefix, rel.as_posix())
                    minio_client.fput_object(bucket_name, object_name, str(src))
        else:
            object_name = f"{prefix}{Path(local_path).name}"
            minio_client.fput_object(bucket_name, object_name, str(local_path))
        s3_paths[name] = f"s3://{bucket_name}/{prefix}"

    yield s3_paths

    # Cleanup after all tests
    for obj in minio_client.list_objects(
        bucket_name, prefix=base_prefix, recursive=True
    ):
        minio_client.remove_object(bucket_name, obj.object_name)


@pytest.fixture(scope="session")
def upload_to_minio(minio_server):
    """
    Upload any local fixture (file or folder) to Minio, and return its S3 path.
    """
    minio_client = minio_server
    bucket_name = "test-bucket"
    base_prefix = "test_fixtures"

    # Ensure bucket exists
    if not minio_client.bucket_exists(bucket_name):
        minio_client.make_bucket(bucket_name)

    def _upload(local_path):
        local_path = Path(local_path)
        prefix = f"{base_prefix}/{local_path.name}/"

        if local_path.is_dir():
            # upload all files in directory
            for root, _, files in os.walk(local_path):
                for file in files:
                    local_file = Path(root) / file
                    rel_path = local_file.relative_to(local_path)
                    object_name = os.path.join(prefix, rel_path.as_posix())
                    minio_client.fput_object(bucket_name, object_name, str(local_file))
        else:
            # single file
            object_name = f"{prefix}{local_path.name}"
            minio_client.fput_object(bucket_name, object_name, str(local_path))

        return f"s3://{bucket_name}/{prefix}"

    yield _upload

    # optional cleanup after all tests
    objects = minio_client.list_objects(bucket_name, prefix=base_prefix, recursive=True)
    for obj in objects:
        minio_client.remove_object(bucket_name, obj.object_name)


@pytest.fixture(scope="session")
def s3_icechunk_path(minio_server):
    """Generates an icechunk dataset and uploads it to Minio."""
    minio_client = minio_server
    bucket_name = MINIO_BUCKET_NAME
    object_prefix = "test_icechunk/"

    if not minio_client.bucket_exists(bucket_name):
        minio_client.make_bucket(bucket_name)
        print(f"Bucket '{bucket_name}' created.")

    # Upload contents of the existing local icechunk directory to Minio
    local_icechunk_path = "tests/fixtures/icechunk_native"
    for root, _, files in os.walk(local_icechunk_path):
        for file in files:
            local_file_path = os.path.join(root, file)
            relative_path = os.path.relpath(local_file_path, local_icechunk_path)
            s3_object_name = os.path.join(object_prefix, relative_path)
            minio_client.fput_object(bucket_name, s3_object_name, local_file_path)
            print(f"Uploaded {local_file_path} to s3://{bucket_name}/{s3_object_name}")

    s3_path = f"s3://{bucket_name}/{object_prefix}"
    yield s3_path

    # Clean up: remove objects and bucket
    objects_to_delete = minio_client.list_objects(
        bucket_name, prefix=object_prefix, recursive=True
    )
    for obj in objects_to_delete:
        minio_client.remove_object(bucket_name, obj.object_name)
        print(f"Removed s3://{bucket_name}/{obj.object_name}")
    minio_client.remove_bucket(bucket_name)
    print(f"Bucket '{bucket_name}' removed.")


@pytest.mark.s3_test
@patch("titiler.multidim.reader.opener_icechunk")
def test_guess_opener_s3_icechunk_calls_opener_icechunk(
    mock_opener_icechunk, s3_icechunk_path
):
    """
    Test that guess_opener calls opener_icechunk for S3 icechunk paths.
    """

    guess_opener(s3_icechunk_path)
    mock_opener_icechunk.assert_called_once()


@pytest.mark.s3_test
def test_guess_opener_s3_icechunk_fixed(s3_icechunk_path):
    """
    Test that guess_opener successfully opens an S3 icechunk dataset after the fix.
    """

    ds = guess_opener(s3_icechunk_path)
    assert isinstance(ds, xr.Dataset)


class TestIdentifyStorageBackend:
    @pytest.mark.parametrize(
        "expected_format, path",
        [
            (fixture["format"], fixture["path"])
            for fixture_name, fixture in FIXTURE_EXPECTATIONS.items()
        ],
    )
    def test_local(self, expected_format, path):
        format = identify_storage_backend(path)
        print(f"Identified format for {path}: {format}")
        assert format == expected_format

    @pytest.mark.parametrize(
        "expected_format, fixture_name",
        [
            (fixture["format"], fixture_name)
            for fixture_name, fixture in FIXTURE_EXPECTATIONS.items()
        ],
    )
    def test_s3(self, expected_format, fixture_name, s3_fixtures):
        s3_path = s3_fixtures[fixture_name]
        format = identify_storage_backend(s3_path)
        assert format == expected_format
