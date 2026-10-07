"""Load this directory as a package without installing or importing the gateway."""
import importlib.util
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if os.environ.get("HERMES_TEST_SOURCE"):
    sys.path.insert(0, os.environ["HERMES_TEST_SOURCE"])
spec = importlib.util.spec_from_file_location(
    "hermes_teams", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
