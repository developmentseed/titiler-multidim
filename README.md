# titiler-multidim

Example of application built with `titiler.xarray` [package](https://developmentseed.org/titiler/packages/xarray/)

---

**Source Code**: <a href="https://github.com/developmentseed/titiler-multidim" target="_blank">https://github.com/developmentseed/titiler-multidim</a>

---

## Running Locally

```bash
# It's recommended to install dependencies in a virtual environment
uv sync --dev
export TEST_ENVIRONMENT=true  # set this when running locally to mock redis
#optional: Disable caching
#export TITILER_MULTIDIM_ENABLE_CACHE=false
uv run uvicorn titiler.multidim.main:app --reload
```

To access the docs, visit <http://127.0.0.1:8000/api.html>.
![](https://github.com/developmentseed/titiler-multidim/assets/10407788/4368546b-5b60-4cd5-86be-fdd959374b17)

## TiTiler 2 migration

TiTiler 2 is a breaking API upgrade:

- Use `tilesize` (pixels) instead of `tile_scale`; TileJSON defaults to `tilesize=512`, while the map viewer defaults to `tilesize=256`.
- Tile URLs no longer include an `@{scale}x` suffix.
- Set a selector method on the selector itself, for example `sel=time=nearest::2020-01-06`, instead of using `sel_method`.
- TileJSON now includes additional raster metadata fields.

## Authorizing icechunk virtual chunk access

Icechunk datasets can reference "virtual chunks" stored outside the repository (for example NetCDF files in another bucket). Access to those locations is denied unless each container URL prefix is explicitly authorized via the `TITILER_MULTIDIM_AUTHORIZED_CHUNK_ACCESS` setting, a JSON object mapping prefixes to access options:

```bash
export TITILER_MULTIDIM_AUTHORIZED_CHUNK_ACCESS='{
  "s3://nasa-waterinsight/NLDAS3/": {"anonymous": true},
  "s3://podaac-ops-cumulus-protected/MUR-JPL-L4-GLOB-v4.1/": {"from_env": true}
}'
```

> [!WARNING]
> Authorizing a prefix effectively publishes the data under it: any client that can reach this service can craft an icechunk repository whose virtual chunks point at arbitrary byte ranges of any object under an authorized prefix, and read the values back through the tile, point, and statistics endpoints using the service's credentials. Only authorize prefixes whose contents you are willing to expose to every user of the deployment, and keep each prefix as narrow as possible.

The URL scheme of each prefix selects how its options are interpreted:

| Scheme | Options | Credentials used |
| --- | --- | --- |
| `s3://` | `anonymous`, `from_env`, `access_key_id`, `secret_access_key`, `session_token` (set fields are passed to [`icechunk.s3_credentials`](https://icechunk.io/en/latest/icechunk-python/reference/#icechunk.s3_credentials)) | S3 |
| `gs://` or `gcs://` | `anonymous`, `from_env`, `service_account_file`, `service_account_key`, `application_credentials`, `bearer_token` (passed to `icechunk.gcs_credentials`) | Google Cloud Storage |
| `az://` or `azure://` | `from_env`, `access_key`, `sas_token`, `bearer_token` (passed to `icechunk.azure_credentials`) | Azure Blob Storage |

The options for each entry are validated against a typed model for its scheme: an option outside the table above (including icechunk builder arguments that are not JSON-expressible, such as `get_credentials`) is rejected when the configuration is parsed. Credential use is opt-in for every scheme: each entry must select an access mode explicitly (`anonymous`, `from_env`, or explicit credential fields), so an empty entry `{}` is rejected rather than falling back to the service's own ambient credentials.

Icechunk attaches credentials to a container by exact string match against the `url_prefix` the dataset writer declared, trailing slash included. An entry for `s3://bucket/prefix/` does not match a container declared as `s3://bucket/prefix`, so the configured prefixes must reproduce the dataset's container prefixes character for character (`gs://` vs. `gcs://` and `az://` vs. `azure://` likewise follow the dataset's spelling). Schemes must be spelled in lowercase: a prefix like `S3://…` could never match a container, so it is rejected when the configuration is parsed.

Notes:

- The configuration is validated at application startup: an unsupported scheme or an unrecognized option fails the deploy instead of surfacing as request-time errors.
- icechunk has no anonymous Azure credential variant, so `anonymous` is only accepted for `s3://` and `gs://`/`gcs://` entries.
- `file://` prefixes are rejected: virtual chunks must never read the server's local filesystem, which would let any client craft a repository that exfiltrates server files.

## Development

Tests use data generated locally by using `tests/fixtures/generate_test_*.py` scripts.

Install the package using [`uv`](https://docs.astral.sh/uv/getting-started/installation/) with all development dependencies:

```bash
uv sync
uv run pre-commit install
```

To run all the tests:

```bash
uv run pytest
```

To run just one test:

```bash
uv run pytest tests/test_app.py::test_get_info 
```

## VEDA Deployment

* **Production deployments** are handled in the [NASA-IMPACT/veda-deploy](https://github.com/NASA-IMPACT/veda-deploy) repository.
* **Test/dev stack deployments** can be triggered by applying the `deploy-dev` label to a pull request in this repository. Each deployment requests a tile from the public native MUR, virtual MUR, and virtual NLDAS Icechunk stores.
* **CDK synth checks** can be triggered by applying the `run-cdk-checks` label to a pull request in this repository. This check only runs when the label is added, so if new commits are pushed later the label must be removed and added again.

To run the same deployment smoke test manually:

```bash
uv run python scripts/test_deployment.py --api-url https://your-api.execute-api.us-west-2.amazonaws.com
```

The RASI historical Icechunk store is intentionally excluded because its source data are corrupted.

## New Deployments

The following steps detail how to to setup and deploy the CDK stack from your local machine.

1. Install CDK and connect to your AWS account. This step is only necessary once per AWS account.

    ```bash
    # Download titiler repo
    git clone https://github.com/developmentseed/titiler-multidim.git

    # Install with the deployment dependencies
    uv sync --group deployment

    # Install the pinned local CDK CLI
    uv run npm --prefix infrastructure/aws ci

    # Deploys the CDK toolkit stack into an AWS environment
    uv run npm --prefix infrastructure/aws run cdk -- bootstrap

    # or to a specific region and or using AWS profile
    AWS_DEFAULT_REGION=us-west-2 AWS_REGION=us-west-2 AWS_PROFILE=myprofile uv run npm --prefix infrastructure/aws run cdk -- bootstrap
    ```

2. Update settings

    Set environment variable or hard code in `infrastructure/aws/.env` file (e.g `STACK_STAGE=testing`).

3. Pre-Generate CFN template

    ```bash
    uv run npm --prefix infrastructure/aws run cdk -- synth  # Synthesizes and prints the CloudFormation template for this stack
    ```

4. Deploy

    ```bash
    STACK_STAGE=staging uv run npm --prefix infrastructure/aws run cdk -- deploy titiler-xarray-staging

    # Deploy in specific region
    AWS_DEFAULT_REGION=us-west-2 AWS_REGION=us-west-2 AWS_PROFILE=smce-veda STACK_STAGE=production  uv run npm --prefix infrastructure/aws run cdk -- deploy titiler-xarray-production
    ```

**Important**

The Python `aws-cdk-lib` dependency in `pyproject.toml` is the construct library used by `infrastructure/aws/cdk/app.py`. The npm `aws-cdk` dependency in `infrastructure/aws/package.json` provides the `cdk` CLI. Keep the Python library pinned in `pyproject.toml`, keep the CLI pinned in `package.json` and `package-lock.json`, and always invoke CDK through `npm --prefix infrastructure/aws run cdk -- ...` so synth and deploy use the same local CLI version.

In AWS Lambda environment we need to have specific version of botocore, S3FS, FSPEC and other libraries.
To make sure the application will both work locally and in AWS Lambda environment you can install the dependencies using `python -m pip install -r infrastructure/aws/requirement-lambda.txt`

