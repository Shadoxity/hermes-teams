#!/usr/bin/env python3
"""Run tests with an installed Hermes runtime in a throwaway user/profile home.

Usage (with the installed Hermes venv Python):
    python scripts/test_with_hermes.py --hermes-source /path/to/hermes-agent

The source is read-only. No live gateway, credentials, listeners or Teams posts
are needed; integration tests reject unexpected socket connections.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", required=True, type=Path)
    parser.add_argument("--test-deps", type=Path,
                        help="Absolute directory of test packages installed with pip --target outside Hermes")
    parser.add_argument("--integration-only", action="store_true")
    args, extra = parser.parse_known_args()
    source = args.hermes_source.resolve()
    if not (source / "gateway" / "platforms" / "base.py").is_file() or not (source / "hermes_constants.py").is_file():
        parser.error("--hermes-source must point to a Hermes source checkout")
    test_deps = args.test_deps
    if test_deps is not None:
        if not test_deps.is_absolute() or not test_deps.is_dir():
            parser.error("--test-deps must be an existing absolute directory")
        test_deps = test_deps.resolve()
    root = Path(__file__).resolve().parents[1]
    import_paths = [str(source), str(root)]
    if test_deps is not None:
        import_paths.insert(0, str(test_deps))
    with tempfile.TemporaryDirectory(prefix="hermes-teams-tests-") as temporary:
        sandbox = Path(temporary)
        user_home = sandbox / "user"
        profile = user_home / ".hermes"
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("plugins:\n  enabled: []\n  disabled: []\n", encoding="utf-8")
        env = os.environ.copy()
        for key in tuple(env):
            if key.startswith(("TEAMS_", "MSGRAPH_", "HERMES_PROFILE")):
                env.pop(key)
        env.update({
            "HOME": str(user_home), "USERPROFILE": str(user_home),
            "HERMES_HOME": str(profile), "HERMES_TEST_SOURCE": str(source),
            "HERMES_TEAMS_TEST_SANDBOX": str(sandbox), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(import_paths),
            "XDG_CONFIG_HOME": str(sandbox / "config"),
            "XDG_CACHE_HOME": str(sandbox / "cache"),
            "XDG_DATA_HOME": str(sandbox / "data"),
        })
        if test_deps is not None:
            env["HERMES_TEAMS_TEST_DEPS"] = str(test_deps)
        else:
            env.pop("HERMES_TEAMS_TEST_DEPS", None)
        selected = ["tests/test_adapter_integration.py"] if args.integration_only else ["tests"]
        command = [sys.executable, "-B", "-m", "pytest", *selected, "-q", "-o", "cache_dir=" + str(sandbox / "pytest-cache"), *extra]
        print("Testing the external Teams plugin with an isolated Hermes home; production configuration is not loaded.", flush=True)
        return subprocess.run(command, cwd=root, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
