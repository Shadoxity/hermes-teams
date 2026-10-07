# Installation guide for AI agents

Use this runbook to install `hermes-teams` for a user without editing Hermes Agent source. Read [INSTALL.md](INSTALL.md), [NEW_BOT.md](NEW_BOT.md), and the selected Hermes checkout's own instructions before changing anything. Treat repository examples as placeholders, not discovered deployment facts.

## 1. Establish the target and authority

Determine the host, Hermes source path and commit, selected profile/home, Python environment, gateway service owner, current Teams identity, and public messaging route. Inspect the actual configuration and current services; a profile can share one gateway with other profiles. Ask only for missing choices or authorization. Reuse existing user approval instead of asking for the same permission twice.

Record privately:

| Input | How to establish it |
| --- | --- |
| Hermes source/runtime | Inspect the installed checkout, Python environment, and plugin loader. Python 3.11+ is required. |
| Profile home | Resolve the selected profile explicitly; never assume the default profile or the administrator's home. |
| Tenant/subscription | Verify the signed-in identity and selected directory before creating resources. |
| Test team/channel | Obtain the intended destination; a Teams channel link supplies team group GUID, channel ID, and tenant ID. |
| Bot identity | Choose an existing approved identity or a new Single Tenant registration. Preserve a working identity for rollback. |
| File access | Obtain the operator's choice of selected sites or explicitly approved tenant-wide read/write. |
| Publisher/package | User-approved app display name, publisher, legal/support URLs, and icons. |
| Exposure | Working public HTTPS route owned by the selected profile. |

Read secrets through the deployment's protected mechanism. Do not echo tokens, secret values, raw authentication responses, signed URLs, or configuration files containing them. Use a separate administrator context for provisioning; the running bot needs no app-catalog or grant-management privileges.

## 2. Preserve the current installation

Create a protected backup of the selected profile's configuration, secret file/store references, enabled/disabled plugin list, release link, and service configuration. Record hashes/version identifiers without secret values. Check that unrelated profiles and local source changes will be preserved. Do not overwrite or remove an existing user plugin directory.

Keep deployment records, logs, generated Teams ZIPs, and branding outside the source checkout. An upgrade from a differently named custom adapter must also account for its plugin-data mapping directory and explicit toolset selection; do not leave two custom adapters enabled for `teams`.

## 3. Install the external plugin

Prefer a real Git checkout managed by Hermes's native installer. Use the selected Hermes runtime as the deployment user and name the profile explicitly:

```console
hermes --profile <profile-name> plugins install https://github.com/Shadoxity/hermes-teams.git --no-enable --no-deps
```

Public repositories can use anonymous HTTPS. For a private repository, use deployment-approved access; `git@github.com:Shadoxity/hermes-teams.git` is also accepted. A repository-scoped read-only deploy key can provide future fetch access without transferring administrator credentials. Keep private keys/tokens outside the checkout and remote URL. Verify repository access as the update user, record the installed commit, and check `origin` and branch upstream without exposing secrets.

The installer places the repository root at:

```text
<selected profile home>/plugins/hermes-teams/
```

The directory must contain `plugin.yaml`, `__init__.py`, runtime modules, and `skills/`. Inspect this runtime's `plugins install --help` and `plugins update --help` before choosing flags. `--no-enable` avoids requesting immediate activation; `--no-deps` means dependencies must be managed below. On managed-runtime versions, `--no-deps` leaves the plugin disabled and cannot replace an active plugin. A supported `--yes-deps` flag supplies already-authorized dependency consent for noninteractive installs; older versions may not accept it. Do not combine those dependency flags or force-replace an existing installation without its protected backup and a reviewed migration. Do not copy `upstream/` reference code into Hermes's bundled plugin directory.

For regular updates, leave the install unpinned and verify it tracks this repository's `origin/main`; Hermes records source/revision/pin state in profile-local `plugins/.install-metadata.json`. If an immutable revision is required, install with `--ref <full-40-character-commit-SHA>`. Native `plugins update` refuses pinned installs; advancing a pin requires an explicit install with `--force --ref <new-full-40-character-commit-SHA>`. `--ref` does not accept a branch or tag.

A release-directory symlink remains an option for loading, but native updates reject a symlink whose target resolves outside the selected profile's `plugins/` directory. That layout requires its own release/symlink update procedure. A manually copied archive without Git/install provenance cannot use the native Git updater. Prefer the real checkout when the user expects future `hermes plugins update` support.

Use the **selected Hermes runtime's environment manager** to prepare/verify the dependencies listed in `pyproject.toml`: `httpx>=0.27,<1`, `aiohttp>=3.9,<4`, and `microsoft-teams-apps==2.0.13.4`. Managed-runtime versions admit plugin selection and dependencies transactionally; do not mutate a committed dependency generation or assume an old in-checkout venv is the live runtime. This is a source plugin; it does not require packaging the entire flat repository with `pip install .`.

Merge, rather than replace, these settings into the selected profile's config:

```yaml
plugins:
  enabled:
    - hermes-teams
  disabled:
    - teams-platform
  isolation: in_process
platforms:
  teams:
    enabled: true
    extra:
      require_mention: true
      reactions: true
display:
  platforms:
    teams:
      streaming: true
```

Preserve all existing unrelated entries. `teams-platform` is the bundled manifest's name; `platforms/teams` is only its source path. Keep the platform identity `teams`. The tools are `teams_post` and `teams_files`, in toolset `hermes_teams`. Check any explicit tool allowlist/selection instead of assuming new tools are available.

For an existing profile, preserve its sender authorization. For a new profile, explicitly configure `TEAMS_ALLOWED_USERS` with the operator-approved users' Entra/AAD object IDs, or complete the installed Hermes version's supported pairing workflow before the channel test. For example, keep `TEAMS_ALLOWED_USERS=<approved-user-object-id>,<another-approved-user-object-id>` in the selected profile's protected configuration, replacing every placeholder. The adapter prefers the sender's AAD object ID. An empty new-profile allowlist does not mean all channel users are authorized. Do not enable all users merely to bypass a pairing/allowlist failure. Mention gating and sender authorization are separate checks.

### Card skill discovery

Keep the repository's `skills/` directory in the installed release. The plugin registers `hermes-teams:teams-cards` through Hermes's supported skill API; no skill files need to be copied into Hermes source. After the normal plugin reload/restart, verify with these agent tool calls:

```text
skills_list(category="plugin")
skill_view(name="hermes-teams:teams-cards")
skill_view(name="hermes-teams:teams-cards", file_path="references/layout-recipes.md")
```

The first call should list the skill; the others should return its instructions and recipes. Incoming Teams messages prompt agents to consult it when composing cards. Registered plugin skills are available through these tools but are not included in Hermes's automatic system-prompt skill index.

If the operator wants automatic discovery from other channels or the CLI, optionally merge the installed skill directory into the selected profile's configuration, preserving existing entries:

```yaml
skills:
  external_dirs:
    - /absolute/path/to/installed/hermes-teams/skills
```

Replace that path with the discovered stable installation path. This adds the filesystem name `teams-cards` to the general skill index while the qualified plugin name remains available. Do not also copy the same skill into the profile's skills directory. Apply the normal service restart and use a fresh conversation to refresh the system index. `/reload-skills` can rescan filesystem skills and inform the next turn, but it does not reload plugin Python or replace an existing system prompt. See [Hermes skill guidance](https://hermes-agent.nousresearch.com/docs/guides/work-with-skills/).

### Future repository updates

For an unpinned checkout tracking `origin/main`, back up the current revision/configuration, inspect local edits, review/test the proposed revision in isolation, and run:

```console
hermes --profile <profile-name> plugins update hermes-teams
```

For this repository, the updater uses recorded Git provenance and the checkout's remote/upstream with `--ff-only`, rather than a manifest homepage. Older updaters mutate the checkout and can report caution/dependency warnings; a dangerous scan can disable the plugin. Transactional versions stage the candidate and dependencies before publication, and can **block caution findings** without an update-time acceptance prompt. New dependencies may require interactive consent. Initial `--no-deps` does not carry forward to updates.

If a staged update is blocked by reviewed caution findings, retain the backup, inspect the complete scan, and use the supported force-reinstall path after candidate checks. When `plugins install --help` lists `--yes-deps`:

```console
hermes --profile <profile-name> plugins install <recorded-source-URL> --force --yes-deps --no-enable
```

Use the same recorded source, omit unsupported flags on older versions, and retain any explicit pin with the intended new full `--ref`. `--force` accepts caution and replacement, never a dangerous verdict; keep the scanner enabled. On active transactional replacements, `--no-enable` preserves selection and avoids the install command's immediate activation request. It does not disable an already active plugin. Check local-file preservation and the resulting commit.

Run the isolated compatibility checks in step 5 against the installed result before the intended service reload, then verify shared profiles and the selected adapter. Some install/enable/dashboard paths notify a running gateway immediately; inspect the installed implementation and avoid immediate-enable actions before preflight. This runbook creates no scheduler or automatic-apply setting, and does not rely on a core upgrade to update the plugin. See [the update procedure](INSTALL.md#update-from-the-repository) for version differences and pinned installations.

## 4. Provision or verify Microsoft resources

Follow [NEW_BOT.md](NEW_BOT.md) using the agreed tenant and identity. Keep Entra client ID, Entra object ID, Teams manifest ID, and catalog ID distinct. The complete tested setup uses one bot/Graph identity, explicitly approved application `Sites.ReadWrite.All`, and installed-team `ChannelMessage.Read.Group`. Obtain actual administrator consent and verify the result. Selected-site access is an advanced deployment requiring additional channel-folder discovery authorization; the current package builder does not complete that setup. If the operator selects restricted access, resolve and verify that route before activating file features rather than silently broadening permissions.

Store these profile-scoped secrets/settings:

```text
TEAMS_CLIENT_ID=<Entra application/client ID>
TEAMS_CLIENT_SECRET=<protected credential value>
TEAMS_TENANT_ID=<tenant GUID>
```

Leave all `MSGRAPH_*` absent to use the same identity. A separate Graph identity is advanced: the complete `MSGRAPH_CLIENT_ID`, `MSGRAPH_CLIENT_SECRET`, `MSGRAPH_TENANT_ID` triple supplies authentication only. The builder binds RSC to the bot identity, so that grant does not transfer to another Graph caller. A separate caller needs a separately reviewed Teams app association/install/consent flow granting its own message-read access and file permissions before use. This guide does not automate that advanced flow. Never combine parts of two identities.

Configure Azure Bot's real messaging endpoint, enable Microsoft Teams, and have the owner accept the channel publication terms. Leave the Azure WebSocket streaming switch off for this HTTP adapter. Keep the actual ingress port/route from discovery: shared gateways can expose a profile route at a different port than a standalone adapter.

Build the generic package using user-provided IDs, publisher URLs, and PNG icons. Publish through the approved catalog route, install into the designated team, and verify `ChannelMessage.Read.Group` consent for that team. Private/shared channels need the additional channel installation and storage checks.

## 5. Validate before normal use

Use a temporary test profile, not the production profile, for compatibility tests:

```console
<Hermes-runtime-python> scripts/test_with_hermes.py --hermes-source <Hermes-source-path>
```

The runner creates an isolated home and the integration tests block unexpected network access. Resolve the live interpreter and selected dependency environment first; a managed Hermes launcher can use a different interpreter from the checkout's old `venv/`. Use the selected environment's Python so the runner's subprocess inherits the correct dependencies. Install test-only dependencies into a separate directory/environment for that same Python version if needed; `--test-deps <directory>` can supply them. Do not print production environment variables when debugging a failed test.

Validate a sample card with `hermes teams-post ... --validate-only` in the selected configured context. This validates JSON; it does not establish visual Teams rendering or network access.

Apply the reviewed release/configuration through the **existing service's supported reload**. Where a user systemd `hermes-gateway.service` supports it, use `systemctl --user reload hermes-gateway.service`. Verify this service/unit first; do not guess it or start an additional gateway. Check every shared profile reconnects after a shared reload.

Perform these tests in the authorized test channel using harmless data:

1. Send a real user mention, selecting the bot from the Teams mention list. Confirm an agent reply in that same thread.
2. Attach a small test file to a new mention; confirm usable bytes, not just its name/link.
3. Request a specific older message or thread attachment through `teams_files`; confirm the intended file.
4. Upload a generated file; confirm the returned message ID, channel folder, intended-user access, and downloaded byte/hash match.
5. Send report/status/document cards; inspect layout and buttons in the required Teams clients.
6. Request a longer reply; verify edits and the final response stay in the originating thread. Check configured reactions.

Capture only minimal private evidence: test timestamps, message IDs, sizes/hashes, scope, and success/failure. Record untested scenarios explicitly, including personal consent, large files, and private/shared channels if they were not exercised. Never call local/CI checks proof of live Microsoft delivery.

## 6. Diagnose and hand over

An incoming HTTP receipt followed by 401 points to authentication. SDK dispatch followed by a mention-required drop points to mention identity. No receipt requires investigating Teams delivery, Azure channel diagnostics, endpoint routing, and edge filtering. Resource logs are not retroactive; enable approved diagnostics before a fresh reproduction. Do not disable authentication or broaden Graph permissions to fix a transport failure.

Keep `file_targets` mappings profile-owned; use the real team group GUID and channel ID. The plugin can learn them from an authorized incoming activity, or an operator can supply verified values. A `19:...` conversation ID is not a Graph team GUID.

Hand over the installed plugin/Hermes versions, selected profile and identity, verified permissions, completed tests, outstanding limitations, secret-rotation responsibility, and private rollback location. For rollback, restore the selected profile's former configuration/identity and reload its existing service; preserve unrelated profiles, uploaded files, and messages. Removing a plugin does not revoke cloud permissions.
