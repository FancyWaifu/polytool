#!/usr/bin/env python3
"""Tests for lens_settings and device_settings_db settings resolution."""

import sys
import os
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lens_settings
import device_settings_db


class TestGetDeviceFamily(unittest.TestCase):
    """Test lens_settings.get_device_family() returns correct family."""

    def test_cx2070x_usage_page(self):
        self.assertEqual(lens_settings.get_device_family(0xFFA0), "cx2070x")

    def test_dect_usage_page(self):
        self.assertEqual(lens_settings.get_device_family(0xFFA2), "dect")

    def test_bladerunner_hidtidfu(self):
        self.assertEqual(lens_settings.get_device_family(0x0000, "HidTiDfu"), "bladerunner")

    def test_bladerunner_syncdfu(self):
        self.assertEqual(lens_settings.get_device_family(0x0000, "SyncDfu"), "bladerunner")

    def test_bladerunner_studiodfu(self):
        self.assertEqual(lens_settings.get_device_family(0x0000, "StudioDfu"), "bladerunner")

    def test_voyager_bt(self):
        self.assertEqual(lens_settings.get_device_family(0x0000, "btNeoDfu"), "voyager_bt")

    def test_voyager_base_pid(self):
        self.assertEqual(lens_settings.get_device_family(0x0000, "", pid=0x02EA), "voyager_base")

    def test_default_is_dect(self):
        self.assertEqual(lens_settings.get_device_family(0x0000, ""), "dect")


class TestGetSettingsForDevice(unittest.TestCase):
    """Test lens_settings.get_settings_for_device() returns correct profiles."""

    def test_returns_list(self):
        result = lens_settings.get_settings_for_device(0xFFA0)
        self.assertIsInstance(result, list)

    def test_cx2070x_has_settings(self):
        result = lens_settings.get_settings_for_device(0xFFA0)
        self.assertTrue(len(result) > 0)

    def test_settings_have_required_keys(self):
        result = lens_settings.get_settings_for_device(0xFFA0)
        for s in result:
            self.assertIn("name", s)
            self.assertIn("type", s)
            self.assertIn("default", s)

    def test_dect_has_settings(self):
        result = lens_settings.get_settings_for_device(0xFFA2)
        self.assertTrue(len(result) > 0)


class TestSettingsToApiFormat(unittest.TestCase):
    """Test lens_settings.settings_to_api_format() output format."""

    def test_returns_tuple(self):
        defs = [{"name": "Test", "type": "bool", "default": False}]
        result = lens_settings.settings_to_api_format(defs)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_metadata_has_meta_object(self):
        defs = [{"name": "Test", "type": "bool", "default": False}]
        metadata, values = lens_settings.settings_to_api_format(defs)
        self.assertEqual(len(metadata), 1)
        self.assertIn("meta", metadata[0])

    def test_values_include_meta(self):
        """Values MUST include meta or SettingItem renderer returns null."""
        defs = [{"name": "Test", "type": "bool", "default": False}]
        metadata, values = lens_settings.settings_to_api_format(defs)
        self.assertEqual(len(values), 1)
        self.assertIn("meta", values[0])

    def test_meta_has_correct_fields(self):
        defs = [{"name": "Test", "type": "enum", "default": "a", "choices": ["a", "b"]}]
        metadata, values = lens_settings.settings_to_api_format(defs)
        meta = metadata[0]["meta"]
        self.assertEqual(meta["type"], "enum")
        self.assertIn("visible", meta)
        self.assertIn("enabled", meta)
        self.assertIn("read_only", meta)
        self.assertIn("default_value", meta)
        self.assertIn("possible_values", meta)

    def test_bool_has_valueBool(self):
        defs = [{"name": "Test", "type": "bool", "default": True}]
        _, values = lens_settings.settings_to_api_format(defs)
        self.assertIn("valueBool", values[0])

    def test_enum_has_valueEnum(self):
        defs = [{"name": "Test", "type": "enum", "default": "a", "choices": ["a", "b"]}]
        _, values = lens_settings.settings_to_api_format(defs)
        self.assertIn("valueEnum", values[0])

    def test_int_has_valueInt(self):
        defs = [{"name": "Test", "type": "int", "default": 5, "min": 0, "max": 10}]
        _, values = lens_settings.settings_to_api_format(defs)
        self.assertIn("valueInt", values[0])

    def test_writable_family_not_readonly(self):
        defs = [{"name": "Test", "type": "bool", "default": False}]
        _, values = lens_settings.settings_to_api_format(defs, family="cx2070x")
        self.assertFalse(values[0]["meta"]["read_only"])

    def test_dect_family_readonly(self):
        defs = [{"name": "Test", "type": "bool", "default": False}]
        _, values = lens_settings.settings_to_api_format(defs, family="dect")
        self.assertTrue(values[0]["meta"]["read_only"])


class TestTranslateValue(unittest.TestCase):
    """Test device_settings_db.translate_value()."""

    def test_keep_link_up_false(self):
        result = device_settings_db.translate_value("0xfff4", "false")
        self.assertEqual(result, "activeonlyduringcall")

    def test_keep_link_up_true(self):
        result = device_settings_db.translate_value("0xfff4", "true")
        self.assertEqual(result, "alwaysactive")

    def test_passthrough_unknown_setting(self):
        result = device_settings_db.translate_value("0x9999", "somevalue")
        self.assertEqual(result, "somevalue")

    def test_passthrough_unknown_value(self):
        result = device_settings_db.translate_value("0xfff4", "unknown_val")
        self.assertEqual(result, "unknown_val")


class TestGetPidProfile(unittest.TestCase):
    """Test device_settings_db.get_pid_profile()."""

    def test_known_pid_ac28(self):
        result = device_settings_db.get_pid_profile(0xAC28)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, list)
        self.assertTrue(len(result) > 0)

    def test_known_pid_c056(self):
        result = device_settings_db.get_pid_profile(0xC056)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, list)

    def test_unknown_pid_returns_none(self):
        result = device_settings_db.get_pid_profile(0x0001)
        self.assertIsNone(result)

    def test_profile_entries_have_required_keys(self):
        result = device_settings_db.get_pid_profile(0xAC28)
        if result:
            for entry in result:
                self.assertIn("name", entry)
                self.assertIn("type", entry)
                self.assertIn("default", entry)


class TestPreferOfficialSettings(unittest.TestCase):
    """Test PREFER_OFFICIAL_SETTINGS flag behavior."""

    def test_flag_exists(self):
        self.assertTrue(hasattr(lens_settings, "PREFER_OFFICIAL_SETTINGS"))

    def test_flag_is_bool(self):
        self.assertIsInstance(lens_settings.PREFER_OFFICIAL_SETTINGS, bool)

    def test_flag_default_true(self):
        self.assertTrue(lens_settings.PREFER_OFFICIAL_SETTINGS)


class TestAllProfilesHaveRequiredKeys(unittest.TestCase):
    """Test that all hardcoded settings profiles have required keys."""

    def _check_profile(self, profile, name):
        for s in profile:
            self.assertIn("name", s, f"Missing 'name' in {name}")
            self.assertIn("type", s, f"Missing 'type' in {name}")
            self.assertIn("default", s, f"Missing 'default' in {name}")

    def test_dect_settings(self):
        self._check_profile(lens_settings.DECT_SETTINGS, "DECT_SETTINGS")

    def test_cx2070x_settings(self):
        self._check_profile(lens_settings.CX2070X_SETTINGS, "CX2070X_SETTINGS")

    def test_bladerunner_settings(self):
        self._check_profile(lens_settings.BLADERUNNER_SETTINGS, "BLADERUNNER_SETTINGS")

    def test_voyager_bt_settings(self):
        self._check_profile(lens_settings.VOYAGER_BT_SETTINGS, "VOYAGER_BT_SETTINGS")

    def test_voyager_base_settings(self):
        self._check_profile(lens_settings.VOYAGER_BASE_SETTINGS, "VOYAGER_BASE_SETTINGS")


if __name__ == "__main__":
    unittest.main()
