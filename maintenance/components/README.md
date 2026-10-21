# `maintenance.components`

This package contains the focused scanner and cluster-data components retained
by the Expra Connect host. Connect owns identity, trust, transport, and cluster
membership; these components provide host-side scanning and safety, telemetry
models, legacy data codecs, and bounded application history where the harness
still needs them.

Import supported component names through `maintenance.components` when they are
re-exported there. Module-level details are internal unless another module
explicitly imports them.

## Current modules

- `catalog.py` — dashboard feature metadata and capability declarations used by
  the scanner and preferences.
- `scan_support.py` — cancellation, platform guards, directory walking, and
  file hashing/fingerprinting shared by the scanner.
- `downloads.py` — Downloads-root resolution and bounded duplicate/file
  candidate scanning. It discovers candidates only; mutation remains in
  `maintenance.actions`.
- `gpu.py` — GPU probe result normalization, platform-specific probe selection,
  and safe fallback messaging.
- `process_safety.py` — shared process-name, user, and protected-PID policy
  inputs. `maintenance.actions.ProcessManager` remains the final authority for
  termination.
- `temperature.py` — temperature samples, capability states, and bounded
  per-node telemetry state. It has no UI ownership.
- `cluster_roles.py` — retained legacy role/epoch model used by
  `maintenance.cluster` persistence compatibility; it does not own Connect
  membership or trust.
- `cluster_storage.py` — bounded SQLite history and standby batch storage used
  by the Connect-authorized Exp history surfaces. This is application history,
  not the Connect cluster authority.

`RefreshIntervals` is a persisted preference value owned by
`maintenance.preferences`; it is not a scheduler. The retired Tk dashboard
coordinators and old placement policy are not part of the current Qt harness.

## Boundary rules

- `maintenance.scanner` and `maintenance.scanner_support` consume the scan
  primitives; the Connect host provider obtains scanner data through the
  `algo.py` scanner facade.
- Scanner discovery is read-only. Process termination and file cleanup remain
  in `maintenance.actions`.
- Connect identity, pairing, trust, transport, and membership must not be
  duplicated here.
- Cluster-history handlers validate current Connect membership and scope before
  calling the bounded local history stores.
