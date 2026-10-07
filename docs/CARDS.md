# Structured Teams cards

`teams_post` and `hermes teams-post` accept the same structured post object. The renderer uses three distinct layouts without exposing arbitrary Adaptive Card JSON to the caller:

| Template | Layout | Use |
| --- | --- | --- |
| `report` | Accent header, separate summary, emphasized **At a glance** facts, then separated sections | Findings, reports, and weekly summaries |
| `status` | Neutral heading, a status/summary panel, details, then separated sections | Progress, blockers, and requests for attention |
| `document` | Emphasized file title/summary, **File details**, optional review notes, and supplied link buttons | A file that the reader can open using its durable URL |

Status words remain visible as text. Recognized statuses choose a semantic host style: for example, `Ready` uses `good`, `In progress` uses `warning`, and `Blocked` uses `attention`. An unfamiliar status uses neutral emphasis and retains the exact supplied wording. A document layout never invents a file URL or an action.

## Input and examples

Required fields are `template` and `title`. Optional fields remain `summary`, `status`, `facts` (`label`/`value`), `sections` (`title`/`text`), and `actions` (`label`/`url`). No new fields or tool-schema changes are needed. Text is literal: Markdown-looking input is escaped, while link buttons use validated HTTPS URLs.

Start with the [report](../examples/report.json), [status](../examples/status.json), or [document](../examples/document.json) example. Replace all example details and URLs before posting. The example links are not generated artifacts.

```console
hermes teams-post --post-json examples/report.json --chat-id "19:example@thread.tacv2" --validate-only
```

This prints card JSON without contacting Teams. It does not render a visual preview or prove Teams delivery. When deliberately sending, use the intended channel and remove `--validate-only`; retain the thread suffix or supply `--reply-to` when replying.

## Rendering boundaries

The implementation uses small native Python helpers for headers, containers, facts, sections, and fallback text. It emits Adaptive Card **1.4** elements and retains the existing constraints:

- One vertical flow with wrapping text, natural height, and no fixed columns, forced full width, external images, or truncated body text.
- At most twelve facts, six sections, and three `Action.OpenUrl` buttons. Existing text-field limits and HTTPS destination checks still apply.
- A **28 KiB UTF-8 budget** for the card plus its message envelope and repeated fallback content; added layout structure is included in that check.
- The same complete plain-text fallback reading order for all three templates: title, status, summary, facts, sections, and links. Empty content does not generate empty panels.

The host chooses the semantic colors; exact appearance can vary by client and theme. Microsoft recommends narrow-screen design, wrapping text, few columns, and a small set of actions. `Container` styling and grouping work within the existing schema version. Check the finished cards on Teams web/desktop and mobile, including dark mode; automated structure tests do not establish visual acceptance. [Microsoft card design guidance](https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/design-effective-cards), [Container schema](https://learn.microsoft.com/en-us/adaptive-cards/schema-explorer/container)

## Design references and provenance

The component-composition approach and styled-header idea were informed by the unrelated Ruby library [XING's `msteams_hermes`](https://github.com/xing/msteams_hermes/tree/f66c63a1b85478ed2052bfa4b34afd30757ac1af), specifically its [header/body example](https://github.com/xing/msteams_hermes/blob/f66c63a1b85478ed2052bfa4b34afd30757ac1af/complex_example.rb#L13-L73) and [Container representation](https://github.com/xing/msteams_hermes/blob/f66c63a1b85478ed2052bfa4b34afd30757ac1af/lib/msteams_hermes/components/container.rb#L16-L34). This renderer was independently implemented in Python; no Ruby source or dependency was imported, and delivery continues through the existing Teams bot adapter.

That repository's [MIT license](https://github.com/xing/msteams_hermes/blob/f66c63a1b85478ed2052bfa4b34afd30757ac1af/LICENSE.txt#L1-L21) identifies copyright **2022 Rafael Rocha**. If code or substantial portions are reused in a future change, preserve its copyright and permission notice along with the source/commit attribution. This design reference does not replace the existing Hermes upstream provenance.
