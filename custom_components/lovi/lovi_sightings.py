"""Passive Tuya device sighting watcher.

Battery powered Tuya devices (smart locks, door/window sensors, smoke
detectors, ...) sleep almost all of the time to save power.  When they wake
up -- because of a physical interaction or an event -- they only stay online
for a few seconds before going back to sleep.  A one-shot network scan
(broadcast or force scan) will almost always miss them.

The trick is that when such a device wakes up it behaves like a normal Tuya
device: it broadcasts a UDP packet to ports 6666/6667/7000 and briefly
listens on TCP 6668.  This watcher keeps those UDP sockets open for the
lifetime of Home Assistant and records every broadcast it receives into a
persistent sightings registry (device id -> ip/last_seen/version).

Benefits for the Lovi integration:

* A configured device that fell offline (or moved IP) is recovered the moment
  it next wakes: the watcher adopts the freshly sighted IP and triggers a
  refresh while the device is still awake.
* Battery devices are discovered on every installation without touching the
  router or the Tuya developer portal -- the hub simply waits until the device
  wakes naturally, then remembers it.

A slow broadcast sweep is also run periodically so mains powered devices that
never announce themselves are kept fresh.
"""

import asyncio
import json
import logging
import socket
import time

try:
    import tinytuya
except ImportError:  # pragma: no cover
    tinytuya = None

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

LISTEN_PORTS = (6666, 6667, 7000)
STORAGE_VERSION = 1
STORAGE_KEY = "lovi_sightings"

SWEEP_INTERVAL = 300
SIGHTING_FRESHNESS = 60
SAVE_DEBOUNCE = 30

EVENT_DEVICE_SEEN = "lovi_device_seen"


def _parse_packet(data: bytes) -> dict | None:
    """Decrypt a Tuya UDP broadcast packet into its JSON payload."""
    if tinytuya is None:
        return None
    try:
        payload = json.loads(tinytuya.decrypt_udp(data))
        if not isinstance(payload, dict):
            return None
        if payload.get("from") == "app":
            # The Smart Life app announces its own presence so devices reply
            # to it.  Not a sighting of a device, skip it.
            return None
        return payload
    except Exception:
        return None


def get_or_create_watcher(hass: HomeAssistant) -> "DeviceSightingsWatcher | None":
    """Return the shared sightings watcher, creating it if needed.

    The watcher is a singleton stored in hass.data[DOMAIN]["sightings"] so the
    coordinator and the config flow share the same instance.
    """
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    watcher = hass.data[DOMAIN].get("sightings")
    if watcher is None:
        try:
            watcher = DeviceSightingsWatcher(hass)
            hass.data[DOMAIN]["sightings"] = watcher
        except Exception as e:
            _LOGGER.error("[LOVI_SIGHTINGS] failed to create watcher: %s", e)
            return None
    return watcher


class DeviceSightingsWatcher:
    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._devices: dict[str, dict] = {}
        self._by_uuid: dict[str, str] = {}
        self._running = False
        self._transports = []
        self._task: asyncio.Task | None = None
        self._sweep_task: asyncio.Task | None = None
        self._dirty = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def async_start(self):
        if self._running:
            return
        await self._async_load()
        self._running = True
        self._task = self._hass.async_create_task(self._listen_loop())
        self._sweep_task = self._hass.async_create_task(self._sweep_loop())
        _LOGGER.info(
            "[LOVI_SIGHTINGS] watcher started (%d known sightings)",
            len(self._devices),
        )

    async def async_stop(self):
        self._running = False
        for transport in self._transports:
            try:
                transport.close()
            except Exception:
                pass
        self._transports = []
        for task in (self._task, self._sweep_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None
        self._sweep_task = None
        _LOGGER.info("[LOVI_SIGHTINGS] watcher stopped")

    async def _async_load(self):
        try:
            stored = await self._store.async_load()
        except Exception as e:
            _LOGGER.error("[LOVI_SIGHTINGS] failed to load sightings: %s", e)
            stored = None
        if stored and isinstance(stored, dict):
            devices = stored.get("devices", {})
            if isinstance(devices, dict):
                self._devices = devices
                for gwid, sighting in devices.items():
                    uuid = sighting.get("uuid") if isinstance(sighting, dict) else None
                    if uuid:
                        self._by_uuid[uuid] = gwid

    def _schedule_save(self):
        if self._dirty:
            return
        self._dirty = True

        async def _do_save():
            await asyncio.sleep(SAVE_DEBOUNCE)
            self._dirty = False
            try:
                await self._store.async_save({"devices": self._devices})
            except Exception as e:
                _LOGGER.error("[LOVI_SIGHTINGS] failed to save sightings: %s", e)

        self._hass.async_create_task(_do_save())

    def _handle_sighting(self, payload: dict, ip: str):
        gwid = payload.get("gwId")
        if not gwid:
            return
        now = time.time()
        try:
            version = float(payload.get("version"))
        except (TypeError, ValueError):
            version = None

        sighting = {
            "ip": ip,
            "last_seen": now,
            "product_key": payload.get("productKey", ""),
            "version": version,
            "uuid": payload.get("uuid", ""),
            "encrypt": payload.get("encrypt", True),
        }

        existing = self._devices.get(gwid)
        changed = (
            existing is None
            or existing.get("ip") != ip
            or existing.get("last_seen", 0) < now
        )
        self._devices[gwid] = sighting
        if sighting["uuid"]:
            self._by_uuid[sighting["uuid"]] = gwid

        if changed:
            _LOGGER.info(
                "[LOVI_SIGHTINGS] saw device %s at %s (version=%s, product=%s)",
                gwid,
                ip,
                version,
                sighting["product_key"],
            )
            self._schedule_save()

        self._hass.bus.async_fire(
            EVENT_DEVICE_SEEN,
            {
                "device_id": gwid,
                "ip": ip,
                "version": version,
                "product_key": sighting["product_key"],
            },
        )
        self._nudge_configured(gwid, ip, version)

    def _nudge_configured(self, gwid: str, ip: str, version):
        """Update and refresh a configured device that matches this sighting."""
        domain_data = self._hass.data.get(DOMAIN, {})
        for entry_data in domain_data.values():
            if not isinstance(entry_data, dict):
                continue
            device = entry_data.get("device")
            if device is None:
                continue
            try:
                if gwid not in (device._device_id, device.unique_id, device.dev_cid):
                    continue
            except Exception:
                continue
            if device._address == ip:
                continue
            self._hass.async_create_task(self._apply_sighting(device, ip, version))

    async def _apply_sighting(self, device, ip: str, version):
        try:
            await device.async_update_address(ip)
            await device.async_refresh()
            _LOGGER.info(
                "[LOVI_SIGHTINGS] refreshed %s at new IP %s",
                getattr(device, "name", "?"),
                ip,
            )
        except Exception as e:
            _LOGGER.debug(
                "[LOVI_SIGHTINGS] nudge failed for %s: %s",
                getattr(device, "name", "?"),
                e,
            )

    async def _listen_loop(self):
        loop = self._hass.loop
        for port in LISTEN_PORTS:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except (AttributeError, OSError):
                    pass
                sock.bind(("", port))
                sock.setblocking(False)
            except OSError as e:
                _LOGGER.warning("[LOVI_SIGHTINGS] could not bind UDP port %s: %s", port, e)
                try:
                    sock.close()
                except Exception:
                    pass
                continue

            def _make_protocol():
                return _UDPProtocol(self._handle_sighting)

            try:
                transport, _ = await loop.create_datagram_endpoint(
                    _make_protocol, sock=sock
                )
                self._transports.append(transport)
            except Exception as e:
                _LOGGER.warning("[LOVI_SIGHTINGS] could not listen on UDP port %s: %s", port, e)
                try:
                    sock.close()
                except Exception:
                    pass

        if not self._transports:
            _LOGGER.error("[LOVI_SIGHTINGS] no sockets bound, watcher disabled")
            self._running = False
            return

        _LOGGER.info(
            "[LOVI_SIGHTINGS] listening on UDP %s",
            ", ".join(str(p) for p in LISTEN_PORTS),
        )
        while self._running:
            await asyncio.sleep(1)

    async def _sweep_loop(self):
        # Give the listener a moment to start before poking the network.
        await asyncio.sleep(SWEEP_INTERVAL)
        while self._running:
            try:
                await self._hass.async_add_executor_job(self._send_discovery)
            except Exception as e:
                _LOGGER.debug("[LOVI_SIGHTINGS] sweep failed: %s", e)
            await asyncio.sleep(SWEEP_INTERVAL)

    @staticmethod
    def _send_discovery():
        if tinytuya is None:
            return
        try:
            from tinytuya import scanner

            scanner.send_discovery_request()
            return
        except Exception as e:
            _LOGGER.debug("[LOVI_SIGHTINGS] discovery helper unavailable: %s", e)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(b"\x00\x00\x00\x00", ("255.255.255.255", 7000))
            sock.close()
        except Exception:
            pass

    def async_get_sighting(self, device_id: str) -> dict | None:
        return self._devices.get(device_id)

    def async_get_by_uuid(self, uuid: str) -> dict | None:
        gwid = self._by_uuid.get(uuid)
        return self._devices.get(gwid) if gwid else None

    def async_nearby(self, configured: set[str]) -> list[dict]:
        """Sightings for devices that are not yet configured, newest first."""
        now = time.time()
        result = []
        for gwid, sighting in self._devices.items():
            if gwid in configured:
                continue
            if now - sighting.get("last_seen", 0) > 7 * 24 * 3600:
                continue
            result.append({**sighting, "device_id": gwid})
        return sorted(result, key=lambda s: s.get("last_seen", 0), reverse=True)

    async def async_wait_for_sighting(
        self, device_id: str, timeout: float = 20.0
    ) -> dict | None:
        """Wait up to ``timeout`` seconds for a fresh sighting of a device.

        Used by the config flow to catch a battery device being woken during
        setup.  Falls back to a cached sighting (even a stale one) at the end.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            sighting = self.async_get_sighting(device_id) or self.async_get_by_uuid(
                device_id
            )
            if sighting and (time.time() - sighting.get("last_seen", 0)) < SIGHTING_FRESHNESS:
                return sighting
            await asyncio.sleep(0.5)
        return self.async_get_sighting(device_id) or self.async_get_by_uuid(device_id)


class _UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, handler):
        self._handler = handler

    def datagram_received(self, data: bytes, addr):
        payload = _parse_packet(data)
        if payload and payload.get("gwId"):
            self._handler(payload, addr[0])
