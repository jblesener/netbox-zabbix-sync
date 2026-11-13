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

4. **Port fork-only features — complete**
   - [x] Integrate status reports, transition-based journals, custom-field
     filtering, and ownership-safe deleted-source cleanup into the upstream
     sync lifecycle.
   - [x] Port Azure subscription and ONTAP cluster sync, plus VMware/ESXi
     adoption, FQDN normalization, and any associated adapters.
   - [x] Keep each feature's configuration opt-in and preserve current
     defaults.

5. **Update dependencies, documentation, and CI — complete**
   - [x] Reconcile `pyproject.toml`, `requirements.txt`, and `uv.lock` with
     upstream's Jinja2/JSONPath dependencies and newer `pynetbox` baseline,
     while retaining dependencies required by fork features.
   - [x] Align standard documentation with upstream while retaining complete
     documentation for fork-only options and OOB semantics.
   - [x] Retain upstream functional-test CI alongside the fork's image- and
     package-publishing workflows.

6. **Verify behavior — complete**
   - [x] Retain and adapt fork tests for every fork-specific feature.
   - [x] Resolve nested hostgroup ancestry by NetBox record ID when names
     repeat, and remove the corresponding expected-failure markers.
   - [x] Add coverage for OOB modes, multiple interfaces, config-key precedence,
     filtering/cleanup, and status reporting/journaling.
   - [x] Run unit tests, Ruff, type checks, package build, and upstream's
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

## Phase 4 execution record

- The Phase 2 merge retained the fork's sync orchestration and feature
  adapters. Verified that status reports and transition-only journals,
  custom-field filters, ownership-safe cleanup, Azure and ONTAP sync, VMware/
  ESXi adoption, and FQDN normalization remain connected to that lifecycle.
- Confirmed opt-in defaults remain unchanged (`create_journal`, VM/ONTAP/
  Azure sync, adoption, FQDN adoption matching, and deleted-host cleanup all
  remain disabled; custom-field filter maps remain empty). Added explicit
  regression assertions for the previously unasserted defaults.
- Verification: focused status/cleanup/filter/config tests — 76 passed;
  core/device/VM integration tests — 161 passed, 10 subtests; full default
  suite — 498 passed, 162 deselected, 1 xfailed, 16 subtests. Ruff/format and
  `ty check` passed. The deselected container-backed tests remain for Phase 6.

## Phase 5 execution record

- Regenerated `requirements.txt` from the lockfile without a local editable
  project entry. Its `pynetbox` pin is now 7.9.0, matching `pyproject.toml`
  and `uv.lock`, with the upstream Jinja2 and JSONPath dependencies retained.
- Reorganized the README around upstream's overview, features, requirements,
  quick start, installation, configuration, documentation, development, and
  license sections. Kept the full fork-specific configuration reference,
  status-report guidance, and both OOB modes; updated clone and image examples
  to point to this fork.
- Confirmed upstream functional-test CI coexists with the fork's image and
  package publishing workflows; no workflow changes were needed.
- Verification: `uv lock --check` and an exact `uv export` comparison passed;
  default test suite — 498 passed, 162 deselected, 1 xfailed, 16 subtests;
  `uv build` and `git diff --check` passed. Container-backed functional tests
  remain for Phase 6.

## Phase 6 execution record

- Fixed nested hostgroup paths for repeated region names by passing the NetBox
  record through and resolving each ancestor by ID. Name-based lookup remains
  as a fallback for older/string-only records. Removed the unit and functional
  expected-failure markers and added ID-based regression coverage.
- Corrected custom-field queries to use NetBox's `object_type` filter and
  normalized select-field template values across NetBox 4.6 and 4.7. Site maps
  now load omitted site properties before mapping; global preloading remains
  available through `extended_site_properties`.
- Updated functional assertions to distinguish filtered object requests from
  the unfiltered status-report scan, and detailed action journals from
  transition-based status journals. Clarified the corresponding configuration
  and README guidance.
- Verification: default suite — 500 passed, 162 deselected, 16 subtests;
  functional suite on NetBox 4.6.10 and 4.7.0 with Zabbix 7.0 — 160 passed
  and 2 xfailed on each version. Ruff, formatting, `ty check`,
  `uv lock --check`, package build, and `git diff --check` passed.

## Completion criteria

- Fork-specific features and defaults remain available.
- Existing separate-host OOB configuration remains distinct from upstream's
  same-host OOB interface.
- Interface reconciliation supports the fork's configured additional
  interfaces and upstream's interface options.
- Unit and functional tests, lint, type checks, and package build pass.
- Documentation and example configuration describe the merged behavior.
