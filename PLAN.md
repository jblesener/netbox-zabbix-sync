# Upstream Integration Plan

## Assessment

The fork is at `4.0.27` (`f35d11c`); the parent repository,
`TheNetworkGuy/netbox-zabbix-sync`, is at `v4.1.0`. Their common ancestor is
`bf4202a` from March 25, 2026. Since then, upstream has 132 commits and this
fork has 49 fork-only commits. Both sides changed 20 paths, including `core.py`,
`device.py`, `interface.py`, CLI/configuration, documentation, and tests. This
is a substantial integration, not a fast-forward.

Upstream's main structural change is a shared `Host` class, with improvements
including IPMI, preferred-IP selection, config-context rendering, interface
reconciliation, logging, NetBox 4.7 support, and container-backed functional
tests.

Fork-specific behavior to retain includes Azure and ONTAP sync, VMware/ESXi
adoption, custom-field filters, ownership-tagged cleanup of deleted sources,
status reports and transition-based journals, FQDN normalization, and
additional-interface support.

### OOB and interface compatibility

OOB behavior differs in purpose and data model:

- Upstream `oob_sync` adds an OOB interface to the same Zabbix host.
- This fork's `zabbix.oob` creates a separate Zabbix host, with its own
  `oob_device_cf` link.

Keep the separate-host behavior distinct from upstream's interface behavior.
Make any coexistence explicit to avoid accidental duplicate OOB monitoring.
Upstream currently supports at most two interfaces per host, while the fork
supports configured additional interfaces; preserve the fork's broader
behavior in the upstream reconciler. Also reconcile config-context address
keys and precedence rather than silently changing existing configurations.

## Integration strategy

Use upstream `v4.1.0` as the base on a dedicated integration branch. Merge
upstream once and port fork-specific behavior into the upstream `Host` and sync
pipeline. Avoid replaying all 49 commits individually or carrying forward the
fork's older core/device structure wholesale.

## Phases

1. **Establish a baseline — complete**
   - [x] Add/fetch the parent repository as an `upstream` remote and pin the
     integration target to `v4.1.0`.
   - [x] Create a dedicated integration branch without changing the existing
     `main` branch.
   - [x] Run and record the fork's current unit tests, lint, type checks, and
     package build before integrating.

2. **Integrate the upstream foundation — complete**
   - [x] Merge upstream once, preserving the fork's commit history.
   - [x] Resolve shared host lifecycle and sync behavior around upstream's
     `Host` abstraction.
   - [x] Adopt upstream's supported NetBox/API compatibility work and test
     scaffolding without dropping the fork's publishing workflow.

3. **Reconcile overlapping behavior — complete**
   - [x] Preserve both OOB modes with separate, documented configuration and
     tests.
   - [x] Extend interface construction/reconciliation to support the fork's
     `additional_interfaces` while retaining upstream OOB, IPMI, preferred-IP,
     and DNS behavior.
   - [x] Preserve existing fork config-context keys and define their precedence
     relative to upstream keys.
   - [x] Integrate Jinja2 config-context rendering without breaking split OOB
     imports or fork-specific context values.

4. **Port fork-only features**
   - Integrate status reports, transition-based journals, custom-field
     filtering, and ownership-safe deleted-source cleanup into the upstream
     sync lifecycle.
   - Port Azure subscription and ONTAP cluster sync, plus VMware/ESXi
     adoption, FQDN normalization, and any associated adapters.
   - Keep each feature's configuration opt-in and preserve current defaults.

5. **Update dependencies, documentation, and CI**
   - Reconcile `pyproject.toml`, `requirements.txt`, and `uv.lock` with
     upstream's Jinja2/JSONPath dependencies and newer `pynetbox` baseline,
     while retaining dependencies required by fork features.
   - Align standard documentation with upstream while retaining complete
     documentation for fork-only options and OOB semantics.
   - Merge upstream functional-test CI with the fork's image-publishing
     workflow.

6. **Verify behavior**
   - Retain and adapt fork tests for every fork-specific feature.
   - Add coverage for OOB modes, multiple interfaces, config-key precedence,
     filtering/cleanup, and status reporting/journaling.
   - Run unit tests, Ruff, type checks, package build, and upstream's
     container-backed functional suite, including supported NetBox versions.

## Phase 1 execution record (2026-10-29)

- Created `integration/upstream-v4.1.0` from fork commit `f35d11c`; `main`
  remains unchanged.
- Added and fetched the `upstream` remote. `upstream/main` and tag `v4.1.0`
  both resolve to `fa57a8ea3d3ee26c1a24e433e9310c379f397d04`.
- Baseline on Python 3.12.15:
  - `uv run --no-sync pytest tests --cov --cov-report=term --cov-fail-under=70`:
    326 passed; 92.41% total coverage.
  - `uv run --no-sync ruff check .` and
    `uv run --no-sync ruff format --check .`: passed.
  - `uv run --no-sync ty check`: passed.
  - `uv build`: built the 4.0.27 source distribution and wheel.
- At the end of Phase 1, no upstream merge or fork-feature port had been
  started.

## Phase 2 execution record

- Integrated upstream `v4.1.0` on `integration/upstream-v4.1.0`, retaining
  the fork's device/VM sync flow and fork-specific behavior.
- Adopted upstream's shared `Host` base, IP/DNS and preferred-IP handling,
  IPMI support, config-context rendering, and functional-test scaffolding.
- Preserved configured additional interfaces and kept same-host `oob_sync`
  separate from split OOB-host imports. Added focused regression tests; these
  exposed and fixed a cleanup-source marker comparison that could otherwise
  add the same-host OOB interface to a split OOB import.
- Kept the fork's publishing workflows and merged upstream's functional-test
  workflow.
- Verification: `uv run pytest -q` — 492 passed, 162 deselected, 1 xfailed,
  16 subtests passed; focused interface tests — 15 passed; Ruff, `ty check`,
  `uv lock --check`, and `uv build` passed.
- At the end of Phase 2, container-backed functional tests had not been run;
  they require a live NetBox/Zabbix stack. Phases 3–4 were still pending.

## Phase 3 execution record

- Documented same-host OOB interfaces (`oob_sync`) separately from split-host
  imports (`zabbix.oob`) in `README.md` and `config.py.example`; both can be
  enabled together, with the duplicate-monitoring consequence called out.
- Kept `ip` and `dns` as supported fork aliases while defining upstream
  `interface_ip` and `interface_dns` as higher-precedence keys. Primary
  interface construction now applies the same IP precedence as `Host.set_ips`.
- Preserved primary and additional IPMI interfaces by validating the shared
  host-level IPMI settings. Added unit coverage for IPMI, endpoint precedence,
  combined OOB modes, and Jinja rendering of the split OOB context.
- Fixed `NetboxDeviceImport` attribute delegation during shallow copies so
  Jinja rendering can serialize the split-host adapter without recursion.
- Verification: `uv run pytest -q` — 498 passed, 162 deselected, 1 xfailed,
  16 subtests passed; Ruff, formatting, `ty check`, `uv lock --check`, and
  `uv build` passed. The deselected container-backed tests require the live
  stack planned for Phase 6 and were not run here.

## Completion criteria

- Fork-specific features and defaults remain available.
- Existing separate-host OOB configuration remains distinct from upstream's
  same-host OOB interface.
- Interface reconciliation supports the fork's configured additional
  interfaces and upstream's interface options.
- Unit and functional tests, lint, type checks, and package build pass.
- Documentation and example configuration describe the merged behavior.
