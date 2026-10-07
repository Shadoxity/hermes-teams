# Installation and profile configuration

Install this external plugin for a selected Hermes profile without editing the Hermes checkout. Use [the agent guide](AGENT_INSTALL.md) for an end-to-end runbook and [NEW_BOT.md](NEW_BOT.md) for Microsoft setup.

## Preserve the working setup

For every affected Hermes profile, retain a dated copy of its current configuration, environment/secret configuration, service launch definition, enabled/disabled plugin list, and Hermes commit/version. Keep secret backups in the existing protected configuration location, outside this repository. Record the current bot identity, port, endpoint, and Teams app manifest without copying secret values into notes.

Do not overwrite bundled Hermes source files. Only one adapter may own each configured Teams endpoint. Profile configuration and credentials must stay independent when running more than one bot/profile.

## Install the external plugin

Install this repository's **root** as the user plugin directory `hermes-teams` under the selected profile's Hermes user-plugin location:

```text
<profile Hermes home>/plugins/hermes-teams/
    plugin.yaml
    __init__.py
    adapter.py
    cards.py
    files.py
    graph_files.py
    personal_files.py
    plugin_tools.py
    runtime.py
    summary_writer.py
    transport.py
```

The `upstream/` directory contains reference snapshots, not the package to install. Use the deployment host's approved repository access method and record the deployed commit.

Install dependencies into the **same Python environment used by that profile's Hermes gateway**. The tested and pinned SDK is `microsoft-teams-apps==2.0.13.4`, with compatible `aiohttp` and `httpx` dependencies from `pyproject.toml`. Follow that runtime's environment manager and check compatibility before changing dependencies. Python 3.11 or later is required.

Enable the custom plugin using Hermes's **`in_process` plugin execution mode**. The platform adapter and its gateway integration must run in the gateway process. Disable the bundled plugin by its manifest name **`teams-platform`** in that profile's plugin configuration. Its source directory is `platforms/teams`, which is not the canonical disable key. Keep the platform configuration key **`platforms.teams`** enabled; changing that key to `hermes-teams` would break existing routing and configuration.

Merge these entries into the selected profile's existing configuration, preserving unrelated enabled/disabled plugins and all existing Teams settings:

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
    # Retain the profile's existing extra settings and scoped credentials.
```

These lists illustrate the required entries; they are not instructions to replace the profile's complete plugin lists. If an existing configuration contains `platforms/teams`, preserve that legacy entry if needed and add `teams-platform`; the manifest-name entry is required to disable the bundled plugin reliably.

The integration suite verifies the actual bundled manifest disable gate and selection of the external custom factory. Check the selected profile configuration before reloading its gateway.

## Bot identity and scoped settings

Keep these in the selected profile's secret configuration:

```text
TEAMS_CLIENT_ID=<bot app ID>
TEAMS_CLIENT_SECRET=<bot secret>
TEAMS_TENANT_ID=<tenant GUID>
```

The adapter also accepts `client_id`, `client_secret`, and `tenant_id` in `platforms.teams.extra`; retain the existing secret mechanism rather than placing real secrets into a checked-in YAML file. Preserve an existing profile's allowlist and mention requirements. For a fresh profile, configure `TEAMS_ALLOWED_USERS` with operator-approved sender AAD object IDs or complete supported Hermes pairing; an absent allowlist is not an allow-all setting. Personal file-consent clicks are checked through the adapter's configured card-action authorization gate in addition to their bound conversation and intended recipient.

Graph can use the same application identity, or a separate profile-scoped application:

```text
MSGRAPH_CLIENT_ID=<Graph app ID>
MSGRAPH_CLIENT_SECRET=<Graph app secret>
MSGRAPH_TENANT_ID=<Graph tenant GUID>
```

Supply all three `MSGRAPH_*` values together. A partial Graph identity fails explicitly; it is never mixed with the bot's credentials. If all three are absent, Graph uses that profile's Teams identity. The generated package grants RSC to the bot identity only. A different Graph caller needs its own reviewed Teams app association/install/consent and file grants; credentials alone are insufficient. Treat that as an advanced deployment, not the default installation recipe. Bot connector tokens and Graph tokens have different audiences and are not interchangeable.

## Microsoft permissions

Verify permissions for the actual configured identity. The plugin does not create administrator consent, site grants, or Teams app installations.

Choose and record one file-access model with the tenant administrator:

| File-access model | Configuration and scope |
| --- | --- |
| **Tenant-wide files** | Microsoft Graph application **`Sites.ReadWrite.All`** with tenant-admin consent. Allows document/list-item operations across the tenant's site collections, including sites unrelated to installed teams. Installing/uninstalling the Teams app does not narrow/revoke this grant. No per-site grant is required under this model. |
| Selected sites: advanced | Application **`Sites.Selected`** with Entra consent and explicit read/write grants for each required site, plus separate channel-folder discovery authorization. The generated package does not complete this restricted setup. Separate private/shared channel sites need their own grants. |

The tested complete recipe uses the same bot/Graph identity with explicitly approved `Sites.ReadWrite.All` and installed-team message-read RSC. The client always calls `filesFolder`, whose application permission table lists `File.Read.Group` as an alternative but does not list `Sites.Selected`. A selected-site grant or explicit channel mapping therefore does not by itself complete the discovery path. Resolve and verify that advanced setup before activation. See [NEW_BOT.md](NEW_BOT.md#identity-and-permission-choices).

`Sites.FullControl.All` is not required for file-data operations. Do not grant permission-administration roles to the runtime bot. [Microsoft permission definitions](https://learn.microsoft.com/en-us/graph/permissions-reference#sitesreadwriteall)

Channel-message access and bot delivery remain separate from that file-data choice:

| Operation | Permission approach to verify |
| --- | --- |
| Read the designated team's channel messages/replies | Resource-specific `ChannelMessage.Read.Group`, declared/consented for the Teams app in that team, where supported. Avoid a tenant-wide message-read grant as a default. |
| Read/write SharePoint files | Use the chosen broad-file or restricted model above; verify actual consent and a byte-hash round trip. |
| Resolve the channel's `filesFolder` | Verify this endpoint under the chosen resource-scoped model. Its published permission table and the tenant's actual grants must be checked; a SharePoint site grant must not be assumed to cover every Teams discovery endpoint. |
| Post bot messages and cards | Use the existing Teams bot transport. Ordinary application-only Graph posting is not enabled by adding `Teamwork.Migrate.All`; that permission is for migration. |

For selected permissions, use a separate administrative identity to assign the site grants and test denial on an ungranted site. That denial expectation does not apply to the deliberately broad `Sites.ReadWrite.All` deployment. In both models, retain existing file access controls and avoid creating organization-wide or public sharing links.

Private/shared channel sites are separate from the parent team site. For selected access, grant each intended site explicitly. Install/add the Teams app in the relevant host team/channel and validate channel membership boundaries for either permission model. The plugin obtains the current file-folder location instead of assuming a `General` folder. The new package uses manifest 1.25 and `supportsChannelFeatures: "tier1"`; each channel type still requires live acceptance, and foreign-hosted shared-channel sites are outside the single configured tenant's grant.

References: [Teams file scopes](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/bots-filesv4), [channel message permissions](https://learn.microsoft.com/en-us/graph/api/channel-list-messages?view=graph-rest-1.0), [selected SharePoint permissions](https://learn.microsoft.com/en-us/graph/permissions-selected-overview), [filesFolder permissions](https://learn.microsoft.com/en-us/graph/api/channel-get-filesfolder?view=graph-rest-1.0), [channel app/storage boundaries](https://learn.microsoft.com/en-us/microsoftteams/platform/build-apps-for-shared-private-channels).

## Channel discovery and explicit targets

After enabling the plugin in a test profile, mention the bot in the intended channel. An inbound activity containing `team.aadGroupId` and `channel.id` lets the plugin capture the correct mapping. It persists mappings under:

```text
<profile Hermes home>/plugin-data/hermes-teams/channels.json
```

This mapping is needed for scheduled/standalone channel uploads as well as explicit message-file downloads. A Bot Framework `19:...` team/conversation identifier is **not** the Microsoft Graph team GUID. If the inbound activity lacks `aadGroupId`, configure a verified mapping:

```yaml
platforms:
  teams:
    enabled: true
    extra:
      file_targets:
        "19:CHANNEL-ID@thread.tacv2":
          team_id: "00000000-0000-0000-0000-000000000000"
          channel_id: "19:CHANNEL-ID@thread.tacv2"
      reactions: true
      stream_edit_interval: 1.0
```

Replace every placeholder. `team_id` is the team's Entra/Microsoft 365 group GUID; `channel_id` is the Graph channel ID. The mapping key is the bot conversation ID without `;messageid=...`. Do not fabricate a GUID from a Teams thread ID. Preserve the full thread suffix on actual message destinations so replies stay in the original thread.

For standalone posts, retain the profile's correct `service_url` if it differs from the default connector region. Only allowed Microsoft Teams connector service URLs are accepted.

## Enable edited streaming for Teams

The adapter implements message edits; Hermes still needs streaming enabled for the selected profile. Both inspected versions accept a Teams-only override, which preserves other platforms' existing streaming behavior:

```yaml
display:
  platforms:
    teams:
      streaming: true
```

Merge this into the selected profile's existing `display` mapping. The gateway's streaming settings control buffering and transport; retain `transport: auto` or use `edit`. `extra.stream_edit_interval` limits this adapter's edit frequency. Keep sender authorization and the chosen mention policy; a live full-response test is still required.

Leave Azure Bot's **Enable Streaming Endpoint** switch disabled. This adapter accepts ordinary HTTP activities and streams visible replies by editing messages; it does not implement Bot Framework WebSocket streaming extensions. Verify the Teams channel is enabled and its owner-approved publication terms show `acceptedTerms: true`.

## Size and consent controls

| `platforms.teams.extra` setting | Default | Notes |
| --- | --- | --- |
| `max_file_bytes` | Channel: 104857600; personal: 20971520 | A supplied value changes both paths. The personal single-upload path is capped at 52428800 bytes. |
| `personal_file_consent_ttl_seconds` | 900 | Maximum 1800; expired pending bytes are released even without another request. |
| `personal_pending_files` | 8 | Maximum 32; includes uploads in progress. |
| `personal_pending_bytes` | 41943040 | Maximum 83886080; includes uploads in progress. |

Personal consent needs an active, known personal conversation and intended recipient. It does not replace channel Graph permissions and is not available for standalone channel tooling. The recipient must accept before bytes are uploaded. Wrong-user, wrong-chat, wrong-card, repeated, declined, and expired interactions cannot authorize the transfer.

## Preview, test, and switch

Validate without posting:

```console
hermes teams-post --post-json examples/status.json --chat-id "19:example@thread.tacv2" --validate-only
```

For an intentional live post, use a real test-channel ID and replace example links, then omit `--validate-only`. Use `--reply-to <root-message-id>` or the established threaded conversation ID to target a thread. Check the returned success state/message ID and inspect the resulting Teams post.

Complete the integration checks, apply the selected profile configuration, and use its existing gateway service lifecycle. If multiple profiles share one service, verify every profile reconnects after reload. Where the existing user systemd unit supports a drain-aware reload:

```console
systemctl --user reload hermes-gateway.service
```

Verify the selected adapter, profile identity, actual endpoint, and every shared profile. Do not create a second listener for the same endpoint. For dedicated services, use their documented lifecycle. Reconnection alone is not incoming-message acceptance.

## Diagnose missing inbound messages

The adapter logs ingress receipt/completion, SDK dispatch, and mention-required drops using bounded metadata. These entries exclude message text, attachment URLs, authorization headers, and exception bodies. Keep operational metadata private.

Correlate one intentional test's timestamp with the endpoint's ingress records and Azure Bot's channel diagnostics. Receipt followed by HTTP 401 points to authentication; SDK dispatch followed by a mention-required drop points to the mention gate. No endpoint receipt leaves delivery, routing, and edge filtering to investigate. A successful outbound post or an unauthenticated probe does not prove an incoming Teams message reached the adapter.

Azure's channel **Issues** view and existing `ABSBotRequests` logs can help distinguish `ChannelToBot` from `BotToChannel` requests. Resource logs require diagnostic routing and are not collected retrospectively. [Microsoft troubleshooting](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-troubleshoot-general-problems?view=azure-bot-service-4.0), [Bot Service monitoring](https://learn.microsoft.com/en-us/azure/bot-service/monitor-bot-service?view=azure-bot-service-4.0)

## Rollback

1. Identify the affected profile and its existing service owner. For a shared gateway, preserve the other profiles and use its existing service lifecycle.
2. Restore the affected profile's saved plugin/configuration settings and saved bot identity/endpoint mapping: disable `hermes-teams`, re-enable bundled `teams-platform`, and retain `platforms.teams`. If manually reconstructing the old configuration, remove both the canonical `teams-platform` and any legacy `platforms/teams` disable entry for the bundled adapter. Restore the saved release symlink when rolling back to a prior custom release instead.
3. Restore the saved runtime/environment if dependency changes were made during deployment.
4. Run `systemctl --user reload hermes-gateway.service` when this is the verified service owner. Verify all shared profiles reconnect and the selected profile uses the intended restored `teams` factory, then check ordinary messaging. For a dedicated service elsewhere, use its documented lifecycle.

Keep uploaded SharePoint files and existing channel messages; rollback does not delete content. Leave plugin mapping data available for investigation. Review any newly granted Microsoft permissions separately with the tenant administrator; restoring a plugin configuration does not revoke them.
