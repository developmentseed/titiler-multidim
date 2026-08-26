"""Tests for lazy EDL credential sourcing from Secrets Manager."""

import json

import pytest
from earthaccess_auth.exceptions import LoginStrategyUnavailable

ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:edl-token-AbCdEf"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Reset module state, strip ambient EDL env vars, stub EDL login.

    Every successful secret load rebuilds the default auth manager, so the
    EDL Auth is stubbed by default (no network; individual tests override
    it to exercise failure paths) and the process-wide manager is reset so
    tests can't leak identities into each other.
    """
    from earthaccess_auth import credentials

    from titiler.multidim import earthdata

    monkeypatch.setattr(earthdata, "_next_refresh", None)
    monkeypatch.setattr(earthdata, "_last_secret", None)
    for key in earthdata._ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", raising=False)
    monkeypatch.delenv("TITILER_MULTIDIM_AUTHORIZED_CHUNK_ACCESS", raising=False)

    class _OkAuth:
        authenticated = True

        def login(self, strategy):
            return self

    monkeypatch.setattr("earthaccess_auth.auth.Auth", _OkAuth)
    monkeypatch.setattr(credentials, "_default_manager", None)


class StubSecretsClient:
    def __init__(self, secret_string=None, error=None):
        self.secret_string = secret_string
        self.error = error
        self.requested = []

    def get_secret_value(self, SecretId):
        self.requested.append(SecretId)
        if self.error is not None:
            raise self.error
        return {"SecretString": self.secret_string}


def _install(monkeypatch, client):
    from titiler.multidim import earthdata

    monkeypatch.setattr(earthdata, "_secrets_client", lambda arn: client)


def test_noop_without_secret_arn(monkeypatch):
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    client = StubSecretsClient()
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    assert client.requested == []


def test_configured_secret_overrides_ambient_env(monkeypatch):
    """When a secret ARN is configured, the secret is authoritative: a
    stale-but-truthy ambient EARTHDATA_TOKEN must not latch permanently
    and silently disable rotation-without-restart."""
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    monkeypatch.setenv("EARTHDATA_TOKEN", "stale-ambient")
    client = StubSecretsClient(secret_string="from-secret")
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    assert client.requested == [ARN]
    import os

    assert os.environ["EARTHDATA_TOKEN"] == "from-secret"


def test_fetch_failure_falls_back_to_ambient_env(monkeypatch):
    """If the configured secret can't be fetched but a usable ambient
    identity exists, serve with the ambient identity instead of failing,
    and keep retrying the secret on the backoff cadence."""
    import os

    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    monkeypatch.setenv("EARTHDATA_TOKEN", "ambient")
    client = StubSecretsClient(error=RuntimeError("AccessDeniedException"))
    _install(monkeypatch, client)
    ensure_earthdata_credentials()  # must not raise
    assert os.environ["EARTHDATA_TOKEN"] == "ambient"
    ensure_earthdata_credentials()  # inside the backoff window: no re-fetch
    assert client.requested == [ARN]


def test_raw_string_secret_becomes_token(monkeypatch):
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string="edl-token-value\n")
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    import os

    assert os.environ["EARTHDATA_TOKEN"] == "edl-token-value"
    assert client.requested == [ARN]


def test_json_secret_sets_known_keys_only(monkeypatch):
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(
        secret_string=json.dumps(
            {
                "EARTHDATA_USERNAME": "user",
                "EARTHDATA_PASSWORD": "pass",
                "UNRELATED": "nope",
            }
        )
    )
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    import os

    assert os.environ["EARTHDATA_USERNAME"] == "user"
    assert os.environ["EARTHDATA_PASSWORD"] == "pass"
    assert "UNRELATED" not in os.environ


def test_json_secret_without_known_keys_raises(monkeypatch):
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string=json.dumps({"UNRELATED": "nope"}))
    _install(monkeypatch, client)
    with pytest.raises(LoginStrategyUnavailable, match="EARTHDATA_"):
        ensure_earthdata_credentials()


def test_fetch_failure_raises_typed_error(monkeypatch):
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(error=RuntimeError("AccessDeniedException"))
    _install(monkeypatch, client)
    with pytest.raises(LoginStrategyUnavailable, match="secret"):
        ensure_earthdata_credentials()


def test_fetches_only_once(monkeypatch):
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string="tok")
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    ensure_earthdata_credentials()
    assert client.requested == [ARN]


def test_plain_secret_name_is_accepted(monkeypatch):
    """A plain secret name (no colons) is a valid SecretId, default region."""
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", "edl-token")
    captured = {}

    def fake_boto_client(service, region_name=None):
        captured["region"] = region_name
        return StubSecretsClient(secret_string="tok")

    import boto3

    monkeypatch.setattr(boto3, "client", fake_boto_client)
    # go through the real _secrets_client (no _install stub) to exercise parsing
    ensure_earthdata_credentials()
    import os

    assert os.environ["EARTHDATA_TOKEN"] == "tok"
    assert captured["region"] is None


def test_full_arn_selects_its_region(monkeypatch):
    from titiler.multidim.earthdata import _secrets_client

    captured = {}

    def fake_boto_client(service, region_name=None):
        captured["region"] = region_name
        return StubSecretsClient(secret_string="tok")

    import boto3

    monkeypatch.setattr(boto3, "client", fake_boto_client)
    _secrets_client(ARN)
    assert captured["region"] == "us-west-2"


def test_fetch_failure_message_is_sanitized(monkeypatch, caplog):
    """The client-visible message must not leak the ARN or the AWS error;
    both go to the service log instead (str(exc) is returned verbatim to
    unauthenticated HTTP clients by the app's 500 handler)."""
    import logging

    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(error=RuntimeError("AccessDeniedException: role x"))
    _install(monkeypatch, client)
    with caplog.at_level(logging.ERROR, logger="titiler.multidim.earthdata"):
        with pytest.raises(LoginStrategyUnavailable) as excinfo:
            ensure_earthdata_credentials()
    assert ARN not in str(excinfo.value)
    assert "AccessDeniedException" not in str(excinfo.value)
    assert ARN in caplog.text
    assert "AccessDeniedException" in caplog.text


def test_partial_env_identity_falls_back_to_secret(monkeypatch):
    """USERNAME without PASSWORD is unusable; the secret must still load.

    earthaccess-auth's environment strategy needs a truthy token OR truthy
    username+password, so key *presence* must not suppress the fallback.
    """
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    monkeypatch.setenv("EARTHDATA_USERNAME", "user-without-password")
    client = StubSecretsClient(secret_string="tok")
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    assert client.requested == [ARN]
    import os

    assert os.environ["EARTHDATA_TOKEN"] == "tok"


def test_empty_env_identity_falls_back_to_secret(monkeypatch):
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    monkeypatch.setenv("EARTHDATA_TOKEN", "")
    client = StubSecretsClient(secret_string="tok")
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    assert client.requested == [ARN]


def test_stale_unusable_env_key_does_not_shadow_secret(monkeypatch):
    """An ambient EARTHDATA_TOKEN="" is unusable, so the secret still
    loads; but it must not be left behind afterwards, since it would
    outrank the secret's own username/password in the environment login
    strategy (a truthy token wins over username+password) and leave every
    credential request authenticating with an empty bearer token."""
    import os

    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    monkeypatch.setenv("EARTHDATA_TOKEN", "")
    client = StubSecretsClient(
        secret_string=json.dumps(
            {"EARTHDATA_USERNAME": "user", "EARTHDATA_PASSWORD": "pass"}
        )
    )
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    assert "EARTHDATA_TOKEN" not in os.environ
    assert os.environ["EARTHDATA_USERNAME"] == "user"
    assert os.environ["EARTHDATA_PASSWORD"] == "pass"


def test_fetch_failure_does_not_latch(monkeypatch):
    """A failed fetch must be retried once the backoff expires, not cached
    forever."""
    from titiler.multidim import earthdata
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(error=RuntimeError("transient"))
    _install(monkeypatch, client)
    with pytest.raises(LoginStrategyUnavailable):
        ensure_earthdata_credentials()
    client.error = None
    client.secret_string = "tok"
    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)  # backoff elapsed
    ensure_earthdata_credentials()
    import os

    assert os.environ["EARTHDATA_TOKEN"] == "tok"
    assert client.requested == [ARN, ARN]


def test_first_load_fetch_failure_backs_off(monkeypatch):
    """A failing first load must not become a per-request Secrets Manager
    storm (the refresh path already backs off): within the retry window,
    later requests fail fast without another GetSecretValue."""
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(error=RuntimeError("AccessDeniedException"))
    _install(monkeypatch, client)
    with pytest.raises(LoginStrategyUnavailable):
        ensure_earthdata_credentials()
    ensure_earthdata_credentials()  # inside the backoff window: no re-fetch
    assert client.requested == [ARN]


def test_first_load_unusable_secret_backs_off(monkeypatch):
    """An unusable first secret gets the same backoff as a failed fetch."""
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string=json.dumps({"UNRELATED": "nope"}))
    _install(monkeypatch, client)
    with pytest.raises(LoginStrategyUnavailable):
        ensure_earthdata_credentials()
    ensure_earthdata_credentials()  # inside the backoff window: no re-fetch
    assert client.requested == [ARN]


def test_partial_identity_secret_raises(monkeypatch):
    """USERNAME without PASSWORD (or vice versa) is unusable by
    earthaccess-auth's environment strategy; exporting it anyway would
    latch a broken identity until the secret *content* changes, failing
    every request with a misleading error. Fail fast at parse instead."""
    import os

    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string=json.dumps({"EARTHDATA_USERNAME": "u"}))
    _install(monkeypatch, client)
    with pytest.raises(LoginStrategyUnavailable, match="EARTHDATA_"):
        ensure_earthdata_credentials()
    assert "EARTHDATA_USERNAME" not in os.environ


def test_token_with_partial_pair_is_usable(monkeypatch):
    """A usable token plus an unpaired username is fine: the token wins."""
    import os

    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(
        secret_string=json.dumps({"EARTHDATA_TOKEN": "t", "EARTHDATA_USERNAME": "u"})
    )
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    assert os.environ["EARTHDATA_TOKEN"] == "t"


def test_refresh_bumps_deadline_before_network_call(monkeypatch):
    """During a refresh fetch, concurrent requests must serve on the warm
    identity via the pre-lock fast path instead of queueing behind the
    Secrets Manager call: the deadline is pushed before the network I/O
    starts."""
    import time

    from titiler.multidim import earthdata
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)

    observed = []

    class ObservingClient(StubSecretsClient):
        def get_secret_value(self, SecretId):
            observed.append((earthdata._next_refresh, time.monotonic()))
            return super().get_secret_value(SecretId)

    client = ObservingClient(secret_string="tok")
    _install(monkeypatch, client)
    ensure_earthdata_credentials()  # first load

    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    ensure_earthdata_credentials()  # refresh
    deadline_at_fetch, now_at_fetch = observed[1]
    assert deadline_at_fetch is not None and deadline_at_fetch > now_at_fetch


def test_refresh_failure_keeps_serving_warm_identity(monkeypatch, caplog):
    """A transient Secrets Manager failure on a *refresh* (i.e. after a
    successful first load) must not fail requests: the exported env
    identity and the warm default auth manager are still valid (EDL
    tokens live ~60 days). The failure is logged and retried sooner than
    a full refresh interval, without re-fetching on every request."""
    import logging
    import os

    from titiler.multidim import earthdata
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string="tok")
    _install(monkeypatch, client)

    ensure_earthdata_credentials()
    assert os.environ["EARTHDATA_TOKEN"] == "tok"
    assert client.requested == [ARN]

    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    client.error = RuntimeError("transient")
    with caplog.at_level(logging.ERROR, logger="titiler.multidim.earthdata"):
        ensure_earthdata_credentials()  # must not raise
    assert os.environ["EARTHDATA_TOKEN"] == "tok"
    assert len(client.requested) == 2

    # the retry deadline was pushed forward, so an immediate next call
    # must not re-fetch
    ensure_earthdata_credentials()
    assert len(client.requested) == 2


def test_load_and_rotation_rebuild_manager(monkeypatch):
    """Every successful secret load — first load included — installs the
    secret's identity as the process-wide manager, so an identity cached
    earlier (e.g. netrc picked up during a fetch-failure backoff window)
    can never keep shadowing the secret."""
    from titiler.multidim import earthdata
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string="tok-v1")
    _install(monkeypatch, client)

    installed = []

    class StubAuth:
        authenticated = True

        def login(self, strategy):
            assert strategy == "environment"

    monkeypatch.setattr("earthaccess_auth.auth.Auth", StubAuth)
    monkeypatch.setattr(
        "earthaccess_auth.credentials.set_default_auth", installed.append
    )

    ensure_earthdata_credentials()
    import os

    assert os.environ["EARTHDATA_TOKEN"] == "tok-v1"
    assert len(installed) == 1  # first load installs the secret's identity
    assert isinstance(installed[0], StubAuth)

    # within the interval: no re-fetch
    ensure_earthdata_credentials()
    assert client.requested == [ARN]

    # force the interval to elapse, rotate the secret
    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    client.secret_string = "tok-v2"
    ensure_earthdata_credentials()
    assert os.environ["EARTHDATA_TOKEN"] == "tok-v2"
    assert len(installed) == 2


def test_unchanged_secret_recheck_does_not_rebuild(monkeypatch):
    from titiler.multidim import earthdata
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string="tok")
    _install(monkeypatch, client)
    installed = []
    monkeypatch.setattr(
        "earthaccess_auth.credentials.set_default_auth", installed.append
    )
    ensure_earthdata_credentials()
    assert len(installed) == 1  # first load
    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    ensure_earthdata_credentials()
    assert client.requested == [ARN, ARN]
    assert len(installed) == 1  # unchanged secret: no second rebuild


def test_ambient_identity_wins_without_arn(monkeypatch):
    """With no secret ARN configured, an ambient identity latches and is
    never touched — local development and netrc setups stay untouched."""
    from titiler.multidim import earthdata
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("EARTHDATA_TOKEN", "ambient")
    client = StubSecretsClient(secret_string="from-secret")
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    ensure_earthdata_credentials()
    assert client.requested == []
    import os

    assert os.environ["EARTHDATA_TOKEN"] == "ambient"


def test_rebuild_failure_keeps_warm_identity(monkeypatch, caplog):
    """A failed EDL login while applying a rotated secret must not fail
    requests: the previous identity (env vars and the warm default auth
    manager, EDL tokens live ~60 days) is restored and kept serving, the
    raw EDL error goes to the service log only, and the retry is backed
    off instead of re-attempting the login on every request."""
    import logging
    import os

    from titiler.multidim import earthdata
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string="tok-v1")
    _install(monkeypatch, client)

    class StubAuth:
        authenticated = True

        def login(self, strategy):
            msg = "EDL says no: <html>secret stuff</html>"
            raise Exception(msg)

    ensure_earthdata_credentials()
    assert os.environ["EARTHDATA_TOKEN"] == "tok-v1"

    monkeypatch.setattr("earthaccess_auth.auth.Auth", StubAuth)
    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    client.secret_string = "tok-v2"
    with caplog.at_level(logging.ERROR, logger="titiler.multidim.earthdata"):
        ensure_earthdata_credentials()  # must not raise
    assert "EDL says no" in caplog.text
    # the previous identity keeps serving
    assert os.environ["EARTHDATA_TOKEN"] == "tok-v1"
    # backed off: an immediate next call does not re-fetch or re-login
    ensure_earthdata_credentials()
    assert len(client.requested) == 2


def test_rotation_to_unusable_secret_keeps_warm_identity(monkeypatch, caplog):
    """A rotation to a secret with no usable keys must not fail requests
    or drop the exported identity; it is logged and retried with backoff."""
    import logging
    import os

    from titiler.multidim import earthdata
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string="tok-v1")
    _install(monkeypatch, client)
    ensure_earthdata_credentials()

    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    client.secret_string = json.dumps({"UNRELATED": "nope"})
    with caplog.at_level(logging.ERROR, logger="titiler.multidim.earthdata"):
        ensure_earthdata_credentials()  # must not raise
    assert os.environ["EARTHDATA_TOKEN"] == "tok-v1"
    ensure_earthdata_credentials()  # backed off: no immediate re-fetch
    assert len(client.requested) == 2


def test_empty_token_in_secret_does_not_shadow_password(monkeypatch):
    """An empty EARTHDATA_TOKEN value in the secret must be skipped: ""
    is not None in earthaccess-auth's credential branch, so exporting it
    would authenticate every request with an empty bearer token while the
    working username/password sit unused."""
    import os

    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(
        secret_string=json.dumps(
            {
                "EARTHDATA_TOKEN": "",
                "EARTHDATA_USERNAME": "user",
                "EARTHDATA_PASSWORD": "pass",
            }
        )
    )
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    assert "EARTHDATA_TOKEN" not in os.environ
    assert os.environ["EARTHDATA_USERNAME"] == "user"
    assert os.environ["EARTHDATA_PASSWORD"] == "pass"


def test_all_null_secret_values_raise(monkeypatch):
    """A JSON null must not become the literal string "None" in the
    environment (a truthy, unusable identity that latches forever)."""
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string=json.dumps({"EARTHDATA_TOKEN": None}))
    _install(monkeypatch, client)
    with pytest.raises(LoginStrategyUnavailable, match="EARTHDATA_"):
        ensure_earthdata_credentials()
    import os

    assert "EARTHDATA_TOKEN" not in os.environ


EARTHDATA_ACCESS_ENV = json.dumps(
    {"s3://podaac-ops-cumulus-protected/MUR/": {"earthdata": True}}
)


def test_rotation_to_rejected_token_rolls_back(monkeypatch):
    """EDL marks any non-empty token authenticated without a network call,
    so token-shaped rotations need a real check: the rebuild probes one
    configured earthdata endpoint, and a definitive 401 rejects the new
    identity — the previous one is restored and keeps serving."""
    import os

    from earthaccess_auth.exceptions import S3CredentialsRequestFailure

    from titiler.multidim import earthdata
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    monkeypatch.setenv("TITILER_MULTIDIM_AUTHORIZED_CHUNK_ACCESS", EARTHDATA_ACCESS_ENV)
    client = StubSecretsClient(secret_string="tok-v1")
    _install(monkeypatch, client)

    probes = {"fail": False}

    def probe(auth, endpoint):
        if probes["fail"]:
            raise S3CredentialsRequestFailure("rejected", status_code=401)

    monkeypatch.setattr("earthaccess_auth.credentials.fetch_s3_credentials", probe)
    ensure_earthdata_credentials()
    assert os.environ["EARTHDATA_TOKEN"] == "tok-v1"

    probes["fail"] = True
    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    client.secret_string = "tok-bad"
    ensure_earthdata_credentials()  # must not raise
    assert os.environ["EARTHDATA_TOKEN"] == "tok-v1"  # rolled back
    ensure_earthdata_credentials()  # backed off
    assert len(client.requested) == 2


def test_probe_eula_403_does_not_reject_rotation(monkeypatch):
    """An unaccepted EULA on the probe endpoint is a user-side problem,
    not evidence the rotated credentials are bad: accept the identity."""
    import os

    from earthaccess_auth.exceptions import S3CredentialsRequestFailure

    from titiler.multidim import earthdata
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    monkeypatch.setenv("TITILER_MULTIDIM_AUTHORIZED_CHUNK_ACCESS", EARTHDATA_ACCESS_ENV)
    client = StubSecretsClient(secret_string="tok-v1")
    _install(monkeypatch, client)

    def probe(auth, endpoint):
        raise S3CredentialsRequestFailure("EULA not accepted", status_code=403)

    monkeypatch.setattr("earthaccess_auth.credentials.fetch_s3_credentials", probe)
    ensure_earthdata_credentials()
    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    client.secret_string = "tok-v2"
    ensure_earthdata_credentials()
    assert os.environ["EARTHDATA_TOKEN"] == "tok-v2"


def test_binary_secret_is_a_typed_failure_with_backoff(monkeypatch):
    """A SecretBinary-only secret (no SecretString key) must surface as
    the same sanitized failure as an unreadable secret — with backoff —
    not an untyped KeyError that escapes the backoff logic."""

    class BinaryClient(StubSecretsClient):
        def get_secret_value(self, SecretId):
            self.requested.append(SecretId)
            return {"SecretBinary": b"\x00"}

    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = BinaryClient()
    _install(monkeypatch, client)
    with pytest.raises(LoginStrategyUnavailable):
        ensure_earthdata_credentials()
    ensure_earthdata_credentials()  # inside the backoff window: no re-fetch
    assert client.requested == [ARN]


def test_json_string_scalar_secret_strips_quotes(monkeypatch):
    """A secret stored as a JSON string scalar (console/jq round trips)
    must export the decoded token, not one wrapped in quote characters."""
    import os

    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string='"tok-json"\n')
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    assert os.environ["EARTHDATA_TOKEN"] == "tok-json"


def test_prime_earthdata_endpoints_establishes_identity_then_warms(monkeypatch):
    """Priming establishes the EDL identity once, then warms the shared
    per-endpoint credential cache in Python — before icechunk's Rust layer
    can invoke the refreshable callable and wrap failures opaquely."""
    from titiler.multidim import earthdata

    ensured = []
    monkeypatch.setattr(
        earthdata, "ensure_earthdata_credentials", lambda: ensured.append(True)
    )

    class StubManager:
        warmed = []

        def get_credentials(self, endpoint):
            self.warmed.append(endpoint)

    monkeypatch.setattr(
        "earthaccess_auth.credentials.default_manager", lambda: StubManager()
    )
    earthdata.prime_earthdata_endpoints(
        ["https://a/s3credentials", "https://b/s3credentials"]
    )
    assert ensured == [True]
    assert StubManager.warmed == ["https://a/s3credentials", "https://b/s3credentials"]


def test_prime_earthdata_endpoints_propagates_typed_eula(monkeypatch):
    """An unaccepted EULA surfaces as the typed S3CredentialsRequestFailure
    (mapped to HTTP 403 by main.py), not an opaque wrapped error."""
    from earthaccess_auth.exceptions import S3CredentialsRequestFailure

    from titiler.multidim import earthdata

    class RaisingManager:
        def get_credentials(self, endpoint):
            raise S3CredentialsRequestFailure("EULA not accepted")

    monkeypatch.setattr(
        "earthaccess_auth.credentials.default_manager", lambda: RaisingManager()
    )
    with pytest.raises(S3CredentialsRequestFailure, match="EULA"):
        earthdata.prime_earthdata_endpoints(["https://a/s3credentials"])


def test_blank_plain_secret_raises(monkeypatch):
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(secret_string="   \n")
    _install(monkeypatch, client)
    with pytest.raises(LoginStrategyUnavailable, match="empty"):
        ensure_earthdata_credentials()
    import os

    assert "EARTHDATA_TOKEN" not in os.environ
