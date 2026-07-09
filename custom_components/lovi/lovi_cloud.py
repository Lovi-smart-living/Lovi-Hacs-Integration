import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from tuya_sharing import (
    CustomerDevice,
    LoginControl,
    Manager,
    SharingDeviceListener,
    SharingTokenListener,
)

from .const import (
    CONF_DEVICE_CID,
    CONF_ENDPOINT,
    CONF_LOCAL_KEY,
    CONF_TERMINAL_ID,
    CONF_TOKEN_INFO,
    CONF_USER_CODE,
    DOMAIN,
    TUYA_CLIENT_ID,
    TUYA_RESPONSE_CODE,
    TUYA_RESPONSE_MSG,
    TUYA_RESPONSE_QR_CODE,
    TUYA_RESPONSE_RESULT,
    TUYA_RESPONSE_SUCCESS,
    TUYA_SCHEMA,
)

_LOGGER = logging.getLogger(__name__)

HUB_CATEGORIES = [
    "wgsxj",
    "lyqwg",
    "bywg",
    "zigbee",
    "wg2",
    "dgnzk",
    "videohub",
    "xnwg",
    "qtyycp",
    "alexa_yywg",
    "gywg",
    "cnwg",
    "wnykq",
]

STORAGE_VERSION = 1
STORAGE_KEY = "lovi_cloud_credentials"


@dataclass
class CloudAuthData:
    user_code: str
    terminal_id: str
    endpoint: str
    token_info: dict[str, Any]


class LoviCloud:
    def __init__(self, hass: HomeAssistant):
        _LOGGER.info("[LOVI_CLOUD] Initializing LoviCloud instance")
        self._hass = hass
        self._login_control = LoginControl()
        self._auth: CloudAuthData | None = None
        self._user_code: str | None = None
        self._qr_code: str | None = None
        self._error_code: str | None = None
        self._error_msg: str | None = None
        self._manager: Manager | None = None
        self._token_listener: TokenListener | None = None
        self._device_listener: DeviceListener | None = None
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        _LOGGER.info("[LOVI_CLOUD] LoviCloud initialized, auth=%s", self._auth is not None)

    async def async_initialize(self):
        _LOGGER.info("[LOVI_CLOUD] async_initialize called")
        try:
            await self._restore_cached_auth()
            _LOGGER.info("[LOVI_CLOUD] async_initialize complete, is_authenticated=%s", self.is_authenticated)
        except Exception as e:
            _LOGGER.error("[LOVI_CLOUD] async_initialize failed with exception: %s", e, exc_info=True)
            raise

    async def _restore_cached_auth(self):
        _LOGGER.info("[LOVI_CLOUD] _restore_cached_auth: attempting to load from Store")
        try:
            stored = await self._store.async_load()
            _LOGGER.info("[LOVI_CLOUD] Store load result: %s", stored is not None)
        except Exception as e:
            _LOGGER.error("[LOVI_CLOUD] Store async_load failed: %s", e, exc_info=True)
            stored = None

        if stored:
            _LOGGER.info("[LOVI_CLOUD] Found stored credentials with keys: %s", list(stored.keys()))
            self._auth = CloudAuthData(
                user_code=stored.get("user_code", ""),
                terminal_id=stored.get("terminal_id", ""),
                endpoint=stored.get("endpoint", ""),
                token_info=stored.get("token_info", {}),
            )
            self._user_code = stored.get("user_code", "")
            self._qr_code = stored.get("qr_code", "")
            domain_data = self._hass.data.get(DOMAIN, {})
            if domain_data:
                domain_data["auth_cache"] = stored
            _LOGGER.info("[LOVI_CLOUD] Successfully restored cloud auth from persistent storage")
            return

        _LOGGER.info("[LOVI_CLOUD] No stored credentials found, checking memory cache")
        domain_data = self._hass.data.get(DOMAIN, {})
        cached = domain_data.get("auth_cache")
        if cached:
            _LOGGER.info("[LOVI_CLOUD] Found credentials in memory cache")
            self._auth = CloudAuthData(
                user_code=cached.get("user_code", ""),
                terminal_id=cached.get("terminal_id", ""),
                endpoint=cached.get("endpoint", ""),
                token_info=cached.get("token_info", {}),
            )
            _LOGGER.info("[LOVI_CLOUD] Restored cloud auth from memory cache")
        else:
            _LOGGER.info("[LOVI_CLOUD] No cached credentials found anywhere (expected on first run)")

    async def _cache_auth(self):
        _LOGGER.info("[LOVI_CLOUD] _cache_auth called, auth exists=%s", self._auth is not None)
        if DOMAIN not in self._hass.data:
            self._hass.data[DOMAIN] = {}
        if self._auth:
            data = {
                "user_code": self._auth.user_code,
                "terminal_id": self._auth.terminal_id,
                "endpoint": self._auth.endpoint,
                "token_info": self._auth.token_info,
                "qr_code": self._qr_code or "",
            }
            self._hass.data[DOMAIN]["auth_cache"] = data
            try:
                await self._store.async_save(data)
                _LOGGER.info("[LOVI_CLOUD] Cloud auth saved to persistent storage successfully")
            except Exception as e:
                _LOGGER.error("[LOVI_CLOUD] Failed to save auth to persistent storage: %s", e, exc_info=True)

    def _clear_auth_cache(self):
        _LOGGER.info("[LOVI_CLOUD] _clear_auth_cache called")
        if DOMAIN in self._hass.data:
            self._hass.data[DOMAIN]["auth_cache"] = None
        try:
            self._hass.async_create_task(self._store.async_remove())
            _LOGGER.info("[LOVI_CLOUD] Auth cache cleared from storage")
        except Exception as e:
            _LOGGER.error("[LOVI_CLOUD] Failed to remove auth from storage: %s", e)

    def _ensure_manager(self):
        _LOGGER.info("[LOVI_CLOUD] _ensure_manager called, manager exists=%s, auth exists=%s", self._manager is not None, self._auth is not None)
        if self._manager is not None:
            return
        if not self._auth:
            _LOGGER.warning("[LOVI_CLOUD] _ensure_manager: no auth data available, cannot create manager")
            return
        self._token_listener = TokenListener(self._hass)
        try:
            self._manager = Manager(
                TUYA_CLIENT_ID,
                self._auth.user_code,
                self._auth.terminal_id,
                self._auth.endpoint,
                self._auth.token_info,
                self._token_listener,
            )
            self._device_listener = DeviceListener(self._hass, self._manager)
            self._manager.add_device_listener(self._device_listener)
            _LOGGER.info("[LOVI_CLOUD] Manager created successfully")
        except Exception as e:
            _LOGGER.error("[LOVI_CLOUD] Failed to create Manager: %s", e, exc_info=True)
            self._manager = None

    async def async_get_qr_code(self, user_code: str | None = None) -> bool:
        _LOGGER.info("[LOVI_CLOUD] async_get_qr_code called, user_code=%s", user_code)
        if not user_code:
            user_code = self._user_code
            if not user_code:
                _LOGGER.error("[LOVI_CLOUD] Cannot get QR code without a user code")
                self._error_code = None
                self._error_msg = "QR code requires a user code"
                return False

        try:
            response = await self._hass.async_add_executor_job(
                self._login_control.qr_code,
                TUYA_CLIENT_ID,
                TUYA_SCHEMA,
                user_code,
            )
            _LOGGER.info("[LOVI_CLOUD] QR code response received, success=%s", response.get(TUYA_RESPONSE_SUCCESS, False))
            if response.get(TUYA_RESPONSE_SUCCESS, False):
                self._user_code = user_code
                self._qr_code = response[TUYA_RESPONSE_RESULT][TUYA_RESPONSE_QR_CODE]
                _LOGGER.info("[LOVI_CLOUD] QR code obtained successfully")
                return True

            _LOGGER.error("[LOVI_CLOUD] Failed to get QR code: %s", response)
            self._error_code = response.get(TUYA_RESPONSE_CODE, "")
            self._error_msg = response.get(TUYA_RESPONSE_MSG, "Unknown error")
            return False
        except Exception as e:
            _LOGGER.error("[LOVI_CLOUD] Exception in async_get_qr_code: %s", e, exc_info=True)
            self._error_code = "EXCEPTION"
            self._error_msg = str(e)
            return False

    async def async_login(self) -> bool:
        _LOGGER.info("[LOVI_CLOUD] async_login called")
        if not self._user_code or not self._qr_code:
            _LOGGER.warning("[LOVI_CLOUD] Login attempted without successful QR scan")
            self._error_code = "NO_QR"
            self._error_msg = "Login attempted without QR code"
            return False

        try:
            success, info = await self._hass.async_add_executor_job(
                self._login_control.login_result,
                self._qr_code,
                TUYA_CLIENT_ID,
                self._user_code,
            )
            _LOGGER.info("[LOVI_CLOUD] Login result success=%s", success)
            if success:
                self._auth = CloudAuthData(
                    user_code=self._user_code,
                    terminal_id=info[CONF_TERMINAL_ID],
                    endpoint=info[CONF_ENDPOINT],
                    token_info={
                        "t": info["t"],
                        "uid": info["uid"],
                        "expire_time": info["expire_time"],
                        "access_token": info["access_token"],
                        "refresh_token": info["refresh_token"],
                    },
                )
                _LOGGER.info("[LOVI_CLOUD] Login succeeded, saving auth")
                await self._cache_auth()
                self._manager = None
                self._ensure_manager()
            else:
                _LOGGER.warning("[LOVI_CLOUD] Login failed: %s", info)
                self._error_code = info.get(TUYA_RESPONSE_CODE, "")
                self._error_msg = info.get(TUYA_RESPONSE_MSG, "Unknown error")
                self._clear_auth_cache()
                self._auth = None
            return success
        except Exception as e:
            _LOGGER.error("[LOVI_CLOUD] Exception in async_login: %s", e, exc_info=True)
            self._error_code = "EXCEPTION"
            self._error_msg = str(e)
            return False

    async def async_refresh_token(self) -> bool:
        _LOGGER.info("[LOVI_CLOUD] async_refresh_token called")
        if not self._auth:
            _LOGGER.warning("[LOVI_CLOUD] Cannot refresh token without existing auth")
            return False

        try:
            self._ensure_manager()
            if self._manager is None:
                _LOGGER.warning("[LOVI_CLOUD] Cannot refresh token: manager is None")
                return False

            # The tuya_sharing library handles token refresh automatically.
            # Trigger a device cache update which will auto-refresh the token
            # if needed, and the TokenListener will capture the new token info.
            _LOGGER.info("[LOVI_CLOUD] Triggering device cache update to refresh token if needed")
            try:
                await self._hass.async_add_executor_job(self._manager.update_device_cache)
            except Exception as cache_err:
                _LOGGER.warning("[LOVI_CLOUD] Device cache update during refresh: %s", cache_err)

            # Check if the token listener received updated token info
            if self._token_listener and self._token_listener.last_token_info:
                new_info = self._token_listener.last_token_info
                if new_info.get("access_token") != self._auth.token_info.get("access_token"):
                    self._auth.token_info = new_info
                    await self._cache_auth()
                    _LOGGER.info("[LOVI_CLOUD] Token refreshed successfully")
                    return True
                else:
                    _LOGGER.info("[LOVI_CLOUD] Token is still valid, no refresh needed")
                    return True

            # If listener wasn't triggered but the cache update succeeded,
            # the token is still valid - consider it a success
            _LOGGER.info("[LOVI_CLOUD] Token appears valid (no refresh triggered by library)")
            return True
        except Exception as e:
            _LOGGER.error("[LOVI_CLOUD] Token refresh failed with exception: %s", e, exc_info=True)
            return False

    async def async_get_devices(self) -> dict[str, Any]:
        _LOGGER.info("[LOVI_CLOUD] async_get_devices called")
        self._ensure_manager()
        if self._manager is None:
            _LOGGER.error("[LOVI_CLOUD] Cannot get devices without cloud authentication")
            return {}

        try:
            await self._hass.async_add_executor_job(self._manager.update_device_cache)
            _LOGGER.info("[LOVI_CLOUD] Device cache updated, device count=%s", len(self._manager.device_map))
        except Exception as e:
            _LOGGER.error("[LOVI_CLOUD] Failed to update device cache: %s", e, exc_info=True)
            return {}

        cloud_devices = {}
        domain_data = self._hass.data.get(DOMAIN, {})
        for device in self._manager.device_map.values():
            try:
                cloud_device = {
                    "category": device.category,
                    "id": device.id,
                    "ip": device.ip,
                    CONF_LOCAL_KEY: device.local_key
                    if hasattr(device, CONF_LOCAL_KEY)
                    else "",
                    "name": device.name,
                    "node_id": device.node_id if hasattr(device, "node_id") else "",
                    "online": device.online,
                    "product_id": device.product_id,
                    "product_name": device.product_name,
                    "uid": device.uid,
                    "uuid": device.uuid,
                    "support_local": device.support_local,
                    CONF_DEVICE_CID: None,
                    "version": None,
                    "is_hub": (
                        device.category in HUB_CATEGORIES
                        or not hasattr(device, "local_key")
                    ),
                }

                existing_id = domain_data.get(cloud_device["id"]) if domain_data else None
                existing_uuid = (
                    domain_data.get(cloud_device["uuid"]) if domain_data else None
                )
                existing = existing_id or existing_uuid
                cloud_device["exists"] = existing and existing.get("device")
                if hasattr(device, "node_id"):
                    index = "/".join([cloud_device["id"], cloud_device["node_id"]])
                else:
                    index = cloud_device["id"]
                cloud_devices[index] = cloud_device
            except Exception as e:
                _LOGGER.error("[LOVI_CLOUD] Failed to process device %s: %s", getattr(device, 'id', 'unknown'), e)

        _LOGGER.info("[LOVI_CLOUD] async_get_devices returning %d devices", len(cloud_devices))
        return cloud_devices

    async def async_get_device_info(self, device_id: str) -> dict[str, Any] | None:
        _LOGGER.info("[LOVI_CLOUD] async_get_device_info called for device %s", device_id)
        self._ensure_manager()
        if self._manager is None:
            _LOGGER.warning("[LOVI_CLOUD] Cannot get device info: manager is None")
            return None
        try:
            await self._hass.async_add_executor_job(self._manager.update_device_cache)
        except Exception as e:
            _LOGGER.error("[LOVI_CLOUD] Failed to update device cache for info: %s", e)
            return None
        device = self._manager.device_map.get(device_id)
        if device is None:
            _LOGGER.warning("[LOVI_CLOUD] Device %s not found in cloud", device_id)
            return None
        return {
            "id": device.id,
            "name": device.name,
            "ip": device.ip,
            "online": device.online,
            "local_key": device.local_key if hasattr(device, "local_key") else "",
            "category": device.category,
            "product_id": device.product_id,
            "product_name": device.product_name,
            "uid": device.uid,
            "uuid": device.uuid,
            "support_local": device.support_local,
            "node_id": device.node_id if hasattr(device, "node_id") else "",
        }

    async def async_get_datamodel(self, device_id: str) -> dict[str, Any] | None:
        _LOGGER.info("[LOVI_CLOUD] async_get_datamodel for device %s", device_id)
        self._ensure_manager()
        if self._manager is None:
            return None
        try:
            response = await self._hass.async_add_executor_job(
                self._manager.customer_api.get,
                f"/v1.0/m/life/devices/{device_id}/status",
            )
            if response.get("result"):
                response = response["result"]
            transform = []
            for entry in response.get("dpStatusRelationDTOS", []):
                if entry["supportLocal"]:
                    transform.append(
                        {
                            "id": entry["dpId"],
                            "name": entry["dpCode"],
                            "type": entry["valueType"],
                            "format": entry["valueDesc"],
                            "enumMap": entry["enumMappingMap"],
                        }
                    )
            return transform
        except Exception as e:
            _LOGGER.error("[LOVI_CLOUD] Failed to get datamodel: %s", e, exc_info=True)
            return None

    def logout(self):
        _LOGGER.info("[LOVI_CLOUD] logout called")
        self._clear_auth_cache()
        self._auth = None
        self._manager = None
        self._token_listener = None
        self._device_listener = None

    @property
    def is_authenticated(self) -> bool:
        return self._auth is not None

    @property
    def last_error(self) -> dict[str, Any] | None:
        if self._error_code is not None:
            return {
                TUYA_RESPONSE_MSG: self._error_msg,
                TUYA_RESPONSE_CODE: self._error_code,
            }
        return None

    @property
    def token_expire_time(self) -> int:
        if self._auth:
            return self._auth.token_info.get("expire_time", 0)
        return 0

    @property
    def auth_data(self) -> CloudAuthData | None:
        return self._auth


class DeviceListener(SharingDeviceListener):
    def __init__(self, hass: HomeAssistant, manager: Manager):
        self._hass = hass
        self._manager = manager

    def update_device(self, device: CustomerDevice, updated_status_properties: list[str] | None) -> None:
        _LOGGER.info("[DEVICE_LISTENER] Update for device %s (properties: %s)", device.id, updated_status_properties)

    def add_device(self, device: CustomerDevice) -> None:
        _LOGGER.info("[DEVICE_LISTENER] New device added: %s", device.id)

    def remove_device(self, device_id: str) -> None:
        _LOGGER.info("[DEVICE_LISTENER] Device removed: %s", device_id)


class TokenListener(SharingTokenListener):
    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self.last_token_info: dict[str, Any] | None = None

    def update_token(self, token_info: dict[str, Any]) -> None:
        self.last_token_info = token_info
        _LOGGER.info("[TOKEN_LISTENER] Token updated, new expire_time: %s", token_info.get("expire_time"))