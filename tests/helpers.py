"""Functions to simplify writing tests."""

import collections
import http.server
import os
import re
import socketserver
import threading

import boto3.session
import httpx2 as httpx
import icechunk
import xarray as xr


def find_string_in_stream(response: httpx.Response, target: str) -> bool:
    """
    Search for a string in a streaming response.
    """
    # Ensure the response is valid
    response.raise_for_status()
    buffer = ""
    # Read the response in chunks
    for chunk in response.iter_bytes():
        buffer += chunk.decode()
        # Check if the target string is in our buffer
        if target in buffer:
            return True
        # Optional: To avoid the buffer getting too large, you can clear it or manage its size
        buffer = buffer[
            -len(target) :
        ]  # Keep only the tail end of the buffer with length equal to the target string
    return False


# --- performance counters ----------------------------------------------------
#
# Each helper patches a third-party module (icechunk, xarray, boto3), never
# titiler.multidim.reader: the `app` fixture re-imports every titiler.multidim
# module, so a patch on a reader module imported earlier lands on a dead copy.


STORAGE_KINDS = ("repo", "config.yaml", "refs", "snapshots", "manifests", "chunks")
_STORAGE_KIND = re.compile(rf"/({'|'.join(map(re.escape, STORAGE_KINDS))})(/|$)")


class _RangeHandler(http.server.SimpleHTTPRequestHandler):
    """Serve any local file, honouring `Range` (icechunk reads manifests by range).

    `SimpleHTTPRequestHandler` ignores `Range`, and object_store then fails
    the read, so this handler answers 206 with a `Content-Range`.
    """

    requests: collections.Counter

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="/", **kwargs)

    def log_message(self, *args):  # silence per-request stderr lines
        pass

    def do_GET(self):
        if match := _STORAGE_KIND.search(self.path):
            self.requests[match[1]] += 1
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            body = f.read()
        if match := re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", "")):
            start = int(match[1])
            end = int(match[2]) if match[2] else len(body) - 1
            part = body[start : end + 1]
            self.send_response(206)
            self.send_header(
                "Content-Range", f"bytes {start}-{start + len(part) - 1}/{len(body)}"
            )
            body = part
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def count_storage_requests(monkeypatch) -> collections.Counter:
    """Route local Icechunk repositories through a counting HTTP server.

    Returns a Counter of icechunk storage requests keyed by `STORAGE_KINDS`,
    the unit the performance roadmap's numbers are in. `opener_icechunk`
    builds `file://` storage with `icechunk.local_filesystem_storage`; that
    is patched to return `icechunk.http_storage` for the same directory,
    served from the filesystem root so any local repository works.
    """
    handler = type(
        "CountingHandler", (_RangeHandler,), {"requests": collections.Counter()}
    )
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(
        icechunk,
        "local_filesystem_storage",
        lambda path: icechunk.http_storage(
            f"http://127.0.0.1:{server.server_address[1]}{os.path.abspath(path)}/"
        ),
    )
    return handler.requests


def count_opens(monkeypatch) -> collections.Counter:
    """Count repository opens (`local_filesystem_storage`) and `xr.open_dataset`.

    Call after `count_storage_requests` if both are used: this wraps whatever
    `icechunk.local_filesystem_storage` currently is.
    """
    counts: collections.Counter = collections.Counter()
    storage, open_dataset = icechunk.local_filesystem_storage, xr.open_dataset

    def counting_storage(*args, **kwargs):
        counts["repository"] += 1
        return storage(*args, **kwargs)

    def counting_open_dataset(*args, **kwargs):
        counts["dataset"] += 1
        return open_dataset(*args, **kwargs)

    monkeypatch.setattr(icechunk, "local_filesystem_storage", counting_storage)
    monkeypatch.setattr(xr, "open_dataset", counting_open_dataset)
    return counts


def count_boto3_sessions(monkeypatch) -> collections.Counter:
    """Count `boto3.Session()` constructions, as rasterio's `AWSSession` makes them.

    rasterio only builds one when `AWS_ACCESS_KEY_ID` is in the environment
    (always on Lambda, never in CI), so this also sets fake credentials.
    """
    counts: collections.Counter = collections.Counter()
    init = boto3.session.Session.__init__

    def counting_init(self, *args, **kwargs):
        counts["boto3.Session"] += 1
        init(self, *args, **kwargs)

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(boto3.session.Session, "__init__", counting_init)
    return counts
