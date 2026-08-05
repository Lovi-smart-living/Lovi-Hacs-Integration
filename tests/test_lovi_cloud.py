"""Tests for cloud device "already configured" detection."""

from custom_components.lovi.const import CONF_DEVICE_CID, CONF_DEVICE_ID, DOMAIN
from custom_components.lovi.lovi_cloud import (
    _configured_device_ids,
    _device_is_configured,
)


class _FakeEntry:
    def __init__(self, data):
        self.data = data


class _FakeConfigEntries:
    def __init__(self, entries):
        self._entries = entries

    def async_entries(self, domain):
        return self._entries if domain == DOMAIN else []


class _FakeHass:
    def __init__(self, entries):
        self.config_entries = _FakeConfigEntries(entries)


def _device(device_id, uuid="", node_id=""):
    return {"id": device_id, "uuid": uuid, "node_id": node_id}


def test_no_entries_means_not_configured():
    hass = _FakeHass([])
    assert _configured_device_ids(hass) == set()
    assert not _device_is_configured(_device("dev1"), set())


def test_matching_device_id_is_configured():
    hass = _FakeHass([_FakeEntry({CONF_DEVICE_ID: "dev1"})])
    ids = _configured_device_ids(hass)
    assert _device_is_configured(_device("dev1"), ids)
    assert not _device_is_configured(_device("dev2"), ids)


def test_removed_entry_no_longer_configured():
    hass = _FakeHass([_FakeEntry({CONF_DEVICE_ID: "dev1"})])
    ids = _configured_device_ids(hass)
    assert _device_is_configured(_device("dev1"), ids)

    hass = _FakeHass([])
    ids = _configured_device_ids(hass)
    assert not _device_is_configured(_device("dev1"), ids)


def test_subdevice_matches_by_uuid_or_node_id():
    entry = _FakeEntry({CONF_DEVICE_ID: "hub1", CONF_DEVICE_CID: "sub-uuid"})
    ids = _configured_device_ids(_FakeHass([entry]))
    assert _device_is_configured(_device("sub1", uuid="sub-uuid"), ids)
    assert _device_is_configured(_device("sub1", node_id="sub-uuid"), ids)
    assert not _device_is_configured(_device("sub1", uuid="other"), ids)


def test_configured_ids_combines_id_and_cid():
    entries = [
        _FakeEntry({CONF_DEVICE_ID: "a", CONF_DEVICE_CID: "a-cid"}),
        _FakeEntry({CONF_DEVICE_ID: "b"}),
    ]
    assert _configured_device_ids(_FakeHass(entries)) == {
        "a",
        "a-cid",
        "b",
    }
