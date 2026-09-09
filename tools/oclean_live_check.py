"""Live hardware check against a real Oclean brush – no Home Assistant required.

What it does, mirroring ``OcleanCoordinator._poll_device()``:

1. waits (up to ``--wait`` seconds) for the device to advertise – wake the brush
   by pressing its button if it has gone to sleep;
2. connects, optionally clears a stale Windows bond (``unpair``) which otherwise
   makes every GATT operation fail with "Unreachable";
3. reads the BLE Device Information Service and selects the protocol profile via
   ``protocol_for_protocol_model()``;
4. subscribes to the profile's notify characteristics, with the same CCCD-clear
   retry the coordinator uses;
5. sends the profile's query commands – **never** 0x0202 (clearRunningDate);
6. reassembles the ``*B#`` session stream and parses every 42-byte record with the
   integration's own parser;
7. writes a JSON report (``--out``) and prints a summary.

The report contains raw notification bytes and the device MAC address, so it is
**personal data** – keep it out of the repository.

Usage:
    python tools/oclean_live_check.py --address 70:28:45:68:4A:77 --wait 600
    python tools/oclean_live_check.py --address ... --no-calibrate --out report.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import importlib.util
import json
import logging
import struct
import sys
import time
import types
from pathlib import Path
from typing import Any

_BASE = Path(__file__).parent.parent / "custom_components/oclean_ble"


def _load(name: str) -> types.ModuleType:
    full = f"custom_components.oclean_ble.{name}"
    spec = importlib.util.spec_from_file_location(full, _BASE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "custom_components.oclean_ble"
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


for _pkg in ("custom_components", "custom_components.oclean_ble"):
    if _pkg not in sys.modules:
        _stub = types.ModuleType(_pkg)
        _stub.__path__ = [str(_BASE)]
        _stub.__package__ = _pkg
        sys.modules[_pkg] = _stub

_const = _load("const")
_parser = _load("parser")
_protocol = _load("protocol")

CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
DIS_MODEL = _const.DIS_MODEL_UUID
DIS_FW = _const.DIS_SW_REV_UUID
DIS_HW = _const.DIS_HW_REV_UUID
BATTERY = _const.BATTERY_CHAR_UUID
T1_MAGIC = b"\x2a\x42\x23"
RECORD_SIZE = _parser.T1_C3352G_RECORD_SIZE

_LOGGER = logging.getLogger("oclean_live")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")


class Session:
    """Collects notifications and reassembles the *B# record stream."""

    def __init__(self, layout: str) -> None:
        self.layout = layout
        self.raw: list[str] = []
        self.parsed: list[dict[str, Any]] = []
        self.collected: dict[str, Any] = {}
        self.buf = bytearray()
        self.expected = 0
        self.in_progress = False
        self.record_count = 0
        # set on every notification so the command loop can pace itself like
        # the app (wait for the answer before sending the next command)
        self.answered = asyncio.Event()

    def _accept(self, parsed: dict[str, Any]) -> None:
        if not parsed:
            return
        self.parsed.append(parsed)
        self.collected.update(parsed)

    def _flush(self) -> None:
        buf = bytes(self.buf)
        n = len(buf) // RECORD_SIZE
        self.in_progress = False
        self.buf = bytearray()
        self.expected = 0
        _LOGGER.info("  *B# complete: parsing %d record(s) (%d bytes)", n, len(buf))
        for i in range(n):
            rec = buf[i * RECORD_SIZE : (i + 1) * RECORD_SIZE]
            self._accept(_parser.parse_t1_c3385w0_record(rec, coverage_norm_threshold=9))

    def handle(self, _sender: Any, data: bytearray) -> None:
        raw = bytes(data)
        self.raw.append(raw.hex())
        self.answered.set()
        if self.in_progress:
            self.buf.extend(raw)
            _LOGGER.debug("  *B# continuation +%d (%d/%d)", len(raw), len(self.buf), self.expected)
            if len(self.buf) >= self.expected:
                self._flush()
            return

        parsed = _parser.parse_notification(raw, self.layout)
        _LOGGER.info("  NOTIFY %s -> %s", raw.hex(), parsed or "(no fields)")

        if len(raw) >= 8 and raw[2:5] == T1_MAGIC:
            payload = raw[2:]
            count = (payload[3] << 8) | payload[4]
            if count > 0:
                self.record_count = count
                self.buf = bytearray(payload[5:])
                self.expected = count * RECORD_SIZE
                self.in_progress = True
                _LOGGER.info("  *B# header: count=%d expected=%d inline=%d", count, self.expected, len(self.buf))
                if len(self.buf) >= self.expected:
                    self._flush()
                return
        self._accept(parsed)

    def flush_partial(self) -> None:
        if self.in_progress and self.buf:
            self.expected = (len(self.buf) // RECORD_SIZE) * RECORD_SIZE
            self._flush()


async def wait_for_device(address: str, wait_s: float, interval: float = 5.0):
    """Wait until the device advertises, then return a BLEDevice."""
    from bleak import BleakScanner

    deadline = time.monotonic() + wait_s
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        dev = await BleakScanner.find_device_by_address(address, timeout=min(interval, 10.0))
        if dev is not None:
            _LOGGER.info("found %s after %d scan(s)", dev.address or address, attempt)
            return dev
        _LOGGER.info("not advertising yet (scan %d) – press the brush button to wake it", attempt)
    return None


async def run(args: argparse.Namespace) -> int:
    from bleak import BleakClient

    _LOGGER.info("waiting up to %ss for %s ...", args.wait, args.address)
    device = await wait_for_device(args.address, args.wait)
    if device is None:
        _LOGGER.error("device never appeared – press the brush button and retry")
        return 1

    report: dict[str, Any] = {
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
        "address": args.address,
        "advertised_name": device.name,
    }

    client = BleakClient(device, timeout=args.timeout)
    await client.connect()
    _LOGGER.info("connected: %s", client.is_connected)

    if args.unpair:
        try:
            await client.unpair()
            _LOGGER.info("stale bond removed (unpair)")
            await client.disconnect()
            await asyncio.sleep(1.0)
            client = BleakClient(await wait_for_device(args.address, 30.0), timeout=args.timeout)
            await client.connect()
            _LOGGER.info("reconnected after unpair: %s", client.is_connected)
        except Exception as exc:
            _LOGGER.warning("unpair failed: %s", exc)

    # --- DIS -----------------------------------------------------------------
    dis: dict[str, str | None] = {}
    for key, uuid in (("model", DIS_MODEL), ("fw", DIS_FW), ("hw", DIS_HW)):
        try:
            dis[key] = (await client.read_gatt_char(uuid)).decode("utf-8").strip("\x00").strip()
        except Exception as exc:
            _LOGGER.warning("DIS %s read failed: %s", key, exc)
            dis[key] = None
    report["dis"] = dis
    _LOGGER.info("DIS: %s", dis)

    profile = _protocol.protocol_for_model(dis.get("model"))
    report["profile"] = {
        "name": profile.name,
        "settings_layout": profile.settings_layout,
        "supports_pagination": profile.supports_pagination,
        "uses_t1_calibration": profile.uses_t1_calibration,
        "notify_chars": list(profile.notify_chars),
        "query_commands": [[u, c.hex()] for u, c in profile.query_commands],
        "is_known_model": _protocol.is_known_model(dis.get("model")),
    }
    _LOGGER.info("profile: %s (layout=%s)", profile.name, profile.settings_layout)

    session = Session(profile.settings_layout)

    # --- time calibration ----------------------------------------------------
    if args.calibrate:
        if profile.uses_t1_calibration:
            now = datetime.datetime.now().astimezone()
            off = now.utcoffset()
            off_min = int(off.total_seconds() / 60) if off else 0
            payload = bytes(
                [
                    now.year - 2000,
                    now.month,
                    now.day,
                    now.hour,
                    now.minute,
                    now.second,
                    (now.weekday() + 1) % 7,
                    _const.oclean_tz_index(off_min),
                ]
            )
            cmd = _const.CMD_CALIBRATE_TIME_T1_PREFIX + payload
        else:
            cmd = _const.CMD_CALIBRATE_TIME_PREFIX + struct.pack(">I", int(time.time()))
        try:
            await client.write_gatt_char(profile.write_char, cmd, response=True)
            _LOGGER.info("calibration sent: %s", cmd.hex())
        except Exception as exc:
            _LOGGER.warning("calibration failed: %s", exc)
    else:
        _LOGGER.info("calibration skipped (--no-calibrate)")

    # --- subscribe (with the coordinator's CCCD-clear retry) -----------------
    subscribed: list[str] = []
    for uuid in profile.notify_chars:
        ok = False
        for attempt in ("direct", "cccd-clear"):
            try:
                if attempt == "cccd-clear":
                    try:
                        await client.write_gatt_char(CCCD_UUID, b"\x00\x00", response=True)
                    except Exception as exc:
                        _LOGGER.debug("CCCD clear on %s failed: %s", uuid[-8:], exc)
                await client.start_notify(uuid, session.handle)
                ok = True
                break
            except Exception as exc:
                _LOGGER.debug("start_notify %s (%s) failed: %s", uuid[-8:], attempt, exc)
        if ok:
            subscribed.append(uuid)
            _LOGGER.info("subscribed: %s", uuid[-8:])
        else:
            _LOGGER.warning("could NOT subscribe: %s", uuid[-8:])
    report["subscribed"] = subscribed

    # --- query commands, paced exactly like the app --------------------------
    # APK: single-thread queue, wait for the answer (receiveTimeout 5000 ms),
    # then SystemClock.sleep(100) before the next command (g/e.java:139).
    sent: list[str] = []
    for char_uuid, cmd in profile.query_commands:
        session.answered.clear()
        try:
            await client.write_gatt_char(char_uuid, cmd, response=True)
            sent.append(cmd.hex())
            _LOGGER.info("sent %s via %s", cmd.hex(), char_uuid[-8:])
        except Exception as exc:
            _LOGGER.warning("send %s failed: %s", cmd.hex(), exc)
            continue
        try:
            await asyncio.wait_for(session.answered.wait(), timeout=args.command_wait)
            _LOGGER.info("  answered")
        except asyncio.TimeoutError:
            _LOGGER.warning("  no answer within %.1fs – stopping the sequence", args.command_wait)
            break
        await asyncio.sleep(0.1)
    report["commands_sent"] = sent

    _LOGGER.info("collecting notifications for %ss ...", args.collect)
    await asyncio.sleep(args.collect)
    session.flush_partial()

    # --- battery -------------------------------------------------------------
    try:
        batt = _parser.parse_battery(bytes(await client.read_gatt_char(BATTERY)))
        report["battery"] = batt
        _LOGGER.info("battery: %s", batt)
    except Exception as exc:
        _LOGGER.warning("battery read failed: %s", exc)
        report["battery"] = None

    try:
        await client.disconnect()
    except Exception:
        pass

    # --- results -------------------------------------------------------------
    report["notifications"] = session.raw
    report["parsed"] = session.parsed
    report["collected"] = session.collected
    report["record_count"] = session.record_count

    print()
    print("=" * 68)
    print(f"  Live check – {dis.get('model')} (fw {dis.get('fw')}, hw {dis.get('hw')})")
    print("=" * 68)
    print(f"  profile           {profile.name}  (0302 layout: {profile.settings_layout})")
    print(f"  subscribed        {[u[-8:] for u in subscribed]}")
    print(f"  commands sent     {sent}")
    print(f"  notifications     {len(session.raw)}")
    print(f"  *B# record count  {session.record_count}")
    print(f"  battery           {report['battery']}")
    for key in sorted(session.collected):
        print(f"  {key:24s} {session.collected[key]}")
    print("=" * 68)

    if args.out:
        out = Path(args.out)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        _LOGGER.info("report written to %s (contains the MAC – do not commit)", out)

    if not session.raw:
        print("\n  WARNING: no notification received at all – the CCCD subscribe or the")
        print("  query commands did not reach the device.")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Live BLE check of an Oclean brush")
    ap.add_argument("--address", "-a", required=True, help="Bluetooth MAC address")
    ap.add_argument("--wait", type=float, default=120.0, help="seconds to wait for advertising (default 120)")
    ap.add_argument("--timeout", type=float, default=25.0, help="connection timeout")
    ap.add_argument("--collect", type=float, default=12.0, help="seconds to collect notifications")
    ap.add_argument(
        "--command-wait",
        type=float,
        default=5.0,
        help="seconds to wait for each command's answer (APK receiveTimeout 5000 ms)",
    )
    ap.add_argument("--no-calibrate", action="store_true", help="skip the 0201 time-calibration write")
    ap.add_argument("--no-unpair", action="store_true", help="do not remove a stale Windows bond first")
    ap.add_argument("--out", help="write a JSON report to this path (personal data!)")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
