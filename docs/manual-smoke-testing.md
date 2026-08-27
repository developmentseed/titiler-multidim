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

Details in the response bodies are intentionally generic; the specifics
(raw DAAC responses, endpoints) are in the Lambda's CloudWatch logs.

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
