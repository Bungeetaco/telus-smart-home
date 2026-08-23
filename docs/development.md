# Development & releasing

Contributor notes for working on this project and cutting a release.

## Repository layout

- `custom_components/telus_smarthome/` — the Home Assistant custom integration. See [ha-integration.md](ha-integration.md).
- `camera-bridge/` — the standalone camera-bridge container. See [camera-bridge.md](camera-bridge.md).
- `docs/` — this documentation set (start at [README.md](README.md) / [architecture.md](architecture.md)).

## Versioning & releases

This project follows [semantic versioning](https://semver.org/). **The single source of truth for the version is `custom_components/telus_smarthome/manifest.json` (`"version"`)** — Home Assistant displays it, and HACS uses it to detect that an update is available. `hacs.json` carries no version; the camera bridge has no separate version string (it builds from the working tree).

Bump the version on **every** user-visible change — this is easy to forget, so treat it as part of the change, not an afterthought:

| Change | Bump |
| --- | --- |
| Bug fix, no behavior change | PATCH — `x.y.Z` |
| New backward-compatible feature (e.g. two-way audio) | MINOR — `x.Y.0` |
| Breaking change (config/entity removal, incompatible behavior) | MAJOR — `X.0.0` |

Release steps:

1. Bump `"version"` in `custom_components/telus_smarthome/manifest.json`.
2. Commit it: `git commit -m "Bump version to X.Y.Z"` (plus the change itself, if not already committed).
3. Tag it to match, and push:
   ```bash
   git tag vX.Y.Z
   git push origin <branch> --tags
   ```

**Keep the git tag and the manifest version in lockstep** — a checkout of tag `vX.Y.Z` should report version `X.Y.Z` to HA/HACS. (During early `1.5.x` development the manifest was bumped without matching tags — the last tag was `v1.4.0` while the manifest read `1.5.1`. Going forward every version bump gets its tag.)

A bridge-only change still rides in the same integration version bump — note it in the version's changes so the two components stay releasable together.

## Building & running locally

- **Camera bridge:** `cd camera-bridge && docker compose up -d --build` (needs `--network host` and a GPU render node for hardware encode — see [setup.md](setup.md) and [configuration.md](configuration.md)). Code is baked into the image, so a code change needs a rebuild.
- **Integration:** copy `custom_components/telus_smarthome/` into Home Assistant's `config/custom_components/` and add it via the UI, or install through HACS as a custom repository (see [setup.md](setup.md)). Integration code changes require a Home Assistant restart to load.

## Anonymization note for docs

The docs in this folder are written to be shareable: they describe the software generically and use placeholders (`<ha-host>`, `<doorbell-ip>`, `<unit-id>`, `<did>`, `<mac>`, …) instead of any real deployment's IPs, account/device IDs, hostnames, paths, or secrets. Keep new docs to the same standard.
