# elkbledom god-object refactor — requirements & design

Status: authoritative design. This document is the contract implementers follow verbatim.
Scope: decompose `BLEDOMInstance` in `custom_components/elkbledom/elkbledom.py`
(~1116 lines) into a clean layered architecture behind a thin, byte-compatible
`BLEDOMInstance` facade, then (in later phases) migrate entities onto the new
layers. All ten behavioral invariants defined in §5 must be preserved exactly.

All file:line citations are to the current tree
(`custom_components/elkbledom/…`, latest package `elkbledom.py` = 1116 lines).
Correctness gate: `python -m py_compile custom_components/elkbledom/*.py`
plus the §4 surface-parity review and the §5 invariant audit. There is no HA
runtime here — see §8.

---

## 1. Overview & goals

### 1.1 Goal

Refactor the elkbledom BLE LED-strip integration so that the single ~1000-line
`BLEDOMInstance` class (`elkbledom.py:139`) is split into four cohesive modules
with one responsibility each, coordinated by a thin facade that preserves the
*entire* public surface the entities/`__init__`/`config_flow` depend on. Then,
in optional-but-planned phases, migrate the entities onto the new layers and
consolidate the hardcoded protocol frames.

The instruction is to **implement all phases** — Phase 0 is REQUIRED, Phases 1-4
are marked OPTIONAL but are also to be implemented, in order, each independently
compilable.

### 1.2 The god-object problem

`BLEDOMInstance` currently owns, in one class, all of the following concerns —
each of which changes for different reasons and should live in its own module:

| Concern | Current location (elkbledom.py) |
|---|---|
| BLE connection lifecycle | `_ensure_connected` 822-966, `_disconnected` 1044-1049, `_reset_disconnect_timer` 1033-1042, `_disconnect` 1051-1056, `_execute_timed_disconnect` 1081-1088, `_execute_disconnect` 1090-1116, `stop` 1058-1079 |
| Write path + pacing | `_write` 322-331, `_write_while_connected` 333-346, `_write_many` 348-359, `COMMAND_GAP` 40, `MIC_EXIT_SETTLE` 43, `_last_write_at` 161 |
| Retry policy | `retry_bluetooth_connection_error` 51-88 |
| Availability + callback registry | `register_callback` 267-277, `_fire_callbacks` 279-282, `available` 284-287, `_set_available` 289-295, `_async_update_ble` 297-307, `_async_unavailable` 309-320, `_callbacks`/`_available`/`_rssi_latest` 187-189 |
| MELK/MODELX login | inline in `_ensure_connected` 870-905, `LOGIN_STEP_DELAY` 47 |
| Characteristic resolution + notifications | `_resolve_characteristics` 986-1031, `_notification_handler` 969-984 |
| Optimistic state cache | `_is_on`…`_color_temp` 168-180 |
| Model indirection | `_detect_model` 221-252, `Model` + `models.json` |
| Hardcoded protocol frames | mic/scheduler/login byte-lists inline in setters 376/664/676/683/696/722/731/896/898 |
| HA brightness-mode policy | `apply_brightness_mode` 254-262, `set_brightness` 569-633 |
| Mic-exit policy | `_exit_mic_mode` 361-379 |

Problems this causes: connection/transport bugs and protocol/state bugs share
one file and one lock; the retry decorator is annotated `"BLEDOMInstance"` and
cannot be reused; the byte frames are duplicated (e.g. the mic-off frame appears
at 376 and 696); and there is no seam at which a `DataUpdateCoordinator` or a
mock transport could be inserted for testing.

### 1.3 Design principles

1. **Behavior-preserving.** Bodies move verbatim; only `self.X` bindings and the
   cross-layer hops in §2.4 change. Do not "clean up" logic while moving it.
2. **Facade compatibility is non-negotiable** (§4). Entities/`__init__`/
   `config_flow` do not change in Phase 0.
3. **One bidirectional seam only.** Everything flows device → (transport,
   protocol, state); the *sole* upward hop is transport → protocol during
   characteristic resolution (§2.4).
4. **`models.json` stays the per-model data source.** `Model` is unchanged; only
   the model-*independent* hardcoded frames are consolidated into `ElkProtocol`.

---

## 2. Target architecture

### 2.1 The five components

| Component | Module | Owns |
|---|---|---|
| **BLETransport** | `transport.py` | Connection lifecycle, write path + pacing, `@retry` decorator (definition), availability + callback registry, MELK/MODELX login, characteristic resolution + notifications, idle-disconnect, both locks, the BLE client/device |
| **ElkProtocol** | `protocol.py` | Model indirection (`Model` + `model_name` + `forced_model`), `_detect_model`/handle refinement, UUID lookups, and (Phase 1+) the pure builders for the model-independent hardcoded frames |
| **ElkState** | `state.py` | The optimistic cache dataclass + base/rgb coupling helpers + `restore()` |
| **ElkDevice** | `device.py` | Intents → frames → transport writes; holds `state`, `protocol`, `transport`, `brightness_mode`; owns brightness-mode policy, `_exit_mic_mode`, mic-EQ clamp; the `@retry` decorator is *applied* here |
| **BLEDOMInstance** | `elkbledom.py` | Thin facade: constructs the stack, delegates the whole public surface, exposes the 8 externally-written private attrs as property/setter pairs into `ElkState`, and re-exports the 2 `@callback` methods. Also re-exports `DeviceData` (which now lives in `device_data.py`, §2.5). |

### 2.2 Shared collaborator: `ModelContext`

The single hardest coupling is `model_name`: it is **read** by the framing layer
(`self._model.get_*_cmd(self._model_name, …)`) and **mutated** by
characteristic resolution during connect (`_resolve_characteristics` calls
`_detect_model(char_handle)` at 1014-1016 then re-reads `get_write_uuid` at
1018-1020). Transport owns resolution; device/protocol owns framing. They share
one mutable `model_name` + resolved UUIDs.

`ElkProtocol` is that shared holder (it *is* the `ModelContext` — no separate
class is required). It is constructed once in the facade, passed to **both**
transport and device, and is the only object either layer mutates across the
seam.

### 2.3 Dependency diagram

```
                         __init__.py / config_flow.py / entity.py
                         light.py / number.py / select.py / switch.py
                                        │  (unchanged in Phase 0)
                                        ▼
                          ┌──────────────────────────────┐
                          │  BLEDOMInstance  (facade)     │  elkbledom.py
                          │  + re-exports DeviceData (§2.5)│
                          └───────────────┬───────────────┘
                                          │ builds & holds
                 ┌────────────────────────┼─────────────────────────┐
                 ▼                        ▼                          ▼
         ┌──────────────┐        ┌────────────────┐         ┌───────────────┐
         │  ElkDevice   │ ─────▶ │  BLETransport  │ ──┐     │   ElkState    │
         │  device.py   │ writes │  transport.py  │   │     │   state.py    │
         │              │ ─────▶ │                │   │     └───────▲───────┘
         │  holds:      │  reads └────────┬───────┘   │             │ reads/writes
         │   state ─────┼──────────────────────────────┼─────────────┘
         │   protocol ──┼───────┐         │ resolve-char hop (the ONE upward seam)
         │   transport  │       ▼         ▼
         └──────────────┘   ┌────────────────────┐
                            │    ElkProtocol      │  protocol.py
                            │  (== ModelContext)  │
                            │  Model + models.json│  model.py / models.json  (unchanged)
                            └────────────────────┘
```

Edges:
- **facade → {device, transport, state, protocol}**: construction + delegation.
- **device → transport**: `write_frame`, `write_frames`, `connect`, `stop`,
  `set_available`, `fire_callbacks`, `register_callback`, `name`, `rssi`,
  `is_connected`, `available`.
- **device → protocol**: `model`, `model_name`, `get_*_cmd`, frame builders.
- **device → state**: read/write the cache.
- **transport → protocol**: `read_uuid()`, `write_uuid()`,
  `refine_by_handle(handle)`, and the login/notification/read-uuid family
  predicates. **This is the only upward edge.**
- **transport → state**: none. **protocol → anything**: none (leaf).

### 2.4 The one genuinely cross-layer interaction (call it out)

`_resolve_characteristics` (1007-1020) does handle-based model refinement:
after finding the write char it calls `_detect_model(char_handle)` (mutating
`model_name`) then **re-reads `get_write_uuid`** because the model may have
changed. In the split, resolution lives in transport but model/detection live in
protocol. Preserve it exactly by having transport call `protocol.write_uuid()` /
`protocol.read_uuid()` for lookups, and after resolving the write handle call
`protocol.refine_by_handle(handle)` then re-read `protocol.write_uuid()`. Login
(889) likewise reads `protocol.write_uuid()`. ⇒ **transport holds a reference to
protocol.** Everything else is device → down.

### 2.5 Module import graph — MUST stay acyclic (P1)

`python -m py_compile` compiles each file in isolation and **never executes an
import**, so an import cycle sails through the compile gate and only explodes when
HA imports the package. Two cycles are latent and are forbidden by construction:

- **`transport.py` ↔ `elkbledom.py` via `DeviceData`.** Transport's ctor runs the
  discovery scan (196-211) that constructs `DeviceData`, but `config_flow.py:3`
  imports `DeviceData` from `elkbledom.py`. If `DeviceData` stayed in
  `elkbledom.py`, `transport.py` would need `from .elkbledom import DeviceData`
  while the facade does `from .transport import BLETransport` → every consumer
  imports the facade first, so `elkbledom` starts executing, hits its
  `from .transport import BLETransport`, which runs `transport.py`, which hits
  `from .elkbledom import DeviceData` against a **partially-initialized**
  `elkbledom` (its `DeviceData` class not yet bound) → `ImportError` at load.
  **Fix (Phase 0, sub-step 0b):** move `DeviceData` into its own `device_data.py`;
  `transport.py` and `elkbledom.py` both import it from there; `elkbledom.py`
  **re-exports** it (`from .device_data import DeviceData`) so `config_flow.py:3`
  is untouched. Transport keeps the discovery scan verbatim (196-211).
- **`model.py` ↔ `protocol.py` (Phase 1).** Do NOT make `Model` delegate to
  `ElkProtocol.sync_time` — `protocol.py` imports `model.py`, so the back-import
  is a cycle. See §3.2 / §6-Phase-1 / MEDIUM-6.

Acyclic import order (leaf → root), each module importing only from earlier ones:
`model.py` → `device_data.py` → `state.py` → `protocol.py` → `transport.py` →
`device.py` → `elkbledom.py`.
- `device_data.py` imports `model.py`.
- `protocol.py` imports `model.py`.
- `transport.py` imports `device_data.py` (runtime, ctor scan) + constants; its
  `protocol: "ElkProtocol"` parameter is a **string annotation only** — transport
  does NOT import `protocol.py` (it duck-types the passed instance), so there is
  no transport↔protocol import edge.
- `device.py` imports `transport.py` + `protocol.py` + `state.py`.
- `elkbledom.py` (facade) imports all of the above. **No module imports
  `elkbledom.py`.**

The import graph is verified offline by the §6-checklist smoke test (stub the
absent HA/bleak deps, then actually `import` each module), because py_compile
cannot catch a cycle.

---

## 3. Exact interfaces (the implementer's contract)

Signatures below are the verbatim contract. Where a body is "move verbatim,
rebind `self.X`", the cited current lines are the source of truth for behavior.

### 3.1 `state.py` — `ElkState`

```python
from dataclasses import dataclass
from typing import Optional, Tuple

_MISSING = object()

@dataclass
class ElkState:
    # Optimistic cache. Defaults MUST match elkbledom.py:168-180 exactly.
    is_on: Optional[bool] = None                          # 168
    rgb_color: Optional[Tuple[int, int, int]] = None      # 169
    rgb_color_base: Tuple[int, int, int] = (255, 255, 255)# 170  base w/o brightness scaling
    brightness: int = 255                                 # 171
    effect: Optional[int] = None                          # 172
    effect_speed: int = 50                                # 173  (NOT 0 — see risk §8.2)
    color_temp_kelvin: Optional[int] = None               # 174
    mic_effect: Optional[int] = None                      # 175
    mic_sensitivity: int = 50                             # 176
    mic_enabled: bool = False                             # 177
    color_temp: Optional[int] = None                      # 180  0-100 warm; set_color_temp path

    # --- base/rgb coupling helpers (the exact couplings that exist today) ---
    def apply_color(self, rgb: Tuple[int, int, int], is_base: bool) -> None:
        """Mirror set_color 555-558: always set rgb_color; set base only if is_base."""
        self.rgb_color = rgb
        if is_base:
            self.rgb_color_base = rgb

    def apply_scaled_color(self, scaled_rgb: Tuple[int, int, int]) -> None:
        """Mirror set_brightness RGB path 593: rgb_color = scaled, base preserved."""
        self.rgb_color = scaled_rgb

    def get_color_base(self) -> Tuple[int, int, int]:
        """Mirror elkbledom.py:264."""
        return self.rgb_color_base

    # --- optional convenience for the entity-migration phase (Phase 3) ---
    def restore(self, *, is_on=_MISSING, brightness=_MISSING, rgb_color=_MISSING,
                color_temp_kelvin=_MISSING, effect=_MISSING, effect_speed=_MISSING,
                mic_enabled=_MISSING, couple_base: bool = True) -> None:
        """Assign only provided fields. When rgb_color is provided and couple_base,
        also set rgb_color_base = rgb_color (mirrors light.py:186). None-guards and
        the try/except around RGB stay in the caller; restore() only skips _MISSING."""
        ...
```

Notes:
- `apply_color` cannot express the color-temp RGB-fallback (which *decouples*
  base from rgb: `rgb_color`=scaled, `rgb_color_base`=unscaled interpolated
  white, elkbledom.py:539-540). Keep raw field assignment available for that
  path — do **not** route it through `apply_color` (see §8.3).
- `restore()` is used only in Phase 3. In Phase 0 the facade property/setters
  carry light.py's conditional restore logic unchanged, so `restore()` is not on
  the Phase-0 critical path.

### 3.2 `protocol.py` — `ElkProtocol` (== ModelContext)

```python
from typing import List, Optional
from .model import Model

class ElkProtocol:
    """Model indirection + model-independent frame builders.

    Shared holder passed to BOTH transport and device. Constructed in the facade,
    which performs the pre-connect _detect_model() so entities can read
    model/model_name at construction (before any connect).
    """

    def __init__(self, hass, device_name_getter, forced_model: Optional[str] = None) -> None:
        # device_name_getter: Callable[[], str|None] -> transport/facade supplies
        # the current BLE device name lazily (name may only exist after _device
        # is set). Mirrors reads of self._device.name in _detect_model. The getter
        # MUST tolerate being called before _transport exists and return None for a
        # missing device (use getattr, not attribute access — LOW-7; see §3.5).
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
    def model(self) -> Model: return self._model
    @property
    def model_name(self) -> Optional[str]: return self._model_name
    @property
    def forced_model(self) -> Optional[str]: return self._forced_model

    def detect_model(self, char_handle: Optional[int] = None) -> None:
        """Was _detect_model (221-252). Populates self._model / self._model_name.
        forced_model wins; else handle-based when char_handle given; else name-based;
        fallback 'ELK-BLEDOM'."""
        ...

    def refine_by_handle(self, char_handle: int) -> None:
        """Transport calls this from _resolve_characteristics after finding the
        write handle. == detect_model(char_handle) (elkbledom.py:1016)."""
        self.detect_model(char_handle)

    # ---- UUID lookups the transport consumes (from _resolve_characteristics) ----
    def read_uuid(self) -> Optional[str]:  return self._model.get_read_uuid(self._model_name)   # 999
    def write_uuid(self) -> Optional[str]: return self._model.get_write_uuid(self._model_name)  # 1008/1018

    # ---- connection-family predicates (name-prefix logic; Phase 0 keeps the
    #      same startswith checks, centralized here so transport/device call these
    #      instead of hardcoding strings). Phase 1 folds all duplicated checks in. ----
    def requires_login(self, name: str) -> bool:      # 872
        n = (name or "").lower(); return n.startswith("melk") or n.startswith("modelx")
    def requires_read_uuid(self, name: str) -> bool:  # 1026-1029 (inverted)
        n = (name or "").lower(); return not (n.startswith("melk") or n.startswith("modelx"))
    def uses_notifications(self, name: str) -> bool:  # 957 / 1111
        n = (name or "").lower(); return not (n.startswith("melk") or n.startswith("ledble"))
    def mic_effect_extended(self, name: str) -> bool: # 661-662 (device uses this)
        n = (name or "").lower(); return n.startswith("melk") or n.startswith("modelx")

    def login_frames(self) -> tuple:      return (bytes([0x7e,0x07,0x83]), bytes([0x7e,0x04,0x04]))  # 896/898
    def login_step_delay(self) -> float:  return 1.0   # LOGIN_STEP_DELAY

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
```

Phase-0 note: `detect_model`/`refine_by_handle`/`read_uuid`/`write_uuid`/the
predicates and the eager model resolution are all Phase 0. The static frame
builders are physically added in Phase 0 (they are pure and free) but device
only *switches to calling them* in Phase 1; until then device keeps the inline
byte-lists (and its inline mic-name clamp — §8.8). Either ordering compiles — the
mandate is that both exist identically.

`sync_time`/`custom_time` single-home (MEDIUM-6): `Model.get_sync_time_cmd`/
`get_custom_time_cmd` (model.py:362-373) are model-independent already. Do **NOT**
make `Model` delegate to `ElkProtocol.sync_time` — `protocol.py` imports
`model.py`, so a back-import creates a `model ↔ protocol` cycle (§2.5). If a
single home is wanted in Phase 1, have **device** call the `@staticmethod`
`ElkProtocol.sync_time(...)` directly and leave `model.py`'s methods untouched
(or delete the now-unused `Model` copies **without** adding any import of
`protocol` into `model.py`). `models.json` and `model.py`'s import list stay
unchanged.

### 3.3 `transport.py` — `BLETransport` + module members

Module-level (moved verbatim from elkbledom.py):
`DEFAULT_ATTEMPTS=3` (31), `BLEAK_BACKOFF_TIME=0.25` (33), `COMMAND_GAP=0.15`
(40), `MIC_EXIT_SETTLE=0.3` (43, referenced by device — see note), `LOGIN_STEP_DELAY=1.0`
(47), `RETRY_BACKOFF_EXCEPTIONS=(BleakDBusError,)` (48),
`retry_bluetooth_connection_error` (51-88, annotation `"BLEDOMInstance"` →
`"ElkDevice"`), `class CharacteristicMissingError` (90-91).

`MIC_EXIT_SETTLE` is *used* by device (`_exit_mic_mode`, `disable_mic`); define
it in `transport.py` and import into `device.py`, or define in a shared
`const`-like spot. Keep one definition.

```python
class BLETransport:
    def __init__(self, address: str, hass, delay, *, protocol: "ElkProtocol") -> None:
        # Assign ONLY the transport fields enumerated in the "Transport fields"
        # paragraph below — do NOT sweep the 150-189 line range wholesale (P6): it
        # straddles ElkState (168-180), ElkProtocol _model/_model_name (178-179),
        # and ElkDevice _brightness_mode (191-193), which belong to other layers.
        # Ctor sequence, verbatim from the facade:
        # - self.loop = asyncio.get_running_loop()                 (141) MUST run on the loop
        # - fetch async_ble_device_from_address(..., connectable=True) -> self._device (196-199)
        # - scan async_discovered_service_info -> self._device_data (203-208)
        #   (constructs DeviceData, imported from .device_data — §2.5)
        # - raise ConfigEntryNotReady if not self._device          (210-211)
        # protocol supplies UUIDs + handle refinement; transport holds the ref (§2.4).
        ...

    # ---- identity / state (properties) ----
    @property
    def name(self) -> str: ...          # self._device.name                     (411-412)
    @property
    def address(self) -> str: ...       #                                       (382-383)
    @property
    def rssi(self) -> int: ...          # _rssi_latest or _device_data.rssi or 0 (414-418)
    @property
    def available(self) -> bool: ...    #                                       (284-287)
    @property
    def is_connected(self) -> bool: ... # bool(_client and _client.is_connected) — query_state guard (757)

    # ---- availability / callbacks ----
    def set_available(self, available: bool) -> None: ...  # was _set_available (289-295); PUBLIC now
    def fire_callbacks(self) -> None: ...                  # was _fire_callbacks (279-282); device calls on mic change
    def register_callback(self, cb) -> "Callable[[], None]": ...  # returns unregister (267-277)

    # ---- connection + writes (the whole ElkDevice -> BLE surface) ----
    async def connect(self) -> None: ...        # async with _operation_lock: await _ensure_connected()  (784-785)
    async def write_frame(self, data) -> None: ...    # == _write   (322-331); keeps `if not data: return`
    async def write_frames(self, *frames) -> None: ...# == _write_many (348-359); keeps falsy-item filter
    async def stop(self) -> None: ...                 # == stop (1058-1079)
    def refresh_device_data(self) -> None: ...        # self._device_data.update_device() (794-795), None-guarded

    # ---- @callback handlers (facade re-exports the bound methods to __init__.py) ----
    @callback
    def _async_update_ble(self, service_info, change) -> None: ...  # (297-307) two positional args
    @callback
    def _async_unavailable(self, service_info) -> None: ...          # (309-320) one positional arg; connected-guard

    # ---- private (move verbatim, rebind self.X, apply the §2.4 protocol hops) ----
    async def _ensure_connected(self) -> None: ...      # 822-966; uses _connect_lock; establish_connection(max_attempts=1);
                                                        # login via protocol.requires_login/login_frames/login_step_delay;
                                                        # notifications via protocol.uses_notifications
    def _resolve_characteristics(self, services) -> bool: ...  # 986-1031; read=protocol.read_uuid(), write=protocol.write_uuid();
                                                        # on handle: protocol.refine_by_handle(h) then re-read write_uuid;
                                                        # return via protocol.requires_read_uuid(name)
    def _notification_handler(self, sender, data) -> None: ...  # 969-984 log-only
    async def _write_while_connected(self, data) -> None: ...   # 333-346 pacing + set_available(True)
    def _reset_disconnect_timer(self) -> None: ...      # 1033-1042
    def _disconnected(self, client) -> None: ...        # 1044-1049
    def _disconnect(self) -> None: ...                  # 1051-1056 strong-ref task
    async def _execute_timed_disconnect(self) -> None: ...  # 1081-1088
    async def _execute_disconnect(self, timed: bool = False) -> None: ...  # 1090-1116 stale-check + op-lock
```

Transport fields (all verbatim from elkbledom.py, rebind only): `loop` (141),
`_address` (142), `_hass` (145), `_delay` (144), `_device` (150), `_device_data`
(151), `_connect_lock` (152), `_operation_lock` (157), `_client` (158),
`_last_write_at` (161), `_disconnect_timer` (162), `_disconnect_task` (165),
`_cached_services` (166), `_expected_disconnect` (167), `_read_uuid` (181),
`_write_uuid` (182), `_callbacks` (187), `_available` (188), `_rssi_latest`
(189). `reset`/`forced_model`/`brightness_mode`/`config_name` are **not**
transport params.

**`_set_available` → `set_available` rename checklist (HIGH-3).** The method
becomes a PUBLIC `set_available`, but the bodies that move into transport still
call the old private name internally — every site must be updated or it is a
runtime `AttributeError` (invisible to py_compile):
- `_write_while_connected` success path (346),
- `_ensure_connected` success path (952),
- `_async_update_ble` (307),
- `_async_unavailable` (320).
The `@retry` decorator's `self._set_available(False)` now binds to **ElkDevice**
(the decorator is applied there), which provides `_set_available` →
`transport.set_available` (§3.4). Safety net: keep a private alias on transport —
`_set_available = set_available` (class-body alias) — so both names resolve even
if a site is missed.

### 3.4 `device.py` — `ElkDevice`

```python
class ElkDevice:
    def __init__(self, transport: "BLETransport", protocol: "ElkProtocol",
                 state: "ElkState", brightness_mode: str = "auto") -> None:
        self.transport = transport
        self.protocol = protocol
        self.state = state
        mode = (brightness_mode or "auto").lower()
        self._brightness_mode = mode if mode in ("auto", "rgb", "native") else "auto"  # 192-193

    # ---- decorator-binding proxies (so the copied @retry body is byte-identical) ----
    @property
    def name(self): return self.transport.name                 # decorator logs use self.name (61,76,...)
    def _set_available(self, ok: bool) -> None:                # decorator flips availability (71,75,82)
        self.transport.set_available(ok)

    # ---- read-through state used by facade props ----
    @property
    def brightness_mode(self): return self._brightness_mode    # 406-408
    @property
    def min_color_temp_kelvin(self): return self.protocol.model.get_min_color_temp_kelvin(self.protocol.model_name)  # 432-434
    @property
    def max_color_temp_kelvin(self): return self.protocol.model.get_max_color_temp_kelvin(self.protocol.model_name)  # 436-438

    # ---- policy (no retry) ----
    async def apply_brightness_mode(self, mode: str) -> None: ...  # 254-262 (compare/set/log; no connect)

    # ---- mic-exit policy (invariant #7) ----
    async def _exit_mic_mode(self) -> None:
        if not self.state.mic_enabled:                          # 373
            return
        await self.transport.write_frame(ElkProtocol.mic_power(False))  # 376 (raw write; outer @retry covers it)
        self.state.mic_enabled = False                          # 377
        self.transport.fire_callbacks()                         # 378
        await asyncio.sleep(MIC_EXIT_SETTLE)                    # 379

    # ---- intents: EVERY method below carries @retry_bluetooth_connection_error,
    #      identical decoration to elkbledom.py. Bodies move verbatim; apply the
    #      EXPLICIT rebind partition below — NOT a blanket `self._X -> self.state.X`
    #      regex (P2), which would wrongly sweep _brightness_mode (device-local) and
    #      miss self._device.name. Name-by-name; anything not listed is a bug: ----
    #
    #    self.state.* (the 11 ElkState fields ONLY):
    #        is_on, rgb_color, rgb_color_base, brightness, effect, effect_speed,
    #        color_temp_kelvin, mic_effect, mic_sensitivity, mic_enabled, color_temp
    #    self._brightness_mode  -> self._brightness_mode   (device-local; NOT state — 575/259/408)
    #    self._model / self._model_name -> self.protocol.model / self.protocol.model_name
    #    self._device.name (set_mic_effect, 661) -> self.transport.name   (P2/MEDIUM-5)
    #    self._write            -> self.transport.write_frame
    #    self._write_many       -> self.transport.write_frames
    #    self._fire_callbacks   -> self.transport.fire_callbacks
    #    self._client / self._device / self._device_data -> self.transport.*
    #    ElkDevice owns NO lock — see the update()/query_state() structural note below (P6).
    async def set_color_temp(self, value: int) -> None: ...                     # 473-481
    async def set_color_temp_kelvin(self, value: int, brightness: int) -> None: ...  # 483-542
    async def set_color(self, rgb, is_base_color: bool = False) -> None: ...    # 544-558
    async def set_white(self, intensity: int) -> None: ...                     # 560-567
    async def set_brightness(self, intensity: int) -> None: ...                # 569-633 (owns auto/rgb/native policy)
    async def set_effect_speed(self, value: int) -> None: ...                  # 635-642
    async def set_effect(self, value: int) -> None: ...                        # 644-649
    async def set_mic_effect(self, value: int) -> None: ...                    # 651-668; name = self.transport.name (P2, NOT self._device.name).
    #   Phase 0: inline clamp verbatim — max_effect = 0x87 if self.transport.name.lower().startswith(("melk","modelx")) else 0x83
    #   Phase 1: value = protocol.mic_eq(value, extended=protocol.mic_effect_extended(self.transport.name))[3]  (byte-identical clamp)
    async def set_mic_sensitivity(self, value: int) -> None: ...              # 670-678 (NO callback fire)
    async def enable_mic(self) -> None: ...                                    # 680-686
    async def disable_mic(self) -> None: ...                                   # 688-700 (fire + sleep MIC_EXIT_SETTLE)
    async def turn_on(self) -> None: ...                                       # 702-706
    async def turn_off(self) -> None: ...                                      # 708-712
    async def set_scheduler_on(self, days, hours, minutes, enabled) -> None: ...   # 714-722
    async def set_scheduler_off(self, days, hours, minutes, enabled) -> None: ...  # 724-731
    async def sync_time(self) -> None: ...                                     # 733-748 (weekday isoweekday()%7)
    async def custom_time(self, hour, minute, second, day_of_week) -> None: ...# 750-753
    async def update(self) -> None: ...                                        # 771-820 (seed-before-connect; transport.connect; refresh_device_data)

    async def query_state(self) -> None: ...   # 755-769 — NOT @retry; guarded on transport.is_connected; via write_frame
    def get_color_base(self): return self.state.get_color_base()               # 264
```

Why `@retry` stays on device intents (invariant #4): `set_brightness` auto-mode
catches a failed native write and falls back to RGB *inside* the retried region
(619-625); the whole intent must be retryable as a unit. The decorator body is
byte-for-byte 51-88, now binding `self=ElkDevice` which exposes `.name` and
`._set_available`. Internal RGB-scaling writes in `set_brightness`/
`set_color_temp_kelvin` go through `transport.write_frame` **directly** (not
through the decorated `set_color`) precisely so retries don't nest (536-538,
586-594).

**`update()`/`query_state()` are structural moves, not "verbatim + rebind" (P6).**
`ElkDevice` declares **no** lock. `update()`'s `async with self._operation_lock:
await self._ensure_connected()` (784-785) collapses to a single
`await self.transport.connect()` (which takes transport's op-lock and connects as
one unit — invariant #1). Do **not** re-declare an `_operation_lock` on
`ElkDevice`: a second lock would not serialize against transport's writes/teardown
(silently voiding invariant #1). `query_state()`'s guard
`if not self._client or not self._client.is_connected:` (757) becomes
`if not self.transport.is_connected:` and its command still routes through
`self.transport.write_frame` (so the op-lock is held, matching every other write).
`update()`'s `self._device_data.update_device()` (794-795) maps to
`self.transport.refresh_device_data()` (None-guarded inside transport).

### 3.5 `elkbledom.py` — `BLEDOMInstance` facade (+ `DeviceData`)

```python
class BLEDOMInstance:
    def __init__(self, address, reset: bool, delay: int, hass, forced_model: str = None,
                 brightness_mode: str = "auto", config_name: str = None) -> None:
        # Signature IDENTICAL to elkbledom.py:140. Two positional call sites (§4.1).
        self._address = address
        self._reset = reset
        self._delay = delay
        self._hass = hass
        self._forced_model = forced_model
        self._config_name = config_name
        # Build the stack (order matters — §8.1):
        # getattr, not `self._transport.name` — _transport is assigned two lines
        # later, so an early call (or a future reorder) must degrade to None, not
        # AttributeError (LOW-7). The getter is only actually invoked at detect_model().
        self._protocol  = ElkProtocol(hass,
                                      device_name_getter=lambda: getattr(getattr(self, "_transport", None), "name", None),
                                      forced_model=forced_model)
        self._state     = ElkState()
        self._transport = BLETransport(address, hass, delay, protocol=self._protocol)  # raises ConfigEntryNotReady (210-211)
        self._protocol.detect_model()   # eager pre-connect resolution (214) — entities read model/model_name now
        self._device    = ElkDevice(self._transport, self._protocol, self._state, brightness_mode)

    # ---- read properties: straight delegation (§4.2) ----
    @property
    def address(self):      return self._address
    @property
    def reset(self):        return self._reset
    @property
    def delay(self):        return self._delay
    @property
    def forced_model(self): return self._forced_model
    @property
    def config_name(self):  return self._config_name or self.name          # 397-404
    @property
    def name(self):         return self._transport.name
    @property
    def available(self):    return self._transport.available
    @property
    def rssi(self):         return self._transport.rssi
    @property
    def brightness_mode(self): return self._device.brightness_mode
    @property
    def model(self):        return self._protocol.model
    @property
    def model_name(self):   return self._protocol.model_name
    @property
    def min_color_temp_kelvin(self): return self._device.min_color_temp_kelvin
    @property
    def max_color_temp_kelvin(self): return self._device.max_color_temp_kelvin
    # ---- public read getters backed by ElkState — define EACH literally; do NOT
    #      collapse to a comment (MEDIUM-4). Entities read every one (§4.2). ----
    @property
    def is_on(self):             return self._state.is_on             # light.py:77; config_flow.py:269
    @property
    def rgb_color(self):         return self._state.rgb_color         # light.py:121,122
    @property
    def brightness(self):        return self._state.brightness        # light.py:73
    @property
    def color_temp_kelvin(self): return self._state.color_temp_kelvin # light.py:81
    @property
    def effect(self):            return self._state.effect            # keep-alive (§4.2)
    @property
    def effect_speed(self):      return self._state.effect_speed      # light.py:116; number.py:44
    @property
    def mic_effect(self):        return self._state.mic_effect        # keep-alive (§4.2)
    @property
    def mic_sensitivity(self):   return self._state.mic_sensitivity   # keep-alive (§4.2)
    @property
    def mic_enabled(self):       return self._state.mic_enabled       # switch.py:55

    # ---- async intents: straight delegation ----
    async def set_brightness(self, i): return await self._device.set_brightness(i)
    # ... all 19 async methods delegate to self._device (apply_brightness_mode,
    #     set_color, set_color_temp_kelvin, set_white, set_effect, set_effect_speed,
    #     set_mic_effect, set_mic_sensitivity, enable_mic, disable_mic, turn_on,
    #     turn_off, set_scheduler_on/off, sync_time, update, set_color_temp,
    #     custom_time, query_state). get_color_base -> self._device.get_color_base().
    async def stop(self):              return await self._transport.stop()

    # ---- callback registry + the two HA @callback shims ----
    def register_callback(self, cb):   return self._transport.register_callback(cb)
    @callback
    def _async_update_ble(self, si, change): self._transport._async_update_ble(si, change)
    @callback
    def _async_unavailable(self, si):        self._transport._async_unavailable(si)

    # ---- the 8 externally-written privates: data-descriptor property+setter into ElkState ----
    @property
    def _is_on(self):        return self._state.is_on
    @_is_on.setter
    def _is_on(self, v):     self._state.is_on = v
    @property
    def _brightness(self):   return self._state.brightness
    @_brightness.setter
    def _brightness(self, v): self._state.brightness = v
    @property
    def _rgb_color(self):    return self._state.rgb_color
    @_rgb_color.setter
    def _rgb_color(self, v): self._state.rgb_color = v
    @property
    def _rgb_color_base(self):    return self._state.rgb_color_base
    @_rgb_color_base.setter
    def _rgb_color_base(self, v): self._state.rgb_color_base = v
    @property
    def _color_temp_kelvin(self):    return self._state.color_temp_kelvin
    @_color_temp_kelvin.setter
    def _color_temp_kelvin(self, v): self._state.color_temp_kelvin = v
    @property
    def _effect(self):    return self._state.effect
    @_effect.setter
    def _effect(self, v): self._state.effect = v
    @property
    def _effect_speed(self):    return self._state.effect_speed
    @_effect_speed.setter
    def _effect_speed(self, v): self._state.effect_speed = v
    @property
    def _mic_enabled(self):    return self._state.mic_enabled
    @_mic_enabled.setter
    def _mic_enabled(self, v): self._state.mic_enabled = v
```

Why the leading-underscore property/setters are safe and correct: a `property`
is a *data descriptor* (defines both `__get__` and `__set__`), so it takes
precedence over the instance `__dict__` for **both** read and write. External
`instance._is_on = True` (light.py:160) routes through the setter into the same
`ElkState` the device reads — no divergence. `light.py:186`
(`_rgb_color_base = _rgb_color`) works because the getter returns
`state.rgb_color` and the setter stores into `state.rgb_color_base`. Requirement:
`__init__` must build `self._state`/`self._device` **before** any external code
assigns these, and must never store any of the 8 as plain instance attributes
(§8.1).

`_async_update_ble`/`_async_unavailable` are `@callback`-decorated methods on the
facade (not lazily-created delegates) because `__init__.py:129,137` capture the
bound reference at setup time and store it for `async_on_unload` teardown; the
`@callback` marker must survive on the facade object.

`DeviceData` (elkbledom.py:92-137) **moves to its own `device_data.py`** in
Phase 0 (sub-step 0b) to break the transport↔facade import cycle (§2.5), and is
**re-exported** from `elkbledom.py` (`from .device_data import DeviceData`) so
`config_flow.py:3` (`from .elkbledom import DeviceData`) keeps resolving
unchanged. Its class body is otherwise untouched until Phase 2 (§8.9).

### 3.6 Module import blocks (enumerate — `py_compile` can't catch a missing one)

`python -m py_compile` never resolves names, so a missing module-level import is
an invisible `NameError` at first use (HIGH-2/P3). Each new module's import block
is part of the contract.

**`state.py`**
```python
from dataclasses import dataclass
from typing import Optional, Tuple
```

**`device_data.py`** (moved verbatim with `DeviceData`, §2.5)
```python
import logging
from bleak.backends.device import BLEDevice
from home_assistant_bluetooth import BluetoothServiceInfo
from homeassistant.components.bluetooth import (
    async_discovered_service_info, async_ble_device_from_address,
)
from .model import Model
LOGGER = logging.getLogger(__name__)
```

**`protocol.py`**
```python
import logging
from typing import List, Optional
from .model import Model
LOGGER = logging.getLogger(__name__)          # detect_model logs (221-252)
```

**`transport.py`**
```python
import asyncio, logging
from typing import Any, TypeVar, cast, Optional, List
from collections.abc import Callable
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakDBusError
from bleak_retry_connector import (
    BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS,
    BleakClientWithServiceCache, BleakNotFoundError, establish_connection,
)
from homeassistant.components.bluetooth import (
    async_ble_device_from_address, BluetoothServiceInfoBleak, BluetoothChange,
)
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady
from .device_data import DeviceData            # runtime use in ctor scan (§2.5)
LOGGER = logging.getLogger(__name__)
# `protocol: "ElkProtocol"` is a STRING annotation only — do NOT import
# protocol.py here (transport duck-types the instance; keeps the graph acyclic, §2.5).
```

**`device.py`** (HIGH-2/P3 — the ones the reviews flagged as silently missing)
```python
import asyncio, datetime, traceback, logging
from typing import Tuple
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS  # update() except clause (812)
from .transport import retry_bluetooth_connection_error, MIC_EXIT_SETTLE
from .protocol import ElkProtocol             # static frame builders (used Phase 1; harmless import in Phase 0)
from .state import ElkState                   # type hint only
LOGGER = logging.getLogger(__name__)
```
`update()` (771-820) references `BLEAK_EXCEPTIONS` (812) + `traceback` (819);
`sync_time()` references `datetime` (735/740); every mic/backoff sleep references
`asyncio`; all logs reference `LOGGER`. The `@retry` decorator body binds
`BLEAK_EXCEPTIONS`/`BleakNotFoundError`/`RETRY_BACKOFF_EXCEPTIONS`/`asyncio` in
**transport's** namespace (where the decorator is *defined*), so device does not
re-import those for the decorator — only `BLEAK_EXCEPTIONS` for its own inline
`update()` except clause.

---

## 4. Compatibility contract (the facade must preserve all of this)

Everything outside `elkbledom.py` touches `BLEDOMInstance` only through the
members enumerated here. Nothing else references transport internals (`_write`,
`_ensure_connected`, `_client`, `_operation_lock`, … are confined to
`elkbledom.py` — verified). This is the complete Phase-0 surface.

### 4.1 Constructor (signature + defaults frozen)

```python
BLEDOMInstance(address, reset: bool, delay: int, hass,
               forced_model: str = None, brightness_mode: str = "auto",
               config_name: str = None)
```

Two positional call sites — the parameter order and defaults are pinned by both:
- `__init__.py:120` — 7 args:
  `BLEDOMInstance(entry.data[CONF_MAC], reset, delay, hass, forced_model, brightness_mode, entry.data.get("name"))`
- `config_flow.py:265` — 5 args:
  `BLEDOMInstance(self.mac, False, 120, self.hass, self._model_name)` — relies on
  `brightness_mode="auto"` and `config_name=None` defaults.

Construction-time contract that must hold:
- Raise `ConfigEntryNotReady` when no connectable device is found (210-211).
- `_detect_model()` runs **synchronously at construction** (214); `model` and
  `model_name` are populated before any connect. Callers guarantee models are
  loaded via `ensure_models_loaded()` (`__init__.py:118`, `config_flow.py`
  paths). **Model resolution must stay eager** — entities probe capabilities in
  their `__init__` (see §4.4).
- `asyncio.get_running_loop()` is grabbed at construction (141) → the transport
  must be built on the event loop; keep facade `__init__` synchronous and
  constructing the transport inline (§8.1).

### 4.2 Read properties required externally (17 load-bearing + keep-alive extras)

| Property | Backed by | External readers |
|---|---|---|
| `available` | transport | entity.py:45 |
| `address` | facade field | __init__.py:130,136; entity.py:51,53; light.py:68; number.py:38,86; select.py:34,82,102; switch.py:49 |
| `reset` | facade field | light.py:258; __init__.py:306 |
| `delay` | facade field | __init__.py:307 |
| `forced_model` | facade field | __init__.py:308 |
| `config_name` | facade | entity.py:52 |
| `name` | transport | select.py:48 (+ logging) |
| `is_on` | state.is_on | light.py:77; config_flow.py:269 |
| `rgb_color` | state.rgb_color | light.py:121,122 |
| `brightness` | state.brightness | light.py:73 |
| `min_color_temp_kelvin` | device→protocol/Model | light.py:89,327 |
| `max_color_temp_kelvin` | device→protocol/Model | light.py:85,327 |
| `color_temp_kelvin` | state.color_temp_kelvin | light.py:81 |
| `effect_speed` | state.effect_speed | light.py:116,346,347; number.py:44,45 |
| `mic_enabled` | state.mic_enabled | switch.py:55 |
| `model_name` | protocol | light.py:48-52,104,221,341; number.py:28,90; select.py:37; switch.py:51 |
| `model` | protocol | light.py:48-52,104,221,341; number.py:28,90; select.py:37; switch.py:51 |

Keep-alive (no current external reader, expose anyway — zero risk, avoids silent
behavior loss): `brightness_mode` (406), `rssi` (414), `effect` (445),
`mic_effect` (453), `mic_sensitivity` (457).

### 4.3 Async methods required externally (19) + `@callback` (2)

`apply_brightness_mode` (__init__.py:300; select.py:111,129),
`register_callback` (entity.py:34 — **must return an unregister callable**;
entity passes it straight to `async_on_remove`), `set_color_temp_kelvin`
(light.py:288,330), `set_color` (light.py:261,296,309), `set_white`
(light.py:297,332), `set_brightness` (light.py:316,334), `set_effect_speed`
(light.py:347; number.py:64), `set_effect` (light.py:344), `set_mic_effect`
(select.py:57), `set_mic_sensitivity` (number.py:110), `enable_mic`
(switch.py:59), `disable_mic` (switch.py:64), `turn_on` (light.py:257;
config_flow.py:272,276), `turn_off` (light.py:353; config_flow.py:270,274,278),
`set_scheduler_on` (__init__.py:242), `set_scheduler_off` (__init__.py:262),
`sync_time` (__init__.py:223), `update` (light.py:357; config_flow.py:267),
`stop` (__init__.py:145,284; config_flow.py:284).

Keep (public today, no external caller): `set_color_temp` (percent variant, 473),
`custom_time` (750), `query_state` (755), `get_color_base` (264).

`@callback` methods (2), bound-method references passed to HA at setup:
- `_async_update_ble(service_info, change)` — `__init__.py:129`, two positional
  args, `@callback`.
- `_async_unavailable(service_info)` — `__init__.py:136`, one positional arg,
  `@callback`.
Both must remain accessible as bound `@callback` attributes on the facade
object (not lazy delegates) so `async_on_unload` teardown captures a stable
reference.

### 4.4 Capability probes at entity `__init__` (why model resolution must stay eager)

Entities probe these on `model`/`model_name` **before any connect**, at their own
`__init__`, to decide `supported_color_modes`, `LightEntityFeature.EFFECT`,
whether the Effect-Speed number entity exists (number.py:28), and mic-entity
`entity_registry_enabled_default`: `model.get_white_cmd`, `get_color_temp_cmd`,
`get_color_cmd`, `get_effect_cmd`, `get_effect_speed_cmd`, `get_supports_mic`,
`get_effects_class`, `get_effects_list` (confirmed: light.py:221 reads
`model.get_effects_class(model_name)`). If model resolution moves into the
connect path, these break — the facade must resolve the model synchronously in
`__init__` (§4.1).

### 4.5 Externally-mutated private attributes (8) — the crux

Written directly by the restore paths; the facade must keep them assignable
(data-descriptor property+setter into `ElkState`, §3.5). Two are read back
immediately.

From `light.py async_added_to_hass`:
| Attr | Writers (light.py) | Notes |
|---|---|---|
| `_is_on` | 160,163,168,237 | True/False on restore; default False |
| `_brightness` | 175,238 | guarded vs None; default 255 |
| `_rgb_color` | 181,186 | from tuple; **read back** at 186 |
| `_rgb_color_base` | 186 | set `= _rgb_color` — coupling must hold |
| `_color_temp_kelvin` | 198 | |
| `_effect` | 224 | effects-enum `.value` |
| `_effect_speed` | 230 | int |

From `switch.py async_added_to_hass`:
| Attr | Writer | Notes |
|---|---|---|
| `_mic_enabled` | 75 | seeds mic-on so next normal command emits explicit mic-off |

The `_rgb_color`/`_rgb_color_base` coupling (invariant #8): restore sets
base = current color (light.py:186) so the first post-restart brightness change
scales the *restored* color, not default white. The same coupling appears
internally in `set_color` (555-558) and `set_brightness` (579). The 8 names must
remain assignable on the facade object itself.

### 4.6 `__init__` callback-registration + options-listener contract

- `bluetooth.async_register_callback(hass, instance._async_update_ble,
  BluetoothCallbackMatcher(address=instance.address, connectable=True), PASSIVE)`
  (__init__.py:126-133).
- `bluetooth.async_track_unavailable(hass, instance._async_unavailable,
  instance.address, connectable=True)` (__init__.py:134-138).
- `instance.address` read for both matcher + tracker — available immediately
  post-construction.
- HA-stop and unload both `await instance.stop()` (145,284).
- Options listener `_async_update_listener` reads `instance.reset`,
  `instance.delay`, `instance.forced_model`, and calls
  `await instance.apply_brightness_mode(...)` — none may trigger a reconnect
  (`apply_brightness_mode` only mutates the mode, 254-262).

### 4.7 Module-level import

`from .elkbledom import DeviceData` (`config_flow.py:3`) — `DeviceData` moves to
`device_data.py` in Phase 0 (§2.5) and is **re-exported** from `elkbledom.py`
(`from .device_data import DeviceData`), so this import keeps resolving unchanged
through Phase 1. Its class body is untouched until Phase 2 (§8.9).

---

## 5. Invariant-preservation checklist

Each of the 10 required invariants, mapped to where it lives in the new design.

**1. `_operation_lock` serializes connect+write as one unit; idle-disconnect
can't run between `_ensure_connected` and the write; per-device.**
→ **transport.** `write_frame` (329-331), `write_frames` (356-359), `connect`
(784-785), and `_execute_disconnect` (1094) all take `self._operation_lock`;
`_ensure_connected` takes only `_connect_lock` (833). Lock objects are
per-`BLETransport` (one per device), moved verbatim; device never touches the
lock. Because `_execute_disconnect` also takes the op-lock, teardown can never
null `_client` mid-write.

**2. `COMMAND_GAP` min-spacing between consecutive writes; `MIC_EXIT_SETTLE`
after mic-off.**
→ **transport** for `COMMAND_GAP` (min-gap in `_write_while_connected` 337-340,
only delays bunched writes; `_last_write_at` stamped after write 343).
→ **device** for `MIC_EXIT_SETTLE` (`_exit_mic_mode` 379, `disable_mic` 699 —
mic-exit is a device policy; the settle sleep is issued by the caller after the
`write_frame(mic_off)`, exactly mirroring 376→379). Constants live in
`transport.py`.

**3. Idle-disconnect timer + timed-disconnect stale-check; `stop()` cancels
timer+task with self-await guard.**
→ **transport** verbatim: `_reset_disconnect_timer` (1033-1042), `_disconnect`
strong-ref task (1051-1056), `_execute_timed_disconnect` (1081-1088),
`_execute_disconnect` stale-check `if timed and self._disconnect_timer is not
None: return` (1099-1101), `stop` self-await guard
`task is not asyncio.current_task()` (1071). Facade `stop()` delegates.

**4. `@retry`: 3 attempts, `BleakNotFoundError` not retried, backoff; on final
failure `_set_available(False)`.**
→ **decorator defined in transport.py, applied on ElkDevice intents.** Body is
byte-for-byte 51-88 (only annotation `"BLEDOMInstance"`→`"ElkDevice"`). It reads
`self.name` and calls `self._set_available(False)`; `ElkDevice` provides
`name`→`transport.name` and `_set_available`→`transport.set_available`. Retry
still wraps the *whole* intent (incl. `_exit_mic_mode`, multi-writes, brightness
auto-fallback, cache-after-write) — not a single GATT write.

**5. Availability: `_set_available` fires callbacks only on change; advertisement
→ available; `track_unavailable`(not connected) → unavailable; write/connect →
available; connected-guard.**
→ **transport.** `set_available` change-only (289-295); `_async_update_ble` sets
`_device`/`_rssi_latest` + `set_available(True)` (305-307); `_async_unavailable`
returns early if `_client and _client.is_connected` else `set_available(False)`
(318-320); `_write_while_connected` `set_available(True)` (346); success in
`_ensure_connected` `set_available(True)` (952). Registry (`_callbacks`) lives in
transport; device mic-changes route through `transport.fire_callbacks()` so both
sources fire the same subscriber list.

**6. Fresh connectable device via advertisement callback + `ble_device_callback`
re-query; `establish_connection(max_attempts=1)`.**
→ **transport,** `_ensure_connected` verbatim (841-860): `_async_update_ble`
refreshes `_device` (305); `ble_device_callback=lambda: async_ble_device_from_
address(hass, address, connectable=True) or self._device` (851-854);
`max_attempts=1` (859, outer `@retry` owns the budget); `TimeoutError` re-raised
not swallowed (861-866).

**7. Mic-exit-before-normal-command; `_fire_callbacks` on every mic-state change;
mic EQ clamp (DOM 0x80-0x83, MELK 0x80-0x87).**
→ **device + protocol.** `_exit_mic_mode` on device (361-379), called at the top
of `set_color`, `set_color_temp_kelvin`, `set_white`, `set_brightness`,
`set_effect` — exactly as today (and deliberately *not* by `set_effect_speed`,
schedulers, sync_time, turn_on/off). `fire_callbacks()` on **every** mic
transition: `set_mic_effect` (667), `enable_mic` (685), `disable_mic` (698),
`_exit_mic_mode` (378). Clamp lives in `ElkProtocol.mic_eq(value, extended=…)`
(0x80-0x83 / 0x80-0x87), `extended` computed by device via
`protocol.mic_effect_extended(name)` (661-662). Switch relies on
`instance.mic_enabled` as the single source of truth (switch.py:29-41,55).

**8. Brightness modes auto/rgb/native + exact scaling/fallback; cache-after-
successful-write ordering; `_rgb_color_base` vs `_rgb_color` coupling.**
→ **device + state.** `set_brightness` whole body moves verbatim (569-633):
always scales from `state.rgb_color_base` (579), `has_rgb` probe (584),
`write_rgb_scaled`/`write_native` inner fns, auto-mode `except Exception` that
re-raises only if `not has_rgb` (619-625), and `state.brightness = value` only
after success (633). `ElkState.apply_color`/`apply_scaled_color` express the
coupling; the color-temp RGB fallback uses raw field writes (539-540, §8.3).

**9. Per-model frames via `Model` + effect/brightness value substitution +
`_scale_intensity`.**
→ **protocol + Model (unchanged).** `model.py`/`models.json` untouched. Device
calls `self.protocol.model.get_*_cmd(self.protocol.model_name, …)`. The
`model_name` + handle refinement live in `ElkProtocol` (`detect_model`/
`refine_by_handle`), and the transport→protocol resolve hop (§2.4) preserves the
1014-1020 sequence exactly.

**10. Full public surface (methods + properties) + externally-written private
attrs + the `__init__` callback hooks.**
→ **facade.** All delegations (§3.5, §4.2-4.4), the 8 data-descriptor property/
setter pairs into `ElkState` (§4.5), and the 2 `@callback` shims re-exporting
`transport._async_update_ble`/`_async_unavailable` (§4.6). `register_callback`
forwards to transport and returns the unregister closure. `DeviceData` stays
exported (§4.7).

---

## 6. Phase plan

Order/risk: **0 (REQUIRED)** → 1 (low) → 2 (medium) → 3 (higher) → 4 (last).
Every phase (and every Phase-0 sub-step) is independently `py_compile`-clean.
The instruction is to implement ALL phases.

### Phase 0 — REQUIRED: extract layers behind the facade

Entities/`__init__`/`config_flow` are **not edited**. Done in 4 compile-checked
sub-steps for reviewable diffs:

- **0a** — add `state.py` (`ElkState`) + `protocol.py` (`ElkProtocol`, move
  `_detect_model`→`detect_model`, add UUID lookups + predicates + the static
  frame builders). Facade still god-object but now delegates model+state to
  them. Compile.
- **0b** — add `device_data.py` (move `DeviceData` verbatim out of `elkbledom.py`
  to break the transport↔facade import cycle — §2.5; `elkbledom.py` re-exports it
  so `config_flow.py:3` is untouched). Add `transport.py`; move connection/write/
  availability/login/char-resolution + the `@retry` decorator +
  `CharacteristicMissingError` + constants; transport imports `DeviceData` from
  `device_data.py`. Apply the `_set_available`→`set_available` rename at all 4
  internal call sites (§3.3 rename checklist). Facade holds a `BLETransport`; its
  `_write*`/`_ensure_connected`/disconnect methods become one-liners into
  transport. Wire the transport→protocol resolve hop (§2.4). Compile **and run the
  import smoke test** (checklist item 5) — this is the sub-step where the cycle
  would appear.
- **0c** — add `device.py`; move all intents (`set_*`/`turn_*`/scheduler/
  `sync_time`/`custom_time`/`update`/`query_state`/`apply_brightness_mode`/
  `_exit_mic_mode`). Facade delegates intents to `ElkDevice`. Compile.
- **0d** — collapse the facade to pure delegation + the 8 property/setter pairs
  + the 2 `@callback` shims; delete dead god-object members. `DeviceData` stays.
  Compile.

**Per-sub-step verification checklist:**
1. `python -m py_compile custom_components/elkbledom/*.py` = OK.
2. Surface-parity: every name in §4.2/§4.3 and every attr in §4.5 resolves on
   `BLEDOMInstance` (grep the §4 lists against the facade).
3. Invariant audit against §5.
4. Confirm **no** entity/`__init__`/`config_flow`/`entity.py` file changed
   (`git diff --name-only` shows only `elkbledom.py` + the 5 new files:
   `state.py`, `protocol.py`, `device_data.py`, `transport.py`, `device.py`).
5. **Import-resolution smoke test (catches what py_compile cannot — P4).** py_compile
   executes no imports, so the P1 cycle and the P3 missing-import `NameError`s are
   invisible to it. Inject `types.ModuleType` stubs into `sys.modules` for the
   absent deps (`homeassistant` + `.core`/`.exceptions`/`.components.bluetooth`,
   `bleak`/`bleak.*`, `bleak_retry_connector`, `home_assistant_bluetooth`), each
   carrying the few referenced names, then `importlib.import_module` each of the 5
   new files **and** `elkbledom`. This actually runs the module bodies, surfacing
   any cycle/`NameError` offline. Keep the stub script under `docs/` or `scripts/`
   (never shipped in the package).
6. **Residual-reference grep (catches a half-migrated donor — P4).** After a
   sub-step relocates a field/method set, grep the DONOR (`elkbledom.py`) for the
   moved names and assert zero references outside the delegation shims:
   after 0b — no `self._client`/`self._operation_lock`/`self._connect_lock`/
   `self._last_write_at`/`self._ensure_connected`/`self._write_while_connected` in
   the facade; after 0c — no `self._model`/`self._rgb_color_base`/
   `self._brightness_mode`/`self._exit_mic_mode` outside facade property bodies.

### Phase 1 — OPTIONAL: consolidate hardcoded frames into `ElkProtocol`

Switch device off the inline byte-lists onto the `ElkProtocol` builders already
added in Phase 0 (`mic_power` 376/683/696, `mic_eq` 664, `mic_sensitivity` 676,
`scheduler` 722/731, `sync_time` — device calls the `@staticmethod`
`ElkProtocol.sync_time` directly; `login_frames` 896/898 is transport-side). Fold
device's ONE remaining inline name check — the `set_mic_effect` mic clamp
(661-662) — into `protocol.mic_eq(value, extended=protocol.mic_effect_extended(name))`.
Transport already routes its login/read-uuid/notification name checks through the
`ElkProtocol` predicates from Phase 0 (§8.8), so no transport name-string work
remains. **Do NOT** make `model.py` delegate to `protocol.py` (import cycle —
§2.5/§3.2/MEDIUM-6). Touches `device.py`+`protocol.py` (and `transport.py` only if
a residual inline string is found). Low risk (pure relocation). **Verification:**
compile + import smoke; assert each builder's emitted byte-list is identical to
the original literal frame (unit-compare); §5 re-audit of invariant #7 clamp; no
entity/config file changed.

### Phase 2 — OPTIONAL: `DeviceData` cleanup

`DeviceData` (now in `device_data.py` after Phase 0, §2.5) duplicates
address/rssi/model-detect and does a blocking `async_ble_device_from_address` in
`__init__` (102). Fold its rssi role into `BLETransport` and its config-flow
"is_supported" role into a small `protocol.is_supported(name)` (or keep the thin
`DeviceData` shim in `device_data.py` delegating to protocol). The
`from .elkbledom import DeviceData` re-export already resolves; repoint
`config_flow.py:3` to `from .device_data import DeviceData` **only if** the shim
is removed. Medium risk (touches setup/discovery UX). **Verification:** compile +
import smoke; config-flow discovery path still imports a working symbol;
`is_supported` returns the same boolean as `Model.detect_model(name) is not None`
(96-97); manual review of the discovery step (untestable without HA — §8).

### Phase 3 — OPTIONAL: entities talk to `ElkDevice` directly

Store the facade in `hass.data[DOMAIN][entry_id]` but expose `.device`; migrate
`light.py`/`number.py`/`select.py`/`switch.py` to call `instance.device.*` and
to use `ElkState.restore()` instead of poking `_is_on`/`_mic_enabled` etc. Once
every consumer is migrated, delete the 8 property/setter shims from the facade.
Higher risk: touches every entity + `__init__.py`. Do only after Phase 0 is
proven. **Verification:** compile all; grep confirms no remaining
`_instance._is_on`-style private pokes; restore logic (light.py:159-238,
switch.py:74-76) reproduced via `state.restore()` with the same None-guards and
base-coupling; §5 invariant #8/#10 re-audit; per-entity manual review.

### Phase 4 — OPTIONAL (last / likely skip): `DataUpdateCoordinator`

These strips have **no readback**: `_notification_handler` only logs echoes
(969-984) and `update()` seeds state without reading (778-782). A coordinator
adds little and risks re-introducing polling that `_attr_should_poll=False` /
`PARALLEL_UPDATES=0` deliberately avoid (entity.py:27, light.py:27).
**Recommendation:** defer/skip unless a real status frame is reverse-engineered.
If implemented, it orchestrates only availability + optimistic-state push (not
polling): a `DataUpdateCoordinator` whose `_async_update_data` returns the
`ElkState` snapshot and whose availability mirrors `transport.available`, with
entities becoming `CoordinatorEntity`. **Verification:** compile; confirm no new
BLE traffic is introduced (no periodic connect); entities still push on mic/
availability change; §5 full re-audit.

---

## 7. File layout

New files (all in `custom_components/elkbledom/`):

| File | Contents |
|---|---|
| `state.py` | `ElkState` dataclass (11 fields, defaults per 168-180) + `apply_color`/`apply_scaled_color`/`get_color_base`/`restore` |
| `device_data.py` | `DeviceData` (moved verbatim from elkbledom.py:92-137, §2.5) — breaks the transport↔facade import cycle. Imports `Model`; `elkbledom.py` re-exports the class. Body untouched until Phase 2. |
| `protocol.py` | `ElkProtocol` (== ModelContext): `Model` holder + `model`/`model_name`/`forced_model`, `detect_model`/`refine_by_handle`, `read_uuid`/`write_uuid` **(model-table lookups only — holds NO resolved GATT UUIDs, P5)**, connection-family predicates, `login_frames`/`login_step_delay`, and static frame builders (mic ×3, scheduler, sync_time, login) |
| `transport.py` | Module constants (`DEFAULT_ATTEMPTS`, `BLEAK_BACKOFF_TIME`, `COMMAND_GAP`, `MIC_EXIT_SETTLE`, `LOGIN_STEP_DELAY`, `RETRY_BACKOFF_EXCEPTIONS`), `retry_bluetooth_connection_error`, `CharacteristicMissingError`, `BLETransport` |
| `device.py` | `ElkDevice`: all intents (`@retry`-decorated) + `apply_brightness_mode` + `_exit_mic_mode` + `query_state` + decorator-binding proxies (`name`, `_set_available`) + color-temp-kelvin props |

Edited files:

| File | Phase | Change |
|---|---|---|
| `elkbledom.py` | 0 | Becomes the thin `BLEDOMInstance` facade + a `from .device_data import DeviceData` re-export (§2.5). God-object body removed. |
| `model.py` | 1 | **Unchanged** — do NOT delegate `get_sync_time_cmd`/`get_custom_time_cmd` to `ElkProtocol` (import cycle, MEDIUM-6/§2.5). If a single home is wanted, device calls `ElkProtocol.sync_time` directly. `models.json` untouched. |
| `config_flow.py` | 2 | Only if the `DeviceData` shim is removed: repoint `from .elkbledom import DeviceData` → `from .device_data import DeviceData`. The re-export means Phase 0/1 need no edit here. |
| `__init__.py` | 3 | Store facade but migrate entity access to `.device`; unchanged in Phase 0. |
| `light.py`, `number.py`, `select.py`, `switch.py` | 3 | Call `instance.device.*`; use `ElkState.restore()`. Unchanged in Phase 0. |
| `entity.py` | 3 | `register_callback`/`available`/`address` via `.device`/transport if desired. Unchanged in Phase 0. |

**No file other than `elkbledom.py` (+ the 5 new files: `state.py`, `protocol.py`,
`device_data.py`, `transport.py`, `device.py`) changes in Phase 0.**

---

## 8. Risks & untestable-without-HA caveats

### 8.1 Facade construction ordering (data-descriptor safety)
The facade `__init__` MUST build `protocol → state → transport → device` **before**
any external code assigns the 8 privates, and MUST never store any of the 8 as a
plain instance attribute (that would shadow the data descriptor and desync from
`ElkState`). `asyncio.get_running_loop()` is captured in the transport ctor (141)
so the facade ctor must stay synchronous and construct the transport inline on the
loop, exactly as today. Both call sites are async contexts.

### 8.2 `effect_speed` default is 50, not 0
`ElkState.effect_speed` defaults to 50 (elkbledom.py:173). `number.py` keeps a
local `_effect_speed=0` but reads `instance.effect_speed` first (number.py:44-46).
Do **not** "normalize" the state default to 0.

### 8.3 Color-temp RGB fallback decouples base/rgb
`set_color_temp_kelvin`'s RGB emulation sets `rgb_color`=scaled and
`rgb_color_base`=unscaled interpolated white (539-540) — the two diverge.
`apply_color` cannot express this; use raw `state.rgb_color = …` /
`state.rgb_color_base = …` writes there. Routing it through the coupling helper
is a regression.

### 8.4 `set_brightness` auto-fallback catches generic `Exception`
The auto-mode native→RGB fallback catches `except Exception` (621), not just
`BLEAK_EXCEPTIONS`, and re-raises only when `not has_rgb` (622-623). Preserve the
exact except type; narrowing it stops a transient error from falling back.

### 8.5 `MIC_EXIT_SETTLE` / raw writes bypass the decorated setters
`_exit_mic_mode` and `set_brightness`/`set_color_temp_kelvin` internal writes go
through `transport.write_frame` **directly** (not the decorated `set_color`) so
retries don't nest (invariant #4). Keep them on `write_frame`, and keep the
`MIC_EXIT_SETTLE` sleeps on the device side (376→379, 699).

### 8.6 The transport↔protocol resolve hop is the only bidirectional edge
`_resolve_characteristics` mutates model state via `protocol.refine_by_handle`
then re-reads `protocol.write_uuid()` (1014-1020). If this hop is dropped, a
handle-refined model silently keeps the wrong write UUID. Login (889) also reads
`protocol.write_uuid()`. Verify this seam explicitly in the 0b review.

### 8.7 No HA runtime — the correctness gate is static
`homeassistant`/`bleak` are not importable here, so there is no runtime import
check or integration test. Correctness rests on:
(a) `python -m py_compile custom_components/elkbledom/*.py` after every sub-step;
(b) the §4 surface-parity grep (every public name + the 8 privates + the 2
`@callback`s resolve on the facade);
(c) the §5 invariant audit;
(d) byte-compare of any relocated frame builder against its literal source;
(e) the offline **import-resolution smoke test** (§6 checklist item 5) — the ONLY
gate that catches the P1 import cycle and the P3 missing-import `NameError`s,
since py_compile executes no imports;
(f) the **residual-reference grep** (§6 item 6) that catches a half-migrated donor
still poking a relocated attribute.
Behavioral BLE correctness (pacing timings, login handshake, reconnect under a
busy radio) cannot be exercised offline — flag these for on-device smoke testing
after merge.

### 8.8 Name-prefix logic — who uses the predicates when (resolves LOW-9)
`melk`/`modelx`/`ledble` name checks appear in transport (login 872, notifications
957/1111, read-uuid requirement 1026), device (`set_mic_effect` mic clamp
661-662), and `select.py` (options list 48-51). The `ElkProtocol` predicates
(`requires_login`/`requires_read_uuid`/`uses_notifications`/`mic_effect_extended`)
are added in **Phase 0** (0a). The consistent Phase-0 story (a single story so the
0b/0c diff review is unambiguous):
- **transport** (freshly written in 0b) calls the protocol predicates from the
  start — byte-equivalent to the inline strings, introduces no new duplication.
  §3.3 and §5 (#5/#6) reflect this.
- **device** keeps its ONE inline name check — the `set_mic_effect` clamp
  (`max_effect = 0x87 if self.transport.name.lower().startswith(("melk","modelx"))
  else 0x83`, verbatim from 662, only rebinding `self._device.name`→
  `self.transport.name`, P2) — through Phase 0; Phase 1 folds it into
  `protocol.mic_eq`/`mic_effect_extended`.
- **select.py**'s copy is removed only in Phase 3.

### 8.9 `DeviceData` blocking call in ctor
`DeviceData.__init__` calls `async_ble_device_from_address` synchronously (102).
Moving the class to `device_data.py` in Phase 0 (§2.5) is a **pure relocation** —
the body is byte-identical; do not "fix" the blocking call earlier than Phase 2.
`config_flow.py:3` keeps importing the symbol via the `elkbledom.py` re-export,
and transport's discovery scan (196-211) constructs it exactly as before.

### 8.10 Dead `_notification_received` write (harmless — LOW-8/P7)
`_notification_handler` sets `self._notification_received = True` (982) but the
attribute is never initialized or read anywhere. It moves to `BLETransport` with
the handler and stays dead — do **not** expose it on the facade (nothing reads
it). Optionally delete the line; not required for behavior parity. Noted so a
reviewer doesn't hunt for a nonexistent reader.
