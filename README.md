# titiler-multidim

Example of application built with `titiler.xarray` [package](https://developmentseed.org/titiler/packages/xarray/)

---

**Source Code**: <a href="https://github.com/developmentseed/titiler-multidim" target="_blank">https://github.com/developmentseed/titiler-multidim</a>

---

## Running Locally

```bash
# It's recommended to install dependencies in a virtual environment
uv sync --dev
uv run uvicorn titiler.multidim.main:app --reload
```

To access the docs, visit <http://127.0.0.1:8000/api.html>.
![](https://github.com/developmentseed/titiler-multidim/assets/10407788/4368546b-5b60-4cd5-86be-fdd959374b17)

## Mosaic requests

Every Xarray data endpoint accepts one to twenty ordered `url` query values. Reuse the same `variable`, `group`, `decode_times`, and `sel` parameters for each source; no MosaicJSON or STAC manifest is needed. Overlapping valid pixels use the first URL by default (set `pixel_selection` to another supported rio-tiler strategy when needed).

```bash
curl 'http://127.0.0.1:8000/tiles/WebMercatorQuad/0/0/0.png?url=https%3A%2F%2Fexample.com%2Fpriority.zarr&url=https%3A%2F%2Fexample.com%2Ffallback.zarr&variable=temperature&sel=time%3D0'
```

All sources must expose compatible selected data.

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
| `s3://` | `anonymous`, `from_env`, `access_key_id`, `secret_access_key`, `session_token`, `earthdata` (set fields are passed to [`icechunk.s3_credentials`](https://icechunk.io/en/latest/icechunk-python/reference/#icechunk.s3_credentials); `earthdata` instead fetches refreshable credentials from NASA Earthdata for the entry's registered bucket, see [NASA Earthdata access](#nasa-earthdata-access)) | S3 |
| `gs://` or `gcs://` | `anonymous`, `from_env`, `service_account_file`, `service_account_key`, `application_credentials`, `bearer_token` (passed to `icechunk.gcs_credentials`) | Google Cloud Storage |
| `az://` or `azure://` | `from_env`, `access_key`, `sas_token`, `bearer_token` (passed to `icechunk.azure_credentials`) | Azure Blob Storage |

The options for each entry are validated against a typed model for its scheme: an option outside the table above (including icechunk builder arguments that are not JSON-expressible, such as `get_credentials`) is rejected when the configuration is parsed. Credential use is opt-in for every scheme: each entry must select an access mode explicitly (`anonymous`, `from_env`, `earthdata` (s3 only), or explicit credential fields), so an empty entry `{}` is rejected rather than falling back to the service's own ambient credentials.

Icechunk attaches credentials to a container by exact string match against the `url_prefix` the dataset writer declared, trailing slash included. An entry for `s3://bucket/prefix/` does not match a container declared as `s3://bucket/prefix`, so the configured prefixes must reproduce the dataset's container prefixes character for character (`gs://` vs. `gcs://` and `az://` vs. `azure://` likewise follow the dataset's spelling). Schemes must be spelled in lowercase: a prefix like `S3://…` could never match a container, so it is rejected when the configuration is parsed.

Notes:

- The configuration is validated at application startup: an unsupported scheme or an unrecognized option fails the deploy instead of surfacing as request-time errors.
- icechunk has no anonymous Azure credential variant, so `anonymous` is only accepted for `s3://` and `gs://`/`gcs://` entries.
- `file://` prefixes are rejected: virtual chunks must never read the server's local filesystem, which would let any client craft a repository that exfiltrates server files.

## NASA Earthdata access

To serve an icechunk dataset whose virtual chunks live in a protected
NASA Earthdata bucket, add an entry for that container prefix with
`"earthdata": true`:

```bash
TITILER_MULTIDIM_AUTHORIZED_CHUNK_ACCESS='{
  "s3://asdc-prod-protected/": {"earthdata": true}
}'
```

The service exchanges its Earthdata Login identity for the DAAC's
temporary S3 credentials and refreshes them as they expire. The bucket
must appear in [earthaccess-auth]'s CMR-derived registry (checked at
startup), and each entry is an explicit per-prefix grant — see the
authorization warning above for what granting one publishes. Earthdata
credentials apply to virtual chunk containers only: the icechunk store
itself and zarr/NetCDF sources are read with the service's own ambient
credentials.

Requirements:

- An EDL identity: `EARTHDATA_TOKEN` or
  `EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD` in the environment, a
  `.netrc` entry for `urs.earthdata.nasa.gov`, or — recommended for
  deployments — `TITILER_MULTIDIM_EARTHDATA_SECRET_ARN` pointing at a
  Secrets Manager secret holding either shape (a plain token string, or
  JSON with `EARTHDATA_*` keys). The secret is resolved lazily at first
  earthdata use (SnapStart-safe) and re-read periodically, so rotating
  it — EDL tokens expire after ~60 days — needs no redeploy. Usable
  ambient `EARTHDATA_*` variables take precedence over the secret. The
  service's IAM role must allow `secretsmanager:GetSecretValue` on the
  ARN (a plain secret name is also accepted and resolved in the default
  region).
- The EDL profile must have accepted the relevant DAAC EULAs; rejected
  requests surface as HTTP 403 with the EULA URLs in the message.
- Deployment must run in `us-west-2` (Earthdata's S3 credentials are
  region-locked).

For the CDK deployment, set `earthdata_secret_arn` in the stack's
`AppSettings`.

[earthaccess-auth]: https://github.com/earthaccess-dev/earthaccess-auth

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
* The Lambda runs outside a VPC, using standard outbound networking to reach S3 buckets in any AWS region. It continues to use the imported reader IAM role; the role's IAM policy and each bucket policy still govern S3 access.
* **CDK synth checks** run automatically on pull requests, including pull requests from forks. The filter fails closed: only pull requests limited to documentation, tests, markdown, and unrelated workflows report the check as skipped (which still satisfies the required status check on `main`); anything else — including application source, which the CDK app imports and the Lambda image bundles — runs the full check. The check is fully anonymous: `cdk synth` runs with a dummy reader role ARN that is parsed but never resolved and no AWS credentials, validating the synthesized template and Lambda asset sizes without access to any AWS account.

To run the same deployment smoke test manually:

```bash
uv run python scripts/test_deployment.py --api-url https://your-api.execute-api.us-west-2.amazonaws.com
```

For manual checks beyond the automated tiles — the TEMPO earthdata path,
its failure modes, and the map viewer — see
[docs/manual-smoke-testing.md](docs/manual-smoke-testing.md).

The RASI historical Icechunk store is intentionally excluded because its source data are corrupted.

## Local CDK deployment runbook

Deployments require Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Node.js/npm, Docker, and the AWS CLI configured with credentials that can bootstrap and deploy the stack. The Lambda image is built by Docker during CDK synthesis.

From a fresh checkout:

```bash
git clone https://github.com/developmentseed/titiler-multidim.git
cd titiler-multidim

# Select the AWS account, region, deployment stage, and existing reader role.
# Run `aws configure --profile myprofile` first if this profile is not configured.
export AWS_PROFILE=myprofile
export AWS_DEFAULT_REGION=us-west-2
export STAGE=testing
export TITILER_MULTIDIM_READER_ROLE_ARN=arn:aws:iam::123456789012:role/reader

aws sts get-caller-identity
uv sync --group deployment
uv run npm --prefix infrastructure/aws ci
```

Bootstrap the CDK toolkit once for each account and region:

```bash
uv run npm --prefix infrastructure/aws run cdk -- bootstrap
```

Deploy the stack:

```bash
uv run npm --prefix infrastructure/aws run cdk -- deploy --all \\
  --require-approval never \\
  --outputs-file "$HOME/cdk-outputs.json"
```

The Lambda runs outside a VPC and uses standard outbound networking, so S3 buckets in any AWS region are reachable without VPC configuration. The CDK stack imports the reader IAM role rather than changing it. The role's IAM policy and each bucket policy must permit the required S3 access.

The Python `aws-cdk-lib` dependency in `pyproject.toml` is the construct library used by `infrastructure/aws/cdk/app.py`. The npm `aws-cdk` dependency in `infrastructure/aws/package.json` provides the pinned `cdk` CLI. Always invoke CDK through `npm --prefix infrastructure/aws run cdk -- ...` so synth and deploy use the same local CLI version.

