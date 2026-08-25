"""Tests for lazy EDL credential sourcing from Secrets Manager."""

import json

import pytest
from earthaccess_auth.exceptions import LoginStrategyUnavailable

ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:edl-token-AbCdEf"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Reset the module's once-guard and strip ambient EDL env vars."""
    from titiler.multidim import earthdata

    monkeypatch.setattr(earthdata, "_next_refresh", None)
    monkeypatch.setattr(earthdata, "_last_secret", None)
    for key in earthdata._ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", raising=False)


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


def test_existing_env_identity_wins(monkeypatch):
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    monkeypatch.setenv("EARTHDATA_TOKEN", "already-set")
    client = StubSecretsClient(secret_string="from-secret")
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    assert client.requested == []
    import os

    assert os.environ["EARTHDATA_TOKEN"] == "already-set"


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
    """A failed fetch must be retried on the next request, not cached forever."""
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    client = StubSecretsClient(error=RuntimeError("transient"))
    _install(monkeypatch, client)
    with pytest.raises(LoginStrategyUnavailable):
        ensure_earthdata_credentials()
    client.error = None
    client.secret_string = "tok"
    ensure_earthdata_credentials()
    import os

    assert os.environ["EARTHDATA_TOKEN"] == "tok"
    assert client.requested == [ARN, ARN]


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


def test_rotation_reexports_and_rebuilds_manager(monkeypatch):
    """After the refresh interval, a changed secret is re-exported and the
    process-wide credential manager is rebuilt around the new identity."""
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
    assert installed == []  # first load: manager builds lazily from env

    # within the interval: no re-fetch
    ensure_earthdata_credentials()
    assert client.requested == [ARN]

    # force the interval to elapse, rotate the secret
    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    client.secret_string = "tok-v2"
    ensure_earthdata_credentials()
    assert os.environ["EARTHDATA_TOKEN"] == "tok-v2"
    assert len(installed) == 1
    assert isinstance(installed[0], StubAuth)


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
    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    ensure_earthdata_credentials()
    assert client.requested == [ARN, ARN]
    assert installed == []


def test_usable_ambient_identity_latches(monkeypatch):
    """Operator-provided usable env identity permanently wins over the secret."""
    from titiler.multidim import earthdata
    from titiler.multidim.earthdata import ensure_earthdata_credentials

    monkeypatch.setenv("TITILER_MULTIDIM_EARTHDATA_SECRET_ARN", ARN)
    monkeypatch.setenv("EARTHDATA_TOKEN", "ambient")
    client = StubSecretsClient(secret_string="from-secret")
    _install(monkeypatch, client)
    ensure_earthdata_credentials()
    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    ensure_earthdata_credentials()
    assert client.requested == []
    import os

    assert os.environ["EARTHDATA_TOKEN"] == "ambient"


def test_rebuild_failure_is_sanitized(monkeypatch, caplog):
    """A raw EDL/HTTP failure while rebuilding the manager after rotation
    must not leak to the client; the sanitized LoginStrategyUnavailable
    message is returned instead, with details going to the service log."""
    import logging

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
    import os

    assert os.environ["EARTHDATA_TOKEN"] == "tok-v1"

    monkeypatch.setattr("earthaccess_auth.auth.Auth", StubAuth)
    monkeypatch.setattr(earthdata, "_next_refresh", 0.0)
    client.secret_string = "tok-v2"
    with caplog.at_level(logging.ERROR, logger="titiler.multidim.earthdata"):
        with pytest.raises(LoginStrategyUnavailable) as excinfo:
            ensure_earthdata_credentials()
    assert "EDL says no" not in str(excinfo.value)
    assert "EDL says no" in caplog.text
