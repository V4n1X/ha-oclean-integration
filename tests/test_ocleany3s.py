"""OCLEANY3S-specific tests (Oclean X Pro (S), APK protocol ID 9 / case 14).

Every assertion in this module is grounded in the decompiled OClean Care+ 4.0.4
APK (see docs/OCLEANY3S-AUDIT.md for the full citation list):

* ``OCLEANY3S`` is protocol ID 9 (``com/ocleanble/lib/device/DeviceType.java:147``),
  handled by APK class ``g.w0`` with mode 1 (``i/a.java:320-341``, case 14).
* The ``g.w0`` family uses its own 0302 device-settings payload layout
  (``g/w0.java:1219-1272``) — no battery byte, no modeNum byte.
* The 42-byte ``*B#`` record carries ``gestureCode`` in byte 18 and the APK's
  canonical 13-element ``gestureArray`` in bytes 18-30 (``g/w0.java:1034-1073``).
* The 8 tooth zones are ``gestureArray[5:13]`` = record bytes 23-30
  (``com/google/firebase/b.java:1020-1034``), and the coverage threshold for
  OCLEANY3S is 9.0 (``com/google/firebase/b.java:1091-1099``).
* ``0x0202`` is ``clearRunningDate``, not a query (``g/w0.java:503-515`` +
  ``com/ocleanble/lib/OcleanBleManager.java:677``) and must never be polled.
"""

from __future__ import annotations

import pytest

from custom_components.oclean_ble.const import (
    CMD_CLEAR_RUNNING_DATA,
    CMD_QUERY_DEVICE_SETTINGS,
    CMD_QUERY_RUNNING_DATA_T1,
    CMD_QUERY_STATUS,
    DATA_BATTERY,
    DATA_BRUSH_HEAD_DAYS,
    DATA_BRUSH_HEAD_USAGE,
    DATA_BRUSH_MODE,
    DATA_LAST_BRUSH_AREAS,
    DATA_LAST_BRUSH_COVERAGE,
    DATA_LAST_BRUSH_DURATION,
    DATA_LAST_BRUSH_GESTURE_ARRAY,
    DATA_LAST_BRUSH_GESTURE_CODE,
    DATA_LAST_BRUSH_PNUM,
    DATA_LAST_BRUSH_POWER_ARRAY,
    DATA_LAST_BRUSH_PRESSURE_RATIO,
    DATA_LAST_BRUSH_SCORE,
    DATA_MODEL_ID,
    DIS_MODEL_UUID,
    SEND_BRUSH_CMD_UUID,
    SETTINGS_LAYOUT_GENERIC,
    SETTINGS_LAYOUT_W0,
    WRITE_CHAR_UUID,
)
from custom_components.oclean_ble.parser import (
    _parse_device_settings_response,
    parse_notification,
    parse_t1_c3385w0_record,
)
from custom_components.oclean_ble.protocol import TYPE1, TYPE1_Y3, is_known_model, protocol_for_model
from tests.integration_helpers import make_coordinator, run_poll
from tests.simulator import OcleanDeviceSimulator

_MAC = "70:28:45:83:2A:C9"

# ---------------------------------------------------------------------------
# 0302 payload in the g/w0 layout (g/w0.java:1219-1272)
# ---------------------------------------------------------------------------


def _w0_settings_payload() -> bytes:
    """Build a 32-byte 0302 payload using the g/w0 field map."""
    p = bytearray(32)
    p[0] = 7  # deviceTheme – NOT battery (a value <= 100, so a naive
    # battery read at byte 0 would silently report 7 %)
    p[4:8] = (1).to_bytes(4, "big")  # voice type word
    p[8] = 0  # volumeSwitch (0 = on)
    p[9] = 1  # volume index
    p[10] = 0  # calendarSwitch
    p[11] = 72  # pNum
    p[12] = 5  # brushMode
    p[13] = 0  # splashPrevent
    p[15] = 42  # headUsedTimeLong (1 byte)
    p[16] = 26  # year (+2000)
    p[17:23] = bytes([3, 11, 20, 2, 23, 0])  # month/day/hour/min/sec
    p[23] = 1  # overPressure
    p[24] = 1  # areaRemind
    p[25] = 15  # timezone index
    p[25:27] = (90).to_bytes(2, "big")  # headMaxTimeLong
    p[27:29] = (23).to_bytes(2, "big")  # headUsedDays
    p[29:31] = (33).to_bytes(2, "big")  # headUsedTimes
    p[31] = 4  # deviceLanguage
    return bytes(p)


def _generic_settings_payload() -> bytes:
    """Build a 32-byte 0302 payload using the legacy/generic field map."""
    p = bytearray(32)
    p[0] = 27  # batteryLevel
    p[5] = 3  # modeNum
    p[25:27] = (90).to_bytes(2, "big")  # headMaxTimeLong
    p[27:29] = (1234).to_bytes(2, "big")  # headUsedTimeLong
    p[29:31] = (23).to_bytes(2, "big")  # headUsedDays
    p[31] = 33  # headUsedTimes
    return bytes(p)


# ---------------------------------------------------------------------------
# 42-byte *B# record with a year byte (g/w0 family)
# ---------------------------------------------------------------------------


def _y3s_record() -> bytes:
    """Build a realistic 42-byte OCLEANY3S session record."""
    r = bytearray(42)
    r[0] = 26  # year - 2000
    r[1:6] = bytes([3, 11, 20, 2, 23])  # month/day/hour/min/sec
    r[6] = 0  # pNum
    r[7:9] = (120).to_bytes(2, "big")  # duration
    r[9:11] = (120).to_bytes(2, "big")  # validDuration
    r[11:16] = bytes([5, 20, 75, 0, 0])  # pressureRatio (sums to 100)
    r[16] = 0
    r[17] = 31  # timezone index
    r[18] = 42  # gestureCode (= gestureArray[0])
    r[19:23] = bytes([0, 0, 0, 0])
    r[23:31] = bytes([28, 20, 25, 12, 10, 10, 10, 0])  # the 8 tooth zones
    r[30:33] = bytes([0x00, 0xE4, 0x39])  # powerArray nibble source
    r[33] = 91  # score
    r[34] = 3  # point
    r[35:42] = b"\xff" * 7
    return bytes(r)


def _star_b_packets(record: bytes) -> list[bytes]:
    """Split a 42-byte record into 0307 header + 2 continuation packets."""
    header = bytearray(20)
    header[0:2] = b"\x03\x07"
    header[2:5] = b"\x2a\x42\x23"  # *B#
    header[5:7] = (1).to_bytes(2, "big")  # record count
    header[7:20] = record[0:13]
    return [bytes(header), record[13:33], record[33:42]]


def _coordinator():
    return make_coordinator(_MAC, "Oclean X Pro (S)")


def _y3s_client(**kwargs):
    """Client that identifies as OCLEANY3S and replays a full session burst."""
    sim = (
        OcleanDeviceSimulator()
        .with_battery(82)
        .with_read_char_responses(
            {
                DIS_MODEL_UUID: b"OCLEANY3S",
                # 0303 STATE response: payload byte 3 = 0x52 = 82 % battery
                "5f78df94-798c-46f5-990a-855b673fbb86": bytes.fromhex("0303020e46520100"),
            }
        )
        .add_notification(bytes.fromhex("0303020e46520100"))
    )
    if kwargs.get("settings", True):
        sim.add_notification(bytes.fromhex("0302") + _w0_settings_payload())
    for packet in _star_b_packets(_y3s_record()):
        sim.add_notification(packet)
    return sim.build_client()


# ===========================================================================
# 1. Model → protocol mapping
# ===========================================================================


class TestOcleanY3SProtocolMapping:
    def test_known_model(self):
        assert is_known_model("OCLEANY3S") is True

    def test_maps_to_g_w0_family_profile(self):
        assert protocol_for_model("OCLEANY3S") is TYPE1_Y3

    def test_not_the_generic_type1_profile(self):
        assert protocol_for_model("OCLEANY3S") is not TYPE1

    def test_uses_w0_settings_layout(self):
        assert protocol_for_model("OCLEANY3S").settings_layout == SETTINGS_LAYOUT_W0


# ===========================================================================
# 2. Poll command sequence
# ===========================================================================


class TestOcleanY3SPollCommands:
    def test_does_not_send_clear_running_data(self):
        """0x0202 is clearRunningDate – polling it must be impossible."""
        cmds = [cmd for _, cmd in TYPE1_Y3.query_commands]
        assert CMD_CLEAR_RUNNING_DATA not in cmds

    def test_sends_expected_queries(self):
        """APK-exact routing: 0303/030201 on fbb85, only 0307 on fbb89."""
        assert TYPE1_Y3.query_commands == (
            (WRITE_CHAR_UUID, CMD_QUERY_STATUS),
            (WRITE_CHAR_UUID, CMD_QUERY_DEVICE_SETTINGS),
            (SEND_BRUSH_CMD_UUID, CMD_QUERY_RUNNING_DATA_T1),
        )

    def test_only_0307_uses_the_brush_cmd_characteristic(self):
        """g/w0.java:324/:375 write status+settings to fbb85; only :360 uses fbb89."""
        brush_cmds = [cmd for char, cmd in TYPE1_Y3.query_commands if char == SEND_BRUSH_CMD_UUID]
        assert brush_cmds == [CMD_QUERY_RUNNING_DATA_T1]


# ===========================================================================
# 3. 0302 device-settings layout
# ===========================================================================


class TestOcleanY3SSettingsLayout:
    def test_brush_mode_from_byte12(self):
        result = _parse_device_settings_response(_w0_settings_payload(), SETTINGS_LAYOUT_W0)
        assert result[DATA_BRUSH_MODE] == 5

    def test_head_days_from_bytes27_28(self):
        result = _parse_device_settings_response(_w0_settings_payload(), SETTINGS_LAYOUT_W0)
        assert result[DATA_BRUSH_HEAD_DAYS] == 23

    def test_head_times_from_bytes29_30(self):
        result = _parse_device_settings_response(_w0_settings_payload(), SETTINGS_LAYOUT_W0)
        assert result[DATA_BRUSH_HEAD_USAGE] == 33

    def test_device_theme_is_not_reported_as_battery(self):
        """Byte 0 is deviceTheme in the g/w0 layout (7 in our payload)."""
        result = _parse_device_settings_response(_w0_settings_payload(), SETTINGS_LAYOUT_W0)
        assert DATA_BATTERY not in result

    def test_generic_layout_unchanged(self):
        """Other families (g.g/g.n0/g.s/…) keep the historical layout."""
        result = _parse_device_settings_response(_generic_settings_payload(), SETTINGS_LAYOUT_GENERIC)
        assert result[DATA_BATTERY] == 27
        assert result[DATA_BRUSH_MODE] == 3
        assert result[DATA_BRUSH_HEAD_DAYS] == 23
        assert result[DATA_BRUSH_HEAD_USAGE] == 33

    def test_routed_with_layout_through_parse_notification(self):
        payload = bytes.fromhex("0302") + _w0_settings_payload()
        w0 = parse_notification(payload, SETTINGS_LAYOUT_W0)
        generic = parse_notification(payload, SETTINGS_LAYOUT_GENERIC)
        assert w0[DATA_BRUSH_MODE] == 5
        assert generic[DATA_BRUSH_MODE] == 0  # byte 5 is part of the voice word


# ===========================================================================
# 3b. '#'-length frame on the 0302 response (APK w/a.java:26-80)
# ===========================================================================


class TestOcleanY3SInfoFrame:
    """The 0302 response is length-framed: '#' + (len+2) + data."""

    @staticmethod
    def _framed(data: bytes) -> bytes:
        return b"\x23" + bytes([len(data) + 2]) + data

    def test_framed_payload_is_stripped(self):
        data = _w0_settings_payload()
        framed = bytes.fromhex("0302") + self._framed(data)
        result = parse_notification(framed, SETTINGS_LAYOUT_W0)
        assert result[DATA_BRUSH_MODE] == 5
        assert result[DATA_BRUSH_HEAD_DAYS] == 23
        assert result[DATA_BRUSH_HEAD_USAGE] == 33

    def test_framed_and_raw_give_the_same_result(self):
        data = _w0_settings_payload()
        raw = parse_notification(bytes.fromhex("0302") + data, SETTINGS_LAYOUT_W0)
        framed = parse_notification(bytes.fromhex("0302") + self._framed(data), SETTINGS_LAYOUT_W0)
        assert raw == framed

    def test_raw_payload_still_works(self):
        """Firmware that sends the payload unframed must keep working."""
        result = parse_notification(bytes.fromhex("0302") + _w0_settings_payload(), SETTINGS_LAYOUT_W0)
        assert result[DATA_BRUSH_HEAD_DAYS] == 23

    def test_bogus_frame_is_not_stripped(self):
        """A payload whose first byte happens to be 0x23 but with a wrong length."""
        data = bytearray(_w0_settings_payload())
        data[0] = 0x23
        result = parse_notification(bytes.fromhex("0302") + bytes(data), SETTINGS_LAYOUT_W0)
        # not stripped → deviceTheme stays 0x23 (35) and the rest is unshifted
        assert result[DATA_BRUSH_MODE] == 5

    def test_strip_helper_roundtrip(self):
        from custom_components.oclean_ble.parser import _strip_info_frame

        data = _w0_settings_payload()
        assert _strip_info_frame(self._framed(data)) == data
        assert _strip_info_frame(data) == data
        assert _strip_info_frame(b"") == b""
        assert _strip_info_frame(b"\x23") == b"\x23"


# ===========================================================================
# 4. 42-byte *B# record parsing
# ===========================================================================


class TestOcleanY3SRecord:
    def test_gesture_code_is_byte18(self):
        result = parse_t1_c3385w0_record(_y3s_record())
        assert result[DATA_LAST_BRUSH_GESTURE_CODE] == 42

    def test_gesture_array_is_13_elements_from_byte18(self):
        result = parse_t1_c3385w0_record(_y3s_record())
        assert result[DATA_LAST_BRUSH_GESTURE_ARRAY] == [42, 0, 0, 0, 0, 28, 20, 25, 12, 10, 10, 10, 0]

    def test_tooth_zones_are_bytes23_to_30(self):
        result = parse_t1_c3385w0_record(_y3s_record())
        assert list(result[DATA_LAST_BRUSH_AREAS].values()) == [28, 20, 25, 12, 10, 10, 10, 0]

    def test_pressure_ratio_from_bytes11_to_15(self):
        result = parse_t1_c3385w0_record(_y3s_record())
        assert result[DATA_LAST_BRUSH_PRESSURE_RATIO] == [5, 20, 75, 0, 0]

    def test_power_array_nibbles_from_bytes30_to_32(self):
        result = parse_t1_c3385w0_record(_y3s_record())
        assert result[DATA_LAST_BRUSH_POWER_ARRAY] == [0, 0, 0, 0, 3, 2, 1, 0, 0, 3, 2, 1]

    def test_coverage_uses_threshold_9(self):
        """7 of 8 zones clear the 9.0 threshold (APK b.java:1091-1099)."""
        result = parse_t1_c3385w0_record(_y3s_record())
        assert result[DATA_LAST_BRUSH_COVERAGE] == 88

    def test_coverage_threshold_10_would_differ(self):
        """Sanity check that the Y3PD threshold is genuinely different.

        Zones (8, 13, 13, 13, 13, 13, 14, 13) sum to 100 for a 120 s session:
        threshold 9 → raw >= 7.5 → all 8 zones → 100 %.
        threshold 10 → raw >= 8.33 → the 8 s zone fails → 88 %.
        """
        record = bytearray(_y3s_record())
        record[23:31] = bytes([8, 13, 13, 13, 13, 13, 14, 13])
        default = parse_t1_c3385w0_record(bytes(record))
        y3pd = parse_t1_c3385w0_record(bytes(record), coverage_norm_threshold=10)
        assert default[DATA_LAST_BRUSH_COVERAGE] == 100
        assert y3pd[DATA_LAST_BRUSH_COVERAGE] == 88

    def test_score_and_duration(self):
        result = parse_t1_c3385w0_record(_y3s_record())
        assert result[DATA_LAST_BRUSH_SCORE] == 91
        assert result[DATA_LAST_BRUSH_DURATION] == 120
        assert result[DATA_LAST_BRUSH_PNUM] == 0


# ===========================================================================
# 5. End-to-end poll through the coordinator
# ===========================================================================


class TestOcleanY3SEndToEnd:
    @pytest.mark.asyncio
    async def test_model_identified(self):
        result = await run_poll(_coordinator(), _y3s_client())
        assert result[DATA_MODEL_ID] == "OCLEANY3S"

    @pytest.mark.asyncio
    async def test_battery_not_taken_from_settings_byte0(self):
        """Battery must come from 0303 / 0x2A19, never from 0302 byte 0 (theme)."""
        result = await run_poll(_coordinator(), _y3s_client())
        assert result[DATA_BATTERY] == 82

    @pytest.mark.asyncio
    async def test_brush_head_counters_from_w0_offsets(self):
        result = await run_poll(_coordinator(), _y3s_client())
        assert result[DATA_BRUSH_HEAD_DAYS] == 23
        assert result[DATA_BRUSH_HEAD_USAGE] == 33

    @pytest.mark.asyncio
    async def test_session_fields(self):
        result = await run_poll(_coordinator(), _y3s_client())
        assert result[DATA_LAST_BRUSH_SCORE] == 91
        assert result[DATA_LAST_BRUSH_DURATION] == 120
        assert result[DATA_LAST_BRUSH_GESTURE_CODE] == 42
        assert list(result[DATA_LAST_BRUSH_AREAS].values()) == [28, 20, 25, 12, 10, 10, 10, 0]
        assert result[DATA_LAST_BRUSH_COVERAGE] == 88

    @pytest.mark.asyncio
    async def test_poll_does_not_write_0202(self):
        """The poll must never send the destructive clearRunningData command."""
        client = _y3s_client()
        await run_poll(_coordinator(), client)
        written = [call.args[1] for call in client.write_gatt_char.call_args_list if call.args]
        assert CMD_CLEAR_RUNNING_DATA not in written
        assert CMD_QUERY_RUNNING_DATA_T1 in written
