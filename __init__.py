"""External Microsoft Teams platform plugin for Hermes Agent."""


def register(ctx):
    from .adapter import register as register_adapter
    from .plugin_tools import register_tools

    register_adapter(ctx)
    register_tools(ctx)
