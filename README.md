# Lovi

**Zero-touch Lovi Hub provisioning for Home Assistant.**

Lovi handles cloud-LAN bridging for Tuya-based devices — automatic IP discovery, persistent/polling transport negotiation, and health monitoring that won't corrupt your config entry.

## Features

- **LAN Auto-Discovery** — finds devices by `gwId` via `tinytuya.find_device()` when `status()` fails
- **Happy Eyeballs Transport** — starts persistent TCP; if the device drops it 3×, falls back to polling with a 6-hour retry to re-upgrade
- **Smart Health Monitor** — compares cloud IPs against LAN IPs; rejects public/NAT'd IPs so your config entry is never corrupted
- **30‑Second Stability Gate** — persistent promotion requires 30 continuous seconds of successful `receive()` (including 904 "no data" responses)
- **Resilient Startup** — if the stored IP is stale, `async_refresh()` runs LAN discovery before raising `ConfigEntryNotReady`

## Installation via HACS

1. Make sure [HACS](https://hacs.xyz) is installed.
2. Add the custom repository:
   - **HACS → Integrations → ⋮ → Custom repositories**
   - **URL:** `https://github.com/Lovi-smart-living/Lovi-Hacs-Integration`
   - **Category:** Integration
3. Click **Add**, then search for **Lovi** in the HACS store.
4. Install and restart Home Assistant.
5. **Settings → Devices & Services → Add Integration → Lovi**

## Manual Installation

Copy the `custom_components/lovi/` directory into your HA `config/custom_components/` directory and restart.

## Configuration

Configuration is done through the Home Assistant UI via **Settings → Devices & Services → Add Integration → Lovi**. You'll need:

| Field | Description |
|---|---|
| Device ID | The Tuya/Lovi device ID (`gwId`) |
| Local Key | The device's local key |
| Host | IP address of the device on your LAN |
| Protocol Version | Usually `3.4` |

## Deployment

```bash
git push pi main   # deploys to your hub + restarts HA
git push github main  # publishes to HACS
```

## License

Proprietary — Lovi Smart Living
