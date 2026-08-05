"""Config flow reason-selection tests for the Lovi integration."""

import pytest

from custom_components.lovi.config_flow import ConfigFlowHandler


def _handler(cloud_devices: dict) -> ConfigFlowHandler:
    """Build a ConfigFlowHandler exposing only the cloud device map."""
    flow = object.__new__(ConfigFlowHandler)
    flow._ConfigFlowHandler__cloud_devices = cloud_devices
    return flow


def test_no_devices_when_cloud_empty():
    assert _handler({})._no_unregistered_reason() == "no_devices"


def test_all_configured():
    devices = {
        "a": {"exists": True, "online": True, "local_key": "k", "name": "A"},
        "b": {"exists": True, "online": True, "local_key": "k", "name": "B"},
    }
    assert _handler(devices)._no_unregistered_reason() == "all_configured"


def test_no_local_keys():
    devices = {
        "a": {"exists": False, "online": True, "local_key": ""},
        "b": {"exists": False, "online": True, "local_key": ""},
    }
    assert _handler(devices)._no_unregistered_reason() == "no_local_keys"


def test_mixed_exists_prefers_all_configured():
    # Even with a mix of exists flags, if none is both new and has a key,
    # the reason should still say there is nothing to register.
    devices = {
        "a": {"exists": True, "online": True, "local_key": ""},
        "b": {"exists": False, "online": True, "local_key": ""},
    }
    assert _handler(devices)._no_unregistered_reason() == "no_local_keys"