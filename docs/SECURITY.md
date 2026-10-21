# Security Constraints and Historical Implementation Notes

> **Scope update:** The no-log, secret-handling, process-safety, and
> firewall/network-safety constraints below remain durable repository rules.
> Pairing, trust-record, role-RPC, and transport descriptions refer to the
> earlier implementation and are historical; current Connect authority and
> behavior must be verified in the current source and Connect package.

> This file owns durable security rules, no-log
> constraints, pairing trust semantics, process termination safety, and
> fencing/HMAC rules.  It consolidates durable rules from:
> `docs/security_reviews/SEC-20260912-001-review.md`,
> `docs/security_reviews/SEC-20260912-002-review.md`,
> `docs/PHYSICAL_TWO_NODE_ACCEPTANCE_2026-09-17.md §2`,
> and the session-level security constraints applied throughout Phase 12–13B.
> Evidence records in `docs/security_reviews/` and `docs/bug_hunts/` are kept
> as evidence trails; this file is the durable rule source.

---

## Absolute No-Log Rules

These apply everywhere in the codebase, in all new code, in all log messages,
and in all test or diagnostic output:

- **Never log HMAC secrets.**
- **Never log private keys.**
- **Never log fencing token values.** Fencing token *presence* may be recorded
  (e.g., "fencing token present: yes/no"), but the token value must never appear
  in any log, diagnostic, or UI output.
- **Never expose the fence token in user-visible evidence.**
- **Never commit secrets to the repository.**

---

## TLS and Pairing Trust Model

### What TLS provides here

- Every installation binds a TLS-protected pairing listener on `0.0.0.0`
  unconditionally at startup, independent of the discovery preference.
- TLS in this app uses `ssl.CERT_NONE` — **no certificate chain validation**.
  Trust is based solely on a pinned identity fingerprint.
- The "identity fingerprint" is a **public, non-secret, deterministic hash** of
  the node ID.  Any machine on the LAN can compute it.
- **The only thing that makes first-contact pairing safe against an active network
  attacker is a human comparing two fingerprints "through a trusted channel."**
  There is no cryptographic anchor until the human approves.
- TLS private key: explicitly `chmod 0o600` at creation.
- Pairing/grant secrets: stored as plaintext JSON in `cluster.json`.  No extra
  file-permission hardening is confirmed for that file (unlike the TLS key).

### Directional trust storage

Pairing authorization is **directional and asymmetric**:

| Party | Record stored | Meaning |
|---|---|---|
| Initiator (A) | `TrustedNodeRecord(B)` | A trusts B |
| Acceptor (B) | `PeerGrantRecord(A)` | B has granted A access |

**Do not create a reverse `TrustedNodeRecord` on the acceptor.**  This would
change the authorization semantics.  The UI relationship (`has_pair_relationship`)
is a **presentation-only** boolean derived from `peer_grants`; it does not create
trust records and must not be treated as authorization proof.

### Three axes — never collapse

| Axis | What it tracks | Records |
|---|---|---|
| TRUST | Pairing, trusted-node records, peer grants | `TrustedNodeRecord`, `PeerGrantRecord` |
| CLUSTER | Role assignments, joined state, Coordinator epoch | `RoleAssignment`, `coordinator_epoch` |
| CONNECTION | Online/Offline live transport state | `NodeConnectionStatus` |

These axes are independent.  A node can be TRUSTED but not in the cluster.  A
node can be a cluster member but temporarily OFFLINE.  Never infer one axis from
another.

---

## Cluster Authorization and RPC Safety

- **Revoke never notifies the peer being revoked.**  The peer's own grant for
  the revoker survives until the peer independently revokes it.  This is a
  known limitation, not a bug to paper over silently.
- Role assignment RPCs (`assign_role`, `pause_worker`, `resume_worker`,
  `revoke_worker`, `renew_coordinator_lease`) are implemented on the wire but
  have zero production call sites.  Every "role change" a user makes mutates
  only the user's own local `cluster.json`.
- Fencing tokens exist to prevent stale coordinator actions.  Their values must
  never appear in logs or UI (see No-Log Rules above).
- Do not weaken HMAC-based RPC authentication under any circumstances.

---

## Process Termination Safety

Source: `SEC-20260912-001-review.md` (full D7 review, 2026-09-12).

**Required property:** protected, foreign-user, inaccessible, stale, or unknown
processes must never be terminated.

**Controls that must remain in place:**

- Protected PID set includes PID 0, PID 1, the current process, and its ancestry.
- Protected names are centralized and case-folded.
- Windows domain prefixes are normalized before username comparison.
- Incomplete ancestry or lookup failures **fail closed** (deny).
- Create-time checks defend against PID reuse.
- Graceful and force actions use psutil abstraction and collect access errors.
- Child processes are rechecked before force termination.

Entry point chain: `maintenance/dialogs.py` `quit_selected()` →
`_run_shared_process_action` → `manager.request_quit` / `force_quit`.  Only
processes with matching `(pid, create_time)` and passing the protection/user
checks may be acted on.

`maintenance/actions.py` is the **only** place that quits processes or moves
files to Trash.  It rejects protected or foreign-user PIDs and
non-Downloads/symlink/non-file cleanup targets.

---

## Network and Firewall Rules

- **Do not weaken security/firewall configuration automatically.**
- **Do not disable firewalls globally.**
- **Do not open the entire LAN to arbitrary ports.**
- **Do not disable ProtonVPN permanently.**
- **Do not hardcode ProtonVPN-specific behavior into application logic.**
- Firewall exceptions for testing must be scoped to the minimum port and
  duration and reverted immediately after.

---

## Known Open Issues (not exploits — facts to weigh)

1. **Thread-safety bug**: `handle_elevation_request` calls
   `messagebox.askyesno` directly from a non-Tk server thread (unlike its sibling
   `handle_pairing_request`, which correctly marshals onto the Tk main thread).
   Source: `REMOTE_CLUSTER_TRUE_FLOW.md §43/51`.

2. **Pair button blocks Tk UI thread**: the initiator's Pair RPC runs inline on
   the UI thread for up to ~60 seconds.
   Source: `REMOTE_CLUSTER_TRUE_FLOW.md §10/43`.

3. **Manual-host pairing is unimplemented**: `add_manual_host` invents a local
   secret never transmitted to the target; no secret-entry UI exists.
   Source: `REMOTE_CLUSTER_TRUE_FLOW.md §37/51`.

4. **Pairing is not crash-safe (lost-response)**: a target may commit
   `pair_confirm` and lose the response before the initiator receives it.
   Status: **VERIFIED CURRENT**, not **VERIFIED COMPLETE**.
   Source: `REMOTE_CLUSTER_TRUE_FLOW.md` post-audit update.

---

## DO NOT say: "remote cluster is fully validated" merely because automated tests pass

Physical two-node testing is required.  See `docs/PLATFORM.md` and
`docs/PHYSICAL_TWO_NODE_ACCEPTANCE_2026-09-17.md`.

---

## DO NOT blindly delete user state

Before any operation that touches `cluster.json`, TLS keys, or node state,
identify what is there and confirm the operation is reversible or explicitly
approved.
