# Register the bot and Teams application

This guide uses deployment placeholders only. Keep your real application IDs, tenant configuration, credentials, and generated package in a private deployment directory. Use [AGENT_INSTALL.md](AGENT_INSTALL.md) for an agent-oriented end-to-end procedure.

## Identity and permission choices

For a single organization, use a Single Tenant Entra application and Azure Bot registration. The Teams manifest's `bots[].botId` and `webApplicationInfo.id` are the Entra **application/client ID**. The manifest's top-level `id` is a separate **Teams app ID**. Entra object IDs and the organization catalog ID are different values.

Use the bot identity for Graph for the complete setup in this guide. The package builder binds `webApplicationInfo.id` to that bot client ID. Separate Graph credentials do not transfer this RSC grant: a different caller needs its own reviewed Teams app association, installation/consent, and file grants. That advanced deployment is not automated by the builder. [Microsoft RSC setup](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/grant-resource-specific-consent)

Choose the file access model with the tenant administrator:

| Model | Setup |
| --- | --- |
| Tenant-wide files: tested setup | Application `Sites.ReadWrite.All` with explicit tenant-administrator consent, using the same bot/Graph identity. This reaches sites beyond installed teams; installing or removing the Teams app does not narrow this grant. |
| Selected sites: advanced, additional setup required | Application `Sites.Selected`, administrator consent, and explicit read/write grants on intended sites are only the file-data part. The current builder does not grant the separate channel-folder discovery permission required by this client. Complete that authorization and verify it before enabling file features. |

The client always uses `GET /teams/{team-id}/channels/{channel-id}/filesFolder`. Microsoft's application-permissions table lists `File.Read.Group` among supported alternatives, but does not list `Sites.Selected`. A restricted deployment therefore needs a reviewed, consented discovery permission in addition to selected-site writes; the generated package currently requests only message-read RSC. Do not treat a site grant or a `file_targets` mapping as a replacement for this discovery authorization. Restricted/private/shared combinations need their own live validation. [filesFolder permission table](https://learn.microsoft.com/en-us/graph/api/channel-get-filesfolder?view=graph-rest-1.0)

`Sites.FullControl.All` is not needed for this plugin's file-data operations. Do not grant site-permission administration to the runtime bot. For channel-message lookup, declare application RSC `ChannelMessage.Read.Group` in the Teams package and consent for each installed host team. Bot posts/replies use the connector, not Graph migration permissions. [Graph permissions](https://learn.microsoft.com/en-us/graph/permissions-reference), [selected permissions](https://learn.microsoft.com/en-us/graph/permissions-selected-overview)

## Registration

1. Verify the intended tenant and Azure subscription. Create a new Entra application with an operator-chosen display name and **accounts in this organizational directory only**. Record its client ID, object ID, and tenant ID privately.
2. Configure the agreed Graph application permissions and obtain administrator consent. Verify granted permissions, not only the requested permission list. Assign any selected-site grants using a separate administrative identity.
3. Create a client credential and store its value only in the selected Hermes profile's protected secret configuration. Record expiry/rotation responsibility. Do not put it in commands, chat, logs, the Teams ZIP, or Git.
4. Create an Azure Bot using **Single Tenant**, the new client ID, and tenant ID. Set the actual public HTTPS messaging endpoint for that profile. For multiplex gateways, this may be `/p/<profile>/api/messages`; inspect the running route rather than assuming a port or path.
5. Enable the Microsoft Teams channel. The owner must review and accept Microsoft's channel publication terms; verify `acceptedTerms: true` and `isEnabled: true`. Provisioning success alone does not establish terms acceptance. The **Enable Streaming Endpoint** switch is unnecessary for this HTTP adapter; edited replies use ordinary message updates. [Azure registration](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-quickstart-registration?view=azure-bot-service-4.0), [Teams channel setup](https://learn.microsoft.com/en-us/azure/bot-service/channel-connect-teams?view=azure-bot-service-4.0)
6. Configure and load the plugin as described in [INSTALL.md](INSTALL.md). Preserve other profiles and keep one listener owner for each endpoint.

## Build the Teams package

Supply your own approved PNG icons: **192 × 192** color and **32 × 32** white outline with transparency. Supply real publisher, website, privacy, and terms URLs. There are no customer branding assets or generated deployment manifests in this repository.

```console
python scripts/build_teams_package.py --bot-id "<Entra-client-GUID>" --app-id "<Teams-app-GUID>" --display-name "Hermes Teams" --developer-name "<publisher-name>" --website-url "<HTTPS-website>" --privacy-url "<HTTPS-privacy>" --terms-url "<HTTPS-terms>" --color-icon "/private/assets/color.png" --outline-icon "/private/assets/outline.png" --output "/private/deployment/teams-app.zip"
```

The parent output directory must exist. The builder refuses to overwrite files. Optional `--full-name`, `--short-description`, and `--full-description` customize the package copy; `--version` defaults to `1.0.0`. The plugin's internal name stays `hermes-teams` regardless of the Teams display name.

The ZIP contains only `manifest.json`, `color.png`, and `outline.png`. The builder checks GUIDs, text bounds, URL syntax, PNG structure/checksums/dimensions, and the capability layout. It does not register identities, consent, verify URL ownership, or judge icon appearance. Validate and review the package using Teams Developer Portal. [Packaging](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/build-and-test/apps-package)

Manifest 1.25 declares personal/team bot scopes, `supportsFiles: true`, `ChannelMessage.Read.Group`, and `supportsChannelFeatures: "tier1"`. Group-chat scope is omitted because the channel file tools do not support it. `webApplicationInfo.resource` is `api://<bot-id>` for the required RSC field; this does not configure SSO. Keep app IDs stable when updating a package and increase its version.

## Publish, install, and verify

1. Use the tenant's approved custom-app/catalog distribution route. Administrative publishing permission belongs to the publishing identity, not the runtime bot.
2. Add the app to the intended test team and accept its channel-message RSC permission. Confirm the installed app and granted permission belong to the intended client ID.
3. For an organization-catalog app, use its **catalog ID**, not the manifest ID, in an install link:

   `https://teams.microsoft.com/l/app/<catalog-id>?tenantId=<tenant-id>`

   Find the catalog entry by `externalId` (the manifest ID), then use the returned `id`. [Deep-link rules](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/build-and-test/deep-link-application)
4. Standard channels inherit the host-team installation. Private/shared channels additionally require adding the app to the relevant channel, and may use separate SharePoint sites. A single configured tenant grant does not authorize a foreign tenant's sites. [Channel installation requirements](https://learn.microsoft.com/en-us/microsoftteams/platform/build-apps-for-shared-private-channels)
5. Reload the existing gateway through its supported lifecycle, then perform the [acceptance checks](VALIDATION.md): fresh bot mention, incoming file, old-message file, upload/download byte match, card, reactions, and a streamed reply. Verify access as the intended recipient.
6. If acceptance fails, restore the selected profile's protected baseline and reload its existing service. Do not delete messages or files as part of rollback. Review new grants separately; restoring configuration does not revoke Microsoft permissions.
