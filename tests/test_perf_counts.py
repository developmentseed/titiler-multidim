"""Pin what each request type costs in opens, storage requests and boto3 sessions.

These are `main`'s numbers, not targets. A PR that changes one updates the
assertion, and that diff is the PR's request-count report. The comments say
which performance-roadmap issue is expected to lower each number.
"""

import shutil
import socket

import pytest
from helpers import count_boto3_sessions, count_opens, serve_counting

NATIVE = {
    "url": "tests/fixtures/icechunk_native",
    "variable": "CDD0",
    "decode_times": False,
}
TIME = "2023-08-01T00:00:00"


@pytest.mark.parametrize(
    ("path", "params", "repository", "dataset", "metadata", "chunks"),
    [
        # Issue 5 keeps the dataset open across requests (-> 0, metadata -> 1 branch-ref check);
        # Issue 8 serves repeated chunks from memory (chunks -> 0 on a repeat).
        ("/tiles/WebMercatorQuad/2/1/1.png", {"sel": "time=0"}, 1, 1, 9, 9),
        ("/point/-95,35", {"sel": "time=0"}, 1, 1, 9, 1),
        ("/info", {"sel": "time=0"}, 1, 1, 8, 0),
        ("/info", {"show_times": True}, 1, 1, 8, 0),
    ],
    ids=["tile", "point", "info", "info_show_times"],
)
def test_opens_and_storage_requests(
    app,
    monkeypatch,
    storage_requests,
    path,
    params,
    repository,
    dataset,
    metadata,
    chunks,
):
    """Opens and storage requests per request type, on `tests/fixtures/icechunk_native`."""
    opens = count_opens(monkeypatch)

    assert app.get(path, params={**NATIVE, **params}).status_code == 200

    assert (opens["repository"], opens["dataset"]) == (repository, dataset)
    assert storage_requests["chunks"] == chunks
    assert sum(storage_requests.values()) - storage_requests["chunks"] == metadata


def test_coordinate_chunk_fetches_per_open(app, storage_requests, long_icechunk_store):
    """Issue 6: icechunk's chunk cache is off, so the `time` chunk is read 3 times per open."""
    params = {"url": long_icechunk_store, "variable": "data", "sel": f"time={TIME}"}

    assert app.get("/info", params=params).status_code == 200

    assert storage_requests["chunks"] == 3


@pytest.mark.parametrize(
    ("path", "sessions"),
    [
        # Issue 7: rasterio's internal Envs build an AWSSession each (-> 0)
        ("/tiles/WebMercatorQuad/2/1/1.png", 10),
        ("/point/-95,35", 3),
        ("/info", 2),
    ],
    ids=["tile", "point", "info"],
)
def test_boto3_sessions(app, monkeypatch, path, sessions):
    """`boto3.Session()` constructions per request type, with AWS credentials in the environment."""
    counts = count_boto3_sessions(monkeypatch)

    assert app.get(path, params={**NATIVE, "sel": "time=0"}).status_code == 200

    assert counts["boto3.Session"] == sessions


def test_storage_kinds_come_from_the_icechunk_key(app, storage_requests, tmp_path):
    """A `chunks/` or `repo/` directory above the store must not be counted as reads."""
    store = tmp_path / "chunks" / "repo"
    shutil.copytree("tests/fixtures/icechunk_native", store)

    params = {**NATIVE, "url": str(store), "sel": "time=0"}
    assert app.get("/info", params=params).status_code == 200

    assert storage_requests["chunks"] == 0
    assert sum(storage_requests.values()) == 8


def test_counting_server_shuts_down(monkeypatch):
    """`shutdown()` + `server_close()` stop the thread and free the port (fixture teardown)."""
    server, _ = serve_counting(monkeypatch)
    address = server.server_address

    server.shutdown()
    server.server_close()

    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(address, timeout=1).close()
