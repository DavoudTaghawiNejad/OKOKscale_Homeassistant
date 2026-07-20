"""Shared pytest fixtures / import shims.

The pure logic modules (scale_parser, body_composition, assignment,
csv_logger's sync helpers) use ordinary intra-package relative imports
(`from .const import ...`), the same way the real
`custom_components/okok_scale/__init__.py` loads them inside Home
Assistant. That real __init__.py needs the `homeassistant` package
installed, but the pure modules themselves do not.

To unit test the pure modules without requiring `homeassistant` to be
installed, we register lightweight placeholder package objects in
sys.modules *before* anything imports a submodule by its dotted path.
Python then resolves `custom_components.okok_scale.<name>` against these
placeholders (which do nothing) instead of executing the real
custom_components/__init__.py / custom_components/okok_scale/__init__.py
files.

This ONLY happens when `homeassistant` isn't actually importable. This
conftest.py is loaded for every test in this directory regardless of
which file is targeted, so without this guard the placeholder would also
shadow the real package for tests/test_ha_integration.py, which needs
Home Assistant's own loader to import the *real* __init__.py (that's the
whole point of those tests - they caught a real bug that this shim's
blanket stubbing would have silently masked by replacing our component
with an empty stand-in).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CUSTOM_COMPONENTS_DIR = ROOT / "custom_components"
COMPONENT_DIR = CUSTOM_COMPONENTS_DIR / "okok_scale"


def _register_namespace_package(dotted_name: str, path: Path) -> None:
    if dotted_name in sys.modules:
        return
    module = types.ModuleType(dotted_name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[dotted_name] = module


if importlib.util.find_spec("homeassistant") is None:
    _register_namespace_package("custom_components", CUSTOM_COMPONENTS_DIR)
    _register_namespace_package("custom_components.okok_scale", COMPONENT_DIR)
