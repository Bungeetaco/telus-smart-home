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
| **Minor change** — bug fix, tuning, tests, docs, internal cleanup, a small addition nobody has to think about | **PATCH** — `x.y.Z` |
| **Major change** — a new capability, a changed default, anything a user would notice or have to react to | **MINOR** — `x.Y.0` |
| Breaking change (config/entity removal, incompatible behavior) | MAJOR — `X.0.0` |

Bias towards the patch position. A minor bump (`x.Y.0`) is for changes worth
announcing — two-way audio, the stop service, talkdown switching on by default —
not for everything that happens to add a line of code.

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


## Dependency updates (automated)

Upstream versions are surfaced automatically, but applied deliberately -- the
integration **monkeypatches `pyalarmdotcomajax` internals**, so a blind bump can
break login. The setup:

- **[Renovate](https://docs.renovatebot.com/)** (`renovate.json`) opens update
  PRs for everything pinned: `pyalarmdotcomajax` + `beautifulsoup4` (via
  Renovate's built-in `homeassistant-manifest` manager, which reads the
  `requirements[]` array in `manifest.json`), the bridge's `requirements.txt`,
  the Dockerfile base image, and the `MEDIAMTX_VERSION` Dockerfile `ARG` (a
  custom regex manager against GitHub releases -- the only pin with no built-in
  equivalent). *Enable it by installing the Renovate GitHub App on the repo* --
  the config alone does nothing without it. (Dependabot is a zero-install
  fallback but can't cover the MediaMTX `ARG`.)
- **Auto-merge policy** (in `renovate.json`): low-risk updates (`python` base
  image, `beautifulsoup4`) auto-merge once CI is green; **high-risk ones
  (`pyalarmdotcomajax`, `aiortc`, `av`, `mediamtx`) never auto-merge** -- they get
  a `review-required` label.
- **CI smoke test** (`.github/workflows/ci.yml`) runs on every PR:
  1. installs the integration requirements and imports `telus_auth_patch.py`,
     which runs its compatibility guard -- so a `pyalarmdotcomajax` bump that
     moves an internal the patch depends on **fails the PR loudly** instead of
     breaking login at runtime;
  2. installs the bridge `requirements.txt` with `--only-binary :all:`, catching
     an `aiortc`/`av` bump that has no manylinux wheel (which would otherwise try
     to build from source in the image).

**When a `pyalarmdotcomajax` bump PR appears:** if CI is red, the guard caught a
moved internal -- update `_telus_async_login` / the patched attributes in
`telus_auth_patch.py` to match the new version, then bump. If CI is green, the
patch still applies, but still skim the upstream changelog before merging.


## Dependency hardening (2026-08 audit)

A dependency-fragility audit of both trees drove the following changes. The theme:
this project's integration deliberately monkeypatches a library, so the risk is not
"a bump fails loudly" but "a bump changes an internal and something breaks quietly."

**Applied:**

- **Compatibility guards on all three monkeypatches.** `telus_auth_patch.py` now also
  guards the private coroutine it calls, the `pyalarmdotcomajax.const` values it
  rewrites, and the extension class attribute it overwrites;
  `telus_ws_heartbeat_patch.py` and `telus_ws_push_patch.py` gained guards of their own
  (they had none, and both wrap private coroutines). The CI smoke test now imports all
  three and, because the replaced `async_login` populates ~11 PRIVATE INSTANCE
  attributes that no class-level check can see, constructs a throwaway controller and
  asserts each one.
- **Stopped mutating aiohttp's `ClientSession`.** The heartbeat patch used to assign
  `session.ws_connect = ...` and `session._telus_hb_patched = True`; aiohttp warns on
  any attribute outside its `ATTRS` and that surface is slated to close in 4.0. It now
  wraps the session in a small transparent proxy that injects `heartbeat=`, keeping the
  mutation on the library object we already patch.
- **`web.AppKey` instead of plain-string `Application` keys** in the bridge's control
  server (aiohttp emits `NotAppKeyWarning` for str keys; `AppKey` has existed since 3.9).
- **One `Retry-After` parser** (`config.parse_retry_after`) replacing three copies that
  understood only delta-seconds. RFC 9110 also permits an HTTP-date, which previously
  fell through to a fixed 60s -- the exact "fixed short retry re-triggers the same
  window" behaviour that once caused a multi-hour outage. It now parses both and logs
  when it falls back.
- **Socket timeouts on the talkdown PUT session.** The safety deadline is only checked
  between chunks, so a single wedged PUT could pin the per-camera talk lock for
  aiohttp's 300s default; now `sock_connect`/`sock_read` are 5s.
- **`DigestAuthMiddleware`** replaces the hand-rolled RFC 2617 implementation (see E21).
- **Hardened XML parsing** in `mws_client.py` (defusedxml-first, matching `mobile_auth`),
  with `defusedxml` and `yarl` (imported directly but previously undeclared) added to
  `requirements.txt`.
- **Public aiortc imports** (`MediaStreamError`/`MediaStreamTrack` come from the package
  root) and corrected `websockets` type annotations (`ClientConnection`; the legacy
  `WebSocketClientProtocol` alias has not been what `connect()` returns since 14.0).
- **Typo-proof `UnkonwnDevice` import** -- the name is misspelled upstream, so a future
  spelling fix would have been an `ImportError` at integration load.
- **Dropped the unused `beautifulsoup4` requirement** (nothing imports it -- and it was
  in Renovate's auto-merge group, i.e. auto-merging a dependency nothing uses).
- **Renovate:** `aiohttp` and `websockets` added to the review-required group; they carry
  the actual deprecation exposure.
- **New CI job: every bridge module must import.** `py_compile` only catches syntax
  errors; a broken import list is a runtime `NameError` that surfaces as a
  restart-looping container. This was added after exactly that happened during the audit
  fixes.

**Considered and deliberately NOT changed:**

- **`climate.py`'s `self.__dict__.pop("_attr_max_temp")`.** The audit suggested assigning
  the default explicitly, but `ClimateEntity.max_temp` is a *property object*, not a
  value, and HA's default converts `DEFAULT_MAX_TEMP` into the entity's temperature unit.
  Assigning either would break Fahrenheit setups. Removing the attribute so HA's own
  default property applies is the correct behaviour and is already documented in place.
- **`clip_view.py`'s hand-rolled HMAC signing.** HA ships `async_sign_path`, but its
  signatures are bound to a refresh token and die when it is revoked -- incompatible with
  the requirement that links inside already-delivered notifications keep working. The
  local implementation embeds the expiry in the MAC and uses `compare_digest`. Keep it;
  revisit only if the notification-longevity requirement is dropped.

**Still open (low priority):** entity naming derives from `device.__class__.__name__`, so
a library class rename would silently change entity IDs rather than raising;
`mws_client.py` imports two underscore-prefixed helpers from `mobile_auth` (intra-project,
but they should be renamed public); `pylibsrtp` is pinned though only referenced in a
commented-out fallback (it arrives transitively via aiortc anyway).



**Python moves in lockstep.** CI's `setup-python` mirrors the runtime image
(`camera-bridge/Dockerfile`'s `FROM python:<ver>-slim`) — testing on an
interpreter the container does not run is worse than not testing. Renovate
reports both under the dep name `python` and is configured to group them into a
single reviewed PR, so never bump one alone. A runtime interpreter bump for a
24/7 service is not auto-merged.

## Keeping the docs current

Documentation is part of the change, and the whole set has to stay consistent.
Any change to behaviour, configuration, deployment, or the shape of a request or
response updates the docs **in the same commit** -- and updates *every* page the
change makes wrong, not just the nearest one. A set where one page is right and
another contradicts it is worse than one where neither was touched, because a
reader has no way to tell which to trust.

The checklist of which page to touch for which kind of change lives in
[`AGENTS.md`](../AGENTS.md), so humans and agents work from one list rather than
two that drift apart. In short:

- **Root `README.md`** for anything user-visible -- a new capability, entity or
  service, a changed installation step, a new requirement or limitation. Purely
  internal refactors don't need it.
- **[architecture.md](architecture.md)** and **[camera-bridge.md](camera-bridge.md)**
  when modules, flows or component relationships move -- including the diagram.
- **[ha-integration.md](ha-integration.md)** for entities, services, coordinators
  and poll intervals; keep the entity table exhaustive.
- **[control-api.md](control-api.md)** for **any** change to a route or its
  request/response fields. That page is a contract other code is written against.
- **[configuration.md](configuration.md)** for a new knob, env var or interval;
  **[troubleshooting.md](troubleshooting.md)** for a new failure mode; the
  `*-api-findings.md` pages when something is learned about an upstream API; the
  enhancement backlog (`talkdown-fullduplex-protocol.md`) when an idea is
  deferred or closed, rather than losing it.
- **[README.md](README.md)** (this folder's index) whenever a doc is added, so
  the index never goes stale.

**These docs are a source of truth, not a changelog.** They describe what the
system *is* and what is *known*, in the present tense. What changed, when, and
why belongs in the commit and the pull request, which is where anyone asking that
question will look. Concretely, that means no dates on statements, no "corrected
on…", no "this used to say…", no "an earlier version wrongly claimed…". When
something here turns out to be wrong, **rewrite it so the page states the truth**
and delete the wrong claim — do not leave a trail beside it. A page that narrates
its own edit history buries the answer a reader came for.

The one thing that is always worth writing down is **the limit of what is
known**: "unverified", "inferred from the sibling endpoint rather than captured",
"needs hardware this account does not have". That is not history, it is the
current state of the evidence, and it is what stops the next person either
trusting a guess or re-deriving a question that was already asked.

Keep it all anonymized and shareable (placeholders, never real IPs, account or
device IDs, hostnames or secrets).
