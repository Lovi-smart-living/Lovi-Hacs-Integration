import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_DEVICE_CID,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DOMAIN,
)
from .coordinator import LoviCoordinator
from .device import async_delete_device, get_device_id, setup_device
from .helpers.config import get_device_id as get_device_config_id
from .helpers.device_config import get_config
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)
NOT_FOUND = "Configuration file for %s not found"


async def async_migrate_entry(hass, entry: ConfigEntry):
    if entry.version == 1:
        return True
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    device_id = get_device_config_id(entry.data)
    _LOGGER.debug("Setting up entry for device: %s", device_id)

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    coordinator = hass.data[DOMAIN].get("coordinator")
    if coordinator is None:
        _LOGGER.info("[INIT] Coordinator not found, creating new one")
        coordinator = LoviCoordinator(hass)
        try:
            await coordinator.async_setup()
            _LOGGER.info("[INIT] Coordinator setup complete, cloud auth=%s, token_manager=%s, health_monitor=%s",
                         coordinator.cloud.is_authenticated,
                         coordinator.token_manager._running,
                         coordinator.health_monitor._running)
        except Exception as e:
            _LOGGER.error("[INIT] Failed to setup coordinator: %s", e)
        hass.data[DOMAIN]["coordinator"] = coordinator
    else:
        _LOGGER.info("[INIT] Existing coordinator found, cloud auth=%s", coordinator.cloud.is_authenticated)

    config = {**entry.data, **entry.options, "name": entry.title}
    try:
        device = await hass.async_add_executor_job(setup_device, hass, config)
        await device.async_refresh()
    except Exception as e:
        _cleanup_failed_device(hass, device_id)
        raise ConfigEntryNotReady("lovi device not ready") from e

    if not device.has_returned_state:
        _cleanup_failed_device(hass, device_id)
        raise ConfigEntryNotReady("lovi device offline")

    if coordinator.health_monitor and coordinator.health_monitor._running:
        device.set_health_monitor(coordinator.health_monitor)

    device_conf = await hass.async_add_executor_job(
        get_config,
        entry.data[CONF_TYPE],
    )
    if device_conf is None:
        _LOGGER.error(NOT_FOUND, config[CONF_TYPE])
        return False

    entities = set()
    for e in device_conf.all_entities():
        entities.add(e.entity)

    await hass.config_entries.async_forward_entry_setups(entry, entities)
    await async_setup_services(hass, entities)

    if entry.entry_id not in hass.data[DOMAIN]:
        hass.data[DOMAIN][entry.entry_id] = {}

    hass.data[DOMAIN][entry.entry_id]["config_entry"] = entry
    hass.data[DOMAIN][entry.entry_id]["device"] = device
    hass.data[DOMAIN][entry.entry_id]["tuyadevice"] = device._api
    hass.data[DOMAIN][entry.entry_id]["tuyadevicelock"] = device._api_lock

    entry.add_update_listener(async_update_entry)

    return True


def _cleanup_failed_device(hass: HomeAssistant, device_id: str):
    domain_data = hass.data.get(DOMAIN, {})
    stale = domain_data.pop(device_id, None)
    if not stale:
        return
    api = stale.get("tuyadevice")
    if api:
        api.set_socketPersistent(False)
        if api.parent:
            api.parent.set_socketPersistent(False)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    device_id = get_device_config_id(entry.data)
    _LOGGER.debug("Unloading entry for device: %s", device_id)
    config = entry.data
    domain_data = hass.data.get(DOMAIN, {})
    data = domain_data.get(device_id)
    if data is None:
        await async_delete_device(hass, config)
        return True

    device_conf = await hass.async_add_executor_job(
        get_config,
        config[CONF_TYPE],
    )
    if device_conf is None:
        _LOGGER.error(NOT_FOUND, config[CONF_TYPE])
        return False

    entities = {}
    for e in device_conf.all_entities():
        if e.config_id in data:
            entities[e.entity] = True

    for e in entities:
        await hass.config_entries.async_forward_entry_unload(entry, e)

    await async_delete_device(hass, config)
    domain_data.pop(device_id, None)

    if entry.entry_id in hass.data.get(DOMAIN, {}):
        del hass.data[DOMAIN][entry.entry_id]

    remaining_devices = sum(
        1 for k, v in hass.data.get(DOMAIN, {}).items()
        if isinstance(v, dict) and v.get("device") is not None
    )
    if remaining_devices == 0:
        coordinator = hass.data.get(DOMAIN, {}).get("coordinator")
        if coordinator:
            await coordinator.async_cleanup()
            hass.data[DOMAIN].pop("coordinator", None)
            hass.data[DOMAIN].pop("cloud", None)

    return True


async def async_update_entry(hass: HomeAssistant, entry: ConfigEntry):
    _LOGGER.debug("Updating entry for device: %s", get_device_config_id(entry.data))
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
