"""middleware

code originally from https://github.com/sm-Fifteen/asgi-server-timing-middleware by @https://github.com/sm-Fifteen

License: Creative Commons Zero v1.0 Universal
The Creative Commons CC0 Public Domain Dedication waives copyright interest in a work you've created
and dedicates it to the world-wide public domain. Use CC0 to opt out of copyright entirely and ensure
your work has the widest reach.
As with the Unlicense and typical software licenses, CC0 disclaims warranties. CC0 is very similar to the Unlicense.

"""

import inspect
import re
from contextvars import ContextVar
from typing import Callable, Dict, Tuple

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

try:
    import yappi
    from yappi import YFuncStats

except ImportError:  # pragma: nocover
    yappi = None  # type: ignore
    YFuncStats = None


_yappi_ctx_tag: ContextVar[int] = ContextVar("_yappi_ctx_tag", default=-1)


def _get_context_tag():
    return _yappi_ctx_tag.get()


class ServerTimingMiddleware:
    """Timing middleware for ASGI HTTP applications

    The resulting profiler data will be returned through the standard
    `Server-Timing` header for all requests.

    .. _Server-Timing sepcification:
    https://w3c.github.io/server-timing/#the-server-timing-header-field

    """

    def __init__(
        self,
        app: ASGIApp,
        calls_to_track: Dict[str, Tuple[Callable]],
        max_profiler_mem: int = 50_000_000,
    ) -> None:
        """Init the middleware

        Args:
            app (ASGI v3 callable): An ASGI application

            calls_to_track (Dict[str,Tuple[Callable]]): A dict of functions
                keyed by desired output metric name.

                Metric names must consist of a single rfc7230 token

            max_profiler_mem (int): Memory threshold (in bytes) at which yappi's
                profiler memory gets cleared.

        """
        assert yappi is not None, (
            "yappi must be installed to use ServerTimingMiddleware"
        )

        for metric_name, profiled_functions in calls_to_track.items():
            if len(metric_name) == 0:
                raise ValueError("A Server-Timing metric name cannot be empty")

            # https://httpwg.org/specs/rfc7230.html#rule.token.separators
            # USASCII (7 bits), only visible characters (no non-printables or space), no double-quote or delimiter
            if (
                not metric_name.isascii()
                or not metric_name.isprintable()
                or re.search(r'[ "(),/:;<=>?@\[\\\]{}]', metric_name) is not None
            ):
                raise ValueError(
                    '"{}" contains an invalid character for a Server-Timing metric name'.format(
                        metric_name
                    )
                )

            if not all(inspect.isfunction(profiled) for profiled in profiled_functions):
                raise TypeError(
                    'One of the targeted functions for key "{}" is not a function'.format(
                        metric_name
                    )
                )

        self.app = app
        self.calls_to_track = {
            name: list(tracked_funcs) for name, tracked_funcs in calls_to_track.items()
        }
        self.max_profiler_mem = max_profiler_mem

        yappi.set_tag_callback(_get_context_tag)
        yappi.set_clock_type("wall")

        yappi.start()

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        """Handle call."""

        ctx_tag = id(scope)
        _yappi_ctx_tag.set(ctx_tag)

        async def send_wrapper(message: Message):
            """Send Message."""
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)

                tracked_stats: Dict[str, YFuncStats] = {
                    name: yappi.get_func_stats(
                        filter={"tag": ctx_tag},
                        filter_callback=lambda x, f=tracked_funcs: yappi.func_matches(
                            x, f
                        ),
                    )
                    for name, tracked_funcs in self.calls_to_track.items()
                }

                # NOTE (sm15): Might need to be altered to account for various edge-cases
                timing_ms = {
                    name: sum(x.ttot for x in stats) * 1000
                    for name, stats in tracked_stats.items()
                    if not stats.empty()
                }

                server_timing = ",".join(
                    [
                        f"{name};dur={duration_ms:.3f}"
                        for name, duration_ms in timing_ms.items()
                    ]
                )

                if server_timing:
                    timings = response_headers.get("Server-Timing")
                    response_headers["Server-Timing"] = (
                        f"{timings}, {server_timing}" if timings else server_timing
                    )

                if yappi.get_mem_usage() >= self.max_profiler_mem:
                    yappi.clear_stats()

            await send(message)

        await self.app(scope, receive, send_wrapper)
