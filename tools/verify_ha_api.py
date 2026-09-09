"""Verify the integration against a *real* installed Home Assistant.

The unit test suite stubs every ``homeassistant.*`` module, so it cannot catch
API removals or signature changes in Home Assistant itself.  This script does:
it imports the integration against the Home Assistant version that is actually
installed in the current interpreter and checks every core API the integration
relies on.

Usage (with a venv that has Home Assistant installed):

    python tools/verify_ha_api.py

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

_RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> bool:
    _RESULTS.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    return ok


def main() -> int:
    # --- 1. Home Assistant itself -------------------------------------------
    try:
        import homeassistant.const as ha_const

        ha_version = ha_const.__version__
    except ImportError:
        print("FAIL  Home Assistant is not installed in this interpreter")
        print("      create a venv and `pip install homeassistant==2026.9.1`")
        return 2

    print(f"Home Assistant {ha_version} on Python {sys.version.split()[0]}\n")
    check(tuple(int(p) for p in ha_version.split(".")) >= (2026, 9), f"HA version >= 2026.9 ({ha_version})")

    # --- 2. Import every integration module ---------------------------------
    modules = [
        "custom_components.oclean_ble",
        "custom_components.oclean_ble.button",
        "custom_components.oclean_ble.config_flow",
        "custom_components.oclean_ble.const",
        "custom_components.oclean_ble.coordinator",
        "custom_components.oclean_ble.entity",
        "custom_components.oclean_ble.models",
        "custom_components.oclean_ble.number",
        "custom_components.oclean_ble.parser",
        "custom_components.oclean_ble.protocol",
        "custom_components.oclean_ble.select",
        "custom_components.oclean_ble.sensor",
        "custom_components.oclean_ble.statistics",
        "custom_components.oclean_ble.switch",
    ]
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:
            check(False, f"import {name} ({type(exc).__name__}: {exc})")
        else:
            check(True, f"import {name}")

    # --- 3. Recorder statistics API -----------------------------------------
    from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData

    try:
        meta = StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name="Oclean test",
            source="oclean_ble",
            statistic_id="oclean_ble:test_brush_score",
            unit_class="unitless",
            unit_of_measurement="%",
        )
    except Exception as exc:
        check(False, f"StatisticMetaData(mean_type/unit_class) rejected: {exc}")
    else:
        check(meta["mean_type"] is StatisticMeanType.ARITHMETIC, "StatisticMetaData accepts mean_type + unit_class")

    from custom_components.oclean_ble import statistics as stats

    api = stats._load_recorder_api()
    check(api is not None, "_load_recorder_api() resolves on this HA")
    if api is not None:
        _sd, _smd, mean_type, _add = api
        check(mean_type is StatisticMeanType, "recorder exposes StatisticMeanType (mean_type path active)")

    # --- 4. Device registry API ---------------------------------------------
    from homeassistant.helpers import device_registry as dr

    registry_cls = dr.DeviceRegistry
    has_new = hasattr(registry_cls, "async_get_device_by_identifier")
    check(has_new, "DeviceRegistry.async_get_device_by_identifier exists (non-deprecated lookup)")
    if has_new:
        import inspect

        sig = inspect.signature(registry_cls.async_get_device_by_identifier)
        check(
            list(sig.parameters) == ["self", "identifier", "config_entry_id"],
            f"async_get_device_by_identifier signature {sig}",
        )

    # The integration must prefer the new API; verify the fallback wiring.
    coord_src = (_REPO / "custom_components/oclean_ble/coordinator.py").read_text(encoding="utf-8")
    check("async_get_device_by_identifier" in coord_src, "coordinator uses async_get_device_by_identifier")

    # --- 5. Config flow API --------------------------------------------------
    from homeassistant.config_entries import ConfigFlowResult

    check(ConfigFlowResult is not None, "ConfigFlowResult importable")
    from custom_components.oclean_ble.config_flow import OcleanConfigFlow

    check(hasattr(OcleanConfigFlow, "async_step_user"), "config flow defines async_step_user")
    check(hasattr(OcleanConfigFlow, "async_step_bluetooth"), "config flow defines async_step_bluetooth")

    # --- 6. Coordinator / entity API ----------------------------------------
    import inspect as _inspect

    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    params = _inspect.signature(DataUpdateCoordinator.__init__).parameters
    check("config_entry" in params, "DataUpdateCoordinator.__init__ accepts config_entry")
    check("update_interval" in params, "DataUpdateCoordinator.__init__ accepts update_interval")

    from homeassistant.helpers.entity import EntityCategory  # noqa: F401

    check(True, "EntityCategory importable")

    from homeassistant.components.sensor import SensorEntityDescription

    # HA 2026.x uses a FrozenOrThawed metaclass instead of a plain dataclass,
    # so probe an instance rather than __dataclass_fields__.
    description = SensorEntityDescription(key="probe")
    for field in ("suggested_unit_of_measurement", "state_class", "native_unit_of_measurement", "device_class"):
        check(hasattr(description, field), f"SensorEntityDescription has '{field}'")

    from homeassistant.const import PERCENTAGE, UnitOfTime

    check(UnitOfTime.DAYS == "d", "UnitOfTime.DAYS available")
    check(PERCENTAGE == "%", f"PERCENTAGE still defined ({PERCENTAGE})")

    # --- 7. Bluetooth component ---------------------------------------------
    try:
        from homeassistant.components import bluetooth  # noqa: F401
    except Exception as exc:
        check(False, f"homeassistant.components.bluetooth import ({type(exc).__name__}: {exc})")
    else:
        check(True, "homeassistant.components.bluetooth import")

    # --- 8. Manifest sanity --------------------------------------------------
    import json

    manifest = json.loads((_REPO / "custom_components/oclean_ble/manifest.json").read_text(encoding="utf-8"))
    for key in ("domain", "name", "version", "codeowners", "documentation", "issue_tracker", "config_flow"):
        check(key in manifest, f"manifest has '{key}'")
    check(manifest["domain"] == "oclean_ble", "manifest domain matches the package")

    # --- 9. No deprecated device-registry call left in the poll path --------
    # (static check: the deprecated name may only appear in the documented fallback)
    import re

    deprecated_calls = re.findall(r"device_registry\.async_get_device\(", coord_src)
    check(len(deprecated_calls) <= 1, f"at most one deprecated lookup fallback ({len(deprecated_calls)} found)")

    # --- Summary -------------------------------------------------------------
    failed = [label for ok, label in _RESULTS if not ok]
    print()
    print(f"{len(_RESULTS) - len(failed)}/{len(_RESULTS)} checks passed")
    if failed:
        print("\nFAILED:")
        for label in failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
