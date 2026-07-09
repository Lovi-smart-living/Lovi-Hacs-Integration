import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .device_health import DeviceHealthMonitor
from .lovi_cloud import LoviCloud
from .token_manager import TokenManager

_LOGGER = logging.getLogger(__name__)


class LoviCoordinator:
    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self.cloud = LoviCloud(hass)
        self.token_manager = TokenManager(hass, self.cloud)
        self.health_monitor = DeviceHealthMonitor(hass, self.cloud)
        self._started = False

    async def async_setup(self):
        if DOMAIN not in self._hass.data:
            self._hass.data[DOMAIN] = {}

        try:
            await self.cloud.async_initialize()
        except Exception as e:
            _LOGGER.error("Failed to initialize cloud auth: %s", e)

        self._hass.data[DOMAIN]["coordinator"] = self
        self._hass.data[DOMAIN]["cloud"] = self.cloud

        try:
            await self.token_manager.async_start()
        except Exception as e:
            _LOGGER.error("Failed to start token manager: %s", e)

        try:
            await self.health_monitor.async_start()
        except Exception as e:
            _LOGGER.error("Failed to start health monitor: %s", e)

        self._started = True
        _LOGGER.info(
            "[COORDINATOR] Setup complete (auth=%s, token_manager=%s, health_monitor=%s)",
            self.cloud.is_authenticated,
            self.token_manager._running,
            self.health_monitor._running,
        )

    async def async_cleanup(self):
        _LOGGER.info("[COORDINATOR] Cleaning up Lovi coordinator")
        try:
            await self.token_manager.async_stop()
        except Exception as e:
            _LOGGER.error("Error stopping token manager: %s", e)
        try:
            await self.health_monitor.async_stop()
        except Exception as e:
            _LOGGER.error("Error stopping health monitor: %s", e)
        self._started = False

    async def async_refresh_all_devices(self) -> dict[str, Any]:
        if not self.cloud.is_authenticated:
            _LOGGER.warning("[COORDINATOR] Cannot refresh devices: cloud not authenticated")
            return {}
        try:
            devices = await self.cloud.async_get_devices()
            return devices
        except Exception as e:
            _LOGGER.error("[COORDINATOR] Failed to refresh devices: %s", e)
            return {}

    @property
    def is_started(self) -> bool:
        return self._started