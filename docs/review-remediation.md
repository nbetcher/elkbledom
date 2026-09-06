# Review remediation and regression gates

Date: 2026-09-05. Committed baseline:
`4368d4eaaf490061c9fbd6431444b6b0697fa419`, plus the preserved, uncommitted upstream
ports and speed-limit changes. This follow-up implements all nine review findings
and the release commit-identity correction. It does not publish a release or
qualify physical hardware.

## Corrections and historical safeguards

| Finding | Correction | Regression protection and history |
| --- | --- | --- |
| Multiplied proxy retries and timeout fast-fail | A client-local budget caps physical connect calls at three across connector retries, intent retries, and a batched HA action. Connector-wrapped timeouts retry with a fresh HA route; genuinely absent paths fail promptly. | `test_proxy_retry.py` retains the real connector and counts underlying calls on HA 2026.8/2026.9. It covers failure types, third-attempt success, recovery, cancellation, missing paths, and shared batch limits. This corrects the dependency-semantics assumption in `b8d1751` and the inherited NotFound handling. |
| Incomplete clean CI environment | Both validation and release workflows use `scripts/validate.py` with official versioned HA containers and pinned test tools, including an import preflight. | `test_validation.py` guards the workflow/helper contract. Exact helper runs exercise the same clean containers locally. The older bare `pip install homeassistant` path introduced in `4368d4e` failed before test collection because integration dependencies were missing. |
| Explicit ON suppressed by optimistic cache | Every explicit light ON reaches the device. RGB selections also reapply their target brightness, including cached 255, to reset an IR-dimmed native register. Reset-to-white still depends on the previous cached off state. | Tests cover explicit ON and RGB/full-brightness resends despite stale assumed state. Extends the explicit-color resend rationale to the longstanding power path from `7e37adb`. |
| Transient native brightness failure stops effects | Automatic mode chooses the supported encoding; a transport failure retries native brightness and cannot switch to RGB. | `test_transient_native_brightness_failure_retries_without_rgb_fallback` checks the exact attempted frames and retained effect. Preserves the effect-safety intent of `185e3bf`; the latent fallback existed in `7022ba8`. |
| Incorrect native-mode and off-state restoration | Versioned extra restore data stores native settings separately from HA's displayed attributes. Legacy state restoration honors color mode, so derived RGB cannot override Kelvin. | `test_light_restore.py` round-trips HA-generated attributes through JSON, covering on/off, RGB/CT, remembered effects, unsupported models, and malformed data. Fixes assumptions inherited from `d73811e`. |
| RGB normalization changes intensity | Report the preserved RGB tuple unchanged; brightness scaling remains separate. | Non-full-scale and black RGB tuples now join the dim/restore regression. Prevents the old full-scale normalization from surviving the base-color preservation port. |
| Fixed frames advertised as adjustable controls | Model encoders require the corresponding variable placeholders. XSL-/LED LIGHT STRIP retain power but expose no unsupported adjustments. | `test_model_capabilities.py` requires distinct byte-encodable frames for distinct legal inputs. Unsupported device actions fail before power or microphone-mode side effects. Captures from `7d823f7` remain intact as evidence, not adjustable APIs. |
| MELK music effect IDs exceed one byte | Reject unencodable effect IDs and filter them out of the HA effect list and restoration. No speculative opcode mapping or byte truncation. | Every advertised effect is passed through its actual model encoder. The eight IDs 384–391 inherited from `447a333` remain recorded but are not offered as working controls. |
| Duplicate migration drops entity options | Merge options by domain/key before removing duplicates. The oldest row's explicit values, including false and null, win conflicts; missing keys/domains are filled from later rows. | Real HA registry tests cover preservation, conflicts, aliases, linked helpers, and a second idempotent migration. Extends the incomplete metadata correction in `4368d4e`. |
| Release commit attribution | Explicit author and committer are Nick Betcher, `nick@nickbetcher.com`. | A workflow regression asserts the identity and atomic branch/tag push. No commit, push, or workflow dispatch is performed by these changes. |

The proxy budget caps physical attempts, not a new hard wall-clock deadline.
Existing connector connection timeouts and configuration validation deadlines
remain. Cleanup is still awaited rather than forcibly cancelled, preserving the
previous proxy-slot release protection. There are no process-wide dependency
patches or additional scanners in production code.

Legacy off-state records that already lack color/brightness cannot recover those
values retroactively. New native snapshots preserve them going forward. Device
state remains optimistic; it is not firmware readback.

## Current verification

- `python scripts/validate.py --home-assistant 2026.8.0 --pull never`: passed.
- `python scripts/validate.py --home-assistant 2026.9.0 --pull never`: passed.
- Both versions: 134 tests passed, including the actual connector dependency and
  HA-generated restoration/registry cases. Ruff lint/format checks passed.
- Hassfest: one integration, zero invalid integrations.
- Source suites report the upstream HA HTTP/aiohttp inheritance deprecation warning.
- Extracted candidate: all 19 integration modules imported exclusively from the
  archive and all 134 tests passed on HA 2026.9.0.

Candidate: `dist/elkbledom-2.0.17-review-fixes.zip` (43 integration files),
verified byte-for-byte against the current source. SHA-256:
`561131f778dd7cc20c61aedfdb5c66fb30ee02a9290141bb9bee65ba7cc86d33`.
The manifest remains 2.0.17; this uniquely named local candidate is not a release.
Previous candidates were preserved rather than overwritten.

`--pull never` reuses the installed official image locally; GitHub workflows use
the helper's default `--pull always`. The workspace is mounted read-only and each
run installs pinned test tools in a fresh container. Host-only helper tools and
test caches are not packaged into the integration.

Remote workflow success is not claimed: the corrected workflows have not been
pushed or dispatched. The previously failed remote release remains historical
evidence, not a current passing result.

## Remaining release qualification

Use a designated HA installation and active ESPHome proxy to verify proxy-only
routing, all basic controls, slot saturation, interrupted actions, alternate-route
recovery, unload/reload slot counts, and restart restoration. Do not change live
adapters or unrelated devices merely to force a route. Record HA/ESPHome versions,
proxy hardware, controller variant, logs, and observed results.

No physical controller was commanded during this remediation. Conservative speed
limits remain unchanged pending model-specific hardware evidence. Existing
archives, the preservation stash, and other worktrees were not replaced.

## Primary contracts checked

- [HA light contract](https://developers.home-assistant.io/docs/core/entity/light/):
  RGB intensity, color modes, effects and state attributes.
- [HA restore-state implementation](https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/helpers/restore_state.py):
  native extra-state serialization and restoration.
- [HA Bluetooth API](https://developers.home-assistant.io/docs/core/bluetooth/api/):
  connectable lookup, freshness, callbacks and cleanup.
- [Connector implementation](https://github.com/Bluetooth-Devices/bleak-retry-connector/blob/v4.7.0/src/bleak_retry_connector/__init__.py):
  physical retry semantics, timeout wrapping, and cleanup.
- [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy/):
  active connection requirements and finite connection slots.
