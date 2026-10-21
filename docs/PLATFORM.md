# Platform Safety Rules and Historical Runbook Notes

> **Scope update:** The cross-platform security constraints and Linux-only
> development rule remain applicable. The machine-specific paths, published
> wheel commands, and capability details below were written for an earlier
> product version; revalidate them against the current harness and install
> scripts before use.

> This file owns platform-boundary rules, the
> Windows testing protocol, the physical-topology record, and the
> cross-platform capability baseline.  It supersedes:
> `docs/WINDOWS-TESTING-PROTOCOL.md`,
> `docs/WINDOWS_VALIDATION_GUIDE_2026-09-17.md`,
> `docs/platform_audit/PLATFORM-MATRIX.md`,
> `docs/platform_audit/THERMAL-ROOT-CAUSE.md`.
> Historical evidence trails (`docs/WINDOWS-FINDINGS-2026-09-17.md`) are
> kept as evidence records but this file is the durable rule source.

---

## Axiom: Windows is a Platform Difference, Not a Security Exception

**WINDOWS IS A PLATFORM DIFFERENCE, NOT A SECURITY EXCEPTION. FIX THE
PLATFORM BOUNDARY. DO NOT FORK THE TRUST MODEL.**

Any behavior difference on Windows must be fixed by correcting the platform
boundary in the portable code.  It must never be resolved by:

- disabling security checks on Windows
- weakening the firewall on Windows
- forking trust or authorization logic by platform
- opening the entire LAN to arbitrary ports
- hardcoding ProtonVPN-specific behavior into application logic

---

## Physical Topology (current test nodes)

| Role | Machine | NodeId |
|---|---|---|
| Coordinator | Linux m75-node1 (`node-983364764039f9e6625e687273710909`) | Development + test machine |
| Worker | Windows DESKTOP-0C2C5H3 (`node-b31a0913e76db7c60cb7c6ec82313660`) | Black-box runtime machine |

Both machines must be on the same LAN subnet for mDNS discovery.

---

## Windows Machine Role

The Windows machine is a **black-box runtime node only**.  It installs and
runs the published wheel and nothing else.

**Never, on Windows:**
- clone the repository
- edit source code
- run pytest / Ruff / Pyright / mypy / any dev tool
- set up a Python coding/dev environment
- disable the firewall globally
- disable ProtonVPN permanently
- open the entire LAN to arbitrary ports

**State locations:**
- Log: `C:\Users\BTN17\AppData\Local\system-analyzer\system-analyzer.log`
- State dir: `C:\Users\BTN17\AppData\Roaming\system-analyzer\`
  (`cluster.json`, `cluster-history.sqlite3`, `peer-tls.{crt,key}`)
- Console entry point: `system-analyzer`, installed to
  `C:\Users\BTN17\AppData\Roaming\Python\Python312\Scripts`

**Install/update the wheel:**
```powershell
irm https://raw.githubusercontent.com/20204166/exp/main/install/install-online.ps1 | iex
```

---

## Per-Failure Loop

Physical Windows testing is the **only** valid next step when a
Windows-side failure is suspected.  No speculative code audits, no
production-code changes, until a real Windows-wheel failure has been
reproduced on hardware.

### Step 1 — Windows: reproduce

1. Reproduce the failure using the **installed wheel** (never source).
2. Collect exact evidence: UI behavior observed + PowerShell/runtime output
   (commands run, full error text, exit codes).

### Step 2 — Linux: fix

1. Identify the **first broken boundary** from the Windows evidence.
2. Add the strongest Linux-runnable regression test for it.
3. Implement the smallest portable fix.
4. Verify both the native Linux path and the emulated-Windows branch.
5. Build a new wheel.

### Step 3 — Windows: verify fix

1. Install the new wheel (rerun the online installer).
2. Rerun the exact failed scenario and confirm the fix against the same
   evidence collected in Step 1.

**Non-goals** — do **not** preemptively fix the following unless a physical
Windows run actually reproduces it:
- `candidate.addresses[0]` handling
- firewall behavior
- IPv6 handling
- VPN handling
- any other currently hypothetical Windows problem

**Fix constraints:**
- Preserve existing Linux behavior.
- Keep fixes portable / macOS-compatible whenever possible.
- Fix only the first broken boundary actually observed; no speculative
  hardening beyond the reproduced failure.

---

## Cross-Platform Capability Baseline

Source: `docs/platform_audit/PLATFORM-MATRIX.md` (2026-09-12, commit `fa84a68`).
Status vocabulary:

| Code | Meaning |
|---|---|
| VERIFIED | Evidence exists in the current environment or tests |
| IMPL-NOT-NATIVE | Source and fixtures exist; no native host evidence |
| PARTIAL | Only part of the capability or platform path is covered |
| UNSUPPORTED | The current provider does not expose this capability |
| BROKEN | Reproducible contract violation |

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| CPU, memory, storage, network | VERIFIED | IMPL-NOT-NATIVE | IMPL-NOT-NATIVE |
| GPU identity and metrics | PARTIAL | IMPL-NOT-NATIVE | IMPL-NOT-NATIVE |
| Battery charge | VERIFIED | IMPL-NOT-NATIVE | IMPL-NOT-NATIVE |
| Battery temperature | UNSUPPORTED | UNSUPPORTED | UNSUPPORTED |
| CPU/GPU/NVMe thermals | PARTIAL (sensor availability varies) | IMPL-NOT-NATIVE | IMPL-NOT-NATIVE |
| Process review and safe termination | VERIFIED | IMPL-NOT-NATIVE | IMPL-NOT-NATIVE |
| Downloads cleanup / Trash | VERIFIED | IMPL-NOT-NATIVE | IMPL-NOT-NATIVE |

**Verified safe platform adapters** (Phase 1 audit found no unsafe assumptions):
- `scanner_support/smc.py` — Darwin guard: correct platform adapter.
- `preferences.py`, `components/downloads.py`, `scanner_support/storage.py` — platform dispatch injected.
- `scanner_support/dashboard.py` Linux `/proc/swaps` — correctly Linux-only.
- `external_commands.py` — text/JSON runner centralized; accepts caller-supplied creation flags.
- `actions.py` `send2trash` — cleanup is dependency-gated, constrained by action safety policy.

**Not natively verified** (fixture/source only):
- `scanner_support/storage.py` Windows Recycle Bin (`SHQueryRecycleBinW`).
- `scanner_support/dashboard.py` + `temperature_platform.py` Windows thermal providers (ACPI, LibreHardwareMonitor, OpenHardwareMonitor — all failing soft; no native run confirmed).

---

## Thermal Root Cause (non-Linux "Waiting for samples")

Root cause: capability-signal conflation.  CPU/GPU/Storage card capability
is `SUPPORTED` because those cards report non-thermal metrics.  The Thermals
page consumes the same card-level map.  Windows and Apple SMC acquisition can
repeatedly return no samples while leaving card capability `SUPPORTED`.
`TemperatureTelemetry.record_summary` then has no counter or bounded transition
and repeatedly assigns `NO_DATA`.

**Fix boundary:** `TemperatureTelemetry` (bounded `UNSUPPORTED` state transition
after `TemperaturePolicy.unsupported_confirm_samples` empty reads).  **Not** a
platform acquisition change, not a graph-behavior change.

This is a **capability-signal** fix, not a platform exception.  Remote summaries
use the same path — the fix applies uniformly to local and remote nodes.

**Preserve:** `CapabilityState` for resource cards ≠ thermal series state.
Never fabricate samples; always resolve to `UNSUPPORTED` via the bounded counter.

---

## Isolation and Firewall Rules

- Do **not** weaken the host firewall broadly.
- Do **not** open the entire LAN to arbitrary ports.
- Do **not** disable ProtonVPN permanently or globally.
- Do **not** hardcode ProtonVPN-specific behavior into application logic.
- Firewall exceptions, if needed for testing, must be scoped to the minimum
  port and duration required and reverted after the test.

---

## DO NOT say: "remote cluster is fully validated" merely because automated tests pass

Unit tests exercise individual components in isolation.  Physical two-node
testing exercises the real network stack, real TLS handshakes, real mDNS
advertisements, and real wall-clock timing.  These are not equivalent.

The authoritative two-node acceptance runbook is
`docs/PHYSICAL_TWO_NODE_ACCEPTANCE_2026-09-17.md`; it has not yet been fully
executed and its Observed/Evidence columns remain unfilled.
