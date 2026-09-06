"""Exercise retry budgets with the real connector, without a Bluetooth radio."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bleak_retry_connector as connector
import pytest
from bleak.exc import BleakError

import custom_components.elkbledom.transport as transport_module
from custom_components.elkbledom.device import ElkDevice
from custom_components.elkbledom.state import ElkState
from custom_components.elkbledom.transport import BLETransport, NotConnectedError


@pytest.fixture
async def proxy_harness(monkeypatch):
    """Replace the radio client, but retain establish_connection and its retries."""
    first = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="ELK-BLEDOM", source="first")
    second = SimpleNamespace(address=first.address, name=first.name, source="second")
    harness = SimpleNamespace(
        current_route=first,
        replacement_route=second,
        switch_route=False,
        outcomes=[],
        lookups=[],
        connects=[],
        writes=[],
    )
    characteristic = SimpleNamespace(uuid="write", properties=["write"], handle=13)

    class Services:
        def __iter__(self):
            return iter([SimpleNamespace(uuid="service", characteristics=[characteristic])])

        def get_characteristic(self, uuid):
            return characteristic if uuid == "write" else None

    def lookup(_hass, _address, *, connectable):
        assert connectable is True
        harness.lookups.append(harness.current_route)
        return harness.current_route

    def client_init(client, device, **kwargs):
        assert kwargs["_is_retry_client"] is True
        client.test_device = device
        client.test_connected = False

    async def client_connect(client, **_kwargs):
        harness.connects.append(client.test_device)
        outcome = harness.outcomes.pop(0) if harness.outcomes else None
        if outcome is not None:
            if harness.switch_route:
                harness.current_route = harness.replacement_route
            raise outcome
        client.test_connected = True

    async def client_disconnect(client):
        client.test_connected = False

    async def client_write(_client, _uuid, data, *, response):
        assert response is True
        harness.writes.append(bytes(data))

    # Do not mock establish_connection: these tests must detect changes in the
    # dependency's internal transient counter and exception translation.
    monkeypatch.setattr(connector, "IS_LINUX", False)
    monkeypatch.setattr(connector, "wait_for_disconnect", AsyncMock())
    monkeypatch.setattr(
        connector, "_has_valid_services_in_cache", AsyncMock(return_value=False), raising=False
    )
    client_class = connector.BleakClientWithServiceCache
    monkeypatch.setattr(client_class, "__init__", client_init)
    monkeypatch.setattr(client_class, "connect", client_connect)
    monkeypatch.setattr(client_class, "disconnect", client_disconnect)
    monkeypatch.setattr(client_class, "write_gatt_char", client_write)
    monkeypatch.setattr(
        client_class, "is_connected", property(lambda client: client.test_connected)
    )
    monkeypatch.setattr(client_class, "services", property(lambda _client: Services()))
    monkeypatch.setattr(transport_module, "async_ble_device_from_address", lookup)
    monkeypatch.setattr(transport_module, "async_discovered_service_info", lambda *_a, **_k: [])
    monkeypatch.setattr(transport_module, "BLEAK_BACKOFF_TIME", 0)
    monkeypatch.setattr(transport_module, "COMMAND_GAP", 0)
    protocol = SimpleNamespace(
        model_name="ELK-BLEDOM",
        model=SimpleNamespace(
            get_turn_on_cmd=lambda _name: [1], get_turn_off_cmd=lambda _name: [0]
        ),
        requires_login=lambda _name: False,
        uses_notifications=lambda _name: False,
        read_uuid=lambda: None,
        write_uuid=lambda: "write",
        refine_by_handle=lambda _handle: None,
    )
    harness.transport = BLETransport(first.address, SimpleNamespace(), 0, protocol=protocol)
    harness.device = ElkDevice(harness.transport, protocol, ElkState())
    harness.lookups.clear()
    return harness


@pytest.mark.parametrize(
    "message", ["ESP_GATT_CONN_FAIL_ESTABLISH", "connection slot", "ordinary connection failure"]
)
async def test_connection_failures_cannot_multiply_the_physical_budget(proxy_harness, message):
    proxy_harness.outcomes = [BleakError(message) for _ in range(30)]

    with pytest.raises(NotConnectedError):
        await proxy_harness.device.turn_on()

    assert len(proxy_harness.connects) == 3
    assert proxy_harness.writes == []
    assert proxy_harness.device.state.is_on is None
    assert proxy_harness.transport.available is False
    assert proxy_harness.transport._connection_budget is None
    await proxy_harness.transport.stop()


async def test_connector_timeout_retries_with_a_fresh_home_assistant_route(proxy_harness):
    proxy_harness.outcomes = [TimeoutError("transient ESPHome timeout"), None]
    proxy_harness.switch_route = True

    await proxy_harness.device.turn_on()

    assert [device.source for device in proxy_harness.lookups] == ["first", "second"]
    assert [device.source for device in proxy_harness.connects] == ["first", "second"]
    assert proxy_harness.device.state.is_on is True
    assert proxy_harness.writes == [b"\x01"]
    await proxy_harness.transport.stop()


async def test_repeated_connector_timeouts_exhaust_exactly_three_attempts(proxy_harness):
    proxy_harness.outcomes = [TimeoutError("ESPHome timeout") for _ in range(4)]

    with pytest.raises(NotConnectedError):
        await proxy_harness.device.turn_on()

    assert len(proxy_harness.connects) == 3
    assert len(proxy_harness.lookups) == 3
    await proxy_harness.transport.stop()


@pytest.mark.parametrize("failure", [TimeoutError, BleakError])
async def test_the_third_physical_connection_attempt_can_succeed(proxy_harness, failure):
    proxy_harness.outcomes = [failure("ESP_GATT_CONN_FAIL_ESTABLISH") for _ in range(2)]

    await proxy_harness.device.turn_on()

    assert len(proxy_harness.connects) == 3
    assert proxy_harness.device.state.is_on is True
    await proxy_harness.transport.stop()


async def test_genuinely_missing_device_still_fails_without_outer_retries(proxy_harness):
    proxy_harness.outcomes = [BleakError("org.freedesktop.DBus.Error.UnknownObject")]

    with pytest.raises(NotConnectedError):
        await proxy_harness.device.turn_on()

    assert len(proxy_harness.connects) == 1
    await proxy_harness.transport.stop()


async def test_missing_connectable_route_does_not_attempt_radio_io(proxy_harness):
    proxy_harness.current_route = None

    with pytest.raises(NotConnectedError, match="No connectable Bluetooth path"):
        await proxy_harness.device.turn_on()

    assert proxy_harness.connects == []
    await proxy_harness.transport.stop()


async def test_a_new_action_gets_a_new_budget_after_connection_failure(proxy_harness):
    proxy_harness.outcomes = [BleakError("ESP_GATT_CONN_FAIL_ESTABLISH") for _ in range(3)]
    with pytest.raises(NotConnectedError):
        await proxy_harness.device.turn_on()

    await proxy_harness.device.turn_on()

    assert len(proxy_harness.connects) == 4
    assert proxy_harness.device.state.is_on is True
    assert proxy_harness.transport.available is True
    await proxy_harness.transport.stop()


async def test_cancelled_connection_releases_the_operation_without_retrying(proxy_harness):
    proxy_harness.outcomes = [asyncio.CancelledError()]

    with pytest.raises(asyncio.CancelledError):
        await proxy_harness.device.turn_on()

    assert len(proxy_harness.connects) == 1
    assert proxy_harness.transport._connection_budget is None
    assert proxy_harness.transport._operation_lock.locked() is False
    await proxy_harness.device.turn_on()
    assert len(proxy_harness.connects) == 2
    await proxy_harness.transport.stop()


async def test_batched_intents_share_one_physical_connection_budget(proxy_harness):
    proxy_harness.outcomes = [TimeoutError("first attempt"), None]
    async with proxy_harness.transport.batch():
        await proxy_harness.device.turn_on()
        proxy_harness.transport._client.test_connected = False
        proxy_harness.outcomes = [BleakError("ESP_GATT_CONN_FAIL_ESTABLISH") for _ in range(3)]

        with pytest.raises(NotConnectedError):
            await proxy_harness.device.turn_off()

    assert len(proxy_harness.connects) == 3
    assert proxy_harness.writes == [b"\x01"]
    assert proxy_harness.device.state.is_on is True
    await proxy_harness.transport.stop()
