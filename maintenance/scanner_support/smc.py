"""Read Apple SMC temperature keys on Intel Macs via IOKit/ctypes.

``read_smc_temperatures`` is the single entry point: it opens the Apple SMC
service, reads a fixed set of well-known temperature keys, and returns only
plausible Celsius readings grouped by component. Every failure fails closed to
an empty result so the thermals UI degrades to no-data instead of erroring.

All IOKit interaction is lazy and Darwin-only; the module imports cleanly on
Linux and Windows (the structs are just ctypes layouts), so the app and its
tests never touch macOS frameworks elsewhere.
"""

from __future__ import annotations

import ctypes
import math
import struct
import sys
from collections.abc import Callable
from typing import Any

_IOKIT_PATH = "/System/Library/Frameworks/IOKit.framework/IOKit"
_LIB_SYSTEM_PATH = "/usr/lib/libSystem.dylib"

KERNEL_INDEX_SMC = 2
SMC_CMD_READ_BYTES = 5

# Component -> ordered candidate SMC temperature keys. The first key that
# yields a plausible reading wins; Apple Silicon returns errors for these
# temperature keys and is therefore handled as no-data.
SMC_COMPONENT_KEYS: dict[str, tuple[str, ...]] = {
    "cpu": ("TC0P", "TC0C", "TC0D", "TC0E", "TC0F", "TC0H"),
    "gpu": ("TG0P", "TG0D", "TG1P", "TG1D"),
    "storage": ("TH0P", "TH1P", "TN0P", "TN1P"),
    "battery": ("TB0T", "TB1T", "Tp0P"),
}

# Per-component labels mirroring the Linux psutil path in dashboard.py.
COMPONENT_LABELS: dict[str, str] = {
    "cpu": "CPU",
    "gpu": "GPU",
    "storage": "NVMe",
    "battery": "Battery",
}


class _SMCKeyDataVer(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_uint8),
        ("minor", ctypes.c_uint8),
        ("build", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8),
        ("release", ctypes.c_uint16),
    ]


class _SMCKeyDataPLimitData(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint16),
        ("length", ctypes.c_uint16),
        ("cpuPLimit", ctypes.c_uint32),
        ("gpuPLimit", ctypes.c_uint32),
        ("memPLimit", ctypes.c_uint32),
    ]


class _SMCKeyDataKeyInfo(ctypes.Structure):
    _fields_ = [
        ("dataSize", ctypes.c_uint32),
        ("dataType", ctypes.c_uint32),
        ("dataAttributes", ctypes.c_uint8),
        ("group", ctypes.c_uint8),
        ("attributes", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8),
    ]


class _SMCKeyData(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_uint32),
        ("vers", _SMCKeyDataVer),
        ("pLimitData", _SMCKeyDataPLimitData),
        ("keyInfo", _SMCKeyDataKeyInfo),
        ("padding", ctypes.c_char * 8),
        ("result", ctypes.c_uint8),
        ("status", ctypes.c_char),
        ("data8", ctypes.c_uint8),
        ("data16", ctypes.c_uint16),
        ("data32", ctypes.c_uint32),
        ("bytes", ctypes.c_ubyte * 32),
    ]


def _signed_fixed(data: bytes, fraction_bits: int) -> float:
    """Decode an SMC signed fixed-point value (``sp78``, ``sp5a``)."""

    if len(data) < 2:
        return 0.0
    raw = struct.unpack(">h", data[:2])[0]
    return raw / (1 << fraction_bits)


def decode_value(data_type: str, data: bytes) -> float | int | None:
    """Decode raw SMC bytes according to their four-character data type."""

    if not data:
        return None
    if data_type == "sp78":
        return _signed_fixed(data, 8)
    if data_type == "sp5a":
        return _signed_fixed(data, 10)
    if data_type == "flt":
        return struct.unpack(">f", data[:4])[0]
    if data_type == "ui8":
        return data[0]
    if data_type == "ui16":
        return struct.unpack(">H", data[:2])[0]
    if data_type == "si16":
        return struct.unpack(">h", data[:2])[0]
    if data_type in ("ui32", "u32f"):
        return struct.unpack(">I", data[:4])[0]
    if data_type in ("si32", "s32f"):
        return struct.unpack(">i", data[:4])[0]
    return None


def _data_type_text(value: int) -> str:
    return struct.pack(">I", value).decode("ascii", errors="replace")


def _encode_key(key_text: str) -> int:
    return struct.unpack(">I", key_text.encode("ascii"))[0]


def _plausible_celsius(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        numeric_value = float(value)
    except OverflowError:
        return False
    return math.isfinite(numeric_value) and 0 < numeric_value < 250


def _load_io() -> Any | None:
    """Load and configure the IOKit functions used by the SMC reader."""

    if sys.platform != "darwin":
        return None
    try:
        io = ctypes.CDLL(_IOKIT_PATH, use_errno=True)
    except OSError:
        return None
    io.IOServiceGetMatchingService.restype = ctypes.c_uint32
    io.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    io.IOServiceMatching.restype = ctypes.c_void_p
    io.IOServiceMatching.argtypes = [ctypes.c_char_p]
    io.IOServiceOpen.restype = ctypes.c_int
    io.IOServiceOpen.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    io.IOConnectCallStructMethod.restype = ctypes.c_int
    io.IOConnectCallStructMethod.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    io.IOServiceClose.argtypes = [ctypes.c_uint32]
    io.IOObjectRelease.argtypes = [ctypes.c_uint32]
    return io


def _mach_task_self() -> int:
    library = ctypes.CDLL(_LIB_SYSTEM_PATH, use_errno=True)
    function = library.mach_task_self
    function.restype = ctypes.c_uint32
    function.argtypes = []
    return int(function())


def _open_smc(io: Any) -> int | None:
    """Open the Apple SMC service and return its connection id (or ``None``)."""

    try:
        task = _mach_task_self()
        service = io.IOServiceGetMatchingService(0, io.IOServiceMatching(b"AppleSMC"))
    except (AttributeError, OSError):
        return None
    if not service:
        return None
    connection = ctypes.c_uint32()
    try:
        result = io.IOServiceOpen(service, task, 0, ctypes.byref(connection))
    finally:
        io.IOObjectRelease(service)
    if result != 0:
        return None
    return connection.value


def _read_key(io: Any, connection: int, key_text: str) -> tuple[str, bytes] | None:
    """Read one SMC key, returning its data type code and raw bytes."""

    input_data = _SMCKeyData()
    input_data.key = _encode_key(key_text)
    input_data.data8 = SMC_CMD_READ_BYTES
    output_data = _SMCKeyData()
    output_data.data8 = SMC_CMD_READ_BYTES
    output_size = ctypes.c_size_t(ctypes.sizeof(_SMCKeyData))
    result = io.IOConnectCallStructMethod(
        connection,
        KERNEL_INDEX_SMC,
        ctypes.byref(input_data),
        ctypes.sizeof(input_data),
        ctypes.byref(output_data),
        ctypes.byref(output_size),
    )
    if result != 0:
        return None
    data_size = output_data.keyInfo.dataSize
    if data_size == 0 or data_size > 32:
        return None
    return _data_type_text(output_data.keyInfo.dataType), bytes(
        output_data.bytes[:data_size]
    )


def read_smc_temperatures(
    is_valid: Callable[[object], bool] = _plausible_celsius,
) -> dict[str, list[tuple[str, str, float]]]:
    """Return plausible SMC temperature readings per component.

    Returns ``{component: [(sensor_id, sensor_name, celsius), ...]}`` with at
    most one reading per component (the first key that reads plausibly), or an
    empty dict when the SMC is unavailable (Linux/Windows, Apple Silicon, or
    IOKit failure). ``is_valid`` lets callers reuse the app's canonical
    temperature validation.
    """

    io = _load_io()
    if io is None:
        return {}
    connection = _open_smc(io)
    if connection is None:
        return {}
    result: dict[str, list[tuple[str, str, float]]] = {}
    try:
        for component, keys in SMC_COMPONENT_KEYS.items():
            for key_text in keys:
                reading = _read_key(io, connection, key_text)
                if reading is None:
                    continue
                data_type, data = reading
                value = decode_value(data_type, data)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not is_valid(value)
                ):
                    continue
                result[component] = [(f"smc:{key_text}", key_text, float(value))]
                break
    finally:
        io.IOServiceClose(connection)
    return result


__all__ = [
    "COMPONENT_LABELS",
    "SMC_COMPONENT_KEYS",
    "decode_value",
    "read_smc_temperatures",
]
