# Tool reference

These contracts describe the `hermes-teams` plugin. Use the actual installed tool schema if a later version differs. This reference does not supply credentials or destination IDs.

## `teams_post`

```json
{
  "chat_id": "19:CHANNEL-ID@thread.tacv2",
  "reply_to": "ROOT-MESSAGE-ID",
  "post": {
    "template": "status",
    "title": "Review in progress",
    "summary": "The draft is ready for review; feedback is outstanding.",
    "status": "In progress"
  }
}
```

The destination values above are placeholders. Omit `reply_to` for an intended new post. Preserve a thread suffix already present in `chat_id`; if both routing forms are supplied, they must refer to the same known thread. Do not invent a root ID from the current reply's ID.

`post` supports only these fields; every textual value, including fact values, is a string:

| Field | Requirement or limit |
| --- | --- |
| `template` | Required: `report`, `status`, or `document`. |
| `title` | Required; 1–200 characters, one line. |
| `summary` | Optional; up to 2,000 characters. |
| `status` | Optional; up to 80 characters, one line. |
| `facts` | Up to 12 `{label, value}` objects. Label: 1–80 characters, one line; value: 1–500 characters. |
| `sections` | Up to 6 objects. Optional `title`: up to 120 characters, one line; required `text`: 1–2,000 characters. |
| `actions` | Up to 3 `{label, url}` objects. Label: 1–64 characters, one line; URL: up to 2,048 characters. |

Actions accept durable HTTPS URLs on a public host with no embedded credentials or signed token parameters. Use the authenticated SharePoint web link, not an upload-session URL or temporary download URL. URL validation does not establish that the intended recipient has access.

The renderer emits Adaptive Card 1.4 with escaped literal text and `Action.OpenUrl` only. Its 28 KiB UTF-8 budget includes the message envelope and fallback text, so staying within each field limit does not guarantee the combined post fits. The same content is available as plain-text fallback. Do not supply the rendered Adaptive Card object as `post`.

## `teams_files`

To deliver a local channel file:

```json
{
  "action": "upload",
  "chat_id": "19:CHANNEL-ID@thread.tacv2",
  "path": "/absolute/path/to/report.pdf",
  "filename": "report.pdf",
  "caption": "The report contains the findings and recommended next steps.",
  "root_id": "ROOT-MESSAGE-ID"
}
```

Use a real existing path visible to the plugin's host, not an attachment label or a path on a different computer. `filename`, `caption`, and `root_id` are optional. This action posts its own document card with an Open file button; it is not an upload-only primitive. The tool does not expose a `post` field for customizing that automatically generated card.

To retrieve files for analysis, use `action: download`, `chat_id`, and `message_id`; add the correct `root_id` for a reply, or `include_thread: true` to include files from the specified root and its replies. Inspect returned paths/types before claiming to understand a file. Downloading is not a request to redistribute it. Group-chat file uploads and standalone personal consent are outside this channel tool.

Use an already supplied, cached attachment before fetching it again. Downloads return `files: [{path, mime_type, kind, name}]`, possibly empty; an empty result is not proof that a file was read. This operation retrieves Graph reference attachments, not arbitrary URLs or OpenUrl buttons on document cards. Thread retrieval is bounded to 100 messages and 20 reference attachments without a truncation indicator, so do not describe a long thread's result as exhaustive. `include_thread` is a boolean, not the string `"true"`.

## Results and recovery

Posts and uploads return `success`, `message_id`, and `error`. They do **not** normally return a separate `file_url`. Download results return `success` and `files`, or an error.

| Observation | Interpretation and next step |
| --- | --- |
| `success: true` with `message_id` | The plugin has a delivery receipt. Avoid a duplicate card or repeated final answer. |
| Validation failure before send | Correct the input, shorten the content, or explain the unsupported request. |
| Connection error, accepted request without receipt, or unreadable receipt | Delivery is unconfirmed. Inspect the channel through an available authorized read mechanism; do not blindly retry. |
| “File uploaded, but the Teams post failed. File: …” | A file exists. Preserve its durable URL; verify whether a message exists before retrying only the card. Do not repeat the upload. |
| Missing tools or configuration | Provide a draft and identify the missing capability. Do not bypass the plugin with new credentials, cloud permissions, or an unrelated webhook. |

There is no generic read-message, edit-card, or upload-only action in these two tools. Use a separately available authorized read mechanism to verify uncertain delivery. If none is available, report the uncertainty and preserve the partial result for the user.
