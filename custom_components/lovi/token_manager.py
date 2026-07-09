import logging
import time as time_module
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import TOKEN_REFRESH_BUFFER_SECONDS

_LOGGER = logging.getLogger(__name__)


class TokenManager:
    def __init__(self, hass: HomeAssistant, cloud):
        self._hass = hass
        self._cloud = cloud
        self._refresh_task = None
        self._running = False

    async def async_start(self):
        self._running = True
        self._schedule_next_refresh()
        _LOGGER.info("[TOKEN_MANAGER] Token manager started, auth=%s, expire_time=%d",
                     self._cloud.is_authenticated,
                     self._cloud.token_expire_time)

    async def async_stop(self):
        self._running = False
        if self._refresh_task is not None:
            self._refresh_task()
            self._refresh_task = None
        _LOGGER.info("[TOKEN_MANAGER] Token manager stopped")

    def _schedule_next_refresh(self):
        if not self._running or not self._cloud.is_authenticated:
            return

        expire_time = self._cloud.token_expire_time
        now = time_module.time()

        time_until_expiry = max(0, expire_time - now)
        refresh_in = max(
            60,
            int(time_until_expiry - TOKEN_REFRESH_BUFFER_SECONDS),
        )

        _LOGGER.info(
            "[TOKEN_MANAGER] Next token refresh scheduled in %d seconds (expires in %d seconds)",
            refresh_in,
            int(time_until_expiry),
        )

        self._refresh_task = async_call_later(
            self._hass,
            refresh_in,
            self._async_refresh_callback,
        )

    async def _async_refresh_callback(self, _):
        if not self._running or not self._cloud.is_authenticated:
            return

        _LOGGER.info("[TOKEN_MANAGER] Attempting token refresh")
        success = await self._cloud.async_refresh_token()

        if success:
            _LOGGER.info("[TOKEN_MANAGER] Token refreshed successfully, new expire_time=%d",
                         self._cloud.token_expire_time)
            self._schedule_next_refresh()
        else:
            _LOGGER.warning("[TOKEN_MANAGER] Token refresh failed, retrying in 60 seconds")
            self._refresh_task = async_call_later(
                self._hass,
                60,
                self._async_refresh_callback,
            )