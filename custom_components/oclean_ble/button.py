"""Button entities for the Oclean Toothbrush integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_NAME, CONF_MAC_ADDRESS, DOMAIN
from .coordinator import OcleanCoordinator
from .entity import OcleanEntity

# The toothbrush accepts only ONE GATT connection at a time; serialise entity
# actions so two concurrent writes cannot race for the single BLE slot.
PARALLEL_UPDATES = 1

BUTTON_DESCRIPTIONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(
        key="reset_brush_head",
        name="Reset Brush Head",
        icon="mdi:toothbrush-paste",
    ),
    ButtonEntityDescription(
        key="sync_time",
        name="Sync Time",
        icon="mdi:clock-sync",
    ),
    ButtonEntityDescription(
        key="poll_now",
        name="Update Now",
        icon="mdi:refresh",
    ),
    # OCLEANY3S only: recovery when the brush stores too many sessions to read
    # them safely. Disabled by default; unavailable on every other model.
    ButtonEntityDescription(
        key="clear_stored_sessions",
        name="Clear Stored Sessions",
        icon="mdi:delete-sweep",
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Oclean button entities."""
    coordinator: OcleanCoordinator = hass.data[DOMAIN][entry.entry_id]
    mac = entry.data[CONF_MAC_ADDRESS]
    device_name = entry.data.get(CONF_DEVICE_NAME, "Oclean")

    async_add_entities(OcleanButton(coordinator, description, mac, device_name) for description in BUTTON_DESCRIPTIONS)


class OcleanButton(OcleanEntity, ButtonEntity):
    """Oclean button entity (e.g. brush head reset)."""

    def __init__(
        self,
        coordinator: OcleanCoordinator,
        description: ButtonEntityDescription,
        mac: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, mac, device_name, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        if self.entity_description.key == "clear_stored_sessions":
            return super().available and self.coordinator.is_verified_y3s
        return super().available

    async def async_press(self) -> None:
        """Handle button press."""
        if self.entity_description.key == "reset_brush_head":
            await self.coordinator.async_reset_brush_head()
        elif self.entity_description.key == "sync_time":
            await self.coordinator.async_sync_time()
        elif self.entity_description.key == "poll_now":
            await self.coordinator.async_poll_now()
        elif self.entity_description.key == "clear_stored_sessions":
            await self.coordinator.async_clear_stored_sessions()
