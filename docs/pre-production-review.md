# Pre-production review — 2026-09-05

## Verdict

The remediated source and extracted integration archive pass the checks below.
This is a **pre-production candidate, not hardware-qualified or published**.
Two release gates remain: GitHub repository settings required by HACS, and an
observed Home Assistant → ESPHome Bluetooth Proxy → physical controller test.

The integration uses HA's connectable Bluetooth routing; it does not open a
local-only scanner or connect by a bare address. Proxy routing, cancellation,
connection-slot cleanup, and framework contracts have automated coverage.
Those tests cannot establish radio reliability or physical firmware behavior.

## Baseline and preservation

- Reviewed against `180de94ebc031e8c187e148ef7122ddba8784f65` on `main`.
- The earlier checkout was 14 commits behind upstream. Its changes were saved
  in `stash@{0}`, named `pre-production snapshot before upstream reconciliation
  2026-09-05`, before fast-forwarding and porting the remediation.
- The upstream modular architecture remains intact: transport, protocol, model,
  state, device, coordinator, entities, and compatibility facade.
- Existing archives, the stash, and other worktrees were preserved. At review
  completion, the changes were uncommitted; no release, push, or
  repository-setting mutation was part of that review.

## Findings, historical checks, and regression protection

| Area | Correction and why it is needed | History checked and regression guard |
| --- | --- | --- |
| Proxy slot ownership | Keep ownership until disconnection completes; cancellation during MELK login closes the connection; notification cleanup failure cannot skip disconnect. Failed idle disconnect retains usable GATT handles and schedules cleanup again. Stopped instances cannot reconnect. | `b8d1751` introduced unload cleanup; `180de94` corrected its cancellation leak. Preserve the latter's await-in-flight behavior. Tests exercise idle/unload races, failed notification teardown, cancelled login, failed disconnect, and post-unload writes. |
| Proxy routing and availability | Refresh HA's connectable BLEDevice for each connection; keep one connector attempt under the outer retry budget. Use the controller's exact address for case-sensitive HA callback indexes, independently of normalized registry IDs. Remember unavailable advertisements while connected. | `b8d1751` already established fresh-device routing and single retry ownership. Retain those decisions; normalization must not undo them. Tests check fresh route callbacks, retry limits, actual-address subscriptions, and availability after link loss. |
| GATT compatibility | Use `client.services` and select write response mode from the characteristic properties. Reject non-writable characteristics. Notifications are optional because this integration only logs command echoes. | The baseline still used the old services API in `BTScan.py`. Transport tests cover write-only, write-without-response, both, non-writable, and missing optional notify characteristics; standalone-tool tests cover the same write policy. |
| Discovery and setup | Require a real confirmation form and a successful GATT probe; never accept an advertisement-only route or bypass failed validation. Clean up partial setup and return retryable setup failures. No power toggle is needed for validation. | `056f188` fixed a retry form that previously aborted discovery. Preserve retryability, but replace the blink-based check with transport validation. Tests cover confirmation, passive discovery rejection, failed probe, retry form, and platform-forwarding cleanup. |
| Light color modes | Validate the supported-mode set itself. WHITE cannot coexist with COLOR_TEMP or stand alone; a dimmable white-only device uses BRIGHTNESS. Represent effect modes and the no-effect value according to HA's contract. Use Kelvin, not the removed mired action argument. | `a7b3142` guarded restored mode membership and `2f12745` corrected temperature-mode brightness. Membership checks alone do not prove that the supported-mode set is valid. Test all 24 shipped models and synthetic white-only/power-only models with HA's actual validator. |
| Effect and optimistic state | Serialize complete actions across entities; publish state after successful writes. Static color exits effects, but native brightness must preserve an active effect. Unsupported actions raise errors instead of silently succeeding. | `185e3bf` fixed the same native-brightness/effect regression: an extra RGB frame stopped the effect. Preserve that behavior with an exact-write regression. Retain the app-proven frames from `8785e9b` and `68d8c5b` with encoder assertions. |
| Model and effect loading | Load JSON off the event loop and publish the cache only after effects are complete. Preserve shared fallback-list references, validate enum members, align MELK microphone capabilities, and clamp restored values. | `dc8e5fe` introduced data-driven definitions. The partial-publication/self-clearing-list problems were found in the remediation, not attributed to that upstream commit. Concurrent-load, fallback-list, enum, model-key, and restored-value tests prevent recurrence. |
| Identity migration | Canonicalize addresses without discarding established entity IDs or user metadata; preflight collisions, preserve the oldest record, reattach helpers, and update the migration version last. Preserve ambiguous historical manual model selections. | Checked existing address/device identity behavior; no equivalent comprehensive migration guard was located. Tests exercise metadata merging and repeat migration against real HA device/entity registries, including linked helper entities. |
| HA lifecycle and actions | Register actions at integration setup, use typed `runtime_data`, reject foreign/unloaded targets, propagate action failures, rediscover on removal, and make config options authoritative for brightness mode. Redact identifiers in diagnostics. | Preserve the shared DeviceInfo decision in `056f188` and coordinator design in `3285f5f`. Regression tests cover registration, target ownership, removal, failed setup cleanup, and loaded/unloaded diagnostics. |
| Release safeguards | Validate against the supported HA versions, pin action revisions, validate before publishing, atomically push the version/tag, and build a verified flat ZIP without replacing existing artifacts. Add an explicit repository-settings failure gate. | The HACS container reported two failures while returning exit code 0 in this environment. The new `gh api` guard independently exits nonzero for those settings. Packaging tests enforce reproducibility, exact contents, and no overwrite. |

## Current documentation and source checks

Checked current primary documentation and the APIs installed in the two HA
containers; historical code was not treated as current API authority.

- [HA Bluetooth integration requirements](https://developers.home-assistant.io/docs/bluetooth/)
  and [Bluetooth APIs](https://developers.home-assistant.io/docs/core/bluetooth/api/):
  dependency, connectable discovery, current BLEDevice lookup, unavailable
  callbacks, and rediscovery. Installed HA/habluetooth source also confirmed
  that callback and device-history address keys are case-sensitive.
- [Config flows](https://developers.home-assistant.io/docs/core/integration/config_flow/)
  and [discovery confirmation](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/discovery/):
  unique-ID progress protection and an actual user confirmation step.
- [Config entries](https://developers.home-assistant.io/docs/config_entries_index/):
  lifecycle, migration, removal, and runtime data; registry contracts were also
  exercised with real HA registries.
- [Light entity contract](https://developers.home-assistant.io/docs/core/entity/light/)
  and [removed light arguments](https://developers.home-assistant.io/blog/2026/02/23/remove-deprecate-light-features/):
  supported color-mode combinations, effect-mode reporting, and Kelvin actions.
- [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy/):
  active GATT connections are different from active scanning. Current supported
  ESP32 and RP2040/RP2350 proxies default to three connection slots. A proxy-only
  installation does not require an adapter on the HA host.
- [HACS repository requirements](https://hacs.xyz/docs/publish/include/#check-repository):
  enabled issues and repository topics are required by the validator.

## Final validation evidence

| Check | Final result |
| --- | --- |
| HA `2026.8.0`, Python `3.14.6` | **44 passed** |
| HA `2026.9.0`, Python `3.14.6` | **44 passed** |
| Ruff `0.13.1` lint and formatting | Passed; 29 Python files formatted |
| Local hassfest container | 1 integration, 0 invalid |
| Workflow YAML / root Python script parsing | All 3 workflows and root scripts parsed |
| `git diff --check` | Passed |
| Extracted archive in isolated temporary tree on HA `2026.9.0` | All 18 Python modules imported from the archive; **44 tests passed** |
| HACS remote repository validation | Failed repository settings: issues disabled and no topics |
| Explicit GitHub repository-settings gate | Confirmed nonzero exit for the current invalid settings |

Source-tree test runs report one upstream HA HTTP/aiohttp inheritance
deprecation warning. It is not emitted by this integration. The test bootstrap
loads HA before integration schemas so HA's validation compatibility layer is
initialized in production order.

The HACS action inspected remote `nbetcher/elkbledom` on `main`, not the
uncommitted local source. Its result must not be described as validating this
candidate. Rerun it after authorized publication of the reviewed changes.
No CI workflow or physical-device test was run remotely.

### Repeating local checks

Use each supported HA image in turn:

```powershell
docker run --rm -v F:/Dev/elkbledom:/workspace -w /workspace --entrypoint /bin/sh ghcr.io/home-assistant/home-assistant:2026.9.0 -c "uv pip install --system --break-system-packages --index-strategy unsafe-best-match -r requirements_test.txt ruff==0.13.1 && ruff check custom_components tests scripts && ruff format --check custom_components tests scripts && pytest -q"
docker run --rm -v F:/Dev/elkbledom:/github/workspace ghcr.io/home-assistant/hassfest:latest
git diff --check
```

The index strategy above permits resolution across the HA image's configured
HA wheel index and PyPI; its first-index default otherwise hides newer test
dependencies. Normal GitHub-hosted Python jobs use pip and do not need it.

## Candidate artifact

- File: `dist/elkbledom-2.0.17-preproduction.zip`
- Manifest version: `2.0.17`; this is a local candidate, not a new published tag.
- Contents: 42 files, flat integration layout, including all 18 Python modules.
- SHA-256: `68f5658c65b4519a9518167afb2fc527c833bd2740b08a2505ca1b3198197abc`
- Every archived byte was compared with the source and ZIP CRCs checked.
- Extract into `config/custom_components/elkbledom/`, not directly into
  `custom_components/`.
- Older archives were not overwritten. To rebuild, use a new output filename:
  `python scripts/build_release.py --output dist/<new-candidate-name>.zip`.

## Remaining release gates

1. **Repository settings:** `gh api repos/nbetcher/elkbledom` returned
   `has_issues: false` and `topics: []`. Enabling Issues and adding relevant
   topics needs an authorized GitHub settings change. The release workflow now
   stops before publishing while either condition remains unmet.
2. **Physical proxy qualification:** run the following on a designated test
   installation. Record HA version, ESPHome firmware, proxy hardware, controller
   model/firmware, logs, and observed results. Automated fake-client tests are
   not a substitute for these observations.

| Scenario | Required observation |
| --- | --- |
| Proxy-only discovery and setup | Confirm the route in HA/ESPHome logs; no local-adapter fallback. Confirmation succeeds without a light-state toggle. |
| Supported light actions | Verify power, RGB or temperature, brightness, effects and effect exit physically. Native brightness must not stop an effect. |
| Shared proxy capacity | With the default 20-second idle delay, slots return to the baseline after idle and after entry unload/reload. |
| Connection interruption | Restart the test proxy or interrupt its link during an action; observe a bounded failure and successful recovery when reachable again. |
| Concurrent entity actions | Exercise light, supported microphone, and effect-speed controls together; no interleaved multi-frame sequences or stuck connection. |
| Removal and restart | Confirm rediscovery, stable entity identities, preserved options, and no lingering callbacks or connection slot. |

Use a test route or log evidence to prove proxy routing; do not disable unrelated
production hardware. Test only model-supported capabilities, and restore any
controller schedules changed during qualification. Do not claim a formal HA
Quality Scale tier: `quality_scale.yaml` remains an explicit work checklist.
