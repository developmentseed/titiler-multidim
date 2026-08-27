# Manual smoke testing a dev deployment

What to check by hand after a `deploy-dev` labeled deployment goes live,
beyond the automated tile requests in `scripts/test_deployment.py`. All
examples use `API_URL` for the deployed endpoint.

## Find the endpoint

The deploy job prints it: open the workflow run for "CDK Deploy Dev
Workflow", expand the "Smoke-test deployed Icechunk tiles" step, and the
first line resolves `API_URL` from `cdk-outputs.json`. It is also the
`Endpoint` output on the CloudFormation stack (`<stack-name>-<stage>` in
the dev account).

```bash
export API_URL=https://<api-id>.execute-api.us-west-2.amazonaws.com
```

## Baseline

```bash
curl -sf "$API_URL/healthz"
uv run python scripts/test_deployment.py --api-url "$API_URL"
```

The script requests one tile from every public store plus the TEMPO
earthdata case. If it passes, most of what follows is confirmation and
poking at the parts a single tile can't show.

The interactive API docs live at `$API_URL/api.html`.

## The earthdata path (TEMPO)

TEMPO is the one store that exercises the full credential chain: Secrets
Manager secret → EDL login → identity probe → `s3credentials` exchange →
virtual chunk reads from `asdc-prod-protected`. Test it in this order —
each step needs strictly more of the chain working than the one before.

```bash
TEMPO="s3://airquality-data-store-develop/tempo/no2/v04-trial"
```

1. **Metadata** — needs store-bucket IAM only, no EDL:

   ```bash
   curl -sf "$API_URL/variables?url=$TEMPO"
   curl -sf "$API_URL/info?url=$TEMPO&variable=vertical_column_troposphere" | head -c 400
   ```

   A failure here is an IAM or store problem (reader role permissions on
   `airquality-data-store-develop`, or the store prefix has graduated past
   `v04-trial`), not an EDL problem.

   `/variables` is the one endpoint that never uses the Redis dataset
   cache, so it fails first when a store moves or permissions change while
   `/tiles` and `/info` are still being served from a warm entry. If they
   disagree, `GET $API_URL/clear_cache` and re-check.

2. **A tile** — needs the whole chain:

   ```bash
   curl -so /tmp/tempo.png -w '%{http_code} %{content_type}\n' \
     "$API_URL/tiles/WebMercatorQuad/4/3/6.png?url=$TEMPO&variable=vertical_column_troposphere&sel=time=2026-08-24T15:40:44&sel_method=nearest&rescale=0,1.5e16&colormap_name=viridis"
   ```

   Expect `200 image/png`. The first request on a cold Lambda is the slow
   one (secret fetch, EDL login, identity probe); repeat it and it should
   come back much faster from the warmed credential cache.

3. **A point value** — same chain, different read path:

   ```bash
   curl -sf "$API_URL/point/-95,35?url=$TEMPO&variable=vertical_column_troposphere&sel=time=2026-08-24T15:40:44&sel_method=nearest"
   ```

4. **Look at it** — open the map viewer in a browser:

   ```text
   $API_URL/WebMercatorQuad/map.html?url=s3://airquality-data-store-develop/tempo/no2/v04-trial&variable=vertical_column_troposphere&sel=time=2026-08-24T15:40:44&sel_method=nearest&rescale=0,1.5e16&colormap_name=viridis
   ```

   TEMPO covers North America; pan there. Tiles outside the scan's swath
   are transparent — that's data coverage, not an error.

### Reading the failure modes

The service maps credential failures deliberately; the status code tells
you whose problem it is.

| Response | Meaning | Fix |
| --- | --- | --- |
| `403` with EULA and application URLs in the body | The EDL identity in the secret hasn't accepted the ASDC EULA or application terms | Log in to EDL as the service identity and accept them, then retry — no redeploy needed |
| `500` "The service's Earthdata Login credentials were rejected" | The secret's token is expired (~60-day lifetime) or wrong | Rotate the secret; the Lambda re-reads it within ~10 minutes, or immediately on a cold start |
| `500` "Authentication with Earthdata Login failed" | EDL itself rejected the login (bad username/password secret, EDL outage) | Check the secret's contents and EDL status |
| `500` "icechunk storage error" | The store itself couldn't be read | Check reader-role S3 permissions and the store prefix |
| Metadata works but every tile fails | Chunk-read-time credential refresh is failing while store metadata (read with ambient IAM) still works | Same 403/500 triage as above — the wrapped error keeps the status distinction |
| `500` "File format identification for extension  is not implemented" (note the empty extension) | Nothing was found at the url: `identify_storage_backend` listed the prefix, got zero objects, and fell back to treating it as a single file | Fix the url, or check the store prefix still exists — this is never a format problem |
| `500` "no non-interactive EDL login strategy available" | The Lambda has no `TITILER_MULTIDIM_EARTHDATA_SECRET_ARN`, so credential loading is a permanent no-op — or the first secret fetch failed and this request landed in the 60s retry window | Check the deployed env var (below); if it is set, look for `could not read the earthdata secret` in the logs and treat it as a `GetSecretValue` failure |

Details in the response bodies are intentionally generic; the specifics
(raw DAAC responses, endpoints) are in the Lambda's CloudWatch logs.

## Checking the reader role's permissions

The Lambda runs as an externally managed role imported with
`mutable=False` (`infrastructure/aws/cdk/app.py`), so CDK cannot grant it
anything — every permission must already exist on the role before the
deploy. Two grants matter:

- `secretsmanager:GetSecretValue` on the EDL secret (plus `kms:Decrypt`
  if it is encrypted with a CMK)
- `s3:ListBucket` and `s3:GetObject` on the icechunk store bucket

The role does **not** need access to `asdc-prod-protected`: those are the
virtual chunk reads, and they use the DAAC temporary credentials from the
`s3credentials` exchange, not ambient IAM. Only the store bucket is read
with the role (`from_env=True` in `opener_icechunk`).

Start from what is actually deployed rather than the GitHub Actions
variables:

```bash
FN=<function-name>
ROLE=$(aws lambda get-function-configuration --function-name "$FN" --query Role --output text)
SECRET=$(aws lambda get-function-configuration --function-name "$FN" \
  --query 'Environment.Variables.TITILER_MULTIDIM_EARTHDATA_SECRET_ARN' --output text)
```

A `None` for `$SECRET` is itself the answer to a
`no non-interactive EDL login strategy available` failure: the stack was
synthesized without `TITILER_MULTIDIM_EARTHDATA_SECRET_ARN` (an unset
`vars.EARTHDATA_SECRET_ARN` expands to an empty string, and
`StackSettings.model_post_init` then omits the variable entirely). Set the
Actions variable and redeploy; the ARN is read lazily per request, so
nothing else needs clearing.

### Assume the role and try it

The only check that covers identity policy, bucket policy, KMS key policy
and SCPs together:

```bash
CREDS=$(aws sts assume-role --role-arn "$ROLE" --role-session-name perm-check --query Credentials)
export AWS_ACCESS_KEY_ID=$(jq -r .AccessKeyId <<<"$CREDS") \
       AWS_SECRET_ACCESS_KEY=$(jq -r .SecretAccessKey <<<"$CREDS") \
       AWS_SESSION_TOKEN=$(jq -r .SessionToken <<<"$CREDS")

# secret + KMS, in the secret's own region (what _secrets_client does)
aws secretsmanager get-secret-value --secret-id "$SECRET" \
  --region "$(cut -d: -f4 <<<"$SECRET")" --query ARN

# ListBucket — the exact call identify_storage_backend makes
aws s3api list-objects-v2 --bucket airquality-data-store-develop \
  --prefix tempo/ --max-keys 3 --query 'Contents[].Key'

# GetObject, on a real key from the listing above
aws s3api head-object --bucket airquality-data-store-develop --key <key-from-above>
```

An `AccessDenied` names the missing action; a missing `kms:Decrypt`
surfaces on the first call as an `AccessDeniedException` mentioning KMS
rather than Secrets Manager. `unset AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN` when done.

### When the trust policy won't let you assume it

```bash
aws iam simulate-principal-policy --policy-source-arn "$ROLE" \
  --action-names secretsmanager:GetSecretValue --resource-arns "$SECRET" \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text

aws iam simulate-principal-policy --policy-source-arn "$ROLE" \
  --action-names s3:ListBucket --resource-arns arn:aws:s3:::airquality-data-store-develop \
  --context-entries ContextKeyName=s3:prefix,ContextKeyType=string,ContextKeyValues=tempo/ \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text

aws iam simulate-principal-policy --policy-source-arn "$ROLE" \
  --action-names s3:GetObject --resource-arns 'arn:aws:s3:::airquality-data-store-develop/tempo/x' \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text
```

The resource ARN shapes differ — a **bucket** ARN for `ListBucket`, an
**object** ARN for `GetObject`; simulating `ListBucket` against an object
ARN returns a meaningless `implicitDeny`. And `allowed` here is not proof:
the simulator reads identity policies only, so it cannot see a bucket
policy `Deny` or a KMS key policy that omits the role.

### Is the secret even on a CMK?

```bash
aws secretsmanager describe-secret --secret-id "$SECRET" --query KmsKeyId
```

`null` or `alias/aws/secretsmanager` means the AWS-managed key —
`GetSecretValue` alone is enough. Any other key id is a CMK, and the role
needs both `kms:Decrypt` and an entry in the key policy
(`aws kms get-key-policy --key-id <id> --policy-name default`), usually
gated on `kms:ViaService=secretsmanager.<region>.amazonaws.com`.

## Freshness (expected behavior, not a bug)

Forward processing appends TEMPO scans continuously, but readers sit
behind two caches: CloudFront (`max-age=3600`) and the service's Redis
dataset cache. A missing newest scan within that window is caching.
`GET $API_URL/clear_cache` drops the Redis layer if you need to see a
fresh append immediately; CloudFront expires on its own.

## The public stores

The automated script covers one tile each. For a quick interactive check,
the same viewer works for any of them, e.g. virtual MUR:

```text
$API_URL/WebMercatorQuad/map.html?url=s3://nasa-eodc-public/icechunk/MUR-JPL-L4-GLOB-v4.1-virtual-v2-p2&variable=analysed_sst&sel=time=2024-08-01T09:00:00.000000000&decode_times=true&rescale=273,325&colormap_name=thermal
```

## Notes

- Everything must run against the us-west-2 deployment: DAAC temporary S3
  credentials are region-locked, so a stack in another region 403s on
  chunk reads even with perfect auth.
- `histogram` and `statistics` endpoints exist too and read full arrays;
  they're heavier than a tile and not part of smoke testing.
