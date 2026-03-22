#!/usr/bin/env python3
"""Tests for the HTTP API against a running LensServer.

These tests require the server to be running:
    python3 lensserver.py --http 8080

Run with: python3 -m unittest tests.test_http_api -v
"""

import json
import os
import sys
import unittest
import urllib.request
import urllib.error

API_BASE = os.environ.get("POLYTOOL_API", "http://127.0.0.1:8080")


def api_get(path):
    """Make a GET request to the API."""
    url = f"{API_BASE}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code
    except urllib.error.URLError:
        return None, 0


def api_post(path, data):
    """Make a POST request to the API."""
    url = f"{API_BASE}{path}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code
    except urllib.error.URLError:
        return None, 0


def server_available():
    """Check if the server is running."""
    data, status = api_get("/api/health")
    return status == 200


@unittest.skipUnless(server_available(), "LensServer not running on port 8080")
class TestHealthEndpoint(unittest.TestCase):
    def test_health_returns_ok(self):
        data, status = api_get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_health_has_required_fields(self):
        data, _ = api_get("/api/health")
        for field in ["status", "uptime_seconds", "tcp_port", "http_port",
                      "devices", "clients", "native_bridge"]:
            self.assertIn(field, data, f"Missing field: {field}")

    def test_health_device_count_positive(self):
        data, _ = api_get("/api/health")
        self.assertGreater(data["devices"], 0, "No devices detected")

    def test_health_uptime_positive(self):
        data, _ = api_get("/api/health")
        self.assertGreater(data["uptime_seconds"], 0)


@unittest.skipUnless(server_available(), "LensServer not running on port 8080")
class TestDevicesEndpoint(unittest.TestCase):
    def test_devices_list(self):
        data, status = api_get("/api/devices")
        self.assertEqual(status, 200)
        self.assertIn("devices", data)
        self.assertIn("count", data)
        self.assertEqual(len(data["devices"]), data["count"])

    def test_devices_not_empty(self):
        data, _ = api_get("/api/devices")
        self.assertGreater(data["count"], 0, "No devices found")

    def test_device_has_required_fields(self):
        data, _ = api_get("/api/devices")
        required = ["deviceId", "productName", "firmwareVersion",
                    "connected", "attached", "deviceType"]
        for dev in data["devices"]:
            for field in required:
                self.assertIn(field, dev, f"Device missing field: {field}")

    def test_all_devices_connected(self):
        data, _ = api_get("/api/devices")
        for dev in data["devices"]:
            self.assertTrue(dev["connected"])
            self.assertTrue(dev["attached"])

    def test_no_internal_fields_leaked(self):
        """Internal _polytool_dev fields should be stripped."""
        data, _ = api_get("/api/devices")
        for dev in data["devices"]:
            internal = [k for k in dev if k.startswith("_") and k != "_settingsCount" and k != "_hasCachedSettings"]
            self.assertEqual(internal, [], f"Internal fields leaked: {internal}")

    def test_device_detail_by_id(self):
        data, _ = api_get("/api/devices")
        if data["count"] == 0:
            self.skipTest("No devices")
        device_id = data["devices"][0]["deviceId"]
        detail, status = api_get(f"/api/devices/{device_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["deviceId"], device_id)

    def test_device_detail_by_prefix(self):
        """Should find device by ID prefix."""
        data, _ = api_get("/api/devices")
        if data["count"] == 0:
            self.skipTest("No devices")
        device_id = data["devices"][0]["deviceId"]
        prefix = device_id[:4]
        detail, status = api_get(f"/api/devices/{prefix}")
        self.assertEqual(status, 200)

    def test_device_detail_by_name(self):
        """Should find device by product name substring."""
        data, _ = api_get("/api/devices")
        if data["count"] == 0:
            self.skipTest("No devices")
        name = data["devices"][0]["productName"]
        # Use first word of the name
        search = name.split()[0]
        detail, status = api_get(f"/api/devices/{search}")
        self.assertEqual(status, 200)

    def test_device_not_found(self):
        data, status = api_get("/api/devices/nonexistent_device_xyz")
        self.assertEqual(status, 404)
        self.assertIn("error", data)


@unittest.skipUnless(server_available(), "LensServer not running on port 8080")
class TestSettingsEndpoint(unittest.TestCase):
    def _get_first_device_id(self):
        data, _ = api_get("/api/devices")
        if data["count"] == 0:
            self.skipTest("No devices")
        return data["devices"][0]["deviceId"]

    def test_settings_returns_metadata_and_values(self):
        did = self._get_first_device_id()
        data, status = api_get(f"/api/devices/{did}/settings")
        self.assertEqual(status, 200)
        self.assertIn("metadata", data)
        self.assertIn("settings", data)
        self.assertIn("settingsCount", data)

    def test_settings_count_matches(self):
        did = self._get_first_device_id()
        data, _ = api_get(f"/api/devices/{did}/settings")
        self.assertEqual(len(data["settings"]), data["settingsCount"])
        self.assertEqual(len(data["metadata"]), data["settingsCount"])

    def test_settings_have_meta_objects(self):
        """Every setting value MUST have meta or Poly Studio renderer returns null."""
        did = self._get_first_device_id()
        data, _ = api_get(f"/api/devices/{did}/settings")
        for s in data["settings"]:
            self.assertIn("meta", s, f"Setting {s['name']} missing meta object")
            meta = s["meta"]
            self.assertIn("type", meta)
            self.assertIn("visible", meta)
            self.assertIn("read_only", meta)

    def test_settings_have_names_and_values(self):
        did = self._get_first_device_id()
        data, _ = api_get(f"/api/devices/{did}/settings")
        for s in data["settings"]:
            self.assertIn("name", s)
            self.assertIn("value", s)

    def test_metadata_has_possible_values(self):
        did = self._get_first_device_id()
        data, _ = api_get(f"/api/devices/{did}/settings")
        for m in data["metadata"]:
            self.assertIn("meta", m)
            self.assertIn("possible_values", m["meta"])

    def test_all_devices_have_settings(self):
        """Every connected device should have at least 1 setting."""
        devices, _ = api_get("/api/devices")
        for dev in devices["devices"]:
            did = dev["deviceId"]
            data, status = api_get(f"/api/devices/{did}/settings")
            self.assertEqual(status, 200)
            self.assertGreater(data["settingsCount"], 0,
                             f"{dev['productName']} has no settings")


@unittest.skipUnless(server_available(), "LensServer not running on port 8080")
class TestBatteryEndpoint(unittest.TestCase):
    def test_battery_returns_data(self):
        data, _ = api_get("/api/devices")
        if data["count"] == 0:
            self.skipTest("No devices")
        did = data["devices"][0]["deviceId"]
        batt, status = api_get(f"/api/devices/{did}/battery")
        self.assertEqual(status, 200)
        self.assertIn("battery", batt)
        self.assertIn("level", batt["battery"])
        self.assertIn("charging", batt["battery"])
        self.assertIn("available", batt["battery"])


@unittest.skipUnless(server_available(), "LensServer not running on port 8080")
class TestDfuEndpoint(unittest.TestCase):
    def test_dfu_returns_data(self):
        data, _ = api_get("/api/devices")
        if data["count"] == 0:
            self.skipTest("No devices")
        did = data["devices"][0]["deviceId"]
        dfu, status = api_get(f"/api/devices/{did}/dfu")
        self.assertEqual(status, 200)
        self.assertIn("currentVersion", dfu)
        self.assertIn("dfu", dfu)


@unittest.skipUnless(server_available(), "LensServer not running on port 8080")
class TestNativeBridgeEndpoint(unittest.TestCase):
    def test_native_bridge_status(self):
        data, status = api_get("/api/native-bridge")
        self.assertEqual(status, 200)
        self.assertIn("status", data)
        self.assertIn("devices", data)

    def test_native_bridge_active(self):
        data, _ = api_get("/api/native-bridge")
        self.assertEqual(data["status"], "active")
        self.assertGreater(data["deviceCount"], 0)

    def test_native_bridge_device_has_fields(self):
        data, _ = api_get("/api/native-bridge")
        if not data["devices"]:
            self.skipTest("No native bridge devices")
        for dev in data["devices"]:
            self.assertIn("nativeId", dev)
            self.assertIn("name", dev)
            self.assertIn("pid", dev)
            self.assertIn("pidHex", dev)


@unittest.skipUnless(server_available(), "LensServer not running on port 8080")
class TestSettingsProfilesEndpoint(unittest.TestCase):
    def test_profiles_info(self):
        data, status = api_get("/api/settings/profiles")
        self.assertEqual(status, 200)
        self.assertIn("preferOfficialSettings", data)
        self.assertIn("zipDatabase", data)
        self.assertIn("hardcodedProfiles", data)

    def test_zip_database_loaded(self):
        data, _ = api_get("/api/settings/profiles")
        self.assertGreater(data["zipDatabase"]["totalSettings"], 50)
        self.assertGreater(data["zipDatabase"]["totalPidProfiles"], 200)

    def test_hardcoded_profiles_sizes(self):
        data, _ = api_get("/api/settings/profiles")
        profiles = data["hardcodedProfiles"]
        self.assertGreater(profiles["dect"], 30)
        self.assertGreater(profiles["voyager_bt"], 30)
        self.assertGreater(profiles["bladerunner"], 15)


@unittest.skipUnless(server_available(), "LensServer not running on port 8080")
class TestApiIndex(unittest.TestCase):
    def test_index_lists_endpoints(self):
        data, status = api_get("/api")
        self.assertEqual(status, 200)
        self.assertIn("endpoints", data)
        self.assertGreater(len(data["endpoints"]), 5)

    def test_endpoints_have_method_and_path(self):
        data, _ = api_get("/api")
        for ep in data["endpoints"]:
            self.assertIn("method", ep)
            self.assertIn("path", ep)
            self.assertIn("description", ep)


@unittest.skipUnless(server_available(), "LensServer not running on port 8080")
class TestErrorHandling(unittest.TestCase):
    def test_404_on_unknown_path(self):
        data, status = api_get("/api/nonexistent")
        self.assertEqual(status, 404)

    def test_404_on_unknown_device_sub(self):
        devices, _ = api_get("/api/devices")
        if devices["count"] == 0:
            self.skipTest("No devices")
        did = devices["devices"][0]["deviceId"]
        data, status = api_get(f"/api/devices/{did}/nonexistent")
        self.assertEqual(status, 404)

    def test_post_invalid_json(self):
        devices, _ = api_get("/api/devices")
        if devices["count"] == 0:
            self.skipTest("No devices")
        did = devices["devices"][0]["deviceId"]
        url = f"{API_BASE}/api/devices/{did}/settings"
        req = urllib.request.Request(url, data=b"not json", method="POST",
                                    headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have returned 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_post_missing_name(self):
        devices, _ = api_get("/api/devices")
        if devices["count"] == 0:
            self.skipTest("No devices")
        did = devices["devices"][0]["deviceId"]
        data, status = api_post(f"/api/devices/{did}/settings", {"value": "test"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
