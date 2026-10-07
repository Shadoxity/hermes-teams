# Upstream porting guide

## Pinned sources

The baseline is NousResearch/hermes-agent commit [`9dcab1e440cdc482320af002e44036b947d6d709`](https://github.com/NousResearch/hermes-agent/tree/9dcab1e440cdc482320af002e44036b947d6d709). Reference copies belong under `upstream/base/plugins/platforms/teams/`.

| Proposal | Pinned head | Reference directory |
| --- | --- | --- |
| [#118102: reactions](https://github.com/NousResearch/hermes-agent/pull/118102) | `079bb5dd59b361fc618e1ff31ed1d36b96f3704e` | `upstream/pr-118102/` |
| [#118103: files and Graph](https://github.com/NousResearch/hermes-agent/pull/118103) | `bf5cbbad7e6956ae7024e808684ebe549a1320df` | `upstream/pr-118103/` |
| [#118127: streaming](https://github.com/NousResearch/hermes-agent/pull/118127) | `6aef09afe1bb6f91f49d49e2431fadea8c020abb` | `upstream/pr-118127/` |

Each PR snapshot is intended to include `metadata.json`, `files.json`, `diff.patch` and downloaded changed Teams runtime/test files at their original repository-relative paths. The metadata and pinned commits are the provenance record; a PR URL is mutable.

All three proposals were open and unmerged when reviewed on 7 October 2026. Their authors describe the dependency order as reactions, then files, then streaming. The files and streaming diffs include preceding work. **Do not bulk cherry-pick or apply all three complete patches.** Current pinned heads may differ from the original three-commit stack; verify ancestry and compute semantic deltas before porting. Retain upstream authorship, license notices and explicit attribution for copied/adapted code.

## What can remain inside this plugin

| Change | Upstream runtime files | Porting decision |
| --- | --- | --- |
| Reactions | Teams `adapter.py`, `plugin.yaml` | Port locally using existing adapter hooks. |
| File handling | Teams adapter, new `graph_files.py`, manifest; shared Graph client and gateway changes | Port file behavior and helpers locally; replace the shared-client extension locally; omit observation-related behavior. |
| Streaming | Teams adapter plus inherited files/reactions changes | Extract streaming-specific behavior and adapt to the baseline interfaces. |
| Rich posts | Existing card sender is a starting point | Implement local templates and an agent-facing interface; this is additional work. |

The external package has its own name, `hermes-teams`, but registers the platform as `teams`. Do not rename the platform to obtain isolation; configuration/session continuity depends on retaining that identity. The supported registration mechanism replaces the adapter factory without editing Hermes source. Add a test proving actual selection after complete plugin loading.

## Core changes in the files proposal

### `tools/microsoft_graph_client.py`

The PR adds `put_bytes` and raw request content support. Put equivalent functionality in a plugin-local helper/client, or a carefully bounded subclass, instead of patching Hermes's shared client. Preserve retry/auth semantics and test binary request bodies. Also verify redirects and streaming limits rather than assuming the existing download helper is sufficient.

### `gateway/session.py`

The PR inserts Teams-specific model instructions describing `MEDIA:<absolute_path>` delivery and FileConsent/Graph behavior. Provide accurate instructions through the custom adapter's supported prompt context or another supported plugin mechanism. Do not copy its blanket claim that `send_message` is absent without checking the configured tool surface. Instructions must describe implemented behavior, not hoped-for capability.

### `gateway/run.py`

The PR expands observed-message history handling from Telegram to Teams. This supports passive capture of unmentioned channel chatter and is not required for file transfers.

Omit **all** of the associated observation feature initially: the opt-in/default setting, passive transcript writes, shared-session mutations, prompt markers and related event transformations. Keeping those adapter writes while omitting the core replay safeguard could cause old chatter to appear as actionable requests. Explicit message/thread retrieval for requested files is a separate operation.

## Required adaptations and known review points

1. **Import ownership.** The patches contain `from plugins.platforms.teams.graph_files ...`; `graph_files.py` imports a helper back from `plugins.platforms.teams.adapter`. Convert custom-module imports to package-relative imports and test with both plugins discoverable. Audit registration and summary-writer references too.
2. **Profiles.** Keep scoped secret/config access throughout Graph and bot authentication. A custom plugin must not accidentally use the default profile's credentials when serving a second profile.
3. **Thread identifiers.** Separate flat connector conversation IDs from thread-root IDs. Use correct Graph root/reply paths for message lookup. Preserve reply placement for files, cards, streamed continuations and fallback sends.
4. **File permissions.** The files proposal calls `createLink` with `scope: organization`. Do not inherit that default: use the uploaded item's existing permissions and `webUrl` unless a separately authorized sharing policy requires otherwise.
5. **Group chats.** The proposal assumes `/chats/{id}/filesFolder`. Do not treat that as a verified channel-equivalent API. Start with the channel files-folder path and keep unsupported chat cases explicit.
6. **Upload bounds.** The proposal uses a conservative simple-upload threshold and a separate application size cap. Choose documented limits deliberately and test chunk/session behavior. Do not present a hard-coded threshold as the current Microsoft API maximum without checking official documentation.
7. **Text files.** The proposal inlines small text files in channels. Retain a real file-upload path when the user asks for a downloadable file.
8. **Streaming.** Extract the `edit_message` and related update/delete behavior from #118127, including thread-ID handling, edit deduplication and throttling. Keep `draft_stream_is_message = False` and compatible `finalize` behavior.
9. **Alternative streaming PR.** [#124159](https://github.com/NousResearch/hermes-agent/pull/124159) is a smaller adapter-only implementation, but validates raw IDs without stripping `;messageid=` and handles long-message continuations differently. It is reference material, not an additional patch to merge into #118127.
10. **Tests are evidence of their own environment.** Upstream mocked tests do not prove app permissions, real Graph endpoints, Teams SDK compatibility, correct thread placement or recipient access. Adapt them to the package namespace and add integration coverage against the installed baseline.

## Porting workflow

1. Confirm the snapshot metadata/head commits and the baseline license. Keep reference snapshots unchanged.
2. Start from the pinned baseline adapter, with provenance recorded. Make each feature a reviewable local change rather than replacing the adapter wholesale with the largest PR version.
3. Extract reaction behavior, then streaming behavior, then file behavior. Resolve overlapping helpers once and avoid applying inherited changes twice.
4. Keep new Graph functionality, card rendering, configuration and tests in this repository. No Hermes-source edits, runtime monkeypatches or installation steps that overwrite bundled files.
5. Compare the resulting behavior against [IMPLEMENTATION.md](IMPLEMENTATION.md), including omissions and errors. Record deviations from upstream explicitly.
6. Verify package registration separately from feature tests and live tests. Publish or deploy only the reviewed state; do not describe the copied baseline or reference snapshots as completed features.

## Useful source anchors

- [Baseline incoming attachment handling](https://github.com/NousResearch/hermes-agent/blob/9dcab1e440cdc482320af002e44036b947d6d709/plugins/platforms/teams/adapter.py#L547)
- [Baseline card transport](https://github.com/NousResearch/hermes-agent/blob/9dcab1e440cdc482320af002e44036b947d6d709/plugins/platforms/teams/adapter.py#L603)
- [Baseline outgoing media/file behavior](https://github.com/NousResearch/hermes-agent/blob/9dcab1e440cdc482320af002e44036b947d6d709/plugins/platforms/teams/adapter.py#L735)
- [Baseline text-only standalone sender](https://github.com/NousResearch/hermes-agent/blob/9dcab1e440cdc482320af002e44036b947d6d709/plugins/platforms/teams/adapter.py#L189)
- [Existing Graph client](https://github.com/NousResearch/hermes-agent/blob/9dcab1e440cdc482320af002e44036b947d6d709/tools/microsoft_graph_client.py)
- [Existing streaming adapter contract](https://github.com/NousResearch/hermes-agent/blob/9dcab1e440cdc482320af002e44036b947d6d709/gateway/stream_consumer_transport.py#L31)

For a future upstream refresh, record new SHAs, compare each feature's delta, review the installed Hermes interface changes, and rerun the compatibility matrix. Do not refresh these pinned snapshots silently.

## Public distribution metadata

The baseline collection metadata describes its source revision without any deployment host. Only that local descriptive metadata and its checksum were normalized for public distribution; downloaded upstream source, patches, authorship, and license notices remain unchanged.
