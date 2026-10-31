"""Tests for the hardware-verified OCLEANY3S (Oclean X Pro) protocol.

All notification bytes below are copied from HCI captures of the official app
talking to an Oclean X Pro (model OCLEANY3S, firmware 1.0.0.19).
"""

from __future__ import annotations

import calendar
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    """Answers like the captured OCLEANY3S: replies are notified during the write."""

    def __init__(
        self,
        battery: bytes = BATTERY_REPLY,
        dump: list[bytes] | None = None,
        no_notify: tuple[str, ...] = (),
        ack_time: bool = True,
        ack_clear: bool = True,
    ) -> None:
        self.battery = battery
        self.dump = DUMP_TWO_RECORDS if dump is None else dump
        self.no_notify = no_notify
        self.ack_time = ack_time
        self.ack_clear = ack_clear
        self.handlers: dict[str, object] = {}
        self.writes: list[tuple[str, bytes]] = []

    async def start_notify(self, uuid, handler):
        if uuid in self.no_notify:
            raise RuntimeError("notify not possible")
        self.handlers[uuid] = handler

    def _notify(self, uuid: str, data: bytes) -> None:
        handler = self.handlers.get(uuid)
        if handler is not None:
            handler(None, bytearray(data))

    async def write_gatt_char(self, uuid, data, response=True):
        data = bytes(data)
        self.writes.append((uuid, data))
        if uuid == SEND_BRUSH_CMD_UUID and data == b"\x03\x07":
            for pkt in self.dump:
                self._notify(RECEIVE_BRUSH_UUID, pkt)
        elif uuid == WRITE_CHAR_UUID and data == b"\x03\x02\x01":
            for pkt in DEVICE_INFO_NOTIFIES:
                self._notify(READ_NOTIFY_CHAR_UUID, pkt)
        elif uuid == WRITE_CHAR_UUID and data == b"\x03\x03":
            self._notify(READ_NOTIFY_CHAR_UUID, self.battery)
        elif uuid == WRITE_CHAR_UUID and data[:2] == b"\x02\x01" and len(data) == 10:
            if self.ack_time:
                self._notify(READ_NOTIFY_CHAR_UUID, TIME_ACK)
        elif uuid == WRITE_CHAR_UUID and data == b"\x02\x02":
            if self.ack_clear:
                self._notify(READ_NOTIFY_CHAR_UUID, CLEAR_ACK)
        else:
            self._notify(READ_NOTIFY_CHAR_UUID, b"\x02")

    async def read_gatt_char(self, uuid):
        values = {
            "00002a24-0000-1000-8000-00805f9b34fb": b"OCLEANY3S",
            "00002a27-0000-1000-8000-00805f9b34fb": b"Rev.D",
            "00002a28-0000-1000-8000-00805f9b34fb": b"1.0.0.19",
        }
        return bytearray(values.get(uuid, b"\x08"))

    def client(self):
        client = AsyncMock()
        client.start_notify = AsyncMock(side_effect=self.start_notify)
        client.write_gatt_char = AsyncMock(side_effect=self.write_gatt_char)
        client.read_gatt_char = AsyncMock(side_effect=self.read_gatt_char)
        client.stop_notify = AsyncMock()
        client.disconnect = AsyncMock()
        client.services = MagicMock()
        client.services.get_characteristic.return_value = None
        return client

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


async def _poll(fake: FakeY3s, coordinator: OcleanCoordinator | None = None):
    coordinator = coordinator or _make_coordinator()
    collected: dict = {}
    with (
        patch("custom_components.oclean_ble.coordinator.asyncio.sleep", new_callable=AsyncMock),
        patch("custom_components.oclean_ble.coordinator.Y3S_STREAM_IDLE_TIMEOUT", 0.05),
        patch("custom_components.oclean_ble.coordinator.Y3S_REPLY_TIMEOUT", 0.05),
    ):
        sessions = await coordinator._setup_and_read(fake.client(), collected)
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
