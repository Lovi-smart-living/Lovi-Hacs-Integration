import asyncio
import logging
from enum import Enum, auto
from asyncio.exceptions import CancelledError
from threading import Lock
from time import time

import tinytuya
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, callback

from .const import (
    API_PROTOCOL_VERSIONS,
    CONF_DEVICE_CID,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    DEVICE_UNAVAILABLE_TIMEOUT,
    DOMAIN,
)
from .helpers.config import get_device_id
from .helpers.device_config import possible_matches
from .helpers.log import log_json

_LOGGER = logging.getLogger(__name__)


class TransportMode(Enum):
    UNKNOWN = auto()
    PERSISTENT = auto()
    POLLING = auto()


def _collect_possible_matches(cached_state, product_ids):
    return list(possible_matches(cached_state, product_ids))


class TuyaLocalDevice:
    def __init__(
        self,
        name,
        dev_id,
        address,
        local_key,
        protocol_version,
        dev_cid,
        hass: HomeAssistant,
        poll_only=False,
        manufacturer=None,
        model=None,
    ):
        self._name = name
        self._manufacturer = manufacturer
        self._model = model
        self._children = []
        self._force_dps = []
        self._product_ids = []
        self._running = False
        self._shutdown_listener = None
        self._startup_listener = None
        self._api_protocol_version_index = None
        self._api_protocol_working = False
        self._api_working_protocol_failures = 0
        self.dev_cid = dev_cid
        self._device_id = dev_id
        self._address = address
        self._local_key = local_key
        self._health_monitor = None
        self._unavailable_since = None
        try:
            if dev_cid:
                if hass.data[DOMAIN].get(dev_id) and name != "Test":
                    parent = hass.data[DOMAIN][dev_id]["tuyadevice"]
                    parent_lock = hass.data[DOMAIN][dev_id].get(
                        "tuyadevicelock", asyncio.Lock()
                    )
                else:
                    parent = tinytuya.Device(dev_id, address, local_key)
                    parent_lock = asyncio.Lock()
                    if name != "Test":
                        hass.data[DOMAIN][dev_id] = {
                            "tuyadevice": parent,
                            "tuyadevicelock": parent_lock,
                        }
                self._api = tinytuya.Device(
                    dev_cid,
                    cid=dev_cid,
                    parent=parent,
                )
                self._api_lock = parent_lock
            else:
                if hass.data[DOMAIN].get(dev_id) and name != "Test":
                    self._api = hass.data[DOMAIN][dev_id]["tuyadevice"]
                    self._api_lock = hass.data[DOMAIN][dev_id].get(
                        "tuyadevicelock", asyncio.Lock()
                    )
                else:
                    self._api = tinytuya.Device(dev_id, address, local_key)
                    self._api_lock = asyncio.Lock()
                    if name != "Test":
                        hass.data[DOMAIN][dev_id] = {
                            "tuyadevice": self._api,
                            "tuyadevicelock": self._api_lock,
                        }
        except Exception as e:
            _LOGGER.error(
                "%s: %s while initialising device %s",
                type(e).__name__,
                e,
                dev_id,
            )
            raise e

        self._api.set_socketRetryLimit(1)
        if self._api.parent:
            self._api.parent.set_socketRetryLimit(1)

        self._refresh_task = None
        self._protocol_configured = protocol_version
        self._poll_only = poll_only
        self._temporary_poll = False
        self._reset_cached_state()

        self._hass = hass

        self._FAKE_IT_TIMEOUT = 5
        self._CACHE_TIMEOUT = 30
        self._HEARTBEAT_INTERVAL = 10
        self._AUTO_CONNECTION_ATTEMPTS = len(API_PROTOCOL_VERSIONS) * 2 + 1
        self._SINGLE_PROTO_CONNECTION_ATTEMPTS = 3
        self._AUTO_FAILURE_RESET_COUNT = 10
        self._lock = Lock()
        self._transport_mode = TransportMode.UNKNOWN
        self._persistent_receive_failures = 0
        self._persistent_since = 0.0
        self._last_successful_receive = 0.0
        self._next_persistent_retry = 0.0
        self._PERSISTENT_FAILURE_THRESHOLD = 3
        self._PERSISTENT_RETRY_INTERVAL = 21600

    def set_health_monitor(self, health_monitor):
        self._health_monitor = health_monitor

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return self.dev_cid or self._api.id

    @property
    def device_info(self):
        info = {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": self._manufacturer or "Tuya",
        }
        if self._model:
            info["model"] = self._model
        return info

    @property
    def has_returned_state(self):
        cached = self._get_cached_state()
        return len(cached) > 1 or cached.get("updated_at", 0) > 0

    @property
    def health_status(self):
        return {
            "last_seen": self._cached_state.get("updated_at", 0),
            "ip": self._address,
            "protocol_version": self._protocol_configured,
            "working_protocol": self._api_protocol_working,
            "connection_failures": self._api_working_protocol_failures,
            "device_id": self._device_id,
            "transport_mode": self._transport_mode.name.lower(),
            "persistent_receive_failures": self._persistent_receive_failures,
            "persistent_since": self._persistent_since,
            "last_successful_receive": self._last_successful_receive,
            "next_persistent_retry": self._next_persistent_retry,
        }

    @property
    def is_unavailable(self) -> bool:
        """Return True if device has been unavailable for more than the timeout."""
        if self._unavailable_since is None:
            return False
        return (time() - self._unavailable_since) >= DEVICE_UNAVAILABLE_TIMEOUT

    @property
    def unavailable_since(self):
        """Return timestamp when device became unavailable, or None."""
        return self._unavailable_since

    def _mark_unavailable(self):
        """Mark device as unavailable if not already marked."""
        if self._unavailable_since is None:
            self._unavailable_since = time()
            _LOGGER.warning(
                "%s marked as unavailable at %s", self.name, self._unavailable_since
            )

    def _mark_available(self):
        """Mark device as available and clear unavailable timer."""
        if self._unavailable_since is not None:
            _LOGGER.info(
                "%s became available again after %.1fs",
                self.name,
                time() - self._unavailable_since,
            )
        self._unavailable_since = None

    async def async_update_address(self, new_address: str):
        if new_address == self._address:
            return
        _LOGGER.info(
            "Updating device %s address from %s to %s",
            self._device_id,
            self._address,
            new_address,
        )
        self._address = new_address
        self._api.address = new_address
        if self._api.parent:
            self._api.parent.address = new_address
        self._reset_cached_state()
        if self._running:
            self._api.set_socketPersistent(False)
            if self._api.parent:
                self._api.parent.set_socketPersistent(False)

    @callback
    def actually_start(self, event=None):
        _LOGGER.debug("Starting monitor loop for %s", self.name)
        self._running = True
        self._shutdown_listener = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self.async_stop
        )
        if not self._refresh_task:
            self._refresh_task = self._hass.async_create_task(self.receive_loop())

    def start(self):
        if self._hass.is_stopping:
            return
        elif self._hass.is_running:
            if self._startup_listener:
                self._startup_listener()
                self._startup_listener = None
            self.actually_start()
        else:
            self._startup_listener = self._hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self.actually_start
            )

    async def async_stop(self, event=None):
        _LOGGER.debug("Stopping monitor loop for %s", self.name)
        self._running = False
        self._children.clear()
        self._force_dps.clear()
        if self._refresh_task:
            self._api.set_socketPersistent(False)
            if self._api.parent:
                self._api.parent.set_socketPersistent(False)
            await self._refresh_task
        _LOGGER.debug("Monitor loop for %s stopped", self.name)
        self._refresh_task = None

    def register_entity(self, entity):
        should_poll = len(self._children) == 0 and not self._hass.is_running

        self._children.append(entity)
        for dp in entity._config.dps():
            if dp.force and dp.id not in self._force_dps:
                self._force_dps.append(int(dp.id))

        if not self._running and not self._startup_listener:
            self.start()
        if self.has_returned_state:
            entity.async_schedule_update_ha_state()
        elif should_poll:
            entity.async_schedule_update_ha_state(True)

    async def async_unregister_entity(self, entity):
        self._children.remove(entity)
        if not self._children:
            try:
                await self.async_stop()
            except CancelledError:
                pass

    async def receive_loop(self):
        try:
            async for poll in self.async_receive():
                if isinstance(poll, dict):
                    _LOGGER.debug(
                        "%s received %s",
                        self.name,
                        log_json(poll),
                    )
                    full_poll = poll.pop("full_poll", False)
                    self._cached_state = self._cached_state | poll
                    self._cached_state["updated_at"] = time()
                    self._remove_properties_from_pending_updates(poll)
                    self._mark_available()

                    for entity in self._children:
                        try:
                            entity.on_receive(poll, full_poll)
                        except Exception as e:
                            _LOGGER.exception(
                                "%s on_receive error for entity %s: %s",
                                self.name,
                                entity.entity_id,
                                e,
                            )
                        if full_poll:
                            for dp in entity._config.dps():
                                if not dp.persist and dp.id not in poll:
                                    self._cached_state.pop(dp.id, None)
                        entity.schedule_update_ha_state()
                else:
                    _LOGGER.debug(
                        "%s received non data %s",
                        self.name,
                        log_json(poll),
                    )
            _LOGGER.warning("%s receive loop has terminated", self.name)

        except Exception as t:
            _LOGGER.exception(
                "%s receive loop terminated by exception %s", self.name, t
            )
            self._api.set_socketPersistent(False)
            if self._api.parent:
                self._api.parent.set_socketPersistent(False)
            if self._health_monitor and self._running:
                _LOGGER.info(
                    "%s connection lost, triggering health check",
                    self.name,
                )
                self._hass.async_create_task(
                    self._health_monitor.async_on_device_disconnect(self._device_id)
                )

    @property
    def should_poll(self):
        return self._poll_only or self._temporary_poll or not self.has_returned_state

    def pause(self):
        self._temporary_poll = True
        self._api.set_socketPersistent(False)
        if self._api.parent:
            self._api.parent.set_socketPersistent(False)

    def resume(self):
        self._temporary_poll = False

    def _use_persistent(self) -> bool:
        if self._poll_only or self._temporary_poll:
            return False
        if self._transport_mode == TransportMode.POLLING:
            if time() < self._next_persistent_retry:
                return False
        return True

    async def async_receive(self):
        persist = self._use_persistent()
        dps_updated = False

        self._api.set_socketPersistent(persist)
        if self._api.parent:
            self._api.parent.set_socketPersistent(persist)

        last_heartbeat = self._cached_state.get("updated_at", 0)
        while self._running:
            error_count = self._api_working_protocol_failures
            force_backoff = False
            try:
                await self._api_lock.acquire()
                last_cache = self._cached_state.get("updated_at", 0)
                now = time()
                full_poll = False
                should_persist = self._use_persistent()
                if persist != should_persist:
                    persist = should_persist
                    _LOGGER.debug("%s persistant connection set to %s", self.name, persist)
                    self._api.set_socketPersistent(persist)
                    if self._api.parent:
                        self._api.parent.set_socketPersistent(persist)
                    self._last_full_poll = 0

                needs_full_poll = now - self._last_full_poll > self._CACHE_TIMEOUT
                if now - last_cache > self._CACHE_TIMEOUT or (
                    persist and needs_full_poll
                ):
                    if (
                        self._force_dps
                        and not dps_updated
                        and self._api_protocol_working
                    ):
                        poll = await self._retry_on_failed_connection(
                            lambda: self._api.updatedps(self._force_dps),
                            f"Failed to update device dps for {self.name}",
                        )
                        dps_updated = True
                    else:
                        poll = await self._retry_on_failed_connection(
                            lambda: self._api.status(),
                            f"Failed to fetch device status for {self.name}",
                        )
                        dps_updated = False
                        full_poll = True
                    self._last_full_poll = now
                    last_heartbeat = now
                elif persist:
                    if now - last_heartbeat > self._HEARTBEAT_INTERVAL:
                        await self._hass.async_add_executor_job(
                            self._api.heartbeat,
                            True,
                        )
                        last_heartbeat = now
                    try:
                        poll = await self._hass.async_add_executor_job(
                            self._api.receive,
                        )
                        if poll and "Err" in poll and poll["Err"] == "904":
                            poll = None
                        if isinstance(poll, dict) and "Error" in poll:
                            self._persistent_receive_failures += 1
                            self._persistent_since = 0
                            _LOGGER.debug(
                                "%s persistent receive error (%d consecutive)",
                                self.name, self._persistent_receive_failures,
                            )
                            if self._persistent_receive_failures >= self._PERSISTENT_FAILURE_THRESHOLD:
                                self._transport_mode = TransportMode.POLLING
                                self._next_persistent_retry = time() + self._PERSISTENT_RETRY_INTERVAL
                                _LOGGER.warning(
                                    "%s persistent monitoring failed %d consecutive times, switching to polling mode",
                                    self.name, self._persistent_receive_failures,
                                )
                            poll = None
                        else:
                            self._persistent_receive_failures = 0
                            self._last_successful_receive = time()
                            if self._transport_mode != TransportMode.PERSISTENT:
                                if self._persistent_since == 0:
                                    self._persistent_since = time()
                                    _LOGGER.info(
                                        "%s testing persistent connection",
                                        self.name,
                                    )
                                elif time() - self._persistent_since >= 30:
                                    self._transport_mode = TransportMode.PERSISTENT
                                    _LOGGER.info(
                                        "%s switched to persistent monitoring",
                                        self.name,
                                    )
                    except Exception:
                        self._persistent_receive_failures += 1
                        self._persistent_since = 0
                        _LOGGER.debug(
                            "%s persistent receive failed (%d consecutive)",
                            self.name, self._persistent_receive_failures,
                        )
                        if self._persistent_receive_failures >= self._PERSISTENT_FAILURE_THRESHOLD:
                            self._transport_mode = TransportMode.POLLING
                            self._next_persistent_retry = time() + self._PERSISTENT_RETRY_INTERVAL
                            _LOGGER.warning(
                                "%s persistent monitoring failed %d consecutive times, switching to polling mode",
                                self.name, self._persistent_receive_failures,
                            )
                        raise
                else:
                    force_backoff = True
                    poll = None

                if poll:
                    if "Error" in poll:
                        if error_count == self._api_working_protocol_failures:
                            self._api_working_protocol_failures += 1
                        if self._api_working_protocol_failures == 1:
                            _LOGGER.warning(
                                "%s error reading: %s", self.name, poll["Error"]
                            )
                        else:
                            _LOGGER.debug(
                                "%s error reading: %s", self.name, poll["Error"]
                            )
                        if "Payload" in poll and poll["Payload"]:
                            _LOGGER.debug(
                                "%s err payload: %s",
                                self.name,
                                poll["Payload"],
                            )
                    else:
                        if "dps" in poll:
                            poll = poll["dps"]
                        if isinstance(poll, dict):
                            poll["full_poll"] = full_poll
                            yield poll

            except CancelledError:
                self._running = False
                persist = False
                self._api.set_socketPersistent(False)
                if self._api.parent:
                    self._api.parent.set_socketPersistent(False)
                raise
            except Exception as t:
                _LOGGER.debug(
                    "%s receive loop error %s:%s",
                    self.name,
                    type(t).__name__,
                    t,
                )
                persist = False
                self._api.set_socketPersistent(False)
                if self._api.parent:
                    self._api.parent.set_socketPersistent(False)
                force_backoff = True
                if self._health_monitor:
                    self._hass.async_create_task(
                        self._health_monitor.async_on_device_disconnect(
                            self._device_id
                        )
                    )
            finally:
                if self._api_lock.locked():
                    self._api_lock.release()
            if not self.has_returned_state:
                force_backoff = True
                self._mark_unavailable()
            else:
                self._mark_available()
            await asyncio.sleep(5 if force_backoff else 0.1)

        self._api.set_socketPersistent(False)
        if self._api.parent:
            self._api.parent.set_socketPersistent(False)

    def set_detected_product_id(self, product_id):
        self._product_ids.append(product_id)

    async def async_possible_types(self):
        cached_state = self._get_cached_state()
        if len(cached_state) <= 1:
            self._api.set_dpsUsed(
                {
                    "1": None,
                    "2": None,
                    "9": None,
                    "20": None,
                    "60": None,
                    "101": None,
                    "148": None,
                    "201": None,
                }
            )
            await self.async_refresh()
            cached_state = self._get_cached_state()

        return await self._hass.async_add_executor_job(
            _collect_possible_matches,
            cached_state,
            self._product_ids,
        )

    async def async_inferred_type(self):
        best_match = None
        best_quality = 0
        cached_state = self._get_cached_state()
        possible = await self.async_possible_types()
        for config in possible:
            quality = config.match_quality(cached_state, self._product_ids)
            _LOGGER.info(
                "%s considering %s with quality %s",
                self.name,
                config.name,
                quality,
            )
            if quality > best_quality:
                best_quality = quality
                best_match = config

        if best_match:
            return best_match.config_type

        _LOGGER.warning(
            "Detection for %s with dps %s failed",
            self.name,
            log_json(cached_state),
        )

    async def async_refresh(self):
        _LOGGER.debug("Refreshing device state for %s", self.name)
        if not self._running:
            await self._retry_on_failed_connection(
                lambda: self._refresh_cached_state(),
                f"Failed to refresh device state for {self.name}.",
            )
        if not self.has_returned_state:
            new_ip = await self._discover_device_ip()
            if new_ip and new_ip != self._address:
                _LOGGER.info(
                    "%s found at new IP %s via LAN discovery, retrying...",
                    self.name, new_ip,
                )
                await self.async_update_address(new_ip)
                await self._retry_on_failed_connection(
                    lambda: self._refresh_cached_state(),
                    f"Failed to refresh device state for {self.name} after IP update.",
                )

    async def _discover_device_ip(self) -> str | None:
        try:
            result = await self._hass.async_add_executor_job(
                tinytuya.find_device, self._device_id
            )
            if result and result.get("ip"):
                _LOGGER.info(
                    "%s LAN discovery found device at %s (version=%s)",
                    self.name, result["ip"], result.get("version"),
                )
                return result["ip"]
        except Exception as e:
            _LOGGER.debug("%s LAN discovery failed: %s", self.name, e)
        return None

    def get_property(self, dps_id):
        cached_state = self._get_cached_state()
        return cached_state.get(dps_id)

    async def async_set_property(self, dps_id, value):
        await self.async_set_properties({dps_id: value})

    def anticipate_property_value(self, dps_id, value):
        self._cached_state[dps_id] = value

    def _reset_cached_state(self):
        self._cached_state = {"updated_at": 0}
        self._pending_updates = {}
        self._last_connection = 0
        self._last_full_poll = 0

    def _refresh_cached_state(self):
        new_state = self._api.status()
        if new_state:
            if "Err" not in new_state:
                self._cached_state = self._cached_state | new_state.get("dps", {})
                self._cached_state["updated_at"] = time()
                self._mark_available()
                for entity in self._children:
                    for dp in entity._config.dps():
                        if not dp.persist and dp.id not in new_state.get("dps", {}):
                            self._cached_state.pop(dp.id, None)
                    entity.schedule_update_ha_state()
            elif self._api_working_protocol_failures == 1:
                _LOGGER.warning(
                    "%s protocol error %s: %s",
                    self.name,
                    new_state.get("Err"),
                    new_state.get("Error", "message not provided"),
                )
            else:
                _LOGGER.debug(
                    "%s protocol error %s: %s",
                    self.name,
                    new_state.get("Err"),
                    new_state.get("Error", "message not provided"),
                )
        _LOGGER.debug(
            "%s refreshed device state: %s",
            self.name,
            log_json(new_state),
        )
        _LOGGER.debug(
            "new state (incl pending): %s",
            log_json(self._get_cached_state()),
        )
        return new_state

    async def async_set_properties(self, properties):
        if len(properties) == 0:
            return

        self._add_properties_to_pending_updates(properties)
        await self._debounce_sending_updates()

    def _add_properties_to_pending_updates(self, properties):
        now = time()

        pending_updates = self._get_pending_updates()
        for key, value in properties.items():
            pending_updates[key] = {
                "value": value,
                "updated_at": now,
                "sent": False,
            }

        _LOGGER.debug(
            "%s new pending updates: %s",
            self.name,
            log_json(pending_updates),
        )

    def _remove_properties_from_pending_updates(self, data):
        self._pending_updates = {
            key: value
            for key, value in self._pending_updates.items()
            if key not in data or not value["sent"] or data[key] != value["value"]
        }

    async def _debounce_sending_updates(self):
        now = time()
        since = now - self._last_connection
        self._last_connection = now
        waittime = 1 if since < 1.1 and self.should_poll else 0.001

        await asyncio.sleep(waittime)
        await self._send_pending_updates()

    async def _send_pending_updates(self):
        pending_properties = self._get_unsent_properties()

        _LOGGER.debug(
            "%s sending dps update: %s",
            self.name,
            log_json(pending_properties),
        )

        await self._retry_on_failed_connection(
            lambda: self._set_values(pending_properties),
            "Failed to update device state.",
        )

    def _set_values(self, properties):
        try:
            self._lock.acquire()
            self._api.set_multiple_values(properties, nowait=True)
            now = time()
            self._last_connection = now
            pending_updates = self._get_pending_updates()
            for key in properties.keys():
                pending_updates[key]["updated_at"] = now
                pending_updates[key]["sent"] = True
        finally:
            self._lock.release()

    async def _retry_on_failed_connection(self, func, error_message):
        if self._api_protocol_version_index is None:
            await self._rotate_api_protocol_version()
        auto = (self._protocol_configured == "auto") and (
            not self._api_protocol_working
        )
        connections = (
            self._AUTO_CONNECTION_ATTEMPTS
            if auto
            else self._SINGLE_PROTO_CONNECTION_ATTEMPTS
        )

        last_err_code = None
        for i in range(connections):
            try:
                if not self._hass.is_stopping:
                    retval = await self._hass.async_add_executor_job(func)
                    if isinstance(retval, dict) and "Error" in retval:
                        last_err_code = retval.get("Err")
                        if last_err_code == "900":
                            self._cached_state["updated_at"] = time()
                            retval = None
                        else:
                            raise AttributeError(retval["Error"])
                    self._api_protocol_working = True
                    self._api_working_protocol_failures = 0
                    self._mark_available()
                    return retval
            except Exception as e:
                _LOGGER.debug(
                    "Retrying after exception %s %s (%d/%d)",
                    type(e).__name__,
                    e,
                    i,
                    connections,
                )
                self._api.set_socketPersistent(False)
                if self._api.parent:
                    self._api.parent.set_socketPersistent(False)

                if i + 1 == connections:
                    self._reset_cached_state()
                    self._api_working_protocol_failures += 1
                    self._mark_unavailable()
                    if (
                        self._api_working_protocol_failures
                        > self._AUTO_FAILURE_RESET_COUNT
                    ):
                        self._api_protocol_working = False
                        for entity in self._children:
                            entity.async_schedule_update_ha_state()
                        if self._health_monitor:
                            self._hass.async_create_task(
                                self._health_monitor.async_on_device_disconnect(
                                    self._device_id
                                )
                            )
                    if self._api_working_protocol_failures == 1 and not (
                        last_err_code == "914" and self._protocol_configured == "auto"
                    ):
                        _LOGGER.error(error_message)
                    else:
                        _LOGGER.debug(error_message)

                if not self._api_protocol_working:
                    await self._rotate_api_protocol_version()

    def _get_cached_state(self):
        cached_state = self._cached_state.copy()
        return {**cached_state, **self._get_pending_properties()}

    def _get_pending_properties(self):
        return {key: prop["value"] for key, prop in self._get_pending_updates().items()}

    def _get_unsent_properties(self):
        return {
            key: info["value"]
            for key, info in self._get_pending_updates().items()
            if not info["sent"]
        }

    def _get_pending_updates(self):
        now = time()
        pending_updates_sorted = sorted(
            self._pending_updates.items(), key=lambda x: int(x[0])
        )
        self._pending_updates = {
            key: value
            for key, value in pending_updates_sorted
            if not value["sent"]
            or now - value.get("updated_at", 0) < self._FAKE_IT_TIMEOUT
        }
        return self._pending_updates

    async def _rotate_api_protocol_version(self):
        if self._api_protocol_version_index is None:
            try:
                self._api_protocol_version_index = API_PROTOCOL_VERSIONS.index(
                    self._protocol_configured
                )
            except ValueError:
                self._api_protocol_version_index = 0

        elif self._protocol_configured == "auto":
            self._api_protocol_version_index += 1

        if self._api_protocol_version_index >= len(API_PROTOCOL_VERSIONS):
            self._api_protocol_version_index = 0

        new_version = API_PROTOCOL_VERSIONS[self._api_protocol_version_index]
        _LOGGER.debug(
            "Setting protocol version for %s to %s",
            self.name,
            new_version,
        )
        if new_version == 3.22:
            new_version = 3.3
            self._api.disabledetect = False
        else:
            self._api.disabledetect = True

        await self._hass.async_add_executor_job(
            self._api.set_version,
            new_version,
        )
        if self._api.parent:
            await self._hass.async_add_executor_job(
                self._api.parent.set_version,
                new_version,
            )

    @staticmethod
    def get_key_for_value(obj, value, fallback=None):
        keys = list(obj.keys())
        values = list(obj.values())
        return keys[values.index(value)] if value in values else fallback


def setup_device(hass: HomeAssistant, config: dict):
    _LOGGER.info("Creating device: %s", get_device_id(config))
    hass.data[DOMAIN] = hass.data.get(DOMAIN, {})
    device = TuyaLocalDevice(
        config[CONF_NAME],
        config[CONF_DEVICE_ID],
        config[CONF_HOST],
        config[CONF_LOCAL_KEY],
        config[CONF_PROTOCOL_VERSION],
        config.get(CONF_DEVICE_CID),
        hass,
        config[CONF_POLL_ONLY],
        manufacturer=config.get(CONF_MANUFACTURER),
        model=config.get(CONF_MODEL),
    )
    hass.data[DOMAIN][get_device_id(config)] = {
        "device": device,
        "tuyadevice": device._api,
        "tuyadevicelock": device._api_lock,
    }

    return device


async def async_delete_device(hass: HomeAssistant, config: dict):
    device_id = get_device_id(config)
    _LOGGER.info("Deleting device: %s", device_id)
    domain_data = hass.data.get(DOMAIN, {})
    device_entry = domain_data.get(device_id)
    if device_entry is None:
        return

    device = device_entry.get("device")
    if device is not None:
        await device.async_stop()
        device_entry.pop("device", None)
    device_entry.pop("tuyadevice", None)
    device_entry.pop("tuyadevicelock", None)
    if not device_entry:
        domain_data.pop(device_id, None)