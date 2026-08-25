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


def _export(secret_string: str) -> None:
    try:
        parsed = json.loads(secret_string)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        known = {k: v for k, v in parsed.items() if k in _ENV_KEYS}
        if not known:
            msg = (
                "earthdata secret is a JSON object but contains none of "
                f"{', '.join(_ENV_KEYS)}; store the EDL token as a plain "
                "string or under one of those keys"
            )
            raise LoginStrategyUnavailable(msg)
        for key, value in known.items():
            os.environ[key] = str(value)
    else:
        os.environ["EARTHDATA_TOKEN"] = secret_string.strip()


def ensure_earthdata_credentials() -> None:
    """Populate EDL environment credentials from Secrets Manager.

    No-op when the environment already holds a usable ``EARTHDATA_*``
    identity or no secret ARN is configured. The secret may be a plain EDL
    token string, or a JSON object holding any of ``EARTHDATA_TOKEN`` /
    ``EARTHDATA_USERNAME`` / ``EARTHDATA_PASSWORD`` (the username/password
    shape matches titiler-cmr's deployments, so the two services can share
    one secret). The secret is re-read every ``_REFRESH_INTERVAL`` seconds
    so rotation needs neither a redeploy nor a restart. A failed fetch on
    the *first* load is retried on the next call instead of poisoning the
    process. A failed fetch while *refreshing* an already-loaded identity
    does not fail the request either: the warm identity (exported env vars
    plus the default auth manager built around them) is still valid, so the
    failure is logged and retried after ``_RETRY_INTERVAL`` instead.

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
        if _last_secret is None and _usable_env_identity():
            # ambient identity supplied by the operator, not by us: never
            # fetch the secret, never touch these variables again
            _next_refresh = math.inf
            return
        from titiler.multidim.settings import ApiSettings

        arn = ApiSettings().earthdata_secret_arn
        if not arn:
            _next_refresh = math.inf
            return
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
            if _last_secret is not None:
                # a valid identity is already exported and the default auth
                # manager is built around it (EDL tokens live ~60 days), so
                # a transient failure while refreshing must not fail live
                # requests; retry sooner than a full refresh interval
                # without hammering Secrets Manager on every request
                _next_refresh = time.monotonic() + _RETRY_INTERVAL
                return
            # first load: nothing usable is exported yet, so there is no
            # identity to fall back to; _next_refresh is left unset so the
            # next request retries instead of failing forever
            msg = (
                "failed to load Earthdata Login credentials from the "
                "configured secret; see the service logs for details"
            )
            raise LoginStrategyUnavailable(msg) from e
        secret = response["SecretString"]
        if secret != _last_secret:
            rotating = _last_secret is not None
            # drop stale env keys unconditionally: reaching this line means
            # any ambient identity was already judged unusable, and a
            # rotation's shape change (token -> username/password) can't be
            # allowed to leave a stale key shadowing the new credentials
            for key in _ENV_KEYS:
                os.environ.pop(key, None)
            _export(secret)
            if rotating:
                _rebuild_default_auth()
            _last_secret = secret
        _next_refresh = time.monotonic() + _REFRESH_INTERVAL
