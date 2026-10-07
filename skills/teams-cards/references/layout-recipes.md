# Layout recipes

These examples are fictional `post` objects for drafting and validation. They are not evidence about a project. Replace the details and example URLs before publishing; omit unknown fields rather than inventing them. Wrap the chosen object with the actual `chat_id` and optional `reply_to` when calling `teams_post`.

## Report: conclusion, evidence, next steps

Use the accent header for a meaningful report title. Let the summary answer “what did we learn?” Put compact measures in the At a glance panel, then use sections for the evidence and decisions. Choose the sections that the material supports rather than filling a fixed checklist.

```json
{
  "template": "report",
  "title": "Pilot review: ready for a wider trial",
  "summary": "The pilot met its completion target. Two usability issues should be addressed before expanding the trial.",
  "status": "Ready",
  "facts": [
    {"label": "Completed sessions", "value": "18 of 20"},
    {"label": "Open issues", "value": "2 usability issues"}
  ],
  "sections": [
    {"title": "Findings", "text": "Participants completed the main task reliably. Navigation labels and the final confirmation caused confusion."},
    {"title": "Recommendation", "text": "Improve those two interactions, then repeat the trial with a broader group."}
  ],
  "actions": [
    {"label": "Read pilot report", "url": "https://example.com/reports/pilot"}
  ]
}
```

For a long report, carry the conclusion and a few material findings in the card and link to the full document. Do not paste the entire document, a Markdown table, or raw logs into a section.

## Status: state, impact, action

The status layout has a semantic status/summary panel. Put the blocker or milestone in the title, explain the impact, and give a concrete next step. Include an owner or deadline only when supplied or verified. A decision request can invite a reply in the thread; this card does not implement an approval form.

```json
{
  "template": "status",
  "title": "Release review needs a decision",
  "status": "Needs attention",
  "summary": "Implementation is complete. The release window still needs confirmation before scheduling deployment.",
  "facts": [
    {"label": "Current stage", "value": "Release review"}
  ],
  "sections": [
    {"title": "Decision needed", "text": "Confirm the proposed window in this thread, or suggest an alternative."},
    {"title": "Impact", "text": "Deployment remains unscheduled until the review is complete."}
  ],
  "actions": [
    {"label": "Review release notes", "url": "https://example.com/releases/draft"}
  ]
}
```

The renderer recognizes these status groups (case-insensitive):

- Good: `Complete`, `Completed`, `Done`, `Ready`, `Success`, `Successful`, `On track`.
- Warning: `Pending`, `In progress`, `Needs attention`, `Warning`, `At risk`.
- Attention: `Failed`, `Error`, `Blocked`.

Other wording keeps its exact text and uses a neutral style. The semantic color supports the words; it does not replace them or justify changing the facts. A normal short progress reply rarely needs a separate card.

## Document: identify the file and explain its value

Use this when the file is **already hosted** and its durable URL is known. The emphasized header describes the document, followed by optional File details and review notes. The action opens that existing file; it does not create an upload or grant access.

```json
{
  "template": "document",
  "title": "Pilot findings.pdf",
  "summary": "The findings and suggested changes are ready for review.",
  "facts": [
    {"label": "Format", "value": "PDF"},
    {"label": "Version", "value": "Review draft"}
  ],
  "sections": [
    {"title": "Review focus", "text": "Check the recommendations and add any missing observations."}
  ],
  "actions": [
    {"label": "Open findings", "url": "https://example.com/documents/pilot-findings.pdf"}
  ]
}
```

If the file is local, use `teams_files` upload instead. It creates the document card automatically and returns a delivery result, not normally the file's URL. Do not invent a URL or send the hosted-file recipe afterwards to decorate the successful upload. A separately requested companion report can summarize findings, but should add useful content rather than duplicate the document card.

## Choose a useful fallback

- “Make a card with approve/reject buttons”: explain that this post tool provides URL buttons only. Offer a status card asking for a response in the thread or linking to an actual approval system supplied by the user.
- “Turn this long technical table into a card”: summarize the decision and key values in facts; link or attach the full table as a real document when delivery is requested.
- “Stream the report as a series of cards”: use the gateway's ordinary streamed response, then one final card only if requested or useful. The post tool cannot update an existing card; do not create one card per chunk.
- “Just tell the channel the review is done”: use a short text message unless the user asks for a card or the context benefits from structured details.
