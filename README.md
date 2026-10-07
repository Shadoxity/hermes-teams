# Hermes Teams

An external Microsoft Teams plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent): channel file transfers, structured Adaptive Cards, reactions, and replies that update as the agent works. No Hermes source patches are required.

The plugin is **`hermes-teams`**, the platform remains **`teams`**, and its tools belong to **`hermes_teams`**. It replaces the bundled `teams-platform` plugin for the selected profile.

## Install

**Installing with an AI agent? Start with [the agent installation guide](docs/AGENT_INSTALL.md).** It covers discovery, the external plugin, Azure/Entra setup, Teams packaging and consent, verification, and rollback. Supply your own tenant, application, channel, and publisher settings.

For manual setup, follow [Hermes installation](docs/INSTALL.md) and [Teams/Azure registration](docs/NEW_BOT.md). Use a pinned release or commit, the selected Hermes runtime, and a test channel before enabling normal traffic. The tested SDK is `microsoft-teams-apps==2.0.13.4`; Python 3.11 or later is required.

## Features

| Capability | Behavior |
| --- | --- |
| Channel downloads | Download actual SharePoint file bytes from incoming attachments or a specified earlier message/thread. |
| Channel uploads | Upload to the channel's actual files folder and post a document card with an authenticated file link. |
| Formatted posts | Distinct report, status, and document layouts with headings, facts, sections, and HTTPS buttons. |
| Edited streaming | Update the original reply as output arrives, preserving its thread. |
| Reactions | Processing reactions and connector reaction operations, with an opt-out. |
| Personal files | Offer a file-consent card and transfer bytes after the intended recipient accepts. |
| Scheduled channel files | Use a saved channel mapping for standalone uploads and document posts. |

See [card examples and formatting](docs/CARDS.md), [implementation boundaries](docs/IMPLEMENTATION.md), and [validation](docs/VALIDATION.md). Automated tests and successful individual live operations do not establish every complete agent workflow or client presentation.

## Tools

`teams_post` accepts a `chat_id`, a structured `post`, and optional `reply_to`. The post requires `template` (`report`, `status`, or `document`) and `title`; optional fields include `summary`, `status`, `facts`, `sections`, and `actions`. See [report](examples/report.json), [status](examples/status.json), and [document](examples/document.json) inputs.

```json
{
  "chat_id": "19:CHANNEL-ID@thread.tacv2",
  "post": {
    "template": "status",
    "title": "Report ready",
    "status": "Complete",
    "summary": "The requested report is ready to review.",
    "actions": [{"label": "Open report", "url": "https://example.com/report"}]
  }
}
```

`teams_files` supports:

| Action | Fields |
| --- | --- |
| `download` | `chat_id`, `message_id`; optional `root_id` for a reply and `include_thread` for root/reply files. |
| `upload` | `chat_id`, local `path`; optional `filename`, `caption`, and `root_id`. |

An upload can succeed while its accompanying post fails. Inspect the returned result/file link before retrying. A local file path alone is not proof of delivery. Preserve `;messageid=<root-id>` when replying in an existing channel thread.

To validate a card without contacting Teams, use the enabled plugin's CLI:

```console
hermes teams-post --post-json examples/report.json --chat-id "19:CHANNEL-ID@thread.tacv2" --validate-only
```

This emits validated card JSON, not a Teams-rendered preview. Use real approved destinations and links before omitting `--validate-only`.

## Boundaries

- Channel files require Microsoft Graph permissions as well as a Teams bot installation. Ordinary bot messaging credentials do not grant SharePoint access.
- The plugin retains existing file access controls; it does not create public or organization-wide sharing links.
- Channel files default to a 100 MiB limit. Personal file consent defaults to 20 MiB and has separate bounded pending-byte, offer-count, and expiry limits.
- Cards use Adaptive Card 1.4, literal text and `Action.OpenUrl`; they have a 28 KiB envelope budget. Arbitrary actions/card JSON are not accepted by the post tool.
- Group-chat file uploads are not supported by the channel file tool. Private/shared channels require their own installation/storage verification.
- Opaque sharing URLs without a resolvable file path may be rejected. An unavailable attachment is reported as unavailable.
- Unmentioned channel chatter is not passively captured. Sender authorization precedes credentialed file retrieval.

## Development

```console
python -m pip install pytest pytest-asyncio httpx PyYAML
python -m pytest tests -q
```

Hermes integration tests use the real core interfaces and installed SDK. Run the isolated helper using the selected Hermes runtime:

```console
python scripts/test_with_hermes.py --hermes-source /path/to/hermes-agent
```

Keep deployment credentials, configuration, logs, generated Teams packages, and branded assets outside this repository. Follow [AGENTS.md](AGENTS.md) for development boundaries.

## Provenance

Derived from the NousResearch Hermes Teams adapter and pinned upstream proposals, with plugin-local file handling, rich posts, safety checks, and tests. See [porting provenance](docs/PORTING.md), [NOTICE](NOTICE), and [MIT license](LICENSE). Reference snapshots are not executable plugin source.
