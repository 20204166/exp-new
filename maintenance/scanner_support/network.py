"""Network observation and presentation support for the system scanner."""

from __future__ import annotations

from typing import Any

from maintenance.models import CapabilityState, ResourceSummary

from ._compat import scanner_module

# The mixin is completed by SystemScanner, which owns the shared scanner state
# and utility methods used by this responsibility.
# pyright: reportAttributeAccessIssue=false, reportUndefinedVariable=false, reportGeneralTypeIssues=false
# mypy: disable-error-code="attr-defined,misc,has-type,assignment,valid-type,name-defined"


class NetworkMixin:
    """Own network probing, rate calculation, and card presentation."""

    def _read_network_observation(self, psutil_module: Any) -> Any:
        """Read every network sensor once and carry it through one component scan.

        Global counters use ``_component_value`` (missing-psutil aware), while
        the per-interface counters and interface stats use the best-effort
        ``_psutil_value`` boundary. A ``None`` field means its query failed; an
        empty mapping means the query succeeded with no entries.
        """

        return scanner_module._NetworkObservation(
            global_counters=self._component_value(
                lambda: psutil_module.net_io_counters()
            ),
            per_interface_counters=scanner_module.SystemScanner._psutil_value(
                lambda: psutil_module.net_io_counters(pernic=True)
            ),
            interface_stats=scanner_module.SystemScanner._psutil_value(
                lambda: psutil_module.net_if_stats()
            ),
        )

    def _network_resource(
        self,
        observation: Any,
        capability: CapabilityState = CapabilityState.UNKNOWN,
    ) -> ResourceSummary:
        """Build the network card from one shared observation."""

        network = observation.global_counters
        received = network.bytes_recv
        sent = network.bytes_sent
        down_rate, up_rate = self._sample_network_rates(network)
        up_interfaces = self._up_interface_names(observation.interface_stats)
        interface = self._active_interface_from_counters(
            observation.per_interface_counters, up_interfaces
        )
        vpn_interface = self._vpn_interface_from_names(up_interfaces)

        if down_rate is not None and up_rate is not None:
            down_text, up_text = self._rate_pair_text(down_rate, up_rate)
            value = f"↓ {down_text}  ↑ {up_text}"
        else:
            down_text = up_text = "—"
            value = "—"

        if interface:
            subtitle = f"Active: {interface}"
            interface_detail = interface
        elif up_interfaces == set():
            subtitle = "Disconnected"
            interface_detail = "none"
        else:
            subtitle = "No active interface"
            interface_detail = "Unknown"

        return ResourceSummary(
            key="network",
            title="Network",
            value=value,
            subtitle=subtitle,
            percent=None,
            details=(
                f"Download rate: {down_text}",
                f"Upload rate: {up_text}",
                f"Received this boot: {self.format_bytes(received)}",
                f"Sent this boot: {self.format_bytes(sent)}",
                f"Active interface: {interface_detail}",
                f"VPN: {'Connected' if vpn_interface else 'Not detected'}",
            ),
            capability=capability,
        )

    def _network_capability(self, observation: Any) -> CapabilityState:
        """Classify network capability from authoritative probe outcomes.

        Global counters succeeding means the network capability exists (a
        disconnected or down adapter is still a supported card). Absence is
        only claimed when at least one authoritative inventory (per-interface
        counters or interface stats) succeeds and neither shows a non-loopback
        adapter. If every detail probe fails, the card is treated as
        ``SUPPORTED`` (it exists) rather than absent.
        """

        if observation.global_counters is None:
            return CapabilityState.UNKNOWN
        pernic = observation.per_interface_counters
        stats = observation.interface_stats
        if pernic and any(
            not scanner_module.SystemScanner._is_loopback_interface(name)
            for name in pernic
        ):
            return CapabilityState.SUPPORTED
        if stats and any(
            not scanner_module.SystemScanner._is_loopback_interface(name)
            for name in stats
        ):
            return CapabilityState.SUPPORTED
        if pernic is None and stats is None:
            return CapabilityState.SUPPORTED
        return CapabilityState.UNSUPPORTED

    @staticmethod
    def _rate_pair_text(down_rate: float, up_rate: float) -> tuple[str, str]:
        """Return (download, upload) rate text in one shared unit.

        Both directions use the unit of the larger rate so the two values stay
        even on the card and read as a single line.
        """

        largest = max(down_rate, up_rate)
        scale, unit = scanner_module.SystemScanner._byte_unit(largest)
        return (
            f"{down_rate / scale:.2f} {unit}/s",
            f"{up_rate / scale:.2f} {unit}/s",
        )

    def _sample_network_rates(self, counters: Any) -> tuple[float | None, float | None]:
        """Return (download, upload) rates in bytes/sec from counter deltas.

        The first sample has no delta, so it returns ``(None, None)``. Counter
        resets (a smaller reading than the previous one) are clamped to zero
        rather than reported as a negative spurious rate.
        """

        now = scanner_module.time.monotonic()
        sent = counters.bytes_sent
        received = counters.bytes_recv
        with self._network_sample_lock:
            previous = self._network_sample
            self._network_sample = (now, sent, received)
        if previous is None:
            return None, None
        previous_now, previous_sent, previous_received = previous
        elapsed = now - previous_now
        if elapsed <= 0:
            return None, None
        received_delta = max(received - previous_received, 0)
        sent_delta = max(sent - previous_sent, 0)
        return received_delta / elapsed, sent_delta / elapsed

    def _reset_network_sample(self) -> None:
        """Clear the persistent network-rate baseline.

        Used when network polling resumes after a pause so the first resumed
        sample shows ``—`` instead of an average across the whole paused
        interval presented as a current rate.
        """

        with self._network_sample_lock:
            self._network_sample = None

    @staticmethod
    def _is_loopback_interface(name: str) -> bool:
        """Return whether an interface name is the loopback adapter."""

        return name.casefold().startswith("lo")

    @staticmethod
    def _up_interface_names(interface_stats: Any) -> set[str] | None:
        """Return the names of interfaces reported up, or None when unknown.

        ``None`` means the up/down table could not be read; an empty set means
        the table was read and no interface is up.
        """

        if interface_stats is None:
            return None
        return {
            name
            for name, stats in interface_stats.items()
            if getattr(stats, "isup", False)
        }

    @staticmethod
    def _up_interfaces(psutil_module: Any) -> set[str] | None:
        """Return the names of interfaces reported up, or None when unknown.

        Reads the up/down table once via the best-effort boundary, then defers
        to ``_up_interface_names`` so the one-read behaviour is shared with the
        observation-based network card path.
        """

        interface_stats = scanner_module.SystemScanner._psutil_value(
            lambda: psutil_module.net_if_stats()
        )
        return scanner_module.SystemScanner._up_interface_names(interface_stats)

    @staticmethod
    def _active_interface_from_counters(
        per_interface_counters: Any,
        up_interfaces: set[str] | None = None,
    ) -> str | None:
        """Return the busiest up, non-loopback interface, or None.

        Operates purely on already-read per-interface counters and the up/down
        name set so the network card reads each sensor exactly once. ``None``
        ``up_interfaces`` means the up/down table was not read, matching the
        best-effort default of not failing when the table is unavailable.
        """

        if not per_interface_counters:
            return None
        candidates = {
            name: getattr(counters, "bytes_recv", 0)
            + getattr(counters, "bytes_sent", 0)
            for name, counters in per_interface_counters.items()
            if not scanner_module.SystemScanner._is_loopback_interface(name)
            and (up_interfaces is None or name in up_interfaces)
        }
        return (
            max(candidates, key=lambda name: candidates[name]) if candidates else None
        )

    @staticmethod
    def _active_interface(
        psutil_module: Any,
        *,
        up_interfaces: set[str] | None = None,
    ) -> str | None:
        """Return the busiest up, non-loopback interface, or None.

        Interface metadata is best-effort: when per-interface counters or the
        up/down table are unavailable, the card simply omits the interface
        name instead of failing. ``up_interfaces`` may be passed in by the
        caller (already read once) to avoid re-reading the table; ``None``
        means the table was not read here, matching the default behaviour.
        """

        per_nic = scanner_module.SystemScanner._psutil_value(
            lambda: psutil_module.net_io_counters(pernic=True)
        )
        if up_interfaces is None:
            up_interfaces = scanner_module.SystemScanner._up_interfaces(psutil_module)
        return scanner_module.SystemScanner._active_interface_from_counters(
            per_nic, up_interfaces
        )

    @staticmethod
    def _vpn_interface_from_names(up_interfaces: set[str] | None) -> str | None:
        """Return an up tunnel/VPN interface name from an up-interface name set.

        Detection is a conservative, generic device-type heuristic (tun, tap,
        utun, ppp, ipsec, wg) over the interface up/down name set; it carries
        no provider-specific assumptions. Shared by the standalone probe and
        the observation-based network card path.
        """

        if not up_interfaces:
            return None
        prefixes = tuple(scanner_module.SystemScanner.TUNNEL_INTERFACE_PREFIXES)
        for name in up_interfaces:
            folded = name.casefold()
            if any(
                folded == prefix or folded.startswith(prefix) for prefix in prefixes
            ):
                return name
        return None

    @classmethod
    def _vpn_interface(
        cls,
        psutil_module: Any,
        *,
        up_interfaces: set[str] | None = None,
    ) -> str | None:
        """Return an up tunnel/VPN interface name, or None.

        ``up_interfaces`` may be passed in by the caller (already read once)
        to avoid re-reading the table; ``None`` means the table was not read
        here, so it is read once from ``psutil_module``.
        """

        if up_interfaces is None:
            up_interfaces = cls._up_interfaces(psutil_module)
        return cls._vpn_interface_from_names(up_interfaces)
