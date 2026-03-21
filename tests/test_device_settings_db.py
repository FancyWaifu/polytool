#!/usr/bin/env python3
"""Tests for device_settings_db zip database loader."""

import sys
import os
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import device_settings_db


class TestSettingsDbLoaded(unittest.TestCase):
    """Test that SETTINGS_DB loaded from DeviceSettings.zip."""

    def test_settings_db_has_entries(self):
        self.assertGreater(len(device_settings_db.SETTINGS_DB), 50)

    def test_settings_db_is_dict(self):
        self.assertIsInstance(device_settings_db.SETTINGS_DB, dict)


class TestPidProfilesLoaded(unittest.TestCase):
    """Test that PID_PROFILES loaded."""

    def test_pid_profiles_has_entries(self):
        self.assertGreater(len(device_settings_db.PID_PROFILES), 200)

    def test_pid_profiles_is_dict(self):
        self.assertIsInstance(device_settings_db.PID_PROFILES, dict)


class TestSpecificSettings(unittest.TestCase):
    """Test specific settings have correct names."""

    def test_0x90a_restore_defaults(self):
        entry = device_settings_db.SETTINGS_DB.get("0x90a")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["name"], "Restore Defaults")

    def test_0xb05_tone_control(self):
        entry = device_settings_db.SETTINGS_DB.get("0xb05")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["name"], "Tone Control")

    def test_0x60c_active_audio_tone(self):
        entry = device_settings_db.SETTINGS_DB.get("0x60c")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["name"], "Active Audio Tone")


class TestSettingChoices(unittest.TestCase):
    """Test that specific settings have expected choices."""

    def test_0x802_has_aimoderate(self):
        entry = device_settings_db.SETTINGS_DB.get("0x802")
        self.assertIsNotNone(entry)
        choices = entry.get("choices", [])
        self.assertTrue(
            any("aimoderate" in c for c in choices),
            f"0x802 choices {choices} missing 'aimoderate'",
        )

    def test_0x700_has_narrowband(self):
        entry = device_settings_db.SETTINGS_DB.get("0x700")
        self.assertIsNotNone(entry)
        choices = entry.get("choices", [])
        self.assertTrue(
            any("narrowBand" in c or "narrowband" in c for c in choices),
            f"0x700 choices {choices} missing 'narrowBand'",
        )


class TestNormalizeHex(unittest.TestCase):
    """Test _normalize_hex() function."""

    def test_with_0x_prefix(self):
        self.assertEqual(device_settings_db._normalize_hex("0xAB"), "0xab")

    def test_without_prefix(self):
        self.assertEqual(device_settings_db._normalize_hex("AB"), "0xab")

    def test_already_lowercase(self):
        self.assertEqual(device_settings_db._normalize_hex("0xab"), "0xab")

    def test_empty_string(self):
        self.assertEqual(device_settings_db._normalize_hex(""), "")

    def test_none_value(self):
        self.assertEqual(device_settings_db._normalize_hex(None), "")

    def test_with_whitespace(self):
        self.assertEqual(device_settings_db._normalize_hex("  0xAB  "), "0xab")


class TestHidMetadata(unittest.TestCase):
    """Test HID_METADATA loaded."""

    def test_hid_metadata_is_dict(self):
        self.assertIsInstance(device_settings_db.HID_METADATA, dict)


if __name__ == "__main__":
    unittest.main()
