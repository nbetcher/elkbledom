"""Home Assistant lifecycle and config-flow regression tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError

import custom_components.elkbledom as integration
from custom_components.elkbledom import config_flow
from custom_components.elkbledom.config_flow import BLEDOMFlowHandler
from custom_components.elkbledom.transport import NotConnectedError


class FakeServices:
    def __init__(self) -> None:
        self.registered = []

    def async_register(self, domain, name, handler, *, schema) -> None:
        self.registered.append((domain, name, handler, schema))


async def test_async_setup_registers_each_action_once() -> None:
    services = FakeServices()
    hass = SimpleNamespace(services=services)

    assert await integration.async_setup(hass, {}) is True
    assert [item[1] for item in services.registered] == [
        integration.SERVICE_SET_RANDOM_COLOR,
        integration.SERVICE_SET_RGB_COLOR,
        integration.SERVICE_SYNC_TIME,
        integration.SERVICE_SCHEDULE_ON,
        integration.SERVICE_SCHEDULE_OFF,
    ]


async def test_setup_retries_when_initial_proxy_gatt_probe_fails(monkeypatch) -> None:
    stopped = False

    class FakeInstance:
        def __init__(self, *_args, **_kwargs) -> None:
            self.transport = SimpleNamespace(bluetooth_address="AA:BB:CC:DD:EE:FF")

        async def update(self) -> None:
            raise NotConnectedError("no active proxy slot")

        async def stop(self) -> None:
            nonlocal stopped
            stopped = True

    monkeypatch.setattr(integration, "ensure_models_loaded", AsyncMock())
    monkeypatch.setattr(integration, "BLEDOMInstance", FakeInstance)
    monkeypatch.setattr(
        integration.bluetooth,
        "async_address_reachability_diagnostics",
        lambda *_args, **_kwargs: "only passive scanners can see this address",
    )
    hass = SimpleNamespace()
    entry = SimpleNamespace(
        data={"mac": "AA:BB:CC:DD:EE:FF", "name": "Kitchen"},
        options={},
    )

    with pytest.raises(ConfigEntryNotReady):
        await integration.async_setup_entry(hass, entry)

    assert stopped is True


async def test_config_flow_rejects_advertisement_only_discovery() -> None:
    flow = BLEDOMFlowHandler()
    discovery = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        connectable=False,
        name="ELK-BLEDOM",
    )

    result = await flow.async_step_bluetooth(discovery)

    assert result["type"] == "abort"
    assert result["reason"] == "not_connectable"


async def test_bluetooth_discovery_uses_a_confirmation_step(monkeypatch) -> None:
    flow = BLEDOMFlowHandler()
    flow.context = {}
    flow.hass = SimpleNamespace(data={})
    flow._async_current_ids = lambda: set()
    flow._async_current_entries = list
    flow.async_set_unique_id = AsyncMock(return_value=None)
    flow._abort_if_unique_id_configured = lambda: None
    monkeypatch.setattr(config_flow, "ensure_models_loaded", AsyncMock())
    monkeypatch.setattr(
        config_flow,
        "DeviceData",
        lambda _hass, discovery: SimpleNamespace(
            address=discovery.address,
            name=discovery.name,
            is_supported=True,
        ),
    )
    discovery = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        connectable=True,
        name="ELK-BLEDOM",
    )

    result = await flow.async_step_bluetooth(discovery)

    assert result["type"] == "form"
    assert result["step_id"] == "bluetooth_confirm"
    assert flow.mac == "aa:bb:cc:dd:ee:ff"


async def test_failed_validation_cannot_be_bypassed() -> None:
    flow = BLEDOMFlowHandler()
    flow.validate_connection = AsyncMock(return_value=NotConnectedError("failed"))

    result = await flow.async_step_validate({})

    assert result["type"] == "form"
    assert result["step_id"] == "validate"
    assert result["errors"] == {"base": "connect"}


async def test_removed_entry_is_immediately_rediscoverable(monkeypatch) -> None:
    rediscover = Mock()
    monkeypatch.setattr(integration.bluetooth, "async_rediscover_address", rediscover)
    hass = SimpleNamespace()
    entry = SimpleNamespace(data={"mac": "AA-BB-CC-DD-EE-FF"})

    await integration.async_remove_entry(hass, entry)

    assert rediscover.call_args_list == [
        call(hass, "AA-BB-CC-DD-EE-FF"),
        call(hass, "aa:bb:cc:dd:ee:ff"),
        call(hass, "AA:BB:CC:DD:EE:FF"),
    ]


def test_actions_reject_non_elkbledom_entities(monkeypatch) -> None:
    registry = SimpleNamespace(
        async_get=lambda _entity_id: SimpleNamespace(
            platform="other_integration", config_entry_id="other"
        )
    )
    monkeypatch.setattr(integration.er, "async_get", lambda _hass: registry)

    with pytest.raises(ServiceValidationError):
        integration._instances_for_entities(SimpleNamespace(), ["light.not_elkbledom"])


@pytest.mark.parametrize("platform_failure", [False, True])
async def test_setup_uses_controller_address_and_cleans_failed_platforms(
    monkeypatch, platform_failure
) -> None:
    from homeassistant.components.bluetooth.match import BluetoothCallbackMatcherIndex

    instance = SimpleNamespace(
        address="aa:bb:cc:dd:ee:ff",
        transport=SimpleNamespace(bluetooth_address="AA:BB:CC:DD:EE:FF"),
        update=AsyncMock(),
        stop=AsyncMock(),
        _async_update_ble=Mock(),
        _async_unavailable=Mock(),
    )
    coordinator = SimpleNamespace(
        async_config_entry_first_refresh=AsyncMock(),
        async_shutdown=AsyncMock(side_effect=instance.stop),
    )
    matchers = BluetoothCallbackMatcherIndex()
    tracked = []

    def register(_hass, handler, matcher, _mode):
        instance.update.assert_awaited_once()
        matchers.add_callback_matcher({**matcher, "callback": handler})
        return lambda: None

    def track(_hass, _handler, address, **_kwargs):
        tracked.append(address)
        return lambda: None

    monkeypatch.setattr(integration, "ensure_models_loaded", AsyncMock())
    monkeypatch.setattr(integration, "BLEDOMInstance", lambda *_args: instance)
    monkeypatch.setattr(integration, "ElkCoordinator", lambda *_args: coordinator)
    monkeypatch.setattr(integration.bluetooth, "async_register_callback", register)
    monkeypatch.setattr(integration.bluetooth, "async_track_unavailable", track)
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(
                side_effect=RuntimeError("platform failed") if platform_failure else None
            )
        ),
        bus=SimpleNamespace(async_listen_once=lambda *_args: lambda: None),
    )
    entry = SimpleNamespace(
        data={"mac": "aa:bb:cc:dd:ee:ff", "name": "Kitchen"},
        options={},
        async_on_unload=Mock(),
        add_update_listener=lambda *_args: lambda: None,
    )

    if platform_failure:
        with pytest.raises(RuntimeError, match="platform failed"):
            await integration.async_setup_entry(hass, entry)
        instance.stop.assert_awaited_once()
    else:
        assert await integration.async_setup_entry(hass, entry) is True
        instance.stop.assert_not_awaited()
    advertisement = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        connectable=True,
        name="ELK-BLEDOM",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
    )
    assert len(matchers.match_callbacks(advertisement)) == 1
    assert tracked == [advertisement.address]
