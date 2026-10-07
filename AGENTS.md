# Repository boundaries

This is an external Hermes platform plugin. Keep all implementation changes in this repository; never patch a deployed Hermes checkout to make this plugin work.

The plugin name is `hermes-teams`; its platform identity remains `teams`. Preserve profile-scoped credentials and shared gateway authorization. Disable the bundled plugin by its manifest name `teams-platform` when activating this plugin for a profile. `platforms/teams` is its source-directory path, not its canonical disable key; a legacy path entry may be retained alongside the correct manifest name.

Treat `upstream/` as immutable provenance, not executable plugin source. Record a new pinned snapshot separately when updating the port. Preserve upstream MIT attribution.

Run focused tests for changed behavior. Use `scripts/test_with_hermes.py` with the selected host's actual Python/runtime for compatibility tests; keep its source/configuration isolated. Unit and runtime tests do not establish live Teams delivery or Microsoft Graph permissions.

Keep real credentials, channel messages, signed transfer URLs, and production configuration out of this repository. Live posts require an intended test destination. Record deployment and rollback evidence separately from mocked tests.

For installation work, follow `docs/AGENT_INSTALL.md`. Keep customer deployment configuration and branded assets outside this repository. Public examples must use placeholders or reserved example domains.
