# Implementation brief

An external Teams integration providing channel file transfers, edited replies, reactions, and structured posts through the supported Hermes plugin interfaces. Keep Hermes source unchanged. See [VALIDATION.md](VALIDATION.md) for the distinction between code tests and live acceptance.

## Package and compatibility boundary

- Package name: `hermes-teams`; manifest kind: `platform`.
- Keep the registered platform identifier **`teams`**, preserving existing configuration, routing and session identities.
- Root `__init__.py` delegates registration to the package's own `.adapter`; copy the required base `adapter.py` and `summary_writer.py` into this package.
- Use Hermes's supported `ctx.register_platform` replacement mechanism. Do not edit or monkeypatch Hermes modules, copy the package into the Hermes checkout, or rely on filesystem shadowing.
- Prove that the custom adapter is the selected factory after plugin loading, including the installed version's load order. Only one adapter should own each configured Teams endpoint.
- Use package-relative imports between custom modules. Imports of the public/shared Hermes interfaces can remain, but audit compatibility when Hermes is updated.
- Preserve profile-scoped credentials, settings and port selection. Never fall back to a different profile's process-global credentials.

## Required behavior

### Channel file downloads

1. Preserve the tenant, Graph team ID, channel ID, thread-root ID and activity/message ID from the incoming activity. Bot Framework conversation IDs and Graph team IDs are different identifiers; validate rather than guess.
2. Continue supporting existing direct attachments and personal-chat `file.download.info` payloads.
3. When a channel file is missing from the bot activity or appears as a SharePoint reference, fetch the correct Graph message. Replies require the root-message/replies route. Do not assume the visible reply ID is a root ID.
4. Resolve each file reference to the actual drive item and a downloadable resource. Preserve filename and type, enforce size limits, and feed verified bytes into Hermes's existing media cache/event flow.
5. Make downloads from an explicitly identified older message/thread possible without treating all unmentioned channel chatter as new requests. Keep broad passive observation out of this implementation.
6. Report missing permissions, unavailable messages, unsupported files and expired links clearly. Do not silently claim a file was read when only its name or link was available.

### Channel file uploads

1. Resolve the channel's actual `filesFolder` and use the returned drive/folder identifiers. Do not hard-code a general documents folder; private/shared channels can have different storage locations.
2. Upload bytes using a plugin-local Graph helper, using upload sessions when appropriate. Preserve the requested filename, enforce bounds, and choose a non-destructive collision policy such as renaming.
3. Post the resulting authenticated file link or file card through the existing bot into the original channel/thread. Return a useful delivery result and message ID.
4. Preserve inherited channel permissions. Do not create organization-wide or public sharing links automatically.
5. Keep generated text files downloadable when the user requests a file; inlining text is not a substitute for a successful upload.
6. Keep personal-chat FileConsent distinct from channel uploads. Do not assume group chats expose a channel-style `filesFolder` endpoint.
7. Cover both normal gateway delivery and any supported scheduled/standalone delivery path. If standalone file sending is not implemented, return a clear unsupported result instead of silently dropping media.

### Reactions and streaming

- Port reaction handling through the existing adapter lifecycle hooks and action surface; suppress bot-self events and retain the reaction opt-out setting.
- Add `edit_message(..., finalize=...)` using the installed gateway's existing send-then-edit consumer. Preserve optional routing metadata when accepted.
- Handle channel conversation IDs containing `;messageid=` correctly for update/delete/reaction operations without losing the original thread destination for sends.
- Coalesce duplicate edits, respect throttling and provide a clean final-send fallback when updates fail. Avoid duplicated final answers and misplaced continuation messages.
- Keep `draft_stream_is_message` false: this design edits ordinary bot activities and does not introduce a separate native streaming object.

### Rich posts

Reuse the adapter's Adaptive Card transport for a small set of deliberate layouts: update/status, document delivery, and report/summary. Support a title, concise sections, facts, links and file actions. Escape text and validate links/actions; do not accept arbitrary executable actions from generated content.

Decide explicitly how a streamed text preview becomes a final rich post. Preserve one understandable final result in the correct thread, with no duplicate preview or unnecessary replacement card. Ordinary short replies should remain simple text.

Expose rich posting through a supported plugin tool or adapter metadata contract, and provide accurate model-facing instructions. A private `_send_card` helper alone is not an agent-facing capability.

## Graph access and operational requirements

Use cached app-only Graph authentication with profile-scoped settings; keep bot/connector and Graph token audiences separate. Reuse safe shared interfaces or provide a local client extension for uploads rather than modifying `tools/microsoft_graph_client.py`.

Agree the intended teams/sites before granting access. Establish and verify the required channel-message read and file read/write permissions, using appropriately scoped consent where supported. Do not prescribe tenant-wide permissions solely because an upstream PR used them. Permission grants and Teams app-manifest consent are external configuration changes and must be tracked separately from code readiness.

Never log tokens, client secrets, upload-session URLs or preauthenticated download URLs. Enforce safe destinations and redirects, bounded transfers, retry limits and useful error results. The existing shared Graph download helper must not be assumed to follow `/content` redirects correctly without verification.

## Implementation sequence

1. Prove package discovery and same-platform replacement against the pinned Hermes version using a local/test configuration. Keep the copied baseline behavior intact at this stage.
2. Port reactions and streaming as isolated changes with focused tests.
3. Implement the plugin-local Graph client/helper and channel-file download/upload paths; omit passive observation and its related session mutations entirely.
4. Add rich-post templates, the agent-facing interface and prompt guidance.
5. Run the integration matrix below, then document configuration, installation, switch-over and rollback. Switch the production configuration only within the authorized deployment scope.

## Acceptance and evidence

Maintain separate evidence for **unit tests**, **plugin registration/configuration tests**, and **live Teams verification**.

Minimum automated coverage:

- Custom factory wins; built-in fallback/rollback remains available; package imports never bind custom helpers from the built-in package.
- Two profiles use their own identities/settings; missing Graph setup fails clearly.
- Root-message and reply attachments, HTML-only bot activities, multiple files, direct downloads, denied access and expired links.
- File upload target resolution, duplicate names, bounded transfers, upload failures and preserved thread placement.
- Streaming updates/finalization, channel IDs with thread suffixes, throttling, unsupported updates and no duplicate final send.
- Reactions, self-event suppression, opt-out behavior and rich-card payload validation.

Live verification in designated test channels must include: a newly attached document; a file on an earlier root/reply message; download then upload of a generated document; downloadable text output; a long streamed answer; processing reactions; and a rich file-delivery post. Verify actual accessible bytes and channel/thread placement, not merely HTTP success. Include private/shared channels if those are in scope.

Record the tested Hermes commit, custom plugin commit, SDK versions, account/profile, channel type and results without secrets. Any untested scenario stays explicitly unverified. Keep a straightforward rollback to the bundled adapter and preserve configuration backups.
