# Repo Guide

## Documentation Index

Use current source and these current references before editing:

| File | Owns |
|---|---|
| `AGENTS.md` (this file) | Repo law, code structure, test/lint rules |
| `README.md` | Current harness identity and source-run instructions |
| `docs/CURRENT_PROJECT_MAP.md` | Current module ownership and Connect/host boundary |
| `docs/SECURITY.md` | Durable no-log, process-safety, and network-security constraints; re-check implementation claims against source |
| `docs/PLATFORM.md` | Platform-safety rules; machine-specific paths and old release instructions are historical |

`docs/REMOTE_CLUSTER_TRUE_FLOW.md` and
`docs/SYSTEM_ARCHITECTURE_REVIEW.md` describe earlier implementations and are
historical only. Other plans, audits, and bug-hunt documents are historical
evidence; never use them as implementation proof without checking current code.

## Security and Logging Constraints

These apply everywhere, always:

- **Never commit secrets.**
- **Do not log:** HMAC secrets; private keys; fencing token values.  Fencing token *presence* may be recorded, not the value.
- **Do not expose the fence token in user-visible evidence.**
- **Do not weaken security/firewall configuration automatically.**
- **DO NOT blindly delete user state.**
- **DO NOT say "remote cluster is fully validated" merely because automated tests pass.**
- Physical two-node testing is required before claiming network acceptance; the
  2026 runbook describes the earlier product and must not be treated as a
  current harness procedure without review.

## Windows Platform Rules

- **WINDOWS IS A PLATFORM DIFFERENCE, NOT A SECURITY EXCEPTION. FIX THE PLATFORM BOUNDARY. DO NOT FORK THE TRUST MODEL.**
- DO NOT develop on Windows; DO NOT run tests/source tools on Windows.
- DO NOT disable firewalls globally; DO NOT disable ProtonVPN permanently.
- DO NOT open the entire LAN to arbitrary ports; DO NOT hardcode ProtonVPN-specific behavior.
- Windows machine (`DESKTOP-0C2C5H3`) is a black-box runtime node only — install wheel, run app, report results.
- See `docs/PLATFORM.md` for the full protocol and capability matrix.

## Cluster Three Axes (never collapse)

TRUST (pairing/records/grants) · CLUSTER (assignments/joined state) · CONNECTION (Online/Offline).
The Connect package owns the current state model. Verify exact fields and
transitions against the package API; the older audit is historical, not current
implementation proof. See `docs/CURRENT_PROJECT_MAP.md` for the host boundary.


- `main.py` is the Qt Expra Connect harness entrypoint (`python main.py`); it composes `maintenance.ui.qt_connect_harness.QtConnectHarnessWindow` and the Connect runtime adapter. There is no legacy `window.py` or Tk application in the package.
- `expra_connect` owns Connect identity, discovery, trust, pairing, authenticated transport, and membership. `maintenance/connect_runtime.py` adapts runtime lifecycle; `connect_provider.py` supplies host data/actions; `connect_surfaces.py` and `connect_data_surfaces.py` register process-action and cluster-history surfaces.
- `algo.py` remains a scanner facade for the host provider. `maintenance/scanner.py`, `maintenance/actions.py`, models, node contracts, persistence, preferences, and the explicitly reduced `maintenance/components/` facade are retained only where the harness/provider/cluster pipeline depends on them. `maintenance/actions.py` remains the final authority for process termination and Downloads cleanup safety.
- `maintenance/ui/` contains the small Qt harness, Qt application setup, Connect pairing/identity-transition adapters, and shared styles. UI code must not become a second owner of Connect trust, cluster state, or scanner logic.
- Install and build from `pyproject.toml`; `requirements.txt` mirrors runtime dependencies. The harness remains one universal `py3-none-any` wheel. `install/build.sh` and `.ps1` create a universal bundle by default and a native Connect bundle with `--variant native --native-connect-wheel PATH` / `-Variant native -NativeConnectWheel PATH`; both use the same harness wheel. Root `dist/` contains the published wheelhouse and `SHA256SUMS`; local and online installers resolve the compatible Connect wheel from that wheelhouse, preferring native when supported and falling back to universal. Online installer URLs target `20204166/exp-new`, not the separate `20204166/exp` repository. Native Connect wheels require matching CPython/platform tags. `expra-connect>=2.2.1.1,<3` is the declared compatible range; release/install tooling pins the vendored universal companion wheel and must be updated through `maintenance._release update-companion`. Do not rebuild or alter `dist/` unless explicitly requested. The local project is not published to PyPI.
- Tests are `unittest` modules under `tests`; use `scripts/run_tests.sh` for the full suite or focused modules. On Linux it uses `xvfb-run` when available to isolate display-dependent tests. The current harness test surface is Qt-based; old Tk/full-dashboard tests and modules have been pruned.
- If Python code changes, run `ruff check .` and `ruff format --check .` before handing off.
- Test hygiene gate: no test — repo tests or validator/opposer counter-tests — counts as validation evidence unless it passes `ruff check` + `ruff format --check` + `pyright` + `mypy --ignore-missing-imports`. Fix or clearly mark any test that is not check-clean before relying on it; never use an unready test as proof.
- Keep `ruff check .`, `ruff format --check .`, `pyright`, and `mypy --ignore-missing-imports` (app + tests) green repo-wide; fix pre-existing static findings you touch, and fix unrelated baseline findings when asked rather than leaving them to grow.
- `docs/bug_hunts/*` are historical evidence trails, not the source of current behavior.
- Machine-specific OpenCode configuration and reusable agent/skill assets are local development tooling and are not part of the published runtime-focused source set.
- BugGuard reviewer routing: all required opposition roles, including Agent 5, must use separate independent BugGuard reviewer/fallback sessions rather than the main OpenCode agent; never simulate or backfill reviewer evidence.
- Thermal sampling remains in `maintenance/components/temperature.py`; the current minimal harness does not ship a thermal graph UI. Keep telemetry independent of UI code and preserve the distinction between resource `CapabilityState` and thermal-series state: repeated empty reads resolve to `UNSUPPORTED` without fabricating samples.
- Search before create: before adding a reusable helper, search the repository for an existing implementation of the same responsibility. Prefer one canonical implementation and share mechanisms, not specialized meanings; do not duplicate a common operation under a new name.
