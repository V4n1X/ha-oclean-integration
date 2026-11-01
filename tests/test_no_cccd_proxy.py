"""Tests for subscribing to notify characteristics that have no CCCD.

Both cases come from issue #102 (OCLEANY3S behind an ESPHome BLE proxy):
fbb86 and fbb90 have no CCCD, the proxy rejects the descriptor write with
"Invalid handle" on the placeholder handle 0xFFFE, and bleak-esphome then also
removes the subscription it had already registered on the proxy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak import BleakError

from custom_components.oclean_ble.const import (
    DATA_BATTERY,
    READ_NOTIFY_CHAR_UUID,
    RECEIVE_BRUSH_UUID,
)
from custom_components.oclean_ble.coordinator import (
    OcleanCoordinator,
    _is_proxy_client,
    _try_subscribe_no_cccd_via_proxy,
    err_is_invalid_handle,
)
from custom_components.oclean_ble.protocol import TYPE1, TYPE1_Y3S

# Notification bytes from the real capture (snoop13): battery 8 %, device info.
BATTERY_NOTIFY = bytes.fromhex("0303020dc4080200")
DEVICE_INFO_NOTIFY = bytes.fromhex("03022324000000000000000000ff00000000029f")
_INVALID_HANDLE = "Bluetooth GATT Error address=70:28:45:68:4A:77 handle=65534 error=1 description=Invalid handle"
_NO_CCCD = "proxy: Oclean X: Characteristic fbb86 does not have a characteristic client config descriptor."


def _make_coordinator(protocol=TYPE1) -> OcleanCoordinator:
    hass = MagicMock()
    hass.data = {}
    coord = OcleanCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Oclean", 300)
    coord._store_loaded = True
    coord._protocol = protocol
    return coord


class RealSequenceProxyClient:
    """bleak-esphome's start_notify on a characteristic without CCCD, step by step.

    First call: the proxy registration is made and removed again, then
    "does not have a characteristic client config descriptor" is raised. Once a
    placeholder CCCD was injected, bleak-esphome writes it (an ATT write to handle
    0xFFFE that reaches the brush) and raises "Invalid handle".
    """

    def __init__(self) -> None:
        self.public_calls: dict[str, int] = {}
        self.placeholder_writes = 0
        self.proxy_registrations: list[int] = []
        chars = {}
        for uuid, handle in ((READ_NOTIFY_CHAR_UUID, 0x17), (RECEIVE_BRUSH_UUID, 0x1F)):
            char = MagicMock()
            char.uuid = uuid
            char.properties = ["read", "notify"]
            char.handle = handle
            char.get_descriptor.return_value = None
            chars[uuid] = char
        self.services = MagicMock()
        self.services.get_characteristic.side_effect = chars.get
        self._backend = MagicMock()
        self._backend._address_as_int = 0x77284A685F452870
        self._backend._notify_cancels = {}
        api = MagicMock()

        async def _start_notify(address, handle, callback):
            self.proxy_registrations.append(handle)
            return (AsyncMock(), lambda: None)

        api.bluetooth_gatt_start_notify = AsyncMock(side_effect=_start_notify)
        self._backend._client = api

        async def _public_start_notify(uuid, handler):
            self.public_calls[uuid] = self.public_calls.get(uuid, 0) + 1
            if self.public_calls[uuid] == 1:
                raise BleakError(_NO_CCCD)
            self.placeholder_writes += 1
            raise BleakError(_INVALID_HANDLE)

        self.start_notify = AsyncMock(side_effect=_public_start_notify)
        self.stop_notify = AsyncMock()
        self.write_gatt_descriptor = AsyncMock()


class FakeProxyClient:
    """A BleakClient the way habluetooth wraps an ESPHome proxy backend."""

    def __init__(self, *, reject_descriptor: bool = True, register_ok: bool = True) -> None:
        self.register_ok = register_ok
        self.proxy_registrations: list[tuple[int, object]] = []
        self.descriptor_writes = 0
        self.handlers: dict[str, object] = {}
        char = MagicMock()
        char.properties = ["read", "notify"]
        char.handle = 0x17
        self.services = MagicMock()
        self.services.get_characteristic.return_value = char
        self._backend = MagicMock()
        self._backend._address_as_int = 0x77284A685F452870
        self._backend._notify_cancels = {}
        api = MagicMock()

        async def _start_notify(address, handle, callback):
            if not self.register_ok:
                raise BleakError("proxy refused the subscription")
            self.proxy_registrations.append((handle, callback))

            async def _stop():
                return None

            return (_stop, lambda: None)

        api.bluetooth_gatt_start_notify = AsyncMock(side_effect=_start_notify)
        self._backend._client = api

        async def _start_notify_public(uuid, handler):
            self.handlers[uuid] = handler
            if reject_descriptor:
                raise BleakError(_INVALID_HANDLE)

        self.start_notify = AsyncMock(side_effect=_start_notify_public)
        self.stop_notify = AsyncMock()


class TestInvalidHandleDetection:
    def test_proxy_placeholder_handle(self):
        assert err_is_invalid_handle(BleakError(_INVALID_HANDLE)) is True

    def test_real_invalid_handle_is_not_masked(self):
        # A genuinely stale handle has a real handle number, not the placeholder.
        assert err_is_invalid_handle(BleakError("Invalid handle, handle=0x0019")) is False
        assert err_is_invalid_handle(BleakError("Notify acquired")) is False


class TestProxySubscribe:
    @pytest.mark.asyncio
    async def test_registers_without_descriptor_write(self):
        client = FakeProxyClient()
        handler = MagicMock()
        assert await _try_subscribe_no_cccd_via_proxy(client, READ_NOTIFY_CHAR_UUID, handler, MagicMock()) is True
        assert len(client.proxy_registrations) == 1
        handle, callback = client.proxy_registrations[0]
        assert handle == 0x17
        callback(0x17, BATTERY_NOTIFY)
        handler.assert_called_once()
        assert client.descriptor_writes == 0

    @pytest.mark.asyncio
    async def test_registration_is_tracked_for_teardown(self):
        client = FakeProxyClient()
        await _try_subscribe_no_cccd_via_proxy(client, READ_NOTIFY_CHAR_UUID, MagicMock(), MagicMock())
        assert 0x17 in client._backend._notify_cancels

    @pytest.mark.asyncio
    async def test_already_subscribed_is_not_registered_twice(self):
        client = FakeProxyClient()
        client._backend._notify_cancels[0x17] = (lambda: None, lambda: None)
        assert await _try_subscribe_no_cccd_via_proxy(client, READ_NOTIFY_CHAR_UUID, MagicMock(), MagicMock()) is True
        assert client.proxy_registrations == []

    @pytest.mark.asyncio
    async def test_not_a_proxy_connection(self):
        client = MagicMock(spec=["services", "start_notify"])
        assert await _try_subscribe_no_cccd_via_proxy(client, READ_NOTIFY_CHAR_UUID, MagicMock(), MagicMock()) is False

    @pytest.mark.asyncio
    async def test_proxy_refuses(self):
        client = FakeProxyClient(register_ok=False)
        assert await _try_subscribe_no_cccd_via_proxy(client, READ_NOTIFY_CHAR_UUID, MagicMock(), MagicMock()) is False

    @pytest.mark.asyncio
    async def test_characteristic_without_notify_property(self):
        client = FakeProxyClient()
        client.services.get_characteristic.return_value.properties = ["read", "write"]
        assert await _try_subscribe_no_cccd_via_proxy(client, READ_NOTIFY_CHAR_UUID, MagicMock(), MagicMock()) is False


class TestSubscribeNotificationsFallsBack:
    async def _subscribe(self, coordinator, client):
        with patch("custom_components.oclean_ble.coordinator.asyncio.sleep", new_callable=AsyncMock):
            return await coordinator._subscribe_notifications(client, MagicMock())

    @pytest.mark.asyncio
    async def test_y3s_registers_on_the_proxy_without_placeholder_writes(self):
        client = RealSequenceProxyClient()
        subscribed = await self._subscribe(_make_coordinator(TYPE1_Y3S), client)
        assert READ_NOTIFY_CHAR_UUID in subscribed
        assert RECEIVE_BRUSH_UUID in subscribed
        assert client.proxy_registrations == [0x17, 0x1F]
        # No placeholder CCCD: nothing is written to handle 0xFFFE on the brush.
        assert client.placeholder_writes == 0
        assert all(n == 1 for n in client.public_calls.values())

    @pytest.mark.asyncio
    async def test_y3s_treats_placeholder_invalid_handle_as_no_cccd(self):
        client = RealSequenceProxyClient()
        client.start_notify = AsyncMock(side_effect=BleakError(_INVALID_HANDLE))
        subscribed = await self._subscribe(_make_coordinator(TYPE1_Y3S), client)
        assert READ_NOTIFY_CHAR_UUID in subscribed

    @pytest.mark.asyncio
    async def test_other_models_keep_the_original_path(self):
        # Not verified on other models: their no-CCCD handling stays exactly as before
        # (placeholder path, which a proxy rejects; no direct proxy registration).
        client = RealSequenceProxyClient()
        subscribed = await self._subscribe(_make_coordinator(TYPE1), client)
        assert READ_NOTIFY_CHAR_UUID not in subscribed
        assert client.proxy_registrations == []

    @pytest.mark.asyncio
    async def test_other_models_ignore_invalid_handle(self):
        client = RealSequenceProxyClient()
        client.start_notify = AsyncMock(side_effect=BleakError(_INVALID_HANDLE))
        subscribed = await self._subscribe(_make_coordinator(TYPE1), client)
        assert subscribed == frozenset()
        assert client.proxy_registrations == []

    @pytest.mark.asyncio
    async def test_unknown_reason_is_not_treated_as_no_cccd(self):
        client = RealSequenceProxyClient()
        client.start_notify = AsyncMock(side_effect=BleakError("Connection reset"))
        subscribed = await self._subscribe(_make_coordinator(TYPE1_Y3S), client)
        assert READ_NOTIFY_CHAR_UUID not in subscribed
        assert client.proxy_registrations == []


class TestIsProxyClient:
    def test_proxy_backend(self):
        assert _is_proxy_client(RealSequenceProxyClient()) is True

    def test_local_backend(self):
        client = MagicMock()
        client._backend = MagicMock(spec=["start_notify"])
        assert _is_proxy_client(client) is False

    def test_no_backend(self):
        assert _is_proxy_client(MagicMock(spec=["start_notify"])) is False


class TestModelIsPinned:
    """A model identified once must never fall back to the generic UNKNOWN profile."""

    def _coordinator(self, model: str | None = None) -> OcleanCoordinator:
        coord = _make_coordinator()
        coord._identified_model = model
        return coord

    @pytest.mark.asyncio
    async def test_failed_dis_read_keeps_the_identified_model(self):
        coord = self._coordinator("OCLEANY3S")
        client = AsyncMock()
        client.read_gatt_char = AsyncMock(side_effect=BleakError("Invalid handle"))
        collected: dict = {}
        with patch("custom_components.oclean_ble.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._read_device_info_service(client, collected)
        assert collected["model_id"] == "OCLEANY3S"
        assert coord._protocol.verified_y3s_flow is True

    @pytest.mark.asyncio
    async def test_identified_model_is_used_before_the_next_poll(self):
        coord = self._coordinator("OCLEANY3S")
        client = AsyncMock()
        client.read_gatt_char = AsyncMock(side_effect=BleakError("Invalid handle"))
        with (
            patch("custom_components.oclean_ble.coordinator.asyncio.sleep", new_callable=AsyncMock),
            patch.object(coord, "_run_ble_queries", new_callable=AsyncMock) as queries,
        ):
            await coord._setup_and_read(client, {})
        assert coord._protocol.verified_y3s_flow is True
        queries.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_model_is_persisted(self):
        coord = self._coordinator()
        client = AsyncMock()
        client.read_gatt_char = AsyncMock(return_value=bytearray(b"OCLEANY3S"))
        with patch("custom_components.oclean_ble.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._read_device_info_service(client, {})
        assert coord._identified_model == "OCLEANY3S"

        saved: list[dict] = []

        async def _fake_save(data):
            saved.append(data)

        coord._store.async_save = _fake_save
        await coord._save_store()
        assert saved[-1]["identified_model"] == "OCLEANY3S"

    @pytest.mark.asyncio
    async def test_model_is_restored_from_store(self):
        coord = self._coordinator()
        coord._store.async_load = AsyncMock(return_value={"identified_model": "OCLEANY3S"})
        await coord._load_store()
        assert coord._identified_model == "OCLEANY3S"

    def test_never_identified_stays_unknown(self):
        coord = self._coordinator()
        assert coord._identified_model is None
