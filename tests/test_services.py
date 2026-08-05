"""Service registration tests for the Lovi integration."""

from custom_components.lovi.const import DOMAIN
from custom_components.lovi.services import async_setup_services


async def test_registers_once(hass):
    assert not hass.services.has_service(DOMAIN, "send_learned_ir_command")
    assert await async_setup_services(hass, ["remote"]) is True
    assert hass.services.has_service(DOMAIN, "send_learned_ir_command")

    # Registering a second time must not raise (regression for multi-device).
    assert await async_setup_services(hass, ["remote"]) is True


async def test_skips_without_remote(hass):
    assert await async_setup_services(hass, ["light", "switch"]) is True
    assert not hass.services.has_service(DOMAIN, "send_learned_ir_command")