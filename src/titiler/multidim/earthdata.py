"""Lazy EDL credential sourcing for earthdata access paths.

When ``earthdata_secret_arn`` is configured, the first earthdata code path
to run pulls the secret from AWS Secrets Manager and exports it as the
``EARTHDATA_*`` environment variables that earthaccess-auth's
non-interactive login strategy consumes. A usable ambient ``EARTHDATA_*``
identity wins outright, so local development, tests, and netrc setups
behave exactly as before.

The fetch is deliberately lazy rather than at import or app startup: the
Lambda deployment uses SnapStart, which freezes init-time state into the
snapshot. Resolving the secret on first use runs after restore, and
long-lived processes re-read it periodically, so rotation needs neither a
redeploy nor a restart.
"""

import json
import logging
import math
import os
import threading
import time

from earthaccess_auth.exceptions import LoginStrategyUnavailable

logger = logging.getLogger(__name__)

_ENV_KEYS = ("EARTHDATA_TOKEN", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD")

_lock = threading.Lock()

_REFRESH_INTERVAL = 600.0
"""Seconds between secret re-reads in a long-lived process, so rotating the
secret needs no restart. Lambda environments recycle far more often than
this and are unaffected."""

_RETRY_INTERVAL = 60.0
"""Seconds before retrying a *refresh* fetch that failed while a valid
identity was already in place. Sooner than a full refresh interval so
rotation resumes quickly, but not so soon it hammers Secrets Manager on
every request."""

_next_refresh: float | None = None
"""monotonic deadline for the next secret check; None = never checked,
math.inf = latched permanently (ambient identity, or no ARN configured)."""

_last_secret: str | None = None


def _usable_env_identity() -> bool:
    """True if the environment holds credentials earthaccess-auth can use.

    Mirrors the environment login strategy's requirement (a truthy token,
    or truthy username AND password) — key presence alone must not
    suppress the Secrets Manager fallback.
    """
    env = os.environ
    return bool(
        env.get("EARTHDATA_TOKEN")
        or (env.get("EARTHDATA_USERNAME") and env.get("EARTHDATA_PASSWORD"))
    )


def _rebuild_default_auth() -> None:
    """Point earthaccess-auth's default manager at rotated credentials.

    default_manager() caches its Auth for the process lifetime, so after a
    rotation the re-exported environment variables alone would never be
    re-read. Installing a freshly logged-in Auth swaps the manager (and its
    per-endpoint credential cache) for every consumer, including credential
    callables inside datasets unpickled from the shared cache — they call
    default_manager() at module level, not a captured instance.
    """
    from earthaccess_auth.auth import Auth
    from earthaccess_auth.credentials import set_default_auth

    try:
        auth = Auth()
        auth.login(strategy="environment")
    except Exception as e:
        # login() can hit EDL over HTTP (username/password secrets) and
        # raise LoginAttemptFailure with the raw EDL response body, or a
        # bare requests.* exception on a network failure; neither is
        # LoginStrategyUnavailable, so leaving it unsanitized would let the
        # app's 500 handler return it verbatim to an unauthenticated caller
        logger.error("rebuilding the EDL identity after secret rotation failed: %s", e)
        msg = (
            "failed to establish an Earthdata Login identity from the "
            "rotated secret; see the service logs for details"
        )
        raise LoginStrategyUnavailable(msg) from e
    if not auth.authenticated:
        msg = "rotated earthdata secret did not yield a usable EDL identity"
        raise LoginStrategyUnavailable(msg)
    set_default_auth(auth)


def _secrets_client(secret_id: str):
    """Build a Secrets Manager client in the secret's own region.

    Full ARN shape: arn:aws:secretsmanager:<region>:<account>:secret:<name>.
    A plain secret name (no colons) is also a valid SecretId; it resolves
    in boto3's default region.
    """
    import boto3

    parts = secret_id.split(":")
    region = parts[3] if len(parts) > 3 and parts[3] else None
    return boto3.client("secretsmanager", region_name=region)


def _parse_secret(secret_string: str) -> dict[str, str]:
    """Parse the secret into the EARTHDATA_* variables it should export.

    Falsy JSON values are skipped, not exported: "" is not None in
    earthaccess-auth's credential branch, so an empty EARTHDATA_TOKEN
    would shadow a working username/password with an empty bearer token,
    and a JSON null would export the literal string "None" — a truthy,
    unusable identity that latches permanently.

    Raises:
        LoginStrategyUnavailable: If no usable value survives.
    """
    try:
        parsed = json.loads(secret_string)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        known = {k: str(v) for k, v in parsed.items() if k in _ENV_KEYS and v}
        # mirror _usable_env_identity: an unpaired username or password is
        # unusable by the environment strategy, and exporting it anyway
        # would latch a broken identity until the secret *content* changes
        if not (
            known.get("EARTHDATA_TOKEN")
            or (known.get("EARTHDATA_USERNAME") and known.get("EARTHDATA_PASSWORD"))
        ):
            msg = (
                "earthdata secret is a JSON object but does not form a "
                "usable identity: it must hold a non-empty EARTHDATA_TOKEN, "
                "or both EARTHDATA_USERNAME and EARTHDATA_PASSWORD"
            )
            raise LoginStrategyUnavailable(msg)
        return known
    token = secret_string.strip()
    if not token:
        raise LoginStrategyUnavailable("earthdata secret is empty")
    return {"EARTHDATA_TOKEN": token}


def _swap_env(new: dict[str, str]) -> dict[str, str]:
    """Replace the EARTHDATA_* environment identity; return the old one.

    New keys are written before stale ones are removed, so a concurrent
    reader (e.g. earthaccess-auth's environment strategy inside another
    request thread) never observes an *empty* environment mid-swap. The
    per-key writes are not atomic as a set, though: a reader racing the
    swap can briefly see a mixed identity (e.g. new username with the old
    password), failing that one request with a transient login error that
    self-heals on the next.
    """
    # ponytail: per-key os.environ writes; true atomicity would need every
    # reader to take a shared lock, which earthaccess-auth's env strategy
    # doesn't — acceptable because the mixed window is a few instructions
    # wide and the failure mode is one transient, self-healing 500
    old = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
    os.environ.update(new)
    for key in _ENV_KEYS:
        if key not in new:
            os.environ.pop(key, None)
    return old


def prime_earthdata_endpoints(endpoints) -> None:
    """Establish the EDL identity and warm the credential cache in Python.

    Runs before icechunk's Rust layer can invoke the refreshable
    credential callable: Rust re-wraps whatever the callable raises as an
    opaque storage error, losing the typed S3CredentialsRequestFailure
    (EULA URLs) / LoginStrategyUnavailable that main.py maps to HTTP
    403/500. The default manager caches per endpoint, so priming adds no
    extra EDL round trips on subsequent opens.

    Args:
        endpoints: `s3credentials` endpoint URLs to warm.
    """
    ensure_earthdata_credentials()
    from earthaccess_auth.credentials import default_manager

    manager = default_manager()
    for endpoint in endpoints:
        manager.get_credentials(endpoint)


def ensure_earthdata_credentials() -> None:
    """Populate EDL environment credentials from Secrets Manager.

    No-op when the environment already holds a usable ``EARTHDATA_*``
    identity or no secret ARN is configured. The secret may be a plain EDL
    token string, or a JSON object holding any of ``EARTHDATA_TOKEN`` /
    ``EARTHDATA_USERNAME`` / ``EARTHDATA_PASSWORD`` (the username/password
    shape matches titiler-cmr's deployments, so the two services can share
    one secret). The secret is re-read every ``_REFRESH_INTERVAL`` seconds
    so rotation needs neither a redeploy nor a restart. A failed *first*
    load raises, then backs off ``_RETRY_INTERVAL`` — calls inside the
    window return without fetching (credential use then fails downstream
    with a typed, sanitized error) instead of hammering Secrets Manager on
    every request. A failure while *refreshing* an already-loaded identity —
    whether fetching the secret, parsing it, or logging the rotated
    credentials in — does not fail the request either: the warm identity
    (exported env vars plus the default auth manager built around them) is
    still valid, so it keeps serving and the failure is logged and retried
    after ``_RETRY_INTERVAL`` instead.

    Raises:
        LoginStrategyUnavailable: If the secret cannot be fetched on first
            load, or holds no recognized credential keys. Mapped to an HTTP
            error by the app's exception handlers; details go to the
            service log.
    """
    global _next_refresh, _last_secret
    if _next_refresh is not None and time.monotonic() < _next_refresh:
        return
    with _lock:
        if _next_refresh is not None and time.monotonic() < _next_refresh:
            return
        arn = _secret_arn_unless_latched()
        if arn is None:
            return
        refreshing = _last_secret is not None
        if refreshing:
            # push the deadline before the network call so concurrent
            # requests take the pre-lock fast path on the still-valid warm
            # identity instead of queueing behind a (possibly hung) Secrets
            # Manager call; success below replaces this with the full
            # interval, and a failure keeps it as the retry backoff
            _next_refresh = time.monotonic() + _RETRY_INTERVAL
        try:
            response = _secrets_client(arn).get_secret_value(SecretId=arn)
        except Exception as e:
            # str() of this exception is returned to unauthenticated HTTP
            # clients by the app's 500 handler, so the ARN and the raw AWS
            # error stay in the log only
            logger.error(
                "could not read the earthdata secret %r: %s; ensure the "
                "service role allows secretsmanager:GetSecretValue on it",
                arn,
                e,
            )
            if refreshing:
                # the warm identity keeps serving (EDL tokens live ~60
                # days); the deadline pushed above is the retry backoff
                return
            # first load: nothing to fall back to. Back off the same way so
            # a misconfigured deployment doesn't turn every request into a
            # GetSecretValue call serialized on this lock; requests inside
            # the window return without credentials and fail fast
            # downstream with a typed, sanitized error
            _next_refresh = time.monotonic() + _RETRY_INTERVAL
            msg = (
                "failed to load Earthdata Login credentials from the "
                "configured secret; see the service logs for details"
            )
            raise LoginStrategyUnavailable(msg) from e
        secret = response["SecretString"]
        if secret != _last_secret:
            try:
                applied = _apply_secret(secret, rotating=refreshing)
            except LoginStrategyUnavailable:
                # unusable first secret: same backoff as a failed first fetch
                _next_refresh = time.monotonic() + _RETRY_INTERVAL
                raise
            if not applied:
                # a refresh failure must not fail requests: the previous
                # identity keeps serving, retry sooner than a full interval
                _next_refresh = time.monotonic() + _RETRY_INTERVAL
                return
            _last_secret = secret
        _next_refresh = time.monotonic() + _REFRESH_INTERVAL


def _secret_arn_unless_latched() -> str | None:
    """Return the configured secret ARN, or None after latching.

    Latches ``_next_refresh`` permanently (returning None) when the
    operator supplied a usable ambient identity — never fetch the secret,
    never touch those variables again — or when no ARN is configured.
    Call with ``_lock`` held.
    """
    global _next_refresh
    if _last_secret is None and _usable_env_identity():
        _next_refresh = math.inf
        return None
    from titiler.multidim.settings import ApiSettings

    arn = ApiSettings().earthdata_secret_arn
    if not arn:
        _next_refresh = math.inf
        return None
    return arn


def _apply_secret(secret: str, rotating: bool) -> bool:
    """Export a changed secret and rebuild the auth manager around it.

    Returns False when applying a *rotation* failed: the previous identity
    (env vars plus the warm default auth manager built around them, EDL
    tokens live ~60 days) was restored and keeps serving. A failed first
    load raises instead — there is nothing to fall back to.
    """
    try:
        new_env = _parse_secret(secret)
    except LoginStrategyUnavailable as e:
        if not rotating:
            raise
        logger.error(
            "rotated earthdata secret is unusable (%s); keeping the previous identity",
            e,
        )
        return False
    # the swap drops stale keys (a rotation's shape change, token ->
    # username/password, can't leave a stale key shadowing the new
    # credentials) without ever leaving the environment empty
    previous = _swap_env(new_env)
    if rotating:
        try:
            _rebuild_default_auth()
        except LoginStrategyUnavailable:
            # _rebuild_default_auth already logged the raw error
            _swap_env(previous)
            return False
    return True
