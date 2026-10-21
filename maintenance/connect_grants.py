"""Durable, host-approved policy for Exp-owned Connect action surfaces."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from expra_connect import NodeId, RelationshipState
from expra_connect.surfaces.protocol import MAX_SURFACE_ID_LENGTH
from expra_connect.surfaces.registry import SurfaceAccess

from maintenance.persistence import atomic_write_text

LOGGER = logging.getLogger(__name__)
_SCHEMA_VERSION = 2
_LEGACY_SCHEMA_VERSION = 1
_LEGACY_SURFACES = {
    "process_termination": "exp.process-termination",
    "process_force_termination": "exp.process-force-termination",
    "cluster_snapshot_receive": "exp.cluster-history",
    "cluster_standby_receive": "exp.cluster-standby-history",
}


class _SurfaceGrantAdapter(Protocol):
    @property
    def surface_definitions(self) -> Mapping[str, Any]: ...

    def peer_relationship(self, peer_id: NodeId) -> RelationshipState: ...

    def grant_surface_access(
        self, peer_id: NodeId, surface_id: str, *, access: SurfaceAccess
    ) -> Any: ...

    def revoke_surface_access(
        self,
        peer_id: NodeId,
        surface_id: str,
        *,
        access: SurfaceAccess | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SurfaceGrantApproval:
    """An explicit Exp approval, separate from Connect trust and runtime grants."""

    peer_id: NodeId
    surface_id: str
    access: SurfaceAccess

    def __post_init__(self) -> None:
        if not isinstance(self.peer_id, NodeId):
            raise TypeError("surface approval peer must be a NodeId")
        if not isinstance(self.surface_id, str):
            raise TypeError("surface approval id must be a string")
        if not self.surface_id.strip() or len(self.surface_id) > MAX_SURFACE_ID_LENGTH:
            raise ValueError("surface approval id is invalid")
        if not isinstance(self.access, SurfaceAccess):
            raise TypeError("surface approval access must be a SurfaceAccess")


class ConnectSurfaceGrantStore:
    """Persist explicit per-peer app-surface approvals, separate from Pair trust."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> frozenset[SurfaceGrantApproval]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return frozenset()
        except (OSError, UnicodeError) as error:
            LOGGER.warning("Could not read Connect surface policy")
            raise RuntimeError("Connect surface policy is unreadable") from error

        try:
            document = json.loads(text)
            if not isinstance(document, dict) or set(document) != {
                "schema_version",
                "grants",
            }:
                raise ValueError("surface policy document is invalid")
            schema_version = document["schema_version"]
            entries = document["grants"]
            if type(schema_version) is not int:
                raise TypeError("surface policy schema version is invalid")
            if not isinstance(entries, list):
                raise TypeError("surface policy grants must be a list")
            if schema_version == _LEGACY_SCHEMA_VERSION:
                return self._load_v1(entries)
            if schema_version == _SCHEMA_VERSION:
                return self._load_v2(entries)
            raise ValueError("surface policy schema is unsupported")
        except (json.JSONDecodeError, TypeError, KeyError) as error:
            raise ValueError("Connect surface policy is malformed") from error

    @staticmethod
    def _load_v1(entries: list[object]) -> frozenset[SurfaceGrantApproval]:
        approvals: set[SurfaceGrantApproval] = set()
        seen_peers: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"peer_id", "permissions"}:
                raise ValueError("legacy surface policy entry is invalid")
            peer_id = entry["peer_id"]
            permissions = entry["permissions"]
            if (
                not isinstance(peer_id, str)
                or not isinstance(permissions, list)
                or peer_id in seen_peers
                or any(
                    not isinstance(permission, str)
                    or permission not in _LEGACY_SURFACES
                    for permission in permissions
                )
                or len(set(permissions)) != len(permissions)
            ):
                raise ValueError("legacy surface policy entry is invalid")
            seen_peers.add(peer_id)
            node_id = NodeId(peer_id)
            approvals.update(
                SurfaceGrantApproval(
                    node_id, _LEGACY_SURFACES[permission], SurfaceAccess.ACTION
                )
                for permission in permissions
            )
        return frozenset(approvals)

    @staticmethod
    def _load_v2(entries: list[object]) -> frozenset[SurfaceGrantApproval]:
        approvals: set[SurfaceGrantApproval] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "peer_id",
                "surface_id",
                "access",
            }:
                raise ValueError("surface policy entry is invalid")
            peer_id, surface_id, access = (
                entry["peer_id"],
                entry["surface_id"],
                entry["access"],
            )
            if not isinstance(peer_id, str) or not isinstance(access, str):
                raise TypeError("surface policy peer and access must be strings")
            approval = SurfaceGrantApproval(
                NodeId(peer_id), surface_id, SurfaceAccess(access)
            )
            if approval in approvals:
                raise ValueError("duplicate surface policy entry")
            approvals.add(approval)
        return frozenset(approvals)

    def save(self, approvals: Iterable[SurfaceGrantApproval]) -> None:
        # Never turn a read/parse failure into an implicit policy reset.
        self.load()
        entries: list[dict[str, str]] = []
        seen: set[SurfaceGrantApproval] = set()
        try:
            for approval in approvals:
                if not isinstance(approval, SurfaceGrantApproval) or approval in seen:
                    raise ValueError("Connect surface approval is invalid")
                seen.add(approval)
                entries.append(
                    {
                        "peer_id": approval.peer_id.value,
                        "surface_id": approval.surface_id,
                        "access": approval.access.value,
                    }
                )
        except TypeError as error:
            raise ValueError("Connect surface approvals are invalid") from error
        entries.sort(
            key=lambda item: (item["peer_id"], item["surface_id"], item["access"])
        )
        payload = (
            json.dumps(
                {"schema_version": _SCHEMA_VERSION, "grants": entries},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        atomic_write_text(
            self.path,
            payload,
            temp_prefix=".connect-surface-grants-",
            create_directory_message="Could not create Connect policy directory",
            save_message="Could not save Connect surface policy",
            save_error_factory=RuntimeError,
            fsync_warning_template="Could not sync Connect policy directory %s",
            logger=LOGGER,
        )


class ConnectSurfaceGrantPolicy:
    """Reconcile durable host approvals with Connect's ephemeral grants."""

    def __init__(self, store: ConnectSurfaceGrantStore) -> None:
        self.store = store
        self._desired_approvals: frozenset[SurfaceGrantApproval] = frozenset()
        self._applied_approvals: frozenset[SurfaceGrantApproval] = frozenset()

    @property
    def desired_approvals(self) -> frozenset[SurfaceGrantApproval]:
        return self._desired_approvals

    @property
    def applied_approvals(self) -> frozenset[SurfaceGrantApproval]:
        return self._applied_approvals

    def reapply(self, adapter: _SurfaceGrantAdapter) -> frozenset[SurfaceGrantApproval]:
        try:
            desired = self.store.load()
        except Exception as error:
            self._desired_approvals = frozenset()
            try:
                self.clear_runtime(adapter)
            except Exception as clear_error:
                raise RuntimeError(
                    "policy is unreadable and runtime grants could not be cleared"
                ) from clear_error
            raise RuntimeError("surface approval policy is unavailable") from error
        self._desired_approvals = desired
        eligible = frozenset(
            approval for approval in desired if self._is_eligible(adapter, approval)
        )
        for approval in self._applied_approvals - eligible:
            self._revoke_runtime(adapter, approval)
            self._applied_approvals -= {approval}
        for approval in sorted(
            eligible - self._applied_approvals,
            key=_approval_sort_key,
        ):
            self._grant_runtime(adapter, approval)
            self._applied_approvals |= {approval}
        return self._applied_approvals

    def grant(
        self, adapter: _SurfaceGrantAdapter, approval: SurfaceGrantApproval
    ) -> None:
        self._require_eligible(adapter, approval)
        existing = self.store.load()
        if approval in existing:
            self._desired_approvals = existing
            if approval not in self._applied_approvals:
                self._grant_runtime(adapter, approval)
                self._applied_approvals |= {approval}
            return

        self._grant_runtime(adapter, approval)
        try:
            updated = existing | {approval}
            self.store.save(updated)
        except Exception as save_error:
            try:
                self._revoke_runtime(adapter, approval)
            except Exception as revoke_error:
                self._applied_approvals |= {approval}
                raise RuntimeError(
                    "approval save failed and runtime grant compensation failed"
                ) from revoke_error
            raise RuntimeError("approval could not be saved") from save_error
        self._desired_approvals = updated
        self._applied_approvals |= {approval}

    def revoke(
        self, adapter: _SurfaceGrantAdapter, approval: SurfaceGrantApproval
    ) -> None:
        existing = self.store.load()
        if approval not in existing:
            self._desired_approvals = existing
            if approval in self._applied_approvals:
                self._revoke_runtime(adapter, approval)
                self._applied_approvals -= {approval}
            return
        updated = existing - {approval}
        self.store.save(updated)
        self._desired_approvals = updated
        try:
            self._revoke_runtime(adapter, approval)
        except Exception as error:
            raise RuntimeError(
                "approval was removed from policy but runtime revocation failed"
            ) from error
        self._applied_approvals -= {approval}

    def revoke_peer(self, adapter: _SurfaceGrantAdapter, peer_id: NodeId) -> None:
        existing = self.store.load()
        removed = frozenset(item for item in existing if item.peer_id == peer_id)
        updated = existing - removed
        self.store.save(updated)
        self._desired_approvals = updated
        failures: list[Exception] = []
        for approval in removed & self._applied_approvals:
            try:
                self._revoke_runtime(adapter, approval)
            except Exception as error:  # noqa: BLE001 -- Complete revocation pass.
                failures.append(error)
            else:
                self._applied_approvals -= {approval}
        if failures:
            raise RuntimeError(
                "peer approvals were removed from policy but runtime revocation failed"
            ) from failures[0]

    def clear_runtime(self, adapter: _SurfaceGrantAdapter) -> None:
        failures: list[Exception] = []
        for approval in self._applied_approvals:
            try:
                self._revoke_runtime(adapter, approval)
            except Exception as error:  # noqa: BLE001 -- Clear all tracked grants.
                failures.append(error)
            else:
                self._applied_approvals -= {approval}
        if failures:
            raise RuntimeError(
                "one or more runtime approvals could not be cleared"
            ) from failures[0]

    @staticmethod
    def _is_eligible(
        adapter: _SurfaceGrantAdapter, approval: SurfaceGrantApproval
    ) -> bool:
        try:
            ConnectSurfaceGrantPolicy._require_eligible(adapter, approval)
        except (PermissionError, ValueError, KeyError, AttributeError):
            return False
        return True

    @staticmethod
    def _require_eligible(
        adapter: _SurfaceGrantAdapter, approval: SurfaceGrantApproval
    ) -> None:
        relationship = adapter.peer_relationship(approval.peer_id)
        if relationship not in {
            RelationshipState.INBOUND_GRANTED,
            RelationshipState.BIDIRECTIONAL,
        }:
            raise PermissionError("peer has not granted inbound access")
        definitions = adapter.surface_definitions
        definition = definitions.get(approval.surface_id)
        if definition is None:
            raise ValueError("surface is not registered")
        if approval.access is SurfaceAccess.READ:
            if callable(definition.read):
                return
        elif approval.access is SurfaceAccess.REVIEW:
            if callable(definition.review):
                return
        elif definition.actions:
            return
        raise ValueError("surface does not support the approved access mode")

    @staticmethod
    def _grant_runtime(
        adapter: _SurfaceGrantAdapter, approval: SurfaceGrantApproval
    ) -> None:
        adapter.grant_surface_access(
            approval.peer_id, approval.surface_id, access=approval.access
        )

    @staticmethod
    def _revoke_runtime(
        adapter: _SurfaceGrantAdapter, approval: SurfaceGrantApproval
    ) -> None:
        adapter.revoke_surface_access(
            approval.peer_id, approval.surface_id, access=approval.access
        )


def _approval_sort_key(
    approval: SurfaceGrantApproval,
) -> tuple[str, str, str]:
    return approval.peer_id.value, approval.surface_id, approval.access.value
