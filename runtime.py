"""Resolve profile-owned plugin data without hardcoding a Hermes home."""


def profile_home():
    from hermes_constants import get_hermes_home
    return str(get_hermes_home().resolve())
