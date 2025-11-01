"""Temporary test to check that the guess_opener function fails when trying to open an S3 path with the current implementation that relies on os.path.isdir and os.path.exists, which do not work with S3 paths."""

import os
import pytest
import docker
import time
from minio import Minio
from titiler.multidim.reader import guess_opener

# Minio configuration
MINIO_HOST = "127.0.0.1"
MINIO_PORT = "9000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"
MINIO_BUCKET_NAME = "test-bucket"


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
def test_guess_opener_s3_icechunk_fails_without_fix(s3_icechunk_path):
    """
    Test that guess_opener fails for S3 icechunk paths with the current implementation
    due to os.path.isdir and os.path.exists not working on S3.
    """
    # This test expects the current implementation to fail because os.path functions
    # are used on an S3 path.
    os.environ["AWS_ACCESS_KEY_ID"] = MINIO_ACCESS_KEY
    os.environ["AWS_SECRET_ACCESS_KEY"] = MINIO_SECRET_KEY
    os.environ["AWS_DEFAULT_REGION"] = "us-west-2"
    os.environ["AWS_ENDPOINT_URL"] = f"http://{MINIO_HOST}:{MINIO_PORT}"

    with pytest.raises(Exception) as excinfo:
        guess_opener(s3_icechunk_path)

    # The exact error might vary depending on how os.path handles non-local paths,
    # but it should not successfully open the icechunk.
    # We expect it to fall back to xarray_open_dataset which will then fail
    # because it's not a direct zarr store.
    assert "No group found in store" in str(excinfo.value)
