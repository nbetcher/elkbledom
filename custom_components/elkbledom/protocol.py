import logging
from typing import List, Optional

from .model import Model

LOGGER = logging.getLogger(__name__)


class ElkProtocol:
    """Model indirection + model-independent frame builders.

    Shared holder passed to BOTH transport and device (it *is* the
    ``ModelContext`` of the refactor). Constructed in the facade, which performs
    the pre-connect ``detect_model()`` so entities can read ``model`` /
    ``model_name`` at construction (before any connect).

    Pure/leaf layer: it performs no BLE I/O. ``read_uuid()`` / ``write_uuid()``
    are *model-table* lookups used for characteristic resolution + login; the
    resolved, stringified GATT handles the write path dereferences live SOLELY
    on ``BLETransport`` (see the P5 note below).
    """

    def __init__(self, hass, device_name_getter, forced_model: Optional[str] = None) -> None:
        # device_name_getter: Callable[[], str|None] -> transport/facade supplies
        # the current BLE device name lazily (name may only exist after _device
        # is set). Mirrors reads of self._device.name in _detect_model. The getter
        # MUST tolerate being called before _transport exists and return None for a
        # missing device (facade uses getattr, not attribute access — LOW-7).
        self._hass = hass
        self._get_name = device_name_getter
        self._forced_model = forced_model
        self._model: Optional[Model] = None
        self._model_name: Optional[str] = None
        # NOTE (P5): ElkProtocol holds NO resolved GATT UUIDs. read_uuid()/write_uuid()
        # below are *model-table lookups* (Model.get_*_uuid) used for characteristic
        # resolution + login; the resolved, stringified handles the write path
        # dereferences live SOLELY on BLETransport (transport._read_uuid/_write_uuid,
        # consumed by _write_while_connected). Storing a resolved handle here would
        # make the write path use the model's generic UUID and silently break handle
        # refinement (§2.4, invariants #6/#9). Do not add _read_uuid/_write_uuid here.

    # ---- model indirection (from elkbledom.py:221-252) ----
    @property
    def model(self) -> Model:
        return self._model

    @property
    def model_name(self) -> Optional[str]:
        return self._model_name

    @property
    def forced_model(self) -> Optional[str]:
        return self._forced_model

    def detect_model(self, char_handle: Optional[int] = None) -> None:
        """Detect the model using Model manager.

        Was ``_detect_model`` (elkbledom.py:221-252). Populates ``self._model`` /
        ``self._model_name``. forced_model wins; else handle-based when
        char_handle given; else name-based; fallback 'ELK-BLEDOM'.
        """
        if not hasattr(self, "_model") or self._model is None:
            self._model = Model(self._hass)

        name = self._get_name()

        # Use forced model if provided, otherwise auto-detect
        if self._forced_model:
            self._model_name = self._forced_model
            LOGGER.info("%s: Using forced model: %s", name, self._forced_model)
        elif char_handle is not None:
            # Use handle-based detection when available
            detected = self._model.detect_model_by_handle(name or "", char_handle)
            if detected:
                if hasattr(self, "_model_name") and self._model_name and detected != self._model_name:
                    LOGGER.info("%s: Model refined from '%s' to '%s' based on handle 0x%04x",
                                name, self._model_name, detected, char_handle)
                self._model_name = detected
            else:
                LOGGER.warning("Unknown model for device %s with handle 0x%04x", name, char_handle)
                self._model_name = "ELK-BLEDOM"  # Default fallback
        else:
            # Standard name-based detection
            self._model_name = self._model.detect_model(name or "")
            LOGGER.debug("%s: Auto-detected model: %s", name, self._model_name)

        if not self._model_name:
            LOGGER.warning("Unknown model for device %s", name)
            self._model_name = "ELK-BLEDOM"  # Default fallback

    def refine_by_handle(self, char_handle: int) -> None:
        """Transport calls this from _resolve_characteristics after finding the
        write handle. == detect_model(char_handle) (elkbledom.py:1016)."""
        self.detect_model(char_handle)

    # ---- UUID lookups the transport consumes (from _resolve_characteristics) ----
    def read_uuid(self) -> Optional[str]:
        return self._model.get_read_uuid(self._model_name)   # 999

    def write_uuid(self) -> Optional[str]:
        return self._model.get_write_uuid(self._model_name)  # 1008/1018

    # ---- connection-family predicates (name-prefix logic; centralized here so
    #      transport/device call these instead of hardcoding strings). ----
    def requires_login(self, name: str) -> bool:      # 872
        n = (name or "").lower()
        return n.startswith("melk") or n.startswith("modelx")

    def requires_read_uuid(self, name: str) -> bool:  # 1026-1029 (inverted)
        n = (name or "").lower()
        return not (n.startswith("melk") or n.startswith("modelx"))

    def uses_notifications(self, name: str) -> bool:  # 957 / 1111
        n = (name or "").lower()
        return not (n.startswith("melk") or n.startswith("ledble"))

    def mic_effect_extended(self, name: str) -> bool:  # 661-662 (device uses this)
        n = (name or "").lower()
        return n.startswith("melk") or n.startswith("modelx")

    def login_frames(self) -> tuple:      # 896/898
        return (bytes([0x7e, 0x07, 0x83]), bytes([0x7e, 0x04, 0x04]))

    def login_step_delay(self) -> float:  # LOGIN_STEP_DELAY
        return 1.0

    # ---- model-INDEPENDENT frame builders (Phase 1 consolidation target) ----
    #      Model-DEPENDENT frames (turn_on/off, white, color, color_temp, brightness,
    #      effect, effect_speed, query) STAY in models.json via Model.get_*_cmd.
    @staticmethod
    def mic_power(on: bool) -> List[int]:                         # 683 (on) / 376,696 (off)
        return [0x7e, 0x04, 0x07, 0x01 if on else 0x00, 0xff, 0xff, 0xff, 0x00, 0xef]

    @staticmethod
    def mic_eq(value: int, *, extended: bool) -> List[int]:      # 661-664 (clamp is protocol-level)
        value = min(max(int(value), 0x80), 0x87 if extended else 0x83)
        return [0x7e, 0x05, 0x03, value, 0x04, 0xff, 0xff, 0x00, 0xef]

    @staticmethod
    def mic_sensitivity(value: int) -> List[int]:                # 676
        return [0x7e, 0x04, 0x06, value, 0xff, 0xff, 0xff, 0x00, 0xef]

    @staticmethod
    def scheduler(days: int, hours: int, minutes: int, enabled: bool, *, off: bool) -> List[int]:  # 716-722 / 726-731
        value = days + 0x80 if enabled else days
        return [0x7e, 0x08, 0x82, hours, minutes, 0x00, 0x01 if off else 0x00, value, 0xef]

    @staticmethod
    def sync_time(hour: int, minute: int, second: int, weekday: int) -> List[int]:  # model.py:362-373
        return [0x7e, 0x07, 0x83, hour, minute, second, weekday, 0xff, 0xef]
