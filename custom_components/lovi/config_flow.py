import asyncio
import logging
from collections import OrderedDict
from typing import Any

import tinytuya
import voluptuous as vol
from homeassistant.config_entries import (
    CONN_CLASS_LOCAL_PUSH,
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    QrCodeSelector,
    QrCodeSelectorConfig,
    QrErrorCorrectionLevel,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import DOMAIN
from .const import (
    API_PROTOCOL_VERSIONS,
    CONF_DEVICE_CID,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    CONF_USER_CODE,
    DATA_STORE,
)
from .device import TuyaLocalDevice
from .helpers.config import get_device_id
from .helpers.device_config import get_config
from .helpers.log import log_json
from .lovi_cloud import LoviCloud

_LOGGER = logging.getLogger(__name__)
DEVICE_DETAILS_URL = (
    "https://lovi.tech/docs/finding-device-credentials"
)


class ConfigFlowHandler(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_LOCAL_PUSH

    def __init__(self) -> None:
        _LOGGER.info("[CONFIG_FLOW] Initializing ConfigFlowHandler")
        self.device = None
        self.data = {}
        self.cloud = None
        self.__qr_code: str | None = None
        self.__cloud_devices: dict[str, Any] = {}
        self.__cloud_device: dict[str, Any] | None = None
        self._auto_detected_protocol = None

    def init_cloud(self):
        _LOGGER.info("[CONFIG_FLOW] init_cloud called, cloud exists=%s", self.cloud is not None)
        if self.cloud is None:
            self.cloud = LoviCloud(self.hass)

    async def async_step_user(self, user_input=None):
        _LOGGER.info("[CONFIG_FLOW] async_step_user called, user_input=%s", user_input)
        errors = {}

        if self.hass.data.get(DOMAIN) is None:
            _LOGGER.info("[CONFIG_FLOW] Initializing hass.data[%s]", DOMAIN)
            self.hass.data[DOMAIN] = {}
        if self.hass.data[DOMAIN].get(DATA_STORE) is None:
            self.hass.data[DOMAIN][DATA_STORE] = {}

        if user_input is not None:
            mode = user_input.get("setup_mode")
            _LOGGER.info("[CONFIG_FLOW] User selected setup mode: %s", mode)
            try:
                if mode == "cloud" or mode == "cloud_fresh_login":
                    self.init_cloud()
                    if mode == "cloud_fresh_login":
                        _LOGGER.info("[CONFIG_FLOW] Fresh login requested, logging out first")
                        self.cloud.logout()
                        await self.cloud.async_initialize()
                    elif not self.cloud.is_authenticated:
                        _LOGGER.info("[CONFIG_FLOW] Cloud not authenticated, attempting initialize")
                        await self.cloud.async_initialize()

                    if self.cloud.is_authenticated:
                        _LOGGER.info("[CONFIG_FLOW] Cloud authenticated, fetching devices")
                        self.__cloud_devices = await self.cloud.async_get_devices()
                        _LOGGER.info("[CONFIG_FLOW] Found %d cloud devices", len(self.__cloud_devices))
                        return await self.async_step_choose_device()
                    return await self.async_step_cloud()
                if mode == "auto":
                    self.init_cloud()
                    if not self.cloud.is_authenticated:
                        _LOGGER.info("[CONFIG_FLOW] Auto mode: initializing cloud")
                        await self.cloud.async_initialize()
                    if self.cloud.is_authenticated:
                        _LOGGER.info("[CONFIG_FLOW] Auto mode: fetching devices")
                        self.__cloud_devices = await self.cloud.async_get_devices()
                        _LOGGER.info("[CONFIG_FLOW] Found %d devices for auto discovery", len(self.__cloud_devices))
                        return await self.async_step_auto_discover()
                    return await self.async_step_cloud()
                if mode == "manual":
                    _LOGGER.info("[CONFIG_FLOW] Manual mode selected")
                    return await self.async_step_local()
            except Exception as e:
                _LOGGER.error("[CONFIG_FLOW] async_step_user exception: %s", e, exc_info=True)
                if mode in ("cloud", "cloud_fresh_login"):
                    return await self.async_step_cloud()
                if mode == "auto":
                    return await self.async_step_cloud()
                errors["base"] = "unknown_error"

        fields: OrderedDict[vol.Marker, Any] = OrderedDict()
        fields[vol.Required("setup_mode")] = SelectSelector(
            SelectSelectorConfig(
                options=["auto", "cloud", "manual", "cloud_fresh_login"],
                mode=SelectSelectorMode.LIST,
                translation_key="setup_mode",
            )
        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(fields),
            errors=errors or {},
            last_step=False,
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        _LOGGER.info("[CONFIG_FLOW] async_step_cloud called, user_input=%s", user_input)
        errors = {}
        placeholders = {}
        self.init_cloud()

        if user_input is not None:
            try:
                _LOGGER.info("[CONFIG_FLOW] Getting QR code for user_code: %s", user_input.get(CONF_USER_CODE))
                success = await self.cloud.async_get_qr_code(user_input[CONF_USER_CODE])
                if success:
                    self.__qr_code = self.cloud._qr_code
                    _LOGGER.info("[CONFIG_FLOW] QR code obtained successfully")
                    return await self.async_step_scan()
            except Exception as e:
                _LOGGER.error("[CONFIG_FLOW] Exception in async_step_cloud: %s", e, exc_info=True)
                errors["base"] = "login_error"
                placeholders = {"msg": str(e)}
                return self.async_show_form(
                    step_id="cloud",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_USER_CODE, default=user_input.get(CONF_USER_CODE, "")
                            ): str,
                        }
                    ),
                    errors=errors,
                    description_placeholders=placeholders,
                )

            _LOGGER.warning("[CONFIG_FLOW] Failed to get QR code")
            errors["base"] = "login_error"
            placeholders = self.cloud.last_error or {}

        else:
            user_input = {}

        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USER_CODE, default=user_input.get(CONF_USER_CODE, "")
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        _LOGGER.info("[CONFIG_FLOW] async_step_scan called, user_input=%s", user_input)
        if user_input is None:
            _LOGGER.info("[CONFIG_FLOW] Showing QR code for scanning")
            return self.async_show_form(
                step_id="scan",
                data_schema=vol.Schema(
                    {
                        vol.Optional("QR"): QrCodeSelector(
                            config=QrCodeSelectorConfig(
                                data=f"tuyaSmart--qrLogin?token={self.__qr_code}",
                                scale=5,
                                error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                            )
                        )
                    }
                ),
            )

        self.init_cloud()
        try:
            _LOGGER.info("[CONFIG_FLOW] Attempting login after QR scan")
            if not await self.cloud.async_login():
                _LOGGER.warning("[CONFIG_FLOW] Login failed, getting new QR code")
                success = await self.cloud.async_get_qr_code()
                errors = {"base": "login_error"}
                placeholders = self.cloud.last_error or {}
                if success:
                    self.__qr_code = self.cloud._qr_code
                return self.async_show_form(
                    step_id="scan",
                    errors=errors,
                    data_schema=vol.Schema(
                        {
                            vol.Optional("QR"): QrCodeSelector(
                                config=QrCodeSelectorConfig(
                                    data=f"tuyaSmart--qrLogin?token={self.__qr_code}",
                                    scale=5,
                                    error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                                )
                            )
                        }
                    ),
                    description_placeholders=placeholders,
                )
        except Exception as e:
            _LOGGER.error("[CONFIG_FLOW] Login failed with exception: %s", e, exc_info=True)
            try:
                success = await self.cloud.async_get_qr_code()
            except Exception:
                success = False
            errors = {"base": "login_error"}
            placeholders = {"msg": str(e)}
            if success:
                self.__qr_code = self.cloud._qr_code
            return self.async_show_form(
                step_id="scan",
                errors=errors,
                data_schema=vol.Schema(
                    {
                        vol.Optional("QR"): QrCodeSelector(
                            config=QrCodeSelectorConfig(
                                data=f"tuyaSmart--qrLogin?token={self.__qr_code}",
                                scale=5,
                                error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                            )
                        )
                    }
                ),
                description_placeholders=placeholders,
            )

        try:
            _LOGGER.info("[CONFIG_FLOW] Login successful, fetching cloud devices")
            self.__cloud_devices = await self.cloud.async_get_devices()
            _LOGGER.info("[CONFIG_FLOW] Fetched %d cloud devices", len(self.__cloud_devices))
        except Exception as e:
            _LOGGER.error("[CONFIG_FLOW] Failed to fetch cloud devices after login: %s", e, exc_info=True)
            return self.async_abort(reason="cannot_connect")

        return await self.async_step_choose_device()

    async def async_step_auto_discover(self, user_input=None):
        _LOGGER.info("[CONFIG_FLOW] async_step_auto_discover called, user_input=%s", user_input)
        if user_input is not None:
            key = user_input.get("device_id")
            _LOGGER.info("[CONFIG_FLOW] Auto discover selected device key: %s", key)
            if not key:
                return self.async_abort(reason="no_device_selected")
            device_info = self.__cloud_devices.get(key)
            if not device_info:
                _LOGGER.warning("[CONFIG_FLOW] Device key %s not found in cloud devices", key)
                return self.async_abort(reason="no_device_selected")

            self.__cloud_device = device_info
            if device_info.get("ip"):
                self.__cloud_device["ip"] = ""
            return await self.async_step_search()

        unconfigured = []
        for key, info in self.__cloud_devices.items():
            if info.get("exists"):
                continue
            if not info.get("online"):
                continue
            if info.get(CONF_LOCAL_KEY):
                unconfigured.append(
                    SelectOptionDict(
                        value=key,
                        label=f"{info['name']} ({info['product_name']})",
                    )
                )

        _LOGGER.info("[CONFIG_FLOW] Found %d unconfigured online devices", len(unconfigured))
        if not unconfigured:
            _LOGGER.info("[CONFIG_FLOW] No unconfigured devices found")
            return self.async_abort(reason="no_devices")

        return self.async_show_form(
            step_id="auto_discover",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): SelectSelector(
                        SelectSelectorConfig(
                            options=unconfigured,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_choose_device(self, user_input=None):
        _LOGGER.info("[CONFIG_FLOW] async_step_choose_device called, user_input=%s", user_input)
        errors = {}
        if user_input is not None:
            try:
                device_choice = self.__cloud_devices[user_input["device_id"]]
                _LOGGER.info("[CONFIG_FLOW] Chosen device: %s (ip=%s, is_hub=%s)", device_choice.get("id"), device_choice.get("ip"), device_choice.get("is_hub"))
            except KeyError as e:
                _LOGGER.error("[CONFIG_FLOW] Invalid device selection: %s", e)
                errors["base"] = "unknown_error"
                device_choice = None

            if device_choice:
                if device_choice["ip"] != "":
                    if user_input["hub_id"] == "None":
                        device_choice["ip"] = ""
                        self.__cloud_device = device_choice
                        return await self.async_step_search()
                    else:
                        errors["base"] = "does_not_need_hub"
                else:
                    if user_input["hub_id"] != "None":
                        hub_choice = self.__cloud_devices[user_input["hub_id"]]
                        hub_choice["ip"] = ""
                        hub_choice[CONF_DEVICE_CID] = (
                            device_choice["node_id"] or device_choice["uuid"]
                        )
                        if device_choice.get(CONF_LOCAL_KEY):
                            hub_choice[CONF_LOCAL_KEY] = device_choice[CONF_LOCAL_KEY]
                        hub_choice["product_id"] = device_choice["product_id"]
                        self.__cloud_device = hub_choice
                        return await self.async_step_search()
                    else:
                        errors["base"] = "needs_hub"

        device_list = []
        for key in self.__cloud_devices.keys():
            device_entry = self.__cloud_devices[key]
            if device_entry.get("exists"):
                continue
            if device_entry[CONF_LOCAL_KEY] != "":
                if device_entry["online"]:
                    device_list.append(
                        SelectOptionDict(
                            value=key,
                            label=f"{device_entry['name']} ({device_entry['product_name']})",
                        )
                    )
                else:
                    device_list.append(
                        SelectOptionDict(
                            value=key,
                            label=f"{device_entry['name']} ({device_entry['product_name']}) OFFLINE",
                        )
                    )

        _LOGGER.debug(f"[CONFIG_FLOW] Device count: {len(device_list)}")
        if len(device_list) == 0:
            return self.async_abort(reason="no_devices")

        hub_list = []
        hub_list.append(SelectOptionDict(value="None", label="None"))
        for key in self.__cloud_devices.keys():
            hub_entry = self.__cloud_devices[key]
            if hub_entry["is_hub"]:
                hub_list.append(
                    SelectOptionDict(
                        value=key,
                        label=f"{hub_entry['name']} ({hub_entry['product_name']})",
                    )
                )

        _LOGGER.debug(f"[CONFIG_FLOW] Hub count: {len(hub_list) - 1}")

        fields: OrderedDict[vol.Marker, Any] = OrderedDict()
        fields[vol.Required("device_id")] = SelectSelector(
            SelectSelectorConfig(options=device_list, mode=SelectSelectorMode.DROPDOWN)
        )
        fields[vol.Required("hub_id")] = SelectSelector(
            SelectSelectorConfig(options=hub_list, mode=SelectSelectorMode.DROPDOWN)
        )

        return self.async_show_form(
            step_id="choose_device",
            data_schema=vol.Schema(fields),
            errors=errors or {},
            last_step=False,
        )

    @property
    def _device_name_placeholder(self) -> str:
        if self.__cloud_device and self.__cloud_device.get("product_name"):
            parts = []
            if self.__cloud_device.get("name"):
                parts.append(self.__cloud_device["name"])
            parts.append(self.__cloud_device["product_name"])
            return "**" + " — ".join(parts) + "**\n\n"
        return ""

    async def async_step_search(self, user_input=None):
        _LOGGER.info("[CONFIG_FLOW] async_step_search called, user_input=%s", user_input)
        if user_input is not None:
            dev_id = self.__cloud_device.get("id", "DEVICE_KEY_UNAVAILABLE")
            _LOGGER.info("[CONFIG_FLOW] Searching for device %s on network", dev_id)
            self.__cloud_device["ip"] = ""
            try:
                local_device = await self.hass.async_add_executor_job(
                    scan_for_device, self.__cloud_device.get("id")
                )
                _LOGGER.info("[CONFIG_FLOW] Network scan result: %s", local_device)
            except OSError as e:
                _LOGGER.error("[CONFIG_FLOW] Network scan failed with OSError: %s", e)
                local_device = {"ip": None, "version": ""}
            except Exception as e:
                _LOGGER.error("[CONFIG_FLOW] Network scan failed: %s", e, exc_info=True)
                local_device = {"ip": None, "version": ""}

            if local_device.get("ip"):
                _LOGGER.info("[CONFIG_FLOW] Found device at %s (version=%s)", local_device.get("ip"), local_device.get("version"))
                self.__cloud_device["ip"] = local_device.get("ip")
                self.__cloud_device["version"] = local_device.get("version")
                if not self.__cloud_device.get(CONF_DEVICE_CID):
                    self.__cloud_device["local_product_id"] = local_device.get("productKey")
            else:
                _LOGGER.warning("[CONFIG_FLOW] Could not find device %s on network", dev_id)
            return await self.async_step_local()

        return self.async_show_form(
            step_id="search",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._device_name_placeholder,
            },
            errors={},
            last_step=False,
        )

    async def async_step_local(self, user_input=None):
        _LOGGER.info("[CONFIG_FLOW] async_step_local called, user_input_keys=%s", list(user_input.keys()) if user_input else None)
        errors = {}
        devid_opts = {}
        host_opts = {"default": ""}
        key_opts = {}
        proto_opts = {"default": "auto"}
        polling_opts = {"default": False}
        devcid_opts = {}

        if self.__cloud_device is not None:
            devid_opts = {"default": self.__cloud_device.get("id")}
            host_opts = {"default": self.__cloud_device.get("ip")}
            key_opts = {"default": self.__cloud_device.get(CONF_LOCAL_KEY)}
            if self.__cloud_device.get("version"):
                proto_opts = {"default": str(self.__cloud_device.get("version"))}
            if self.__cloud_device.get(CONF_DEVICE_CID):
                devcid_opts = {"default": self.__cloud_device.get(CONF_DEVICE_CID)}

        if user_input is not None:
            _LOGGER.info("[CONFIG_FLOW] Testing connection with device_id=%s, host=%s, proto=%s, device_cid=%s",
                         user_input.get(CONF_DEVICE_ID), user_input.get(CONF_HOST),
                         user_input.get(CONF_PROTOCOL_VERSION), user_input.get(CONF_DEVICE_CID, "NOT_SET"))
            proto = user_input.get(CONF_PROTOCOL_VERSION)
            if proto != "auto":
                user_input[CONF_PROTOCOL_VERSION] = float(proto)
            if CONF_DEVICE_CID in user_input and not user_input[CONF_DEVICE_CID]:
                del user_input[CONF_DEVICE_CID]
            try:
                self.device = await async_test_connection(user_input, self.hass)
                _LOGGER.info("[CONFIG_FLOW] Connection test result: device=%s", self.device is not None)
            except Exception as e:
                _LOGGER.error("[CONFIG_FLOW] Connection test threw exception: %s", e, exc_info=True)
                self.device = None
            if self.device:
                self.data = user_input
                self._auto_detected_protocol = None
                if (
                    user_input.get(CONF_PROTOCOL_VERSION) == "auto"
                    and self.device._protocol_configured != "auto"
                ):
                    self._auto_detected_protocol = self.device._protocol_configured
                    self.data = {
                        **self.data,
                        CONF_PROTOCOL_VERSION: self._auto_detected_protocol,
                    }
                if self.__cloud_device:
                    if self.__cloud_device.get("product_id"):
                        self.device.set_detected_product_id(self.__cloud_device.get("product_id"))
                    if self.__cloud_device.get("local_product_id"):
                        self.device.set_detected_product_id(self.__cloud_device.get("local_product_id"))
                unique_id = user_input.get(CONF_DEVICE_CID, user_input[CONF_DEVICE_ID])
                _LOGGER.info("[CONFIG_FLOW] Setting unique_id=%s, checking if already configured", unique_id)
                try:
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()
                    _LOGGER.info("[CONFIG_FLOW] Unique ID not configured, proceeding to select type")
                except Exception as e:
                    _LOGGER.error("[CONFIG_FLOW] Unique ID check failed: %s", e, exc_info=True)
                    raise
                return await self.async_step_select_type()
            else:
                _LOGGER.warning("[CONFIG_FLOW] Connection test failed for device")
                errors["base"] = "connection"
                devid_opts["default"] = user_input[CONF_DEVICE_ID]
                host_opts["default"] = user_input[CONF_HOST]
                key_opts["default"] = user_input[CONF_LOCAL_KEY]
                if CONF_DEVICE_CID in user_input:
                    devcid_opts["default"] = user_input[CONF_DEVICE_CID]
                proto_opts["default"] = str(user_input[CONF_PROTOCOL_VERSION])
                polling_opts["default"] = user_input[CONF_POLL_ONLY]

        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID, **devid_opts): str,
                    vol.Required(CONF_HOST, **host_opts): str,
                    vol.Required(CONF_LOCAL_KEY, **key_opts): str,
                    vol.Required(
                        CONF_PROTOCOL_VERSION,
                        **proto_opts,
                    ): vol.In(["auto"] + [str(v) for v in API_PROTOCOL_VERSIONS]),
                    vol.Required(CONF_POLL_ONLY, **polling_opts): bool,
                    vol.Optional(CONF_DEVICE_CID, **devcid_opts): str,
                }
            ),
            description_placeholders={
                "device_details_url": DEVICE_DETAILS_URL,
                "device_name": self._device_name_placeholder,
            },
            errors=errors,
        )

    async def async_step_select_type(self, user_input=None):
        _LOGGER.info("[CONFIG_FLOW] async_step_select_type called, user_input=%s", user_input)
        if user_input is not None:
            parts = user_input[CONF_TYPE].split("||", 2)
            self.data[CONF_TYPE] = parts[0]
            if len(parts) > 1 and parts[1]:
                self.data[CONF_MANUFACTURER] = parts[1]
            if len(parts) > 2 and parts[2]:
                self.data[CONF_MODEL] = parts[2]
            _LOGGER.info("[CONFIG_FLOW] User selected type: %s (manufacturer=%s, model=%s)",
                         self.data[CONF_TYPE], self.data.get(CONF_MANUFACTURER), self.data.get(CONF_MODEL))
            return await self.async_step_choose_entities()

        _LOGGER.info("[CONFIG_FLOW] Determining device type automatically")
        all_matches = []
        best_match = 0
        best_matching_type = None
        best_matching_key = None

        try:
            if not self.device:
                _LOGGER.error("[CONFIG_FLOW] self.device is None, cannot determine type")
                return self.async_abort(reason="cannot_connect")

            possible_types = await self.device.async_possible_types()
            type_list = list(possible_types) if possible_types else []
            _LOGGER.info("[CONFIG_FLOW] Found %d possible device types", len(type_list))

            for dev_type in type_list:
                q = dev_type.match_quality(
                    self.device._get_cached_state(),
                    self.device._product_ids,
                )
                for manufacturer, model in dev_type.product_display_entries(
                    self.device._product_ids
                ):
                    key = f"{dev_type.config_type}||{manufacturer or ''}||{model or ''}"
                    parts = [p for p in [manufacturer, model] if p]
                    if parts:
                        label = f"{' '.join(parts)} ({dev_type.config_type})"
                    else:
                        label = f"{dev_type.name} ({dev_type.config_type})"
                    all_matches.append((SelectOptionDict(value=key, label=label), q))
                    if q > best_match:
                        best_match = q
                        best_matching_type = dev_type.config_type
                        best_matching_key = key
        except Exception as e:
            _LOGGER.error("[CONFIG_FLOW] Error determining device type: %s", e, exc_info=True)
            return self.async_abort(reason="not_supported")

        all_matches.sort(key=lambda x: x[1], reverse=True)
        type_options = [opt for opt, _ in all_matches]

        best_match = int(best_match)
        _LOGGER.info("[CONFIG_FLOW] Best device match: %s with quality %d, options=%d",
                      best_matching_type, best_match, len(type_options))

        try:
            dps = self.device._get_cached_state()
        except Exception as e:
            dps = {}
            _LOGGER.error("[CONFIG_FLOW] Failed to get cached state: %s", e)

        if self.__cloud_device:
            _LOGGER.warning(
                "Adding %s device with product id %s",
                self.__cloud_device.get("product_name", "UNKNOWN"),
                self.__cloud_device.get("product_id", "UNKNOWN"),
            )
            if self.__cloud_device.get("local_product_id") and self.__cloud_device.get("local_product_id") != self.__cloud_device.get("product_id"):
                _LOGGER.warning("Local product id differs from cloud: %s", self.__cloud_device.get("local_product_id"))
            try:
                self.init_cloud()
                model = await self.cloud.async_get_datamodel(self.__cloud_device.get("id"))
                if model:
                    _LOGGER.warning("Partial cloud device spec:\n%s", log_json(model))
            except Exception as e:
                _LOGGER.warning("Unable to fetch data model from cloud: %s %s", type(e).__name__, e)
        _LOGGER.warning("Device matches %s with quality of %d%%", best_matching_type, best_match)
        if type_options:
            detected = getattr(self, "_auto_detected_protocol", None)
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_TYPE,
                        default=best_matching_key,
                    ): SelectSelector(SelectSelectorConfig(options=type_options, mode=SelectSelectorMode.DROPDOWN)),
                }
            )
            if detected:
                return self.async_show_form(
                    step_id="select_type_auto_detected",
                    data_schema=schema,
                    description_placeholders={
                        "detected_protocol": str(detected),
                        "device_name": self._device_name_placeholder,
                    },
                )
            return self.async_show_form(
                step_id="select_type",
                data_schema=schema,
                description_placeholders={
                    "device_name": self._device_name_placeholder,
                },
            )
        else:
            _LOGGER.error("[CONFIG_FLOW] No device type options found, aborting")
            return self.async_abort(reason="not_supported")

    async def async_step_select_type_auto_detected(self, user_input=None):
        _LOGGER.info("[CONFIG_FLOW] async_step_select_type_auto_detected")
        return await self.async_step_select_type(user_input)

    async def async_step_choose_entities(self, user_input=None):
        _LOGGER.info("[CONFIG_FLOW] async_step_choose_entities called, user_input=%s", user_input)
        config = await self.hass.async_add_executor_job(
            get_config,
            self.data[CONF_TYPE],
        )
        if config is None:
            _LOGGER.error("[CONFIG_FLOW] No device config found for type %s", self.data.get(CONF_TYPE))
            return self.async_abort(reason="not_supported")

        if user_input is not None:
            title = user_input[CONF_NAME]
            del user_input[CONF_NAME]
            _LOGGER.info("[CONFIG_FLOW] Creating entry for device: %s (type=%s)", title, self.data.get(CONF_TYPE))
            try:
                result = self.async_create_entry(
                    title=title, data={**self.data, **user_input}
                )
                _LOGGER.info("[CONFIG_FLOW] Entry created successfully: %s", result)
                return result
            except Exception as e:
                _LOGGER.error("[CONFIG_FLOW] Failed to create entry: %s", e, exc_info=True)
                raise

        default_name = config.name
        if self.__cloud_device and self.__cloud_device.get("name"):
            default_name = self.__cloud_device["name"]
        _LOGGER.info("[CONFIG_FLOW] Asking user to name device, default=%s", default_name)
        schema = {vol.Required(CONF_NAME, default=default_name): str}

        return self.async_show_form(
            step_id="choose_entities",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "device_name": self._device_name_placeholder,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    def __init__(self):
        pass

    async def async_step_init(self, user_input=None):
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        errors = {}
        config = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            proto = user_input.get(CONF_PROTOCOL_VERSION)
            if proto != "auto":
                user_input[CONF_PROTOCOL_VERSION] = float(proto)
            config = {**config, **user_input}
            device = await async_test_connection(config, self.hass)
            if device:
                return self.async_create_entry(title="", data=user_input)
            else:
                errors["base"] = "connection"

        schema = {
            vol.Required(
                CONF_LOCAL_KEY,
                default=config.get(CONF_LOCAL_KEY, ""),
            ): str,
            vol.Required(CONF_HOST, default=config.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_PROTOCOL_VERSION,
                default=str(config.get(CONF_PROTOCOL_VERSION, "auto")),
            ): vol.In(["auto"] + [str(v) for v in API_PROTOCOL_VERSIONS]),
            vol.Required(
                CONF_POLL_ONLY, default=config.get(CONF_POLL_ONLY, False)
            ): bool,
        }
        cfg = await self.hass.async_add_executor_job(
            get_config,
            config[CONF_TYPE],
        )
        if cfg is None:
            return self.async_abort(reason="not_supported")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
            description_placeholders={"device_details_url": DEVICE_DETAILS_URL},
            errors=errors,
        )


def create_test_device(hass: HomeAssistant, config: dict):
    _LOGGER.info("[TEST_DEVICE] Creating test device, device_id=%s, host=%s, proto=%s",
                  config.get(CONF_DEVICE_ID), config.get(CONF_HOST), config.get(CONF_PROTOCOL_VERSION))
    subdevice_id = config.get(CONF_DEVICE_CID)
    if subdevice_id and isinstance(subdevice_id, str) and len(subdevice_id) < 5:
        _LOGGER.warning("[TEST_DEVICE] Invalid subdevice_id (too short): %s, setting to None", subdevice_id)
        subdevice_id = None
    try:
        device = TuyaLocalDevice(
            "Test",
            config[CONF_DEVICE_ID],
            config[CONF_HOST],
            config[CONF_LOCAL_KEY],
            config[CONF_PROTOCOL_VERSION],
            subdevice_id,
            hass,
            True,
        )
        _LOGGER.info("[TEST_DEVICE] Test device created successfully")
        return device
    except Exception as e:
        _LOGGER.error("[TEST_DEVICE] Failed to create test device: %s", e, exc_info=True)
        raise


async def async_test_connection(config: dict, hass: HomeAssistant):
    _LOGGER.info("[TEST_CONNECTION] Testing connection for device_id=%s", config.get(CONF_DEVICE_ID))
    domain_data = hass.data.get(DOMAIN)
    existing = domain_data.get(get_device_id(config)) if domain_data else None
    if existing and existing.get("device"):
        _LOGGER.info("[TEST_CONNECTION] Pausing existing device to test new connection parameters")
        existing["device"].pause()
        await asyncio.sleep(5)

    retval = None

    if config.get(CONF_PROTOCOL_VERSION) == "auto":
        _LOGGER.info("[TEST_CONNECTION] Auto protocol mode, testing all versions")
        for proto in API_PROTOCOL_VERSIONS:
            proto_config = {**config, CONF_PROTOCOL_VERSION: proto}
            device = None
            try:
                device = await hass.async_add_executor_job(
                    create_test_device, hass, proto_config
                )
                await device.async_refresh()
                if device.has_returned_state:
                    retval = device
                    _LOGGER.info("[TEST_CONNECTION] Connected with protocol version %s", proto)
                    break
                else:
                    _LOGGER.debug("[TEST_CONNECTION] Protocol %s: device created but no state returned", proto)
            except Exception as e:
                _LOGGER.debug("[TEST_CONNECTION] Protocol %s test failed: %s %s", proto, type(e).__name__, e)
            if device is not None:
                device._api.set_socketPersistent(False)
                if device._api.parent:
                    device._api.parent.set_socketPersistent(False)
    else:
        try:
            _LOGGER.info("[TEST_CONNECTION] Testing with protocol version %s", config.get(CONF_PROTOCOL_VERSION))
            device = await hass.async_add_executor_job(
                create_test_device,
                hass,
                config,
            )
            await device.async_refresh()
            retval = device if device.has_returned_state else None
            _LOGGER.info("[TEST_CONNECTION] Test result: %s", "connected" if retval else "no response")
        except Exception as e:
            _LOGGER.warning("[TEST_CONNECTION] Test failed: %s %s", type(e).__name__, e)

    if existing and existing.get("device"):
        _LOGGER.info("[TEST_CONNECTION] Restarting device after test")
        existing["device"].resume()

    if not retval:
        _LOGGER.warning("[TEST_CONNECTION] All connection attempts failed for %s", config.get(CONF_DEVICE_ID))
    return retval


def scan_for_device(devid):
    _LOGGER.info("[SCAN_DEVICE] Scanning network for device %s", devid)
    try:
        result = tinytuya.find_device(dev_id=devid)
        _LOGGER.info("[SCAN_DEVICE] Scan result for %s: %s", devid, result)
        return result
    except Exception as e:
        _LOGGER.error("[SCAN_DEVICE] Scan error: %s", e, exc_info=True)
        return {"ip": None, "version": ""}