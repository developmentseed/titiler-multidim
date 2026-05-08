# Releasing

Deploying titiler-multidim to VEDA environments is configured and executed via the [veda-deploy](https://github.com/NASA-IMPACT/veda-deploy) repo.

## Automated release preparation

A [release-please](https://github.com/googleapis/release-please-action) workflow manages versioning and changelogs. When changes are merged to `main`, it opens or updates a release PR.

## Release Workflow

1. **Open pull requests:** PRs are made to the `main` branch. PRs should include tests and documentation. The PR title must follow [Conventional Commits](https://www.conventionalcommits.org/) format (e.g. `feat:`, `fix:`, `docs:`) so that release-please can categorize changes correctly. pytest should succeed before merging.
2. **Merge the release PR:** release-please opens a release PR that accumulates changes since the last release. Review the changelog in the PR description and merge it when ready. On merge, release-please will create a GitHub release and tag.
3. **Deploy to SMCE Staging:** Once merged, deploy titiler-multidim to the smce-staging environment of veda-deploy.
    - Verify `TITILER_MULTIDIM_GIT_REF` in the [smce-staging environment of veda-deploy](https://github.com/NASA-IMPACT/veda-deploy/settings/environments/4556936903/edit) is set to `main`.
    - Follow the steps in [veda-deploy's How to deploy section](https://github.com/NASA-IMPACT/veda-deploy?tab=readme-ov-file#how-to-deploy). Select `smce-staging` for `Environment to deploy to` and ensure only `DEPLOY_TITILER_MULTIDIM` is checked.
4. **Deploy to MCP Prod:** When it is time to release changes to [veda-deploy's MCP environment](https://github.com/NASA-IMPACT/veda-deploy/settings/environments/2525365130/edit):
    - Use the release tag (e.g. `v0.2.1`) created by release-please.
    - Update the `TITILER_MULTIDIM_GIT_REF` in [veda-deploy's MCP environment](https://github.com/NASA-IMPACT/veda-deploy/settings/environments/2525365130/edit) to the release tag.
    - Follow the steps in [veda-deploy's How to deploy section](https://github.com/NASA-IMPACT/veda-deploy?tab=readme-ov-file#how-to-deploy). Select `mcp-prod` for `Environment to deploy to` and ensure only `DEPLOY_TITILER_MULTIDIM` is checked.
