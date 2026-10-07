"""External Microsoft Teams platform plugin for Hermes Agent."""

from pathlib import Path


def register(ctx):
    from .adapter import register as register_adapter
    from .plugin_tools import register_tools

    register_adapter(ctx)
    register_tools(ctx)
    description = "Compose Teams reports, status updates, and file cards."
    ctx.register_skill(
        "teams-cards",
        Path(__file__).parent / "skills" / "teams-cards" / "SKILL.md",
        description=description,
        frontmatter={"name": "teams-cards", "description": description},
    )
