#!/usr/bin/env python3
"""Tests for device identification in polytool.py."""

import sys
import os
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from polytool import (
    _normalize_version,
    DFU_EXECUTOR_MAP,
    CODENAME_MAP,
    PID_CODENAMES,
    PolyDevice,
    classify_device,
)


class TestNormalizeVersion(unittest.TestCase):
    """Test _normalize_version() with various input formats."""

    def test_underscore_format(self):
        # "0225_0_0" → 225
        self.assertEqual(_normalize_version("0225_0_0"), 225)

    def test_bcd_single_dot(self):
        # "2.25" → 225
        self.assertEqual(_normalize_version("2.25"), 225)

    def test_multi_component_cloud(self):
        # "3861.3039.100" → 3861
        self.assertEqual(_normalize_version("3861.3039.100"), 3861)

    def test_empty_string(self):
        # "" → 0 (falsy check returns 0)
        self.assertEqual(_normalize_version(""), 0)

    def test_zero_string(self):
        # "0" → 0
        self.assertEqual(_normalize_version("0"), 0)

    def test_raw_digits(self):
        # "3861" → 3861
        self.assertEqual(_normalize_version("3861"), 3861)

    def test_bcd_two_digit(self):
        # "38.61" → 3861
        self.assertEqual(_normalize_version("38.61"), 3861)


class TestDfuExecutorMap(unittest.TestCase):
    """Test DFU_EXECUTOR_MAP entries."""

    def test_c053_is_cxeeprom(self):
        self.assertEqual(DFU_EXECUTOR_MAP["c053"], "CxEepromDfu")

    def test_c054_is_cxeeprom(self):
        self.assertEqual(DFU_EXECUTOR_MAP["c054"], "CxEepromDfu")

    def test_c056_is_cxeeprom(self):
        self.assertEqual(DFU_EXECUTOR_MAP["c056"], "CxEepromDfu")

    def test_ac27_is_legacydfu(self):
        self.assertEqual(DFU_EXECUTOR_MAP["ac27"], "LegacyDfu")

    def test_15c_is_syncdfu(self):
        self.assertEqual(DFU_EXECUTOR_MAP["15c"], "SyncDfu")

    def test_4317_is_hidtidfu(self):
        self.assertEqual(DFU_EXECUTOR_MAP["4317"], "HidTiDfu")

    def test_17f_is_btneodfu(self):
        self.assertEqual(DFU_EXECUTOR_MAP["17f"], "btNeoDfu")


class TestCodenameMap(unittest.TestCase):
    """Test CODENAME_MAP lookups."""

    def test_nirvana(self):
        self.assertEqual(CODENAME_MAP["Nirvana"], "Voyager Focus 2")

    def test_sublime(self):
        self.assertEqual(CODENAME_MAP["Sublime"], "Voyager 4200 UC")

    def test_flamingo(self):
        self.assertEqual(CODENAME_MAP["Flamingo"], "Voyager Free 60")

    def test_salmon(self):
        self.assertEqual(CODENAME_MAP["Salmon"], "Blackwire 8225")

    def test_hydra(self):
        self.assertEqual(CODENAME_MAP["Hydra"], "Savi 7300/7400")


class TestPidCodenames(unittest.TestCase):
    """Test PID_CODENAMES for known PIDs."""

    def test_0x011a_is_nirvana(self):
        self.assertEqual(PID_CODENAMES[0x011A], "Nirvana")

    def test_0xac27_is_savi_7310(self):
        self.assertEqual(PID_CODENAMES[0xAC27], "Savi 7310")

    def test_0xac28_is_savi_7320(self):
        self.assertEqual(PID_CODENAMES[0xAC28], "Savi 7320")

    def test_0x015c_is_sync_20(self):
        self.assertEqual(PID_CODENAMES[0x015C], "Sync 20")

    def test_0x4304_is_blackwire_7225(self):
        self.assertEqual(PID_CODENAMES[0x4304], "Blackwire 7225")


class TestPolyDevice(unittest.TestCase):
    """Test PolyDevice dataclass."""

    def test_creation(self):
        dev = PolyDevice(vid=0x047F, pid=0xC056)
        self.assertEqual(dev.vid, 0x047F)
        self.assertEqual(dev.pid, 0xC056)

    def test_pid_hex(self):
        dev = PolyDevice(vid=0x047F, pid=0xC056)
        self.assertEqual(dev.pid_hex, "0xC056")

    def test_vid_hex(self):
        dev = PolyDevice(vid=0x047F, pid=0xC056)
        self.assertEqual(dev.vid_hex, "0x047F")

    def test_id_from_serial(self):
        dev = PolyDevice(vid=0x047F, pid=0xC056, serial="ABCDEF1234567890")
        self.assertEqual(dev.id, "ABCDEF12")

    def test_id_without_serial(self):
        dev = PolyDevice(vid=0x047F, pid=0xC056, serial="")
        self.assertEqual(dev.id, "047fc056")


class TestClassifyDevice(unittest.TestCase):
    """Test classify_device() populates fields correctly."""

    def test_populates_codename(self):
        dev = PolyDevice(vid=0x047F, pid=0xAC28)
        classify_device(dev)
        self.assertEqual(dev.codename, "Savi 7320")

    def test_populates_friendly_name(self):
        dev = PolyDevice(vid=0x047F, pid=0x011A)
        classify_device(dev)
        self.assertEqual(dev.friendly_name, "Voyager Focus 2")

    def test_populates_dfu_executor(self):
        dev = PolyDevice(vid=0x047F, pid=0xC056)
        classify_device(dev)
        self.assertEqual(dev.dfu_executor, "CxEepromDfu")

    def test_unknown_pid_no_codename(self):
        dev = PolyDevice(vid=0x047F, pid=0x0001)
        classify_device(dev)
        self.assertEqual(dev.codename, "")

    def test_populates_lens_product_id(self):
        dev = PolyDevice(vid=0x047F, pid=0xC056)
        classify_device(dev)
        self.assertEqual(dev.lens_product_id, "c056")


if __name__ == "__main__":
    unittest.main()
