"""Unit tests for the Darwin SMC temperature reader (decode logic and gating)."""

import struct
import unittest
from unittest.mock import patch

from maintenance.scanner_support.smc import (
    SMC_COMPONENT_KEYS,
    _data_type_text,
    _encode_key,
    _plausible_celsius,
    decode_value,
    read_smc_temperatures,
)


def _sp78(celsius: float) -> bytes:
    return struct.pack(">h", round(celsius * 256))


class SmcDecodeTests(unittest.TestCase):
    def test_decode_sp78_fixed_point(self) -> None:
        self.assertEqual(decode_value("sp78", _sp78(61.5)), 61.5)
        self.assertEqual(decode_value("sp78", _sp78(-5.0)), -5.0)

    def test_decode_sp5a_fixed_point(self) -> None:
        self.assertEqual(
            decode_value("sp5a", struct.pack(">h", round(30.0 * 1024))), 30.0
        )

    def test_decode_float_and_integer_types(self) -> None:
        self.assertEqual(decode_value("flt", struct.pack(">f", 45.25)), 45.25)
        self.assertEqual(decode_value("ui8", b"\x2d"), 45)
        self.assertEqual(decode_value("ui16", struct.pack(">H", 1000)), 1000)
        self.assertEqual(decode_value("si16", struct.pack(">h", -4)), -4)
        self.assertEqual(decode_value("ui32", struct.pack(">I", 123456)), 123456)

    def test_decode_unknown_type_and_empty_returns_none(self) -> None:
        self.assertIsNone(decode_value("????", b"\x01\x02"))
        self.assertIsNone(decode_value("sp78", b""))

    def test_data_type_text_and_key_encoding_round_trip(self) -> None:
        self.assertEqual(_data_type_text(0x73703738), "sp78")
        self.assertEqual(struct.pack(">I", _encode_key("TC0P")), b"TC0P")

    def test_plausible_celsius_bounds(self) -> None:
        self.assertTrue(_plausible_celsius(45.0))
        self.assertTrue(_plausible_celsius(1))
        self.assertFalse(_plausible_celsius(0))
        self.assertFalse(_plausible_celsius(300))
        self.assertFalse(_plausible_celsius(float("nan")))
        self.assertFalse(_plausible_celsius("45"))


class SmcGatingTests(unittest.TestCase):
    def test_reads_nothing_off_darwin(self) -> None:
        with patch("maintenance.scanner_support.smc.sys.platform", "linux"):
            self.assertEqual(read_smc_temperatures(), {})

    def test_component_keys_cover_expected_components(self) -> None:
        self.assertEqual(
            set(SMC_COMPONENT_KEYS),
            {"cpu", "gpu", "storage", "battery"},
        )
        for keys in SMC_COMPONENT_KEYS.values():
            self.assertTrue(keys)
            for key in keys:
                self.assertEqual(len(key), 4)


if __name__ == "__main__":
    unittest.main()
