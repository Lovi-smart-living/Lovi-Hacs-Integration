import logging
from datetime import timedelta
from ipaddress import ip_address
from typing import Any

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_PROTOCOL_VERSION,
    DEVICE_UNAVAILABLE_TIMEOUT,
    DOMAIN,
    DEFAULT_HEALTH_CHECK_INTERVAL,
)
from .helpers.config import get_device_id

_LOGGER = logging.getLogger(__name__)


class DeviceHealthMonitor:
    def __init__(self, hass: HomeAssistant, cloud):
        self._hass = hass
        self._cloud = cloud
        self._remove_periodic = None
        self._running = False

    async def async_start(self, check_interval: int = DEFAULT_HEALTH_CHECK_INTERVAL):
        self._running = True
        self._remove_periodic = async_track_time_interval(
            self._hass,
            self._async_periodic_check,
            timedelta(seconds=check_interval),
        )
        _LOGGER.debug(
            "Device health monitor started (check interval: %ds)",
            check_interval,
        )

    async def async_stop(self):
        self._running = False
        if self._remove_periodic is not None:
            self._remove_periodic()
            self._remove_periodic = None
        _LOGGER.debug("Device health monitor stopped")

    async def async_check_device(self, device_id: str) -> dict[str, Any] | None:
        if not self._cloud.is_authenticated:
            _LOGGER.warning("Cannot check device %s: cloud not authenticated", device_id)
            return None

        try:
            cloud_info = await self._cloud.async_get_device_info(device_id)
            if cloud_info is None:
                _LOGGER.warning("Device %s not found in cloud", device_id)
                return None

            domain_data = self._hass.data.get(DOMAIN, {})
            config_entry_data = None
            for entry_id, entry_data in domain_data.items():
                if isinstance(entry_data, dict) and entry_data.get("config_entry"):
                    config = entry_data["config_entry"]
                    if config.data.get("device_id") == device_id or config.data.get("device_cid") == device_id:
                        config_entry_data = config
                        break

            if config_entry_data is None:
                _LOGGER.debug("Device %s not registered locally, skipping health check", device_id)
                return None

            current_ip = config_entry_data.data.get(CONF_HOST, "")
            cloud_ip = cloud_info.get("ip", "")

            result = {
                "device_id": device_id,
                "name": cloud_info.get("name", ""),
                "cloud_ip": cloud_ip,
                "local_ip": current_ip,
                "online_in_cloud": cloud_info.get("online", False),
                "needs_ip_update": False,
            }

            if cloud_ip and cloud_ip != current_ip:
                _LOGGER.info(
                    "Device %s has different IP in cloud (%s) vs local (%s)",
                    device_id,
                    cloud_ip,
                    current_ip,
                )
                result["needs_ip_update"] = True

            return result
        except Exception as e:
            _LOGGER.error("Failed to check device %s: %s", device_id, e)
            return None

    async def async_on_device_disconnect(self, device_id: str) -> dict[str, Any] | None:
        _LOGGER.info("Device %s disconnected, checking cloud for updates", device_id)

        result = await self.async_check_device(device_id)
        if result and result.get("needs_ip_update"):
            _LOGGER.info(
                "Updating IP for device %s from %s to %s",
                device_id,
                result["local_ip"],
                result["cloud_ip"],
            )
            await self._async_update_device_ip(device_id, result["cloud_ip"])
            return result

        return None

    @staticmethod
    def _is_private_ip(addr: str) -> bool:
        try:
            return ip_address(addr).is_private
        except ValueError:
            return False

    async def _async_update_device_ip(self, device_id: str, new_ip: str):
        if not self._is_private_ip(new_ip):
            _LOGGER.warning(
                "Ignoring public IP %s for device %s — only private IPs are used for LAN communication",
                new_ip, device_id,
            )
            return
        domain_data = self._hass.data.get(DOMAIN, {})
        for entry_id, entry_data in domain_data.items():
            if isinstance(entry_data, dict) and entry_data.get("config_entry"):
                config_entry = entry_data["config_entry"]
                if config_entry.data.get("device_id") == device_id:
                    hass = self._hass
                    new_data = {**config_entry.data, CONF_HOST: new_ip}
                    hass.config_entries.async_update_entry(
                        config_entry,
                        data=new_data,
                    )

                    device_obj = entry_data.get("device")
                    if device_obj and hasattr(device_obj, "_api"):
                        device_obj._api.address = new_ip
                        _LOGGER.info("Updated local device %s address to %s", device_id, new_ip)

                    _LOGGER.info("Updated config entry for device %s IP to %s", device_id, new_ip)
                    break

    async def _async_periodic_check(self, _):
        if not self._running or not self._cloud.is_authenticated:
            return

        _LOGGER.debug("Running periodic device health check")

        domain_data = self._hass.data.get(DOMAIN, {})
        checked = 0
        updates = 0
        unavailable_recovered = 0

        for entry_data in domain_data.values():
            if isinstance(entry_data, dict) and entry_data.get("config_entry"):
                config_entry = entry_data["config_entry"]
                device_id = config_entry.data.get("device_id") or config_entry.data.get("device_cid")
                if not device_id:
                    continue

                # Check for IP updates from cloud
                result = await self.async_check_device(device_id)
                checked += 1

                if result and result.get("needs_ip_update"):
                    await self._async_update_device_ip(device_id, result["cloud_ip"])
                    updates += 1

                # Check if device has been unavailable for >= 60 seconds
                # and try fetching a new IP from the cloud
                device_obj = entry_data.get("device")
                if device_obj and hasattr(device_obj, 'is_unavailable') and device_obj.is_unavailable:
                    _LOGGER.warning(
                        "Device %s has been unavailable for >= %ds, checking cloud for new IP",
                        device_id,
                        DEVICE_UNAVAILABLE_TIMEOUT,
                    )
                    if result and result.get("cloud_ip") and result["cloud_ip"] != result.get("local_ip"):
                        await self._async_update_device_ip(device_id, result["cloud_ip"])
                        unavailable_recovered += 1
                        _LOGGER.info(
                            "Updated IP for unavailable device %s to %s and attempting recovery",
                            device_id,
                            result["cloud_ip"],
                        )

        if updates > 0 or unavailable_recovered > 0:
            _LOGGER.info(
                "Periodic health check: updated %d IPs, recovered %d unavailable devices (checked %d)",
                updates,
                unavailable_recovered,
                checked,
            )
        else:
            _LOGGER.debug("Periodic health check: all %d devices up to date", checked)
