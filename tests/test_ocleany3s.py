"""Tests for the hardware-verified OCLEANY3S (Oclean X Pro) protocol.

All notification bytes below are copied from HCI captures of the official app
talking to an Oclean X Pro (model OCLEANY3S, firmware 1.0.0.19).
"""

from __future__ import annotations

import asyncio
import calendar
import copy
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak import BleakError

from custom_components.oclean_ble.const import (
    DATA_BATTERY,
    DATA_BRUSH_HEAD_DAYS,
    DATA_BRUSH_HEAD_USAGE,
    DATA_BRUSH_MODE,
    DATA_LAST_BRUSH_AREAS,
    DATA_LAST_BRUSH_COVERAGE,
    DATA_LAST_BRUSH_DURATION,
    DATA_LAST_BRUSH_DURATION_SCHEDULED,
    DATA_LAST_BRUSH_GESTURE_ARRAY,
    DATA_LAST_BRUSH_GESTURE_CODE,
    DATA_LAST_BRUSH_PNUM,
    DATA_LAST_BRUSH_PRESSURE_CODE,
    DATA_LAST_BRUSH_PRESSURE_RATIO,
    DATA_LAST_BRUSH_SCORE,
    DATA_LAST_BRUSH_TIME,
    DATA_MODEL_ID,
    READ_NOTIFY_CHAR_UUID,
    RECEIVE_BRUSH_UUID,
    SEND_BRUSH_CMD_UUID,
    WRITE_CHAR_UUID,
)
from custom_components.oclean_ble.coordinator import OcleanCoordinator, _Y3sLink
from custom_components.oclean_ble.parser import (
    Y3S_BATTERY_ON_CHARGER,
    Y3S_BATTERY_STORED_SESSIONS,
    Y3S_BATTERY_VOLTAGE_MV,
    Y3S_INFO_ACTIVE_PNUM,
    Y3S_INFO_AREA_REMIND,
    Y3S_INFO_HEAD_MAX_TIME,
    Y3S_INFO_HEAD_USED_MINUTES,
    Y3S_INFO_LANGUAGE,
    Y3S_INFO_OVER_PRESSURE,
    Y3sRunningDataStream,
    parse_y3s_battery,
    parse_y3s_device_info,
    parse_y3s_record,
)
from custom_components.oclean_ble.protocol import TYPE1, TYPE1_Y3S, protocol_for_model

# ---------------------------------------------------------------------------
# Captured wire data (snoop13, 2026-10-31, brush off the charger)
# ---------------------------------------------------------------------------

DEVICE_INFO_NOTIFIES = [
    bytes.fromhex("03022324000000000000000000ff00000000029f"),
    bytes.fromhex("03021a0a1f0e2e1500010f00f0007f017d06000000"),
]
BATTERY_REPLY = bytes.fromhex("0303020dc4080200")  # off charger, 3524 mV, 8 %, 2 sessions stored
TIME_ACK = bytes.fromhex("02014f4b")
CLEAR_ACK = bytes.fromhex("02024f4b")
DUMP_TWO_RECORDS = [
    bytes.fromhex("03072a422300021a0a1f06171800007800780807"),
    bytes.fromhex("520000000f001c200a1e0a0e10100a000e100000"),
    bytes.fromhex("5a00ffffffffffffff1a0a1f0619190000780078"),
    bytes.fromhex("0807520000000f0019111d1d00190011110b110a"),
    bytes.fromhex("00005300ffffffffffffff000105120901000078"),
]
DUMP_EMPTY = [bytes.fromhex("03072a422300001a0a1f06171800007800780807")]
# Setting commands and the bytes the brush puts between the echo and "OK" (snoop16/18).
SETTING_ACKS = {
    b"\x02\x06": lambda _d: b"",
    b"\x02\x0b": lambda _d: b"",
    b"\x02\x0f": lambda _d: b"",
    b"\x02\x0d": lambda d: d[2:3] + b"\x1e",
    b"\x02\x12": lambda d: d[2:3],
}


def _records_from(packets: list[bytes]) -> list[bytes]:
    stream = Y3sRunningDataStream()
    for pkt in packets:
        stream.feed(pkt)
    return stream.records()


def _battery_reply(level: int, stored: int) -> bytes:
    return bytes([0x03, 0x03, 0x02, 0x0D, 0xC4, level, stored, 0x00])


# ---------------------------------------------------------------------------
# Protocol mapping
# ---------------------------------------------------------------------------


class TestProtocolMapping:
    def test_y3s_uses_verified_profile(self):
        assert protocol_for_model("OCLEANY3S") is TYPE1_Y3S
        assert TYPE1_Y3S.verified_y3s_flow is True

    @pytest.mark.parametrize("model", ["OCLEANY3", "OCLEANY3T", "OCLEANY3M", "OCLEANY3P", "UNMAPPED"])
    def test_other_models_unchanged(self, model):
        assert protocol_for_model(model) is TYPE1
        assert TYPE1.verified_y3s_flow is False

    def test_profile_characteristics(self):
        assert TYPE1_Y3S.write_char == WRITE_CHAR_UUID
        assert TYPE1_Y3S.notify_chars == (READ_NOTIFY_CHAR_UUID, RECEIVE_BRUSH_UUID)
        assert TYPE1_Y3S.uses_t1_calibration is True


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


class TestParseDeviceInfo:
    def _payload(self) -> bytes:
        # Length byte 0x24 counts two header bytes -> 34 payload bytes; the second
        # notification carries one byte more, which the SDK ignores as well.
        return (DEVICE_INFO_NOTIFIES[0][4:] + DEVICE_INFO_NOTIFIES[1][2:])[: 0x24 - 2]

    def test_payload_length(self):
        assert len(self._payload()) == 34

    @pytest.mark.asyncio
    async def test_link_reassembles_two_notifications(self):
        fake = FakeY3s()
        link = _Y3sLink(MagicMock())
        fake.handlers[READ_NOTIFY_CHAR_UUID] = link.on_notify
        assert await link.request(fake.client(), b"\x03\x02\x01") == self._payload()

    def test_fields(self):
        info = parse_y3s_device_info(self._payload())
        assert info[DATA_BRUSH_MODE] == 0
        assert info[DATA_BRUSH_HEAD_DAYS] == 127
        assert info[DATA_BRUSH_HEAD_USAGE] == 381
        assert info[Y3S_INFO_ACTIVE_PNUM] == 0
        assert info[Y3S_INFO_OVER_PRESSURE] is False
        assert info[Y3S_INFO_AREA_REMIND] is True
        assert info[Y3S_INFO_HEAD_USED_MINUTES] == 671
        assert info[Y3S_INFO_HEAD_MAX_TIME] == 240
        assert info[Y3S_INFO_LANGUAGE] == 6

    def test_after_brush_head_reset(self):
        # Device info captured right after 020F (snoop18): counters 0, max time kept.
        payload = bytes.fromhex("000000000000000000ff0000010000001a0a1f10211200010f00f000000000060000")
        info = parse_y3s_device_info(payload)
        assert info[DATA_BRUSH_MODE] == 1
        assert info[DATA_BRUSH_HEAD_DAYS] == 0
        assert info[DATA_BRUSH_HEAD_USAGE] == 0
        assert info[Y3S_INFO_HEAD_MAX_TIME] == 240

    def test_too_short(self):
        assert parse_y3s_device_info(b"\x00" * 33) == {}


class TestParseBattery:
    def test_off_charger(self):
        result = parse_y3s_battery(BATTERY_REPLY[2:])
        assert result[DATA_BATTERY] == 8
        assert result[Y3S_BATTERY_STORED_SESSIONS] == 2
        assert result[Y3S_BATTERY_VOLTAGE_MV] == 3524
        assert result[Y3S_BATTERY_ON_CHARGER] is False

    def test_on_charger(self):
        result = parse_y3s_battery(bytes.fromhex("010d48030000"))
        assert result[Y3S_BATTERY_ON_CHARGER] is True
        assert result[DATA_BATTERY] == 3
        assert result[Y3S_BATTERY_STORED_SESSIONS] == 0

    def test_too_short(self):
        assert parse_y3s_battery(b"\x02\x0d") == {}


class TestRunningDataStream:
    def test_two_records(self):
        stream = Y3sRunningDataStream()
        for pkt in DUMP_TWO_RECORDS:
            stream.feed(pkt)
        assert stream.count == 2
        assert stream.complete
        records = stream.records()
        assert len(records) == 2
        assert all(len(r) == 42 for r in records)

    def test_stale_padding_ignored(self):
        records = _records_from(DUMP_TWO_RECORDS)
        # The last notification carries 9 stale bytes after the second record.
        assert records[1][-7:] == b"\xff" * 7

    def test_empty_store(self):
        stream = Y3sRunningDataStream()
        stream.feed(DUMP_EMPTY[0])
        assert stream.count == 0
        assert stream.complete
        assert stream.records() == []

    def test_incomplete(self):
        stream = Y3sRunningDataStream()
        for pkt in DUMP_TWO_RECORDS[:3]:
            stream.feed(pkt)
        assert not stream.complete
        assert len(stream.records()) == 1

    def test_data_before_header_ignored(self):
        stream = Y3sRunningDataStream()
        stream.feed(bytes.fromhex("02014f4b"))
        assert stream.count is None
        assert not stream.complete


class TestParseRecord:
    def test_first_record(self):
        record = _records_from(DUMP_TWO_RECORDS)[0]
        result = parse_y3s_record(record)
        # 2026-10-31 06:23:24 at time-zone index 15 (+01:00)
        expected = calendar.timegm(datetime.datetime(2026, 10, 31, 5, 23, 24).timetuple())
        assert result[DATA_LAST_BRUSH_TIME] == expected
        assert result[DATA_LAST_BRUSH_PNUM] == 0
        assert result[DATA_LAST_BRUSH_DURATION] == 120
        assert result[DATA_LAST_BRUSH_DURATION_SCHEDULED] == 120
        assert result[DATA_LAST_BRUSH_SCORE] == 90
        assert result[DATA_LAST_BRUSH_PRESSURE_RATIO] == [8, 7, 82, 0, 0]
        assert result[DATA_LAST_BRUSH_PRESSURE_CODE] == 70
        assert result[DATA_LAST_BRUSH_GESTURE_CODE] == 0
        assert len(result[DATA_LAST_BRUSH_GESTURE_ARRAY]) == 13
        assert result[DATA_LAST_BRUSH_GESTURE_ARRAY][1:5] == [28, 32, 10, 30]
        assert result[DATA_LAST_BRUSH_AREAS] == {
            "upper_left_out": 10,
            "upper_left_in": 14,
            "lower_left_out": 16,
            "lower_left_in": 16,
            "upper_right_out": 10,
            "upper_right_in": 0,
            "lower_right_out": 14,
            "lower_right_in": 16,
        }
        # 7 of 8 zones reach 9 s (share x 120 s / 96); the empty zone does not.
        assert result[DATA_LAST_BRUSH_COVERAGE] == 88

    def test_second_record(self):
        result = parse_y3s_record(_records_from(DUMP_TWO_RECORDS)[1])
        assert result[DATA_LAST_BRUSH_SCORE] == 83
        assert result[DATA_LAST_BRUSH_DURATION] == 120

    def test_short_valid_duration_is_used(self):
        record = bytearray(_records_from(DUMP_TWO_RECORDS)[0])
        record[9:11] = (24).to_bytes(2, "big")
        result = parse_y3s_record(bytes(record))
        assert result[DATA_LAST_BRUSH_DURATION] == 24
        assert result[DATA_LAST_BRUSH_DURATION_SCHEDULED] == 120

    def test_unset_clock_record_skipped(self):
        record = bytearray(_records_from(DUMP_TWO_RECORDS)[0])
        record[0] = 0  # year 2000: clock lost after a full discharge
        assert parse_y3s_record(bytes(record)) == {}

    def test_invalid_date_skipped(self):
        record = bytearray(_records_from(DUMP_TWO_RECORDS)[0])
        record[1] = 13
        assert parse_y3s_record(bytes(record)) == {}

    def test_score_zero_omitted(self):
        record = bytearray(_records_from(DUMP_TWO_RECORDS)[0])
        record[33] = 0
        assert DATA_LAST_BRUSH_SCORE not in parse_y3s_record(bytes(record))

    def test_unknown_tz_index_falls_back_to_local_time(self):
        record = bytearray(_records_from(DUMP_TWO_RECORDS)[0])
        record[17] = 0
        assert parse_y3s_record(bytes(record))[DATA_LAST_BRUSH_TIME] > 0

    def test_too_short(self):
        assert parse_y3s_record(b"\x1a" * 41) == {}


# ---------------------------------------------------------------------------
# Fake brush
# ---------------------------------------------------------------------------


class FakeY3s:
    """Answers like the captured OCLEANY3S.

    Replies are notified during the write, with the real characteristic as sender.
    late_info: the second device-info notify arrives after the write returned
    (as in snoop8). stray: an fbb86 notification sent in the middle of the dump.
    proxy=True on client(): an ESPHome proxy connection whose public start_notify
    fails on fbb86/fbb90 for lack of a CCCD, as bleak-esphome does.
    """

    def __init__(
        self,
        battery: bytes = BATTERY_REPLY,
        dump: list[bytes] | None = None,
        no_notify: tuple[str, ...] = (),
        ack_time: bool = True,
        ack_clear: bool = True,
        late_info: bool = False,
        stray: bytes | None = None,
    ) -> None:
        self.battery = battery
        self.dump = DUMP_TWO_RECORDS if dump is None else dump
        self.no_notify = no_notify
        self.ack_time = ack_time
        self.ack_clear = ack_clear
        self.late_info = late_info
        self.stray = stray
        self.handlers: dict[str, object] = {}
        self.writes: list[tuple[str, bytes]] = []
        self.on_clear = None  # optional hook, called when 0202 is written

    async def start_notify(self, uuid, handler):
        if uuid in self.no_notify:
            raise RuntimeError("notify not possible")
        self.handlers[uuid] = handler

    def _notify(self, uuid: str, data: bytes) -> None:
        handler = self.handlers.get(uuid)
        if handler is not None:
            handler(SimpleNamespace(uuid=uuid), bytearray(data))

    async def write_gatt_char(self, uuid, data, response=True):
        data = bytes(data)
        self.writes.append((uuid, data))
        if uuid == SEND_BRUSH_CMD_UUID and data == b"\x03\x07":
            for i, pkt in enumerate(self.dump):
                if self.stray is not None and i == 1:
                    self._notify(READ_NOTIFY_CHAR_UUID, self.stray)
                self._notify(RECEIVE_BRUSH_UUID, pkt)
        elif uuid == WRITE_CHAR_UUID and data == b"\x03\x02\x01":
            self._notify(READ_NOTIFY_CHAR_UUID, DEVICE_INFO_NOTIFIES[0])
            if self.late_info:
                asyncio.get_running_loop().call_soon(self._notify, READ_NOTIFY_CHAR_UUID, DEVICE_INFO_NOTIFIES[1])
            else:
                self._notify(READ_NOTIFY_CHAR_UUID, DEVICE_INFO_NOTIFIES[1])
        elif uuid == WRITE_CHAR_UUID and data == b"\x03\x03":
            self._notify(READ_NOTIFY_CHAR_UUID, self.battery)
        elif uuid == WRITE_CHAR_UUID and data[:2] == b"\x02\x01" and len(data) == 10:
            if self.ack_time:
                self._notify(READ_NOTIFY_CHAR_UUID, TIME_ACK)
        elif uuid == WRITE_CHAR_UUID and data == b"\x02\x02":
            if self.on_clear is not None:
                self.on_clear()
            if self.ack_clear:
                self._notify(READ_NOTIFY_CHAR_UUID, CLEAR_ACK)
        elif uuid == WRITE_CHAR_UUID and data[:2] in SETTING_ACKS:
            self._notify(READ_NOTIFY_CHAR_UUID, data[:2] + SETTING_ACKS[data[:2]](data) + b"OK")
        else:
            self._notify(READ_NOTIFY_CHAR_UUID, b"\x02")

    async def read_gatt_char(self, uuid):
        values = {
            "00002a24-0000-1000-8000-00805f9b34fb": b"OCLEANY3S",
            "00002a27-0000-1000-8000-00805f9b34fb": b"Rev.D",
            "00002a28-0000-1000-8000-00805f9b34fb": b"1.0.0.19",
        }
        return bytearray(values.get(uuid, b"\x08"))

    def client(self, proxy: bool = False):
        client = AsyncMock()
        client.start_notify = AsyncMock(side_effect=self.start_notify)
        client.write_gatt_char = AsyncMock(side_effect=self.write_gatt_char)
        client.read_gatt_char = AsyncMock(side_effect=self.read_gatt_char)
        client.stop_notify = AsyncMock()
        client.disconnect = AsyncMock()
        client.services = MagicMock()
        client.services.get_characteristic.return_value = None
        client._backend = None
        if proxy:
            self._make_proxy(client)
        return client

    def _make_proxy(self, client) -> None:
        chars = {
            READ_NOTIFY_CHAR_UUID: SimpleNamespace(
                uuid=READ_NOTIFY_CHAR_UUID, handle=0x17, properties=["read", "notify"]
            ),
            RECEIVE_BRUSH_UUID: SimpleNamespace(uuid=RECEIVE_BRUSH_UUID, handle=0x1F, properties=["read", "notify"]),
        }
        client.services.get_characteristic.side_effect = chars.get
        self.proxy_registrations: list[int] = []

        async def public_start_notify(uuid, handler):
            if uuid in chars:
                raise BleakError(
                    f"proxy: Characteristic {uuid} does not have a characteristic client config descriptor."
                )
            self.handlers[uuid] = handler

        async def proxy_start_notify(address, handle, callback):
            uuid = next(u for u, c in chars.items() if c.handle == handle)
            self.proxy_registrations.append(handle)
            self.handlers[uuid] = lambda _sender, data: callback(handle, data)
            return (AsyncMock(), lambda: None)

        client.start_notify = AsyncMock(side_effect=public_start_notify)
        backend = MagicMock()
        backend._address_as_int = 0x77284A685F452870
        backend._notify_cancels = {}
        backend._client.bluetooth_gatt_start_notify = AsyncMock(side_effect=proxy_start_notify)
        client._backend = backend

    def commands(self) -> list[tuple[str, str]]:
        """Writes as (characteristic suffix, opcode hex) – time frame shortened to 0201."""
        return [(uuid[-4:], data[:2].hex() if data[:2] == b"\x02\x01" else data.hex()) for uuid, data in self.writes]


def _make_coordinator(cached_model: str | None = "OCLEANY3S") -> OcleanCoordinator:
    hass = MagicMock()
    hass.data = {}
    coordinator = OcleanCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Oclean", 300)
    if cached_model:
        coordinator._last_raw[DATA_MODEL_ID] = cached_model
    return coordinator


async def _poll(fake: FakeY3s, coordinator: OcleanCoordinator | None = None, proxy: bool = False):
    coordinator = coordinator or _make_coordinator()
    collected: dict = {}
    with (
        patch("custom_components.oclean_ble.coordinator.asyncio.sleep", new_callable=AsyncMock),
        patch("custom_components.oclean_ble.coordinator.Y3S_STREAM_IDLE_TIMEOUT", 0.05),
        patch("custom_components.oclean_ble.coordinator.Y3S_REPLY_TIMEOUT", 0.05),
    ):
        sessions = await coordinator._setup_and_read(fake.client(proxy=proxy), collected)
    return coordinator, collected, sessions


# ---------------------------------------------------------------------------
# Poll flow
# ---------------------------------------------------------------------------


class TestY3sPoll:
    @pytest.mark.asyncio
    async def test_command_sequence_matches_official_app(self):
        fake = FakeY3s()
        await _poll(fake)
        assert fake.commands() == [
            ("bb85", "030201"),
            ("bb85", "0201"),
            ("bb85", "0303"),
            ("bb89", "0307"),
            ("bb85", "0202"),
        ]

    @pytest.mark.asyncio
    async def test_sessions_and_snapshot(self):
        _, collected, sessions = await _poll(FakeY3s())
        assert len(sessions) == 2
        assert collected[DATA_LAST_BRUSH_SCORE] == 83  # the newer of the two records
        assert collected[DATA_LAST_BRUSH_TIME] == max(s[DATA_LAST_BRUSH_TIME] for s in sessions)

    @pytest.mark.asyncio
    async def test_device_values(self):
        coordinator, collected, _ = await _poll(FakeY3s())
        assert collected[DATA_BATTERY] == 8
        assert collected[DATA_BRUSH_HEAD_USAGE] == 381
        assert collected[DATA_BRUSH_HEAD_DAYS] == 127
        assert collected[DATA_BRUSH_MODE] == 0
        # Settings are read back from the brush instead of being assumed.
        assert coordinator.area_remind is True
        assert coordinator.over_pressure is False
        assert coordinator.active_scheme_pnum == 0

    @pytest.mark.asyncio
    async def test_time_frame(self):
        fake = FakeY3s()
        await _poll(fake)
        frame = fake.writes[1][1]
        assert frame[:2] == b"\x02\x01"
        assert len(frame) == 10
        assert 1 <= frame[9] <= 33

    @pytest.mark.asyncio
    async def test_empty_store_sends_no_dump(self):
        fake = FakeY3s(battery=_battery_reply(level=50, stored=0))
        _, _, sessions = await _poll(fake)
        assert ("bb89", "0307") not in fake.commands()
        assert ("bb85", "0202") not in fake.commands()
        assert sessions == []

    @pytest.mark.asyncio
    async def test_large_store_is_not_dumped(self, caplog):
        fake = FakeY3s(battery=_battery_reply(level=50, stored=11))
        await _poll(fake)
        assert ("bb89", "0307") not in fake.commands()
        assert ("bb85", "0202") not in fake.commands()
        assert "Clear Stored Sessions" in caplog.text

    @pytest.mark.asyncio
    async def test_low_battery_is_not_dumped(self):
        fake = FakeY3s(battery=_battery_reply(level=2, stored=2))
        await _poll(fake)
        assert ("bb89", "0307") not in fake.commands()

    @pytest.mark.asyncio
    async def test_no_battery_reply_is_not_dumped(self):
        fake = FakeY3s(battery=b"\x02")  # unsupported -> no stored-session count
        await _poll(fake)
        assert ("bb89", "0307") not in fake.commands()

    @pytest.mark.asyncio
    async def test_incomplete_dump_keeps_sessions_on_brush(self):
        fake = FakeY3s(dump=DUMP_TWO_RECORDS[:3])
        _, _, sessions = await _poll(fake)
        assert ("bb85", "0202") not in fake.commands()
        assert fake.commands().count(("bb89", "0307")) == 1  # never re-sent
        assert len(sessions) == 1  # the complete first record is still imported

    @pytest.mark.asyncio
    async def test_silent_dump_keeps_sessions_on_brush(self, caplog):
        fake = FakeY3s(dump=[])
        _, _, sessions = await _poll(fake)
        assert ("bb85", "0202") not in fake.commands()
        assert sessions == []
        assert "dump stopped after 0 of 0 bytes" in caplog.text

    @pytest.mark.asyncio
    async def test_header_with_zero_count_sends_no_clear(self):
        fake = FakeY3s(dump=DUMP_EMPTY)
        _, collected, sessions = await _poll(fake)
        assert ("bb85", "0202") not in fake.commands()
        assert sessions == []
        assert DATA_LAST_BRUSH_TIME not in collected  # stale padding is not a session

    @pytest.mark.asyncio
    async def test_no_notifications_no_commands(self):
        fake = FakeY3s(no_notify=(READ_NOTIFY_CHAR_UUID, RECEIVE_BRUSH_UUID))
        coordinator, _, _ = await _poll(fake)
        assert fake.writes == []
        assert coordinator._last_subscribe_ok is False

    @pytest.mark.asyncio
    async def test_no_stream_subscription_no_dump(self):
        fake = FakeY3s(no_notify=(RECEIVE_BRUSH_UUID,))
        _, collected, _ = await _poll(fake)
        assert ("bb89", "0307") not in fake.commands()
        assert collected[DATA_BATTERY] == 8

    @pytest.mark.asyncio
    async def test_missing_acks_do_not_abort_the_poll(self, caplog):
        fake = FakeY3s(ack_time=False, ack_clear=False)
        _, _, sessions = await _poll(fake)
        assert len(sessions) == 2
        assert "time sync was not acknowledged" in caplog.text
        assert "clearing the session store was not acknowledged" in caplog.text

    @pytest.mark.asyncio
    async def test_duplicate_records_imported_once(self):
        first = _records_from(DUMP_TWO_RECORDS)[0]
        payload = first + first
        dump = [b"\x03\x07*B#\x00\x02" + payload[:13]] + [payload[i : i + 20] for i in range(13, len(payload), 20)]
        _, _, sessions = await _poll(FakeY3s(dump=dump))
        assert len(sessions) == 1

    @pytest.mark.asyncio
    async def test_unreadable_device_info_keeps_poll_going(self):
        fake = FakeY3s()
        coordinator = _make_coordinator()
        coordinator._apply_y3s_device_info({}, {})
        _, collected, _ = await _poll(fake, coordinator)
        assert collected[DATA_BATTERY] == 8

    @pytest.mark.asyncio
    async def test_first_poll_identifies_model_before_writing(self):
        # No cached model: the DIS read must select the Y3S profile before the
        # first write, so the brush never gets the generic 020E time frame.
        fake = FakeY3s()
        coordinator = _make_coordinator(cached_model=None)
        await _poll(fake, coordinator)
        assert fake.writes[0] == (WRITE_CHAR_UUID, b"\x03\x02\x01")
        assert all(data[:2] != b"\x02\x0e" for _, data in fake.writes)


# ---------------------------------------------------------------------------
# Request/reply channel
# ---------------------------------------------------------------------------


class TestY3sLink:
    @pytest.mark.asyncio
    async def test_unsolicited_notification_ignored(self):
        link = _Y3sLink(MagicMock())
        link.on_notify(None, bytearray(TIME_ACK))  # nothing in flight
        assert link._reply is None

    @pytest.mark.asyncio
    async def test_unsupported_command(self):
        fake = FakeY3s()
        link = _Y3sLink(MagicMock())
        fake.handlers[READ_NOTIFY_CHAR_UUID] = link.on_notify
        assert await link.command(fake.client(), bytes.fromhex("022100")) is False

    @pytest.mark.asyncio
    async def test_foreign_prefix_ignored(self):
        link = _Y3sLink(MagicMock())
        client = AsyncMock()

        async def _write(_uuid, _data, response=True):
            link.on_notify(None, bytearray(bytes.fromhex("02114f4b")))
            link.on_notify(None, bytearray(TIME_ACK))

        client.write_gatt_char = AsyncMock(side_effect=_write)
        assert await link.request(client, bytes.fromhex("02011a0a1f0e2e16060f")) == b"OK"

    @pytest.mark.asyncio
    async def test_no_reply_times_out(self):
        link = _Y3sLink(MagicMock())
        client = AsyncMock()
        client.write_gatt_char = AsyncMock()
        with patch("custom_components.oclean_ble.coordinator.Y3S_REPLY_TIMEOUT", 0.01):
            assert await link.request(client, b"\x03\x03") is None


# ---------------------------------------------------------------------------
# Clear stored sessions (recovery action)
# ---------------------------------------------------------------------------


class TestClearStoredSessions:
    async def _run(self, coordinator, fake):
        with (
            patch("custom_components.oclean_ble.coordinator.bluetooth"),
            patch(
                "custom_components.oclean_ble.coordinator.establish_connection",
                new_callable=AsyncMock,
                return_value=fake.client(),
            ),
            patch("custom_components.oclean_ble.coordinator.asyncio.sleep", new_callable=AsyncMock),
            patch("custom_components.oclean_ble.coordinator.Y3S_REPLY_TIMEOUT", 0.05),
        ):
            await coordinator.async_clear_stored_sessions()

    @pytest.mark.asyncio
    async def test_sends_0202_on_y3s(self):
        fake = FakeY3s()
        await self._run(_make_coordinator(), fake)
        assert fake.writes == [(WRITE_CHAR_UUID, b"\x02\x02")]

    @pytest.mark.asyncio
    async def test_missing_ack_raises(self):
        from bleak import BleakError

        with pytest.raises(BleakError, match="not acknowledged"):
            await self._run(_make_coordinator(), FakeY3s(ack_clear=False))

    @pytest.mark.asyncio
    async def test_rejected_on_other_models(self):
        fake = FakeY3s()
        with pytest.raises(ValueError, match="only supported on the OCLEANY3S"):
            await self._run(_make_coordinator(cached_model="OCLEANY3"), fake)
        assert fake.writes == []

    def test_is_verified_y3s(self):
        assert _make_coordinator().is_verified_y3s is True
        assert _make_coordinator(cached_model="OCLEANY3P").is_verified_y3s is False
        assert _make_coordinator(cached_model=None).is_verified_y3s is False


# ---------------------------------------------------------------------------
# Entities and write actions on the OCLEANY3S
# ---------------------------------------------------------------------------

# Frames the official app sent for "Daily standard cleaning" (pnum 0) and, with
# Bluetooth acceleration off, the split "Medium bleaching" (pnum 76) – snoop16/17.
STANDARD_PLAN_FRAME = bytes.fromhex("0206000406021e06021e06021e06021e0005")
BLEACHING_PLAN_PACKETS = [
    bytes.fromhex("02064c0520201e20231e20181e20181e2a2b"),
    bytes.fromhex("020b20231e0005"),
]


def _with_data(coordinator: OcleanCoordinator, model: str = "OCLEANY3S") -> OcleanCoordinator:
    from custom_components.oclean_ble.models import OcleanDeviceData

    coordinator.data = OcleanDeviceData(model_id=model)
    return coordinator


async def _run_action(coordinator: OcleanCoordinator, fake: FakeY3s, coro_factory):
    with (
        patch("custom_components.oclean_ble.coordinator.bluetooth"),
        patch(
            "custom_components.oclean_ble.coordinator.establish_connection",
            new_callable=AsyncMock,
            return_value=fake.client(),
        ),
        patch("custom_components.oclean_ble.coordinator.asyncio.sleep", new_callable=AsyncMock),
        patch("custom_components.oclean_ble.coordinator.Y3S_REPLY_TIMEOUT", 0.05),
    ):
        await coro_factory(coordinator)


class TestSchemeFrames:
    def test_standard_plan_matches_capture(self):
        from custom_components.oclean_ble.const import OCLEANY3S_SCHEME_HINT_TYPES, OCLEANY3S_SCHEMES
        from custom_components.oclean_ble.coordinator import _build_scheme_packets

        _, steps = OCLEANY3S_SCHEMES[0]
        assert _build_scheme_packets(0, steps, OCLEANY3S_SCHEME_HINT_TYPES[0]) == [STANDARD_PLAN_FRAME]

    def test_bleaching_plan_matches_capture(self):
        from custom_components.oclean_ble.const import OCLEANY3S_SCHEME_HINT_TYPES, OCLEANY3S_SCHEMES
        from custom_components.oclean_ble.coordinator import _build_scheme_packets

        _, steps = OCLEANY3S_SCHEMES[76]
        assert _build_scheme_packets(76, steps, OCLEANY3S_SCHEME_HINT_TYPES[76]) == BLEACHING_PLAN_PACKETS

    def test_other_models_keep_gear_table_codes(self):
        from custom_components.oclean_ble.const import OCLEANY3M_SCHEMES
        from custom_components.oclean_ble.coordinator import _build_scheme_packets

        _, steps = OCLEANY3M_SCHEMES[76]
        assert _build_scheme_packets(76, steps)[0][4] == 0x00

    def test_y3s_plan_list(self):
        from custom_components.oclean_ble.const import SCHEMES_BY_MODEL

        assert sorted(SCHEMES_BY_MODEL["OCLEANY3S"]) == [0, *range(72, 90)]
        assert 90 in SCHEMES_BY_MODEL["OCLEANY3"]


class TestY3sActions:
    @pytest.mark.asyncio
    async def test_scheme_write_is_acknowledged_per_packet(self):
        fake = FakeY3s()
        await _run_action(_with_data(_make_coordinator()), fake, lambda c: c.async_set_brush_scheme(76))
        assert fake.writes == [(WRITE_CHAR_UUID, p) for p in BLEACHING_PLAN_PACKETS]

    @pytest.mark.asyncio
    async def test_area_remind_acknowledged(self):
        fake = FakeY3s()
        coordinator = _make_coordinator()
        await _run_action(coordinator, fake, lambda c: c.async_set_area_remind(False))
        assert fake.writes == [(WRITE_CHAR_UUID, b"\x02\x0d\x00")]
        assert coordinator.area_remind is False

    @pytest.mark.asyncio
    async def test_over_pressure_acknowledged(self):
        fake = FakeY3s()
        await _run_action(_make_coordinator(), fake, lambda c: c.async_set_over_pressure(True))
        assert fake.writes == [(WRITE_CHAR_UUID, b"\x02\x12\x01")]

    @pytest.mark.asyncio
    async def test_reset_brush_head_acknowledged(self):
        fake = FakeY3s()
        await _run_action(_make_coordinator(), fake, lambda c: c.async_reset_brush_head())
        assert fake.writes == [(WRITE_CHAR_UUID, b"\x02\x0f")]

    @pytest.mark.asyncio
    async def test_sync_time_uses_0201_before_any_poll(self):
        fake = FakeY3s()
        coordinator = _make_coordinator()
        assert coordinator._protocol.name == "Unknown"
        await _run_action(coordinator, fake, lambda c: c.async_sync_time())
        assert len(fake.writes) == 1
        assert fake.writes[0][1][:2] == b"\x02\x01"
        assert len(fake.writes[0][1]) == 10

    @pytest.mark.asyncio
    async def test_unacknowledged_write_raises(self):
        from bleak import BleakError

        fake = FakeY3s(ack_time=False)
        with pytest.raises(BleakError, match="not acknowledged"):
            await _run_action(_make_coordinator(), fake, lambda c: c.async_sync_time())

    @pytest.mark.parametrize(
        ("setter", "arg"),
        [
            ("async_set_remind_switch", True),
            ("async_set_running_switch", True),
            ("async_set_brush_head_max_days", 90),
        ],
    )
    @pytest.mark.asyncio
    async def test_unsupported_features_rejected(self, setter, arg):
        fake = FakeY3s()
        with pytest.raises(ValueError, match="not supported on this model"):
            await _run_action(_make_coordinator(), fake, lambda c: getattr(c, setter)(arg))
        assert fake.writes == []

    def test_feature_supported(self):
        y3s = _make_coordinator()
        other = _make_coordinator(cached_model="OCLEANY3")
        for key in ("remind_switch", "running_switch", "brush_head_max_days"):
            assert y3s.feature_supported(key) is False
            assert other.feature_supported(key) is True
        assert y3s.feature_supported("area_remind") is True


class TestEntityAvailability:
    def _coord(self, supported: bool):
        coord = MagicMock()
        coord.data = MagicMock()
        coord.feature_supported.return_value = supported
        return coord

    @pytest.mark.parametrize("supported", [True, False])
    def test_switch(self, supported):
        from custom_components.oclean_ble.switch import SWITCH_DESCRIPTIONS, OcleanSwitch

        desc = next(d for d in SWITCH_DESCRIPTIONS if d.key == "remind_switch")
        switch = OcleanSwitch(self._coord(supported), desc, "AA:BB:CC:DD:EE:FF", "Oclean")
        assert switch.available is supported

    @pytest.mark.parametrize("supported", [True, False])
    def test_number(self, supported):
        from custom_components.oclean_ble.number import NUMBER_DESCRIPTIONS, OcleanNumber

        number = OcleanNumber(self._coord(supported), NUMBER_DESCRIPTIONS[0], "AA:BB:CC:DD:EE:FF", "Oclean")
        assert number.available is supported


# ---------------------------------------------------------------------------
# Review findings (2026-11-01): safety and robustness of the Y3S flow
# ---------------------------------------------------------------------------


def _record_store(coordinator: OcleanCoordinator) -> list[dict]:
    saved: list[dict] = []

    async def _save(data):
        saved.append(copy.deepcopy(data))

    coordinator._store.async_save = _save
    return saved


class TestTimeFrameBytes:
    def test_matches_captured_frame(self):
        # snoop13: the app sent 02 01 1a 0a 1f 0e 2e 16 06 0f on Saturday
        # 2026-10-31 14:46:22 CET (weekday 6, time-zone index 15).
        cet = datetime.timezone(datetime.timedelta(hours=1))

        class FixedDT(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

            def astimezone(self, tz=None):
                return self

        fixed = FixedDT(2026, 10, 31, 14, 46, 22, tzinfo=cet)
        coordinator = _make_coordinator()
        coordinator._protocol = TYPE1_Y3S
        fake_module = SimpleNamespace(datetime=FixedDT, timedelta=datetime.timedelta)
        with patch("custom_components.oclean_ble.coordinator.datetime", fake_module):
            assert coordinator._time_calibration_command() == bytes.fromhex("02011a0a1f0e2e16060f")


class TestReplyRouting:
    @pytest.mark.asyncio
    async def test_device_info_completed_after_the_write_returned(self):
        _, collected, _ = await _poll(FakeY3s(late_info=True))
        assert collected[DATA_BRUSH_HEAD_USAGE] == 381
        assert collected[DATA_BRUSH_HEAD_DAYS] == 127

    @pytest.mark.asyncio
    async def test_fbb86_notification_during_the_dump_is_ignored(self):
        fake = FakeY3s(stray=BATTERY_REPLY)
        _, _, sessions = await _poll(fake)
        assert sorted(s[DATA_LAST_BRUSH_SCORE] for s in sessions) == [83, 90]
        assert ("bb85", "0202") in fake.commands()

    def test_dump_ignores_other_characteristics(self):
        link = _Y3sLink(MagicMock())
        link._stream = Y3sRunningDataStream()
        link.on_notify(SimpleNamespace(uuid=READ_NOTIFY_CHAR_UUID), bytearray(DUMP_TWO_RECORDS[0]))
        assert link._stream.count is None
        link.on_notify(SimpleNamespace(uuid=RECEIVE_BRUSH_UUID), bytearray(DUMP_TWO_RECORDS[0]))
        assert link._stream.count == 2

    def test_replies_ignore_the_dump_characteristic(self):
        link = _Y3sLink(MagicMock())
        link._prefix = b"\x02\x01"
        link.on_notify(SimpleNamespace(uuid=RECEIVE_BRUSH_UUID), bytearray(TIME_ACK))
        assert not link._done.is_set()
        link.on_notify(SimpleNamespace(uuid=READ_NOTIFY_CHAR_UUID), bytearray(TIME_ACK))
        assert link._done.is_set()


class TestBrokenDumpBlock:
    @pytest.mark.asyncio
    async def test_scheduled_polls_never_resend_0307(self):
        coordinator, _, _ = await _poll(FakeY3s(dump=DUMP_TWO_RECORDS[:3]))
        assert coordinator._y3s_dump_blocked is True
        fake = FakeY3s()
        await _poll(fake, coordinator)
        assert ("bb89", "0307") not in fake.commands()
        assert ("bb85", "0202") not in fake.commands()

    @pytest.mark.asyncio
    async def test_block_is_persisted(self):
        coordinator = _make_coordinator()
        saved = _record_store(coordinator)
        await _poll(FakeY3s(dump=DUMP_TWO_RECORDS[:3]), coordinator)
        assert saved[-1]["y3s_dump_blocked"] is True
        coordinator._store.async_load = AsyncMock(return_value=saved[-1])
        restored = _make_coordinator()
        restored._store.async_load = AsyncMock(return_value=saved[-1])
        await restored._load_store()
        assert restored._y3s_dump_blocked is True

    @pytest.mark.asyncio
    async def test_update_now_retries_once_and_a_complete_dump_lifts_the_block(self):
        coordinator, _, _ = await _poll(FakeY3s(dump=DUMP_TWO_RECORDS[:3]))
        coordinator._y3s_retry_dump = True
        fake = FakeY3s()
        await _poll(fake, coordinator)
        assert ("bb89", "0307") in fake.commands()
        assert coordinator._y3s_dump_blocked is False

    @pytest.mark.asyncio
    async def test_empty_store_lifts_the_block(self):
        coordinator, _, _ = await _poll(FakeY3s(dump=DUMP_TWO_RECORDS[:3]))
        await _poll(FakeY3s(battery=_battery_reply(level=50, stored=0)), coordinator)
        assert coordinator._y3s_dump_blocked is False

    @pytest.mark.asyncio
    async def test_no_header_at_all_also_blocks(self):
        coordinator, _, _ = await _poll(FakeY3s(dump=[]))
        assert coordinator._y3s_dump_blocked is True

    @pytest.mark.asyncio
    async def test_clear_stored_sessions_lifts_the_block(self):
        coordinator = _make_coordinator()
        coordinator._y3s_dump_blocked = True
        await TestClearStoredSessions()._run(coordinator, FakeY3s())
        assert coordinator._y3s_dump_blocked is False

    @pytest.mark.asyncio
    async def test_poll_now_sets_the_retry_flag_only_for_that_refresh(self):
        coordinator = _make_coordinator()
        seen: list[bool] = []

        async def _refresh():
            seen.append(coordinator._y3s_retry_dump)

        coordinator.async_refresh = _refresh
        await coordinator.async_poll_now(retry_session_download=True)
        await coordinator.async_poll_now()
        assert seen == [True, False]
        assert coordinator._y3s_retry_dump is False


class TestSessionsSurviveTheClear:
    @pytest.mark.asyncio
    async def test_sessions_are_saved_before_0202(self):
        coordinator = _make_coordinator()
        saved = _record_store(coordinator)
        fake = FakeY3s()
        pending_at_clear: list[int] = []
        fake.on_clear = lambda: pending_at_clear.append(len(saved[-1]["y3s_pending_sessions"]) if saved else -1)
        await _poll(fake, coordinator)
        assert pending_at_clear == [2]

    @pytest.mark.asyncio
    async def test_pending_sessions_return_in_the_next_poll(self):
        # The poll that cleared the brush never reached the import (e.g. timeout).
        coordinator, _, _ = await _poll(FakeY3s())
        assert len(coordinator._y3s_pending_sessions) == 2
        _, collected, sessions = await _poll(FakeY3s(battery=_battery_reply(level=50, stored=0)), coordinator)
        assert len(sessions) == 2
        assert collected[DATA_LAST_BRUSH_SCORE] == 83

    async def _poll_device(self, coordinator, fake, import_mock):
        with (
            patch("custom_components.oclean_ble.coordinator.bluetooth"),
            patch(
                "custom_components.oclean_ble.coordinator.establish_connection",
                new_callable=AsyncMock,
                return_value=fake.client(),
            ),
            patch("custom_components.oclean_ble.coordinator.asyncio.sleep", new_callable=AsyncMock),
            patch("custom_components.oclean_ble.coordinator.import_new_sessions", import_mock),
            patch("custom_components.oclean_ble.coordinator.Y3S_REPLY_TIMEOUT", 0.05),
            patch("custom_components.oclean_ble.coordinator.Y3S_STREAM_IDLE_TIMEOUT", 0.05),
        ):
            return await coordinator._poll_device()

    @pytest.mark.asyncio
    async def test_cleared_after_a_successful_import(self):
        coordinator = _make_coordinator()
        await self._poll_device(coordinator, FakeY3s(), AsyncMock(return_value=0))
        assert coordinator._y3s_pending_sessions == []

    @pytest.mark.asyncio
    async def test_kept_when_the_import_fails(self):
        coordinator = _make_coordinator()
        with pytest.raises(RuntimeError):
            await self._poll_device(coordinator, FakeY3s(), AsyncMock(side_effect=RuntimeError("recorder")))
        assert len(coordinator._y3s_pending_sessions) == 2


class TestDumpGate:
    @pytest.mark.asyncio
    async def test_ten_stored_sessions_are_read(self):
        fake = FakeY3s(battery=_battery_reply(level=50, stored=10))
        await _poll(fake)
        assert ("bb89", "0307") in fake.commands()

    @pytest.mark.asyncio
    async def test_eleven_stored_sessions_are_not_read(self):
        fake = FakeY3s(battery=_battery_reply(level=50, stored=11))
        await _poll(fake)
        assert ("bb89", "0307") not in fake.commands()

    @pytest.mark.asyncio
    async def test_unknown_byte5_means_unknown_count(self):
        fake = FakeY3s(battery=bytes.fromhex("0303020dc4080201"))
        _, collected, _ = await _poll(fake)
        assert ("bb89", "0307") not in fake.commands()
        assert collected[DATA_BATTERY] == 8

    def test_parse_battery_without_byte5_count(self):
        assert Y3S_BATTERY_STORED_SESSIONS not in parse_y3s_battery(bytes.fromhex("020dc4080201"))
        assert Y3S_BATTERY_STORED_SESSIONS not in parse_y3s_battery(bytes.fromhex("020dc40802"))


class TestRecordIntegrity:
    def test_record_without_filler_is_dropped(self):
        record = bytearray(_records_from(DUMP_TWO_RECORDS)[0])
        record[40] = 0x00
        assert parse_y3s_record(bytes(record)) == {}

    def test_shifted_record_is_dropped(self):
        payload = b"".join(_records_from(DUMP_TWO_RECORDS))
        assert parse_y3s_record(payload[20 : 20 + 42]) == {}

    def test_record_from_next_year_is_dropped(self):
        record = bytearray(_records_from(DUMP_TWO_RECORDS)[0])
        record[0] = datetime.datetime.now().year - 2000 + 1
        assert parse_y3s_record(bytes(record)) == {}


class TestUnknownModelFailsClosed:
    @pytest.mark.asyncio
    async def test_poll_sends_nothing(self):
        fake = FakeY3s()

        async def _read(uuid):
            if uuid == "00002a24-0000-1000-8000-00805f9b34fb":
                raise BleakError("read failed")
            return bytearray(b"\x08")

        fake.read_gatt_char = _read
        coordinator, collected, sessions = await _poll(fake, _make_coordinator(cached_model=None))
        assert fake.writes == []
        assert sessions == []
        assert collected[DATA_BATTERY] == 8
        assert coordinator._last_subscribe_ok is True

    @pytest.mark.asyncio
    async def test_actions_send_nothing(self):
        coordinator = _make_coordinator(cached_model=None)
        with patch("custom_components.oclean_ble.coordinator.establish_connection", new_callable=AsyncMock) as connect:
            with pytest.raises(BleakError, match="not identified yet"):
                await coordinator.async_set_area_remind(True)
            connect.assert_not_awaited()


class TestAckedWritesNeedReplies:
    @pytest.mark.asyncio
    async def test_nothing_written_without_fbb86(self):
        fake = FakeY3s(no_notify=(READ_NOTIFY_CHAR_UUID,))
        with pytest.raises(BleakError, match="cannot be received"):
            await _run_action(_make_coordinator(), fake, lambda c: c.async_reset_brush_head())
        assert fake.writes == []


class TestY3sThroughEsphomeProxy:
    @pytest.mark.asyncio
    async def test_full_poll_through_the_proxy(self):
        fake = FakeY3s()
        _, collected, sessions = await _poll(fake, proxy=True)
        assert fake.proxy_registrations == [0x17, 0x1F]
        assert fake.commands() == [
            ("bb85", "030201"),
            ("bb85", "0201"),
            ("bb85", "0303"),
            ("bb89", "0307"),
            ("bb85", "0202"),
        ]
        assert len(sessions) == 2
        assert collected[DATA_BRUSH_HEAD_USAGE] == 381


class TestSchemeSelectFallback:
    def _select(self, y3s: bool):
        from custom_components.oclean_ble.select import OcleanSchemeSelect

        coord = MagicMock()
        coord.is_verified_y3s = y3s
        coord.active_scheme_pnum = None
        coord.data = SimpleNamespace(model_id="OCLEANY3S" if y3s else "OCLEANY3M", brush_mode=0)
        return OcleanSchemeSelect(coord, "AA:BB:CC:DD:EE:FF", "Oclean")

    def test_y3s_does_not_use_the_mode_byte_as_plan(self):
        assert self._select(True).current_option is None

    def test_other_models_keep_the_fallback(self):
        assert self._select(False).current_option is not None


class TestRepeatingWarnings:
    @pytest.mark.asyncio
    async def test_too_many_sessions_warns_once_per_run(self, caplog):
        coordinator = _make_coordinator()
        for _ in range(3):
            await _poll(FakeY3s(battery=_battery_reply(level=50, stored=11)), coordinator)
        warnings = [r for r in caplog.records if r.levelname == "WARNING" and "stores 11 sessions" in r.getMessage()]
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_blocked_download_warns_once_until_the_block_changes(self, caplog):
        coordinator, _, _ = await _poll(FakeY3s(dump=DUMP_TWO_RECORDS[:3]))
        for _ in range(2):
            await _poll(FakeY3s(), coordinator)
        warnings = [r for r in caplog.records if r.levelname == "WARNING" and "not requested again" in r.getMessage()]
        assert len(warnings) == 1
