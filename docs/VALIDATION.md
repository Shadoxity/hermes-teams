# Validation

Validation has three layers: isolated behavior tests, integration against the installed Hermes runtime, and live Microsoft acceptance. Passing one layer does not establish the others.

## Automated checks

Run the isolated suite with `python -m pytest tests -q`. Without Hermes core, the actual-runtime integration module is intentionally skipped. For runtime compatibility, use the selected Hermes Python with:

```console
python scripts/test_with_hermes.py --hermes-source /path/to/hermes-agent
```

The runner creates a temporary profile and strips Teams/Graph credentials from its test environment. Integration tests use actual Hermes/SDK classes with mocked network boundaries and reject unexpected connections. They verify external-plugin discovery, the bundled `teams-platform` disable gate, profile isolation, selected tools, typed activities, attachment authorization, and safe ingress diagnostics.

The generalized source passed **379 cases in 2.83 seconds** on 7 October 2026 against Hermes commit `d1998c30f7eae8a94e30f2396677fae30f22304c`, including **52 actual-runtime integration cases**, with Teams SDK 2.0.13.4. The separate local run passed **327 cases** with the runtime integration module skipped. The tested snapshot included the generic plugin identity, configurable Teams package, revised cards, strict mention matching, and summary-delivery profile/redaction fixes. The Hermes checkout remained unchanged.

An earlier baseline passed 314 against Hermes commit `9dcab1e440cdc482320af002e44036b947d6d709`; that earlier runtime has not been rerun against these latest changes. These are upstream source revisions, not deployment locations. Record a new result after future code changes rather than carrying this count forward automatically.

## Live acceptance matrix

Before public packaging, individual live operations were exercised in a private test environment: root Adaptive Card, threaded reply, same-message edits, reaction add/remove, document-card upload, exact-byte download, and an explicit historical native attachment retrieval. These observations do not establish the complete user-to-agent workflow, current release deployment, all channel types, or visual presentation in every Teams client. Deployment details and file/message identifiers are intentionally kept outside this repository.

Complete this matrix for each installation and record results privately:

| Scenario | Required observation |
| --- | --- |
| Fresh bot mention | An authorized user selects the actual bot; a reply appears in the same thread. |
| Newly attached file | Agent receives usable file bytes and identifies the correct content. |
| Older root/reply attachment | Explicit lookup retrieves the intended message's file through the correct route. |
| Multiple files and Unicode names | Each intended file is preserved without unsafe path construction. |
| Generated document upload | Correct channel folder, successful document post, intended-user access, exact download match. |
| Filename collision | Existing file is preserved according to the documented rename policy. |
| Large upload | Session completes within configured size limits; bytes and access are verified. |
| Missing permission or unavailable file | Useful failure with no false claim that contents were read or delivered. |
| Selected-site denial | An ungranted site is denied; this does not apply to deliberate tenant-wide access. |
| Edited streaming | Updates and final answer stay in the original thread without duplicate finals. |
| Reactions | Processing lifecycle, opt-out, and self-event suppression behave correctly. |
| Cards | Readable layout, fallback, and correct buttons on required desktop/web/mobile clients. |
| Personal file consent | Intended recipient controls transfer; wrong-user/chat, decline, expiry, and replay are rejected. |
| Reload/scheduled delivery | Saved mappings and selected profile are used after service reload. |
| Private/shared channels | Correct installation, site resolution, tenant, and membership boundaries. |

## Inbound diagnostics

The adapter logs bounded activity type/ID, HTTP completion status, SDK dispatch, and safe drop reasons. It excludes message text, attachment URLs, authentication headers, and exception bodies from these diagnostic entries. Keep operational metadata private.

Correlate a fresh test timestamp with gateway ingress and Azure Bot delivery logs. An outbound success or an unauthenticated HTTP probe does not prove incoming Teams delivery. Even an authenticated callback from a different Bot Framework channel verifies only that callback's route. Preserve this distinction when diagnosing an apparent silent bot.

## Release record

Record the plugin and Hermes commits, Python/SDK versions, automated commands/results, completed live cases, limitations, configured permission model, private backup location, and rollback procedure. Keep actual tenant/app/channel identifiers, hostnames, credentials, message contents, file hashes from customer data, and branded assets out of public release notes.
