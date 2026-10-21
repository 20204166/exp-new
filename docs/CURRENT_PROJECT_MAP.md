# Current Project Map

**Status: current source map.** This describes the repository as it exists in
the Qt harness checkout. Verify implementation details in source; old plans,
audits, and bug-hunt records are not current behavior evidence.

## Product boundary

`main.py` starts a small Qt harness for evaluating Expra Connect. It is not the
former full system-monitoring desktop application. Connect owns peer identity,
discovery, pairing, authenticated transport, and membership. Automated tests do
not establish physical two-node or LAN acceptance.

## Ownership map

- `maintenance/connect_runtime.py` adapts lifecycle and the separate Connect
  profile; it does not copy legacy identity or trust state into that profile.
- `maintenance/connect_provider.py` adapts the Connect-authenticated host
  provider contract to Exp's scanner data models.
- `maintenance/connect_surfaces.py` registers host process actions. The final
  process-safety authority remains `maintenance/actions.py`.
- `maintenance/connect_data_surfaces.py` validates Connect-authorized history
  input and delegates to bounded stores in
  `maintenance/components/cluster_storage.py`.
- `maintenance/scanner.py`, `maintenance/scanner_support/`, and selected
  `maintenance/components/` modules provide host-side scans, safety policy,
  telemetry, and compatibility data models still required by the harness.
- `maintenance/ui/` contains Qt presentation, pairing/identity-transition
  dialogs, styles, and Qt-thread delivery. It must not become a second owner of
  Connect trust, cluster state, or scanner logic.
- `maintenance/cluster.py` retains legacy state codecs and path compatibility;
  it is not the authority for Connect membership.

## Documentation status

- At the user's request, historical prose uses “legacy Exp application” in
  place of the former product name. Exact command/configuration literals in
  fenced examples and real API identifiers are retained; prose/table wording
  in old evidence should not be treated as verbatim captured output.
- `docs/REMOTE_CLUSTER_TRUE_FLOW.md` and
  `docs/SYSTEM_ARCHITECTURE_REVIEW.md` are historical audits of earlier
  implementations. Their current-state claims are superseded by this map and
  the source.
- `docs/SECURITY.md` and `docs/PLATFORM.md` retain durable safety constraints;
  implementation-specific examples and machine details require revalidation.
- Other dated plans, audits, reviews, and bug hunts are preserved as historical
  evidence, not rewritten to imply that planned work shipped.
