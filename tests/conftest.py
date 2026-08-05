"""Pytest configuration for Lovi integration tests."""

import sys
from pathlib import Path

# Make the custom_components package importable from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The `hass` fixtures come from pytest-homeassistant-custom-component, which
# registers itself as a pytest plugin via its `pytest11` entry point, so no
# `pytest_plugins` declaration is needed here.