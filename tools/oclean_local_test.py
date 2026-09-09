"""
Oclean HA Integration – local BLE integration test
Simulates exactly one coordinator poll without Home Assistant.

Imports parser.py and const.py directly from the ha-integration folder
and prints the values the HA sensors would display.

Usage:
    python3 oclean_local_test.py                         # auto-scan
    python3 oclean_local_test.py --address <UUID>        # direct connection
    python3 oclean_local_test.py --address <UUID> --brushing
                                                         # extended wait for K3GUIDE /
                                                         # is_brushing (brush during the wait!)
    python3 oclean_local_test.py --address <UUID> --pages 5
                                                         # fetch up to 5 older sessions via 0309
    python3 oclean_local_test.py --address <UUID> --reset-brush-head
                                                         # send CMD_CLEAR_BRUSH_HEAD (020F)
                                                         # ⚠️  resets the brush-head counter!
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import importlib.util
import logging
import struct
import sys
import time
import types
from pathlib import Path
from typing import Any

# ── Direct import from ha-integration (without __init__.py / HA dependencies) ─
# const.py and parser.py have no homeassistant imports; __init__.py does.
# We load the files directly via importlib and provide the package stub.

_BASE = Path(__file__).parent.parent / "custom_components/oclean_ble"


def _load_oclean_module(name: str) -> types.ModuleType:
    """Load a single .py file from the oclean package, without __init__.py."""
    full_name = f"custom_components.oclean_ble.{name}"
    spec = importlib.util.spec_from_file_location(full_name, _BASE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "custom_components.oclean_ble"
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Package stubs (prevents __init__.py from being executed)
for _pkg in ("custom_components", "custom_components.oclean_ble"):
    if _pkg not in sys.modules:
        _stub = types.ModuleType(_pkg)
        _stub.__path__ = [str(_BASE)]
        _stub.__package__ = _pkg
        sys.modules[_pkg] = _stub

_const = _load_oclean_module("const")
_parser_mod = _load_oclean_module("parser")
_protocol_mod = _load_oclean_module("protocol")

# Constants from const.py
BATTERY_CHAR_UUID             = _const.BATTERY_CHAR_UUID
BLE_NOTIFICATION_WAIT         = _const.BLE_NOTIFICATION_WAIT
CHANGE_INFO_UUID              = _const.CHANGE_INFO_UUID
CMD_CALIBRATE_TIME_PREFIX     = _const.CMD_CALIBRATE_TIME_PREFIX
CMD_CALIBRATE_TIME_T1_PREFIX  = _const.CMD_CALIBRATE_TIME_T1_PREFIX
CMD_CLEAR_BRUSH_HEAD          = _const.CMD_CLEAR_BRUSH_HEAD
# 0x0202 is clearRunningDate – it must never be polled (see docs/OCLEANY3S-AUDIT.md).
CMD_CLEAR_RUNNING_DATA        = _const.CMD_CLEAR_RUNNING_DATA
CMD_QUERY_RUNNING_DATA        = _const.CMD_QUERY_RUNNING_DATA
CMD_QUERY_RUNNING_DATA_NEXT   = _const.CMD_QUERY_RUNNING_DATA_NEXT
CMD_QUERY_RUNNING_DATA_T1     = _const.CMD_QUERY_RUNNING_DATA_T1
CMD_QUERY_STATUS              = _const.CMD_QUERY_STATUS
DIS_HW_REV_UUID               = _const.DIS_HW_REV_UUID
DIS_MODEL_UUID                = _const.DIS_MODEL_UUID
DIS_SW_REV_UUID               = _const.DIS_SW_REV_UUID
OCLEAN_SERVICE_UUID           = _const.OCLEAN_SERVICE_UUID
READ_NOTIFY_CHAR_UUID         = _const.READ_NOTIFY_CHAR_UUID
RECEIVE_BRUSH_UUID            = _const.RECEIVE_BRUSH_UUID
SEND_BRUSH_CMD_UUID           = _const.SEND_BRUSH_CMD_UUID
SETTINGS_LAYOUT_GENERIC       = _const.SETTINGS_LAYOUT_GENERIC
TOOTH_AREA_NAMES              = _const.TOOTH_AREA_NAMES
WRITE_CHAR_UUID               = _const.WRITE_CHAR_UUID

# Functions from parser.py / protocol.py / const.py
parse_notification = _parser_mod.parse_notification
parse_battery      = _parser_mod.parse_battery
protocol_for_model = _protocol_mod.protocol_for_model
oclean_tz_index    = _const.oclean_tz_index

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
_LOG = logging.getLogger("oclean_local_test")

# Silence external libraries
for _name in ("bleak", "bleak.backends", "bleak_retry_connector"):
    logging.getLogger(_name).setLevel(logging.WARNING)


# ── Device discovery ──────────────────────────────────────────────────────────

async def find_device(address: str | None = None, timeout: float = 15.0):
    from bleak import BleakScanner

    # Direct connection via known address / CoreBluetooth UUID
    if address:
        _LOG.info("Scanning for device with address %s ...", address)
        device = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if device:
            _LOG.info("Found: %r @ %s", device.name, device.address)
            return device
        _LOG.error("Device %s not found (turned on?)", address)
        return None

    _LOG.info("Searching for Oclean device (service UUID / name) ...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, adv: (
            OCLEAN_SERVICE_UUID.lower() in [s.lower() for s in adv.service_uuids]
            or (d.name and "oclean" in d.name.lower())
        ),
        timeout=timeout,
    )
    if device:
        _LOG.info("Found: %r @ %s", device.name, device.address)
        return device

    _LOG.warning("Oclean service UUID not found – scanning all devices ...")
    devices = await BleakScanner.discover(timeout=timeout)
    for i, d in enumerate(devices):
        print(f"  [{i}] {d.name!r:30s}  {d.address}")
    if not devices:
        _LOG.error("No BLE devices found.")
        return None
    idx = int(input("Index of your Oclean device: "))
    return devices[idx]


# ── Single coordinator poll ───────────────────────────────────────────────────

async def single_poll(
    address: str | None = None,
    brushing_mode: bool = False,
    max_pages: int = 0,
    reset_brush_head: bool = False,
    calibrate: bool = True,
) -> dict[str, Any]:
    """Connect, read data, disconnect – exactly like OcleanCoordinator._setup_and_read().

    Args:
        address:          Device address / CoreBluetooth UUID. Auto-scan if None.
        brushing_mode:    Keep connection open for 30 s to catch K3GUIDE (0340)
                          notifications and is_brushing flag. Brush during this window!
        max_pages:        Fetch up to this many additional pages via CMD 0309.
                          0 = no pagination (default).
        reset_brush_head: Send CMD_CLEAR_BRUSH_HEAD (020F) before disconnecting.
                          ⚠️  This resets the brush head wear counter on the device!
        calibrate:        Send the 0201 time-calibration command (default True, like the
                          coordinator). Use --no-calibrate for a read-only diagnostic run.
    """
    from bleak import BleakClient

    device = await find_device(address=address)
    if device is None:
        return {}

    collected: dict[str, Any] = {}
    all_sessions: list[dict[str, Any]] = []
    seen_ts: set[int] = set()
    session_received = asyncio.Event()
    # Selected after the DIS read; defaults to the generic 0302 layout.
    state: dict[str, Any] = {"layout": SETTINGS_LAYOUT_GENERIC, "model": None}

    def notification_handler(sender: Any, raw: bytearray) -> None:
        data = bytes(raw)
        _LOG.info("NOTIFY  raw=%s", data.hex())
        parsed = parse_notification(data, state["layout"])
        if parsed:
            _LOG.info("NOTIFY  parsed=%s", parsed)
            collected.update(parsed)
            ts = parsed.get("last_brush_time")
            if ts and ts not in seen_ts:
                seen_ts.add(ts)
                all_sessions.append(dict(parsed))
                session_received.set()
        else:
            _LOG.debug("NOTIFY  (no known fields)")

    _LOG.info("Connecting to %s ...", device.name)
    async with BleakClient(device) as client:
        _LOG.info("Connected.")

        await asyncio.sleep(2.0)

        # 0. Device Information Service – selects the protocol profile
        for key, uuid in (
            ("model", DIS_MODEL_UUID),
            ("fw", DIS_SW_REV_UUID),
            ("hw", DIS_HW_REV_UUID),
        ):
            try:
                value = (await client.read_gatt_char(uuid)).decode("utf-8").strip("\x00").strip()
                _LOG.info("DIS %-5s = %r", key, value)
                if key == "model":
                    state["model"] = value or None
            except Exception as e:
                _LOG.warning("DIS %-5s read failed: %s", key, e)

        profile = protocol_for_model(state["model"])
        state["layout"] = profile.settings_layout
        _LOG.info(
            "Protocol profile: %s (settings_layout=%s, pagination=%s)",
            profile.name,
            profile.settings_layout,
            profile.supports_pagination,
        )

        # 1. Time calibration (profile-specific format) – skipped with --no-calibrate
        if calibrate:
            if profile.uses_t1_calibration:
                now = datetime.datetime.now().astimezone()
                offset = now.utcoffset()
                offset_min = int(offset.total_seconds() / 60) if offset is not None else 0
                weekday = (now.weekday() + 1) % 7  # Python Mon=0..Sun=6 → Oclean Sun=0..Sat=6
                payload = bytes(
                    [
                        now.year - 2000,
                        now.month,
                        now.day,
                        now.hour,
                        now.minute,
                        now.second,
                        weekday,
                        oclean_tz_index(offset_min),
                    ]
                )
                cal_cmd = CMD_CALIBRATE_TIME_T1_PREFIX + payload
            else:
                cal_cmd = CMD_CALIBRATE_TIME_PREFIX + struct.pack(">I", int(time.time()))
            try:
                await client.write_gatt_char(profile.write_char, cal_cmd, response=True)
                _LOG.info("✓ Time calibration sent (%s)", cal_cmd.hex())
            except Exception as e:
                _LOG.warning("✗ Time calibration: %s", e)
        else:
            _LOG.info("· Time calibration skipped (--no-calibrate)")

        # 2. Subscribe to the profile's notify characteristics
        for uuid in profile.notify_chars:
            try:
                await client.start_notify(uuid, notification_handler)
                _LOG.info("✓ Subscribed  %s", uuid)
            except Exception as e:
                _LOG.debug("✗ Not available: %s – %s", uuid[-8:], e)

        # 3. Profile query commands (0303 / 030201 / 0307 …) – never 0202
        for char_uuid, cmd in profile.query_commands:
            try:
                await client.write_gatt_char(char_uuid, cmd, response=True)
                _LOG.info("✓ Command %s sent via %s", cmd.hex(), char_uuid[-8:])
            except Exception as e:
                _LOG.warning("✗ Command %s: %s", cmd.hex(), e)

        # 3b. Optional: legacy Type-0 (0308) + explicit 0309 pagination
        if max_pages > 0:
            try:
                await client.write_gatt_char(WRITE_CHAR_UUID, CMD_QUERY_RUNNING_DATA, response=True)
                _LOG.info("✓ CMD_QUERY_RUNNING_DATA (0308) sent")
            except Exception as e:
                _LOG.debug("✗ 0308 skipped: %s", e)

        # 5c. Pagination via 0309
        if max_pages > 0:
            # Wait for first session before paginating
            try:
                await asyncio.wait_for(session_received.wait(), timeout=float(BLE_NOTIFICATION_WAIT))
            except asyncio.TimeoutError:
                _LOG.debug("No session received before pagination start")

            for page in range(max_pages):
                session_received.clear()
                try:
                    await client.write_gatt_char(
                        WRITE_CHAR_UUID, CMD_QUERY_RUNNING_DATA_NEXT, response=True
                    )
                    _LOG.info("✓ CMD_QUERY_RUNNING_DATA_NEXT (0309) sent – page %d", page + 1)
                except Exception as e:
                    _LOG.debug("✗ 0309 page %d failed: %s", page + 1, e)
                    break
                try:
                    await asyncio.wait_for(session_received.wait(), timeout=2.0)
                    _LOG.info("  Session received on page %d", page + 1)
                except asyncio.TimeoutError:
                    _LOG.info("  No more sessions after page %d (timeout)", page)
                    break
        else:
            # Default: just wait for notifications without pagination
            wait_s = 30 if brushing_mode else BLE_NOTIFICATION_WAIT
            if brushing_mode:
                _LOG.info(
                    "⏳ BRUSHING MODE – waiting %d s for K3GUIDE (0340) and is_brushing ...\n"
                    "   → Start brushing now!",
                    wait_s,
                )
            else:
                _LOG.info("Waiting %d s for notifications ...", wait_s)
            await asyncio.sleep(wait_s)

        # 6. Battery level
        _LOG.info("Collected data before battery read: %s", collected)
        try:
            batt_raw = await client.read_gatt_char(BATTERY_CHAR_UUID)
            batt = parse_battery(bytes(batt_raw))
            _LOG.info("✓ Battery read: %d%%", batt or 0)
            if batt is not None:
                collected["battery"] = batt
        except Exception as e:
            _LOG.warning("✗ Battery read: %s", e)

        # 7. Optional: reset brush head counter
        if reset_brush_head:
            _LOG.warning("⚠️  Sending CMD_CLEAR_BRUSH_HEAD (020F) – resets wear counter!")
            try:
                await client.write_gatt_char(WRITE_CHAR_UUID, CMD_CLEAR_BRUSH_HEAD, response=True)
                _LOG.info("✓ Brush head counter reset sent")
            except Exception as e:
                _LOG.warning("✗ Brush head reset failed: %s", e)

        # 8. Unsubscribe
        for uuid in profile.notify_chars:
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass

    if all_sessions:
        _LOG.info("Total sessions received this poll: %d", len(all_sessions))

    return collected


# ── Sensor output ─────────────────────────────────────────────────────────────

def print_sensor_state(data: dict[str, Any]) -> None:
    import datetime

    print()
    print("=" * 60)
    print("  HA sensor values after this poll")
    print("=" * 60)

    def show(label: str, key: str, unit: str = "", fmt=str) -> None:
        val = data.get(key)
        if val is None:
            print(f"  {label:32s}  -- (no value yet)")
        else:
            print(f"  {label:32s}  {fmt(val)}{unit}")

    show("battery",                "battery",              "%")
    show("last_brush_score",       "last_brush_score",     " / 100")
    show("last_brush_duration",    "last_brush_duration",  "s")
    show("last_brush_pressure",    "last_brush_pressure",  "")
    show("last_brush_pnum",        "last_brush_pnum",      "  (scheme ID)")
    show("brush_head_usage",       "brush_head_usage",     "")
    show("is_brushing",            "is_brushing",          "")

    ts = data.get("last_brush_time")
    if ts is not None:
        dt = datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"  {'last_brush_time':32s}  {dt}")
    else:
        print(f"  {'last_brush_time':32s}  -- (no value yet)")

    # Tooth area pressures (extended 0308 format only)
    areas = data.get("last_brush_areas")
    if isinstance(areas, dict):
        brushed = sum(1 for v in areas.values() if v > 0)
        print(f"  {'last_brush_areas':32s}  {brushed}/8 zones brushed")
        for name in TOOTH_AREA_NAMES:
            val = areas.get(name, 0)
            bar = "█" * (val // 32) if val > 0 else "·"
            print(f"    {name:28s}  {val:3d}  {bar}")
    else:
        print(f"  {'last_brush_areas':32s}  -- (extended 0308 format not received)")

    print("=" * 60)
    if not data:
        print("  ⚠  No data received – is the brush turned on and in range?")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(
    address: str | None,
    brushing_mode: bool,
    max_pages: int,
    reset_brush_head: bool,
    calibrate: bool = True,
) -> None:
    if reset_brush_head:
        print()
        print("⚠️  --reset-brush-head will send CMD 020F to the device.")
        print("   This resets the brush head wear counter permanently.")
        confirm = input("   Type YES to continue: ").strip()
        if confirm != "YES":
            print("Aborted.")
            return

    data = await single_poll(
        address=address,
        brushing_mode=brushing_mode,
        max_pages=max_pages,
        reset_brush_head=reset_brush_head,
        calibrate=calibrate,
    )
    print_sensor_state(data)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Oclean HA Integration – local BLE test")
    ap.add_argument(
        "--address", "-a",
        metavar="ADDR",
        help="Device address or CoreBluetooth UUID (macOS), e.g. from a prior scan",
    )
    ap.add_argument(
        "--brushing",
        action="store_true",
        default=False,
        help=(
            "Keep connection open for 30 s to catch K3GUIDE (0340) real-time zone "
            "guidance and is_brushing flag. Start brushing after the script connects."
        ),
    )
    ap.add_argument(
        "--pages",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Fetch up to N additional pages of session history via CMD 0309 "
            "(default: 0 = no pagination). Tests session history paging."
        ),
    )
    ap.add_argument(
        "--reset-brush-head",
        action="store_true",
        default=False,
        help=(
            "Send CMD_CLEAR_BRUSH_HEAD (020F) to reset the brush head wear counter. "
            "⚠️  This modifies device state – confirmation required."
        ),
    )
    ap.add_argument(
        "--no-calibrate",
        action="store_true",
        default=False,
        help=(
            "Skip the 0201 time-calibration write. Use for a read-only diagnostic run "
            "(the coordinator normally calibrates on every poll)."
        ),
    )
    args = ap.parse_args()
    asyncio.run(main(
        address=args.address,
        brushing_mode=args.brushing,
        max_pages=args.pages,
        reset_brush_head=args.reset_brush_head,
        calibrate=not args.no_calibrate,
    ))
