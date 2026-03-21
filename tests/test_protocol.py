#!/usr/bin/env python3
"""Tests for LensServiceApi protocol in lensserver.py."""

import sys
import os
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lensserver


class TestMsgDelim(unittest.TestCase):
    """Test MSG_DELIM is SOH (0x01)."""

    def test_msg_delim_is_soh(self):
        self.assertEqual(lensserver.MSG_DELIM, "\x01")

    def test_msg_delim_ord(self):
        self.assertEqual(ord(lensserver.MSG_DELIM), 1)

    def test_msg_delim_length(self):
        self.assertEqual(len(lensserver.MSG_DELIM), 1)


class TestApiVersion(unittest.TestCase):
    """Test API_VERSION constant."""

    def test_api_version_format(self):
        # Should be a dotted version string
        parts = lensserver.API_VERSION.split(".")
        self.assertEqual(len(parts), 3)

    def test_api_version_is_string(self):
        self.assertIsInstance(lensserver.API_VERSION, str)

    def test_api_version_value(self):
        self.assertEqual(lensserver.API_VERSION, "1.14.1")


class TestHandlerRouting(unittest.TestCase):
    """Test that message handlers are registered correctly."""

    def setUp(self):
        self.server = lensserver.LensServer(port=0)

    def test_handle_message_has_handlers(self):
        """Verify handle_message routes to handler dict internally."""
        # We test by checking the method exists
        self.assertTrue(hasattr(self.server, "handle_message"))

    def test_expected_handlers_exist(self):
        """All expected handler methods must exist on LensServer."""
        expected_methods = [
            "on_register",
            "on_get_device_list",
            "on_get_device_settings",
            "on_get_device_setting",
            "on_set_device_setting",
            "on_get_settings_metadata",
            "on_get_dfu_status",
            "on_get_library_version",
            "on_get_softphones",
            "on_get_primary_device",
            "on_register_softphones",
            "on_get_software_update",
            "on_slew_device_setting",
            "on_schedule_dfu",
            "on_postpone_dfu",
            "on_remove_device",
            "on_softphone_control",
            "on_logs_prepared",
            "on_analytics",
        ]
        for method_name in expected_methods:
            self.assertTrue(
                hasattr(self.server, method_name),
                f"LensServer missing handler: {method_name}",
            )

    def test_new_handlers_present(self):
        """SlewDeviceSetting, RemoveDevice, etc. handlers exist."""
        self.assertTrue(hasattr(self.server, "on_slew_device_setting"))
        self.assertTrue(hasattr(self.server, "on_remove_device"))
        self.assertTrue(hasattr(self.server, "on_softphone_control"))
        self.assertTrue(hasattr(self.server, "on_logs_prepared"))
        self.assertTrue(hasattr(self.server, "on_analytics"))


class TestLensServerClass(unittest.TestCase):
    """Test LensServer instantiation."""

    def test_default_port_zero(self):
        server = lensserver.LensServer(port=0)
        self.assertEqual(server.port, 0)

    def test_devices_dict_empty(self):
        server = lensserver.LensServer(port=0)
        self.assertEqual(server.devices, {})

    def test_clients_list_empty(self):
        server = lensserver.LensServer(port=0)
        self.assertEqual(server.clients, [])

    def test_running_initially_false(self):
        server = lensserver.LensServer(port=0)
        self.assertFalse(server.running)


if __name__ == "__main__":
    unittest.main()
