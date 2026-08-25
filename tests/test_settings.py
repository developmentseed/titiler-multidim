"""ApiSettings/AppSettings tests for earthdata configuration plumbing."""


def make_settings(**kwargs):
    # _env_file=None keeps a developer's local .env out of the assertions
    from titiler.multidim.settings import ApiSettings

    return ApiSettings(_env_file=None, **kwargs)


def test_chunk_access_defaults_empty():
    settings = make_settings()
    assert settings.authorized_chunk_access == {}
    assert settings.earthdata_secret_arn is None


def test_no_registry_expansion_field():
    # the all-CMR expansion flag was removed: earthdata access is granted
    # only through explicit authorized_chunk_access entries
    from titiler.multidim.settings import ApiSettings

    assert "earthdata_access" not in ApiSettings.model_fields


def test_explicit_earthdata_entry_parses():
    from titiler.multidim.chunk_access import S3ChunkAccess

    prefix = "s3://asdc-prod-protected/"
    settings = make_settings(authorized_chunk_access={prefix: {"earthdata": True}})
    entry = settings.authorized_chunk_access[prefix]
    assert isinstance(entry, S3ChunkAccess)
    assert entry.earthdata is True


def test_app_settings_forward_secret_arn():
    from titiler.multidim.settings import AppSettings

    settings = AppSettings(
        _env_file=None,
        reader_role_arn="arn:aws:iam::123456789012:role/reader",
        earthdata_secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:edl",
    )
    assert (
        settings.additional_env["TITILER_MULTIDIM_EARTHDATA_SECRET_ARN"]
        == "arn:aws:secretsmanager:us-west-2:123456789012:secret:edl"
    )
    assert "TITILER_MULTIDIM_EARTHDATA_ACCESS" not in settings.additional_env


def test_app_settings_secret_arn_defaults_off():
    from titiler.multidim.settings import AppSettings

    settings = AppSettings(
        _env_file=None,
        reader_role_arn="arn:aws:iam::123456789012:role/reader",
    )
    assert "TITILER_MULTIDIM_EARTHDATA_SECRET_ARN" not in settings.additional_env
