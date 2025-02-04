"""Functions to simplify writing tests."""

import httpx


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
