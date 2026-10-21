import argparse
import logging
import os
import platform
import sys
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

import algo
from maintenance import instance_lock
from maintenance._version import __version__
from maintenance.actions import ProcessManager
from maintenance.cluster import default_cluster_path
from maintenance.components.cluster_storage import CoordinatorTimeline, StandbyBuffer
from maintenance.connect_data_surfaces import register_cluster_data_surfaces
from maintenance.connect_grants import (
    ConnectSurfaceGrantPolicy,
    ConnectSurfaceGrantStore,
)
from maintenance.connect_provider import ConnectExpHostProvider
from maintenance.connect_runtime import (
    ConnectRuntimeAdapter,
    default_connect_profile_dir,
)
from maintenance.connect_surfaces import (
    register_demo_shared_capability,
    register_process_action_surface,
)
from maintenance.ui.qt_application import run_application
from maintenance.ui.qt_connect_harness import QtConnectHarnessWindow as AppWindow

LOG_DIR_NAME = "expra-connect-harness"
LOG_FILE_NAME = "expra-connect-harness.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

LOGGER = logging.getLogger(__name__)


def default_log_path(
    *,
    environment: dict[str, str] | None = None,
    home: Path | None = None,
    platform_name: str | None = None,
) -> Path:
    """Return the standard per-user log path for the active platform."""

    env = os.environ if environment is None else environment
    home_dir = Path.home() if home is None else home
    system = platform.system() if platform_name is None else platform_name

    if system == "Windows":
        base = env.get("LOCALAPPDATA") or env.get("APPDATA")
        base_dir = Path(base) if base else home_dir / "AppData" / "Local"
    elif system == "Darwin":
        base_dir = home_dir / "Library" / "Logs"
    else:
        base = env.get("XDG_STATE_HOME")
        base_dir = Path(base) if base else home_dir / ".local" / "state"
    return base_dir / LOG_DIR_NAME / LOG_FILE_NAME


def setup_logging() -> Path | None:
    """Configure per-user file logging and return the log path (or None).

    The log uses the platform's per-user state/log convention and is
    size-bounded so a disk-full situation cannot grow logs without limit. If
    the state directory is unavailable, logging falls back to stderr instead
    of failing the app.
    """

    path = default_log_path()
    log_dir = path.parent
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
    except OSError:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        return None

    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return path


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expra-connect-harness",
        description="Evaluate the Expra Connect runtime and cluster pipeline.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"expra-connect-harness {__version__}",
    )
    return parser


def _connect_adapter_factory(
    pairing_approval: Callable[[object], bool],
) -> ConnectRuntimeAdapter:
    """Compose the Exp host provider with one package-owned Connect runtime."""

    profile_dir = default_connect_profile_dir()
    return ConnectRuntimeAdapter(
        profile_dir=profile_dir,
        activation_path=profile_dir.parent / "activation.json",
        legacy_state_paths=(default_cluster_path(),),
        confirm_identity_transition=pairing_approval,
        on_pairing_request=pairing_approval,
        host_provider=ConnectExpHostProvider(algo.Analyzer()),
    )


def _register_connect_surfaces(
    adapter: ConnectRuntimeAdapter,
) -> ConnectSurfaceGrantPolicy:
    """Install host surfaces and return Exp's durable grant policy."""

    profile_state_dir = default_connect_profile_dir().parent
    register_process_action_surface(adapter, ProcessManager())
    register_demo_shared_capability(adapter)
    register_cluster_data_surfaces(
        adapter,
        timeline=CoordinatorTimeline(profile_state_dir / "cluster-history.sqlite3"),
        standby=StandbyBuffer(profile_state_dir / "cluster-standby.sqlite3"),
    )
    policy = ConnectSurfaceGrantPolicy(
        ConnectSurfaceGrantStore(profile_state_dir / "surface-approvals.json")
    )
    return policy


def main() -> int:
    _build_arg_parser().parse_args()

    lock_path = default_cluster_path().parent / "expra-connect-harness.lock"
    lock = instance_lock.acquire(lock_path)
    if lock is None:
        print("Expra Connect harness is already running.", file=sys.stderr)
        raise SystemExit(1)

    try:
        setup_logging()
        LOGGER.info("Expra Connect harness %s started", __version__)
        return run_application(
            sys.argv,
            lambda: AppWindow(
                _connect_adapter_factory,
                on_runtime_started=_register_connect_surfaces,
            ),
        )
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
