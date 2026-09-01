"""Lazy EDL credential sourcing for earthdata access paths.

When ``earthdata_secret_arn`` is configured, the first earthdata code path
to run pulls the secret from AWS Secrets Manager and exports it as the
``EARTHDATA_*`` environment variables that earthaccess-auth's
non-interactive login strategy consumes. The configured secret is
authoritative — a stale ambient ``EARTHDATA_*`` identity must not disable
rotation — but ambient credentials still serve as a fallback while the
secret is unreachable, and outright when no ARN is configured, so local
development, tests, and netrc setups behave exactly as before.

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
    """Check whether the environment holds credentials earthaccess-auth can use.

    Mirrors the environment login strategy's requirement (a truthy token,
    or truthy username AND password) — key presence alone must not
    suppress the Secrets Manager fallback.

    Returns:
        True if the environment forms a usable identity.
    """
    env = os.environ
    return bool(
        env.get("EARTHDATA_TOKEN")
        or (env.get("EARTHDATA_USERNAME") and env.get("EARTHDATA_PASSWORD"))
    )


def _rebuild_default_auth() -> None:
    """Build and install the secret's identity as the default manager.

    default_manager() caches its Auth for the process lifetime, so the
    exported environment variables alone would never be (re-)read once a
    manager exists. Installing a freshly logged-in Auth swaps the manager
    (and its per-endpoint credential cache) for every consumer, including
    the refreshable credential callables icechunk holds inside a dataset —
    they call default_manager() at module level, not a captured instance.

    Raises:
        LoginStrategyUnavailable: When the login fails or yields no usable
            identity (sanitized; the raw error goes to the service log).
    """
    from earthaccess_auth.auth import Auth
    from earthaccess_auth.credentials import S3CredentialManager, set_default_manager

    try:
        auth = Auth()
        auth.login(strategy="environment")
    except Exception as e:
        # login() can hit EDL over HTTP (username/password secrets) and
        # raise LoginAttemptFailure with the raw EDL response body, or a
        # bare requests.* exception on a network failure; neither is
        # LoginStrategyUnavailable, so leaving it unsanitized would let the
        # app's 500 handler return it verbatim to an unauthenticated caller
        logger.error("building the EDL identity from the secret failed: %s", e)
        msg = (
            "failed to establish an Earthdata Login identity from the "
            "earthdata secret; see the service logs for details"
        )
        raise LoginStrategyUnavailable(msg) from e
    if not auth.authenticated:
        msg = "earthdata secret did not yield a usable EDL identity"
        raise LoginStrategyUnavailable(msg)
    manager = S3CredentialManager(auth)
    # probe through the manager that gets installed, so a successful probe
    # warms the very cache prime_earthdata_endpoints and icechunk's refresh
    # callable read — one s3credentials fetch per cold process, not two
    _probe_identity(manager)
    set_default_manager(manager)


def _probe_identity(manager) -> None:
    """Reject an identity a DAAC definitively refuses (HTTP 401).

    EDL marks any non-empty token authenticated without a network call, so
    a rotated-to-garbage token would otherwise install and latch (the
    unchanged secret then short-circuits every later refresh). Probe one
    configured earthdata endpoint through ``manager``: only a 401 rejects
    it — an unaccepted EULA (403), a DAAC outage, an older
    earthaccess-auth without status_code, or no configured earthdata
    entries are not evidence the credentials are bad.

    Args:
        manager: An S3CredentialManager wrapping the candidate identity.
            A successful probe leaves the credentials cached in it.

    Raises:
        LoginStrategyUnavailable: On a definitive 401 (sanitized; the
            caller restores the previous identity on rotation).
    """
    endpoints = _configured_earthdata_endpoints()
    if not endpoints:
        return
    from earthaccess_auth.exceptions import S3CredentialsRequestFailure

    try:
        manager.get_credentials(endpoints[0])
    except S3CredentialsRequestFailure as e:
        if getattr(e, "status_code", None) == 401:
            logger.error("earthdata secret credentials were rejected: %s", e)
            msg = (
                "the earthdata secret's credentials were rejected by the "
                "s3credentials endpoint; see the service logs for details"
            )
            raise LoginStrategyUnavailable(msg) from e
        logger.warning("earthdata identity probe inconclusive: %s", e)
    except Exception as e:
        logger.warning(
            "earthdata identity probe failed (%s); accepting the identity", e
        )


def _configured_earthdata_endpoints() -> list[str]:
    """Return the ``s3credentials`` endpoints of every configured earthdata entry."""
    from titiler.multidim.chunk_access import earthdata_endpoints
    from titiler.multidim.settings import ApiSettings

    entries = ApiSettings().authorized_chunk_access
    # every configured prefix counts as "declared" here: this is identity
    # validation, not per-repo credential scoping
    return earthdata_endpoints(entries, entries.keys())


def _secrets_client(secret_id: str):
    """Build a Secrets Manager client in the secret's own region.

    Args:
        secret_id: A full ARN
            (arn:aws:secretsmanager:<region>:<account>:secret:<name>), or
            a plain secret name (no colons), also a valid SecretId, which
            resolves in boto3's default region.
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

    Returns:
        The EARTHDATA_* variables forming a usable identity.

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
    # a JSON string scalar (a token piped through jq or a console JSON
    # round trip) decodes to the intended value; anything else is the raw
    # token — never export a token wrapped in quote characters
    token = (parsed if isinstance(parsed, str) else secret_string).strip()
    if not token:
        raise LoginStrategyUnavailable("earthdata secret is empty")
    return {"EARTHDATA_TOKEN": token}


def _swap_env(new: dict[str, str]) -> dict[str, str]:
    """Replace the EARTHDATA_* environment identity.

    New keys are written before stale ones are removed, so a concurrent
    reader (e.g. earthaccess-auth's environment strategy inside another
    request thread) never observes an *empty* environment mid-swap. The
    per-key writes are not atomic as a set, though: a reader racing the
    swap can briefly see a mixed identity (e.g. new username with the old
    password), failing that one request with a transient login error that
    self-heals on the next.

    Returns:
        The previous EARTHDATA_* values, for restoring on a failed swap.
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

    No-op when no secret ARN is configured (an ambient ``EARTHDATA_*``
    identity then latches untouched). The secret may be a plain EDL
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
            # missing for SecretBinary-only secrets: the KeyError must take
            # this failure path (typed error + backoff), not escape untyped
            secret = response["SecretString"]
        except Exception as e:
            _on_fetch_failure(arn, e, refreshing)
            return
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


def _on_fetch_failure(arn: str, e: Exception, refreshing: bool) -> None:
    """Handle a failed GetSecretValue; call with ``_lock`` held.

    Returns normally when an identity can keep serving (a warm refreshed
    one, or an ambient env fallback on first load). First loads back off
    ``_RETRY_INTERVAL`` so a misconfigured deployment doesn't turn every
    request into a Secrets Manager call serialized on the module lock.

    Raises:
        LoginStrategyUnavailable: When there is nothing to fall back to
            (sanitized; details go to the service log).
    """
    global _next_refresh
    # str() of this exception is returned to unauthenticated HTTP clients
    # by the app's 500 handler, so the ARN and raw AWS error stay in the
    # log only
    logger.error(
        "could not read the earthdata secret %r: %s; ensure the service "
        "role allows secretsmanager:GetSecretValue on it and the secret "
        "is stored as a SecretString",
        arn,
        e,
    )
    if refreshing:
        # the warm identity keeps serving (EDL tokens live ~60 days); the
        # deadline pushed before the fetch is the retry backoff
        return
    _next_refresh = time.monotonic() + _RETRY_INTERVAL
    if _usable_env_identity():
        # an ambient identity can serve while the secret stays
        # unreachable; keep retrying the authoritative secret on the
        # backoff cadence
        logger.warning(
            "serving with ambient EARTHDATA_* credentials until the "
            "earthdata secret loads"
        )
        return
    # nothing to fall back to: requests inside the window return without
    # credentials and fail fast downstream with a typed, sanitized error
    msg = (
        "failed to load Earthdata Login credentials from the "
        "configured secret; see the service logs for details"
    )
    raise LoginStrategyUnavailable(msg) from e


def _secret_arn_unless_latched() -> str | None:
    """Read the configured secret ARN; call with ``_lock`` held.

    Configuring an ARN is an explicit operator action, so the secret is
    authoritative: a stale-but-truthy ambient ``EARTHDATA_*`` identity
    must not latch and silently disable rotation-without-restart (the
    ambient identity still serves as a fallback while the secret is
    unreachable, and outright when no ARN is configured — local
    development, tests, netrc setups).

    Returns:
        The configured secret ARN, or None when no ARN is configured — in
        which case ``_next_refresh`` latches permanently.
    """
    global _next_refresh
    from titiler.multidim.settings import ApiSettings

    arn = ApiSettings().earthdata_secret_arn
    if not arn:
        _next_refresh = math.inf
        return None
    return arn


def _apply_secret(secret: str, rotating: bool) -> bool:
    """Export a changed secret and rebuild the auth manager around it.

    The manager is rebuilt on every load, first load included: a request
    served during a fetch-failure backoff window may already have built
    the default manager from a fallback identity (ambient env vars,
    netrc), which must not keep shadowing the secret's identity.

    Returns:
        False when applying a *rotation* failed: the previous identity
        (env vars plus the warm default auth manager built around them,
        EDL tokens live ~60 days) was restored and keeps serving. True
        otherwise.

    Raises:
        LoginStrategyUnavailable: On a failed *first* load — there is
            nothing to fall back to.
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
    try:
        _rebuild_default_auth()
    except LoginStrategyUnavailable:
        # _rebuild_default_auth already logged the raw error
        _swap_env(previous)
        if rotating:
            return False
        raise
    return True
