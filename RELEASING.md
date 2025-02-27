# Releasing

Deploying titiler-multidim to VEDA environments is configured and executed via the [veda-deploy](https://github.com/NASA-IMPACT/veda-deploy) repo.

## Release Workflow:

1. **Open pull requests:** PRs are made to the `main` branch. PRs should include tests and documentation. pytest should succeed before merging. If appropriate, changes should be added to the CHANGELOG.md file under the [**Unreleased**](https://github.com/developmentseed/titiler-multidim/blob/remove-automated-deployment-actions/CHANGELOG.md#unreleased) header.
2. **Deploy to SMCE Staging:** Once merged, deploy titiler-multidim to the smce-staging environment of veda-deploy.
    1. Verify `TITILER_MULTIDIM_GIT_REF` in the [smce-staging environment of veda-deploy](https://github.com/NASA-IMPACT/veda-deploy/settings/environments/4556936903/edit) is set to `main`.
    2. Follow the steps in [veda-deploy's How to deploy section](https://github.com/NASA-IMPACT/veda-deploy?tab=readme-ov-file#how-to-deploy). Select `smce-staging` for `Environment to deploy to` and ensure only `DEPLOY_TITILER_MULTIDIM` is checked.
3. **Deploy to MCP Prod:** When it is time to release changes to [veda-deploy's MCP environment](https://github.com/NASA-IMPACT/veda-deploy/settings/environments/2525365130/edit):
    1. Add a new release version heading (e.g. `v0.2.1`) to the top of CHANGELOG.md under the **Unreleased** header, so previously unreleased changes are documented as a part of the new release and new changes can still be added under **Unreleased**.
    2. Tag `main` with the new release version (e.g. `v0.2.1`).
    3. Update the `TITILER_MULTIDIM_GIT_REF` in [veda-deploy's MCP environment](https://github.com/NASA-IMPACT/veda-deploy/settings/environments/2525365130/edit) to the release tag (e.g. `v0.2.1`).
    4. Follow the steps in [veda-deploy's How to deploy section](https://github.com/NASA-IMPACT/veda-deploy?tab=readme-ov-file#how-to-deploy). Select `mcp-prod` for `Environment to deploy to` and ensure only `DEPLOY_TITILER_MULTIDIM` is checked.
