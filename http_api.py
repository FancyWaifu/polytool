#!/usr/bin/env python3
"""
HTTP API — REST interface for LensServer.

Runs alongside the TCP LensServiceApi server, providing a simple HTTP API
for querying devices, settings, battery status, and server health.
Used for testing, automation, and PowerShell integration.

Endpoints:
  GET  /api/health                    Server status and uptime
  GET  /api/devices                   List all connected devices
  GET  /api/devices/:id               Single device details
  GET  /api/devices/:id/settings      Device settings with values
  POST /api/devices/:id/settings      Write a setting {name, value}
  GET  /api/devices/:id/battery       Battery status
  GET  /api/devices/:id/dfu           Firmware update status
  DELETE /api/devices/:id             Remove/forget a device
  GET  /api/settings/profiles         Available settings profiles info
  GET  /api/native-bridge             Native bridge status and devices
"""

import json
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


class APIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the REST API."""

    # Suppress default logging — we use our own
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        """Send a JSON response."""
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json({"error": message}, status)

    def _get_server(self):
        """Get the LensServer instance."""
        return self.server.lens_server

    def _clean_device(self, dev):
        """Strip internal fields from a device dict for API output."""
        return {k: v for k, v in dev.items() if not k.startswith("_")}

    def _find_device(self, device_id):
        """Find a device by ID (exact match or prefix)."""
        server = self._get_server()
        with server._lock:
            # Exact match
            if device_id in server.devices:
                return device_id, server.devices[device_id]
            # Prefix match
            for did, dev in server.devices.items():
                if did.lower().startswith(device_id.lower()):
                    return did, dev
            # Match by product name (case-insensitive)
            for did, dev in server.devices.items():
                if device_id.lower() in dev.get("productName", "").lower():
                    return did, dev
        return None, None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        parts = path.split("/")

        # Route
        if path == "/api/health":
            self._handle_health()
        elif path == "/api/devices":
            self._handle_devices_list()
        elif len(parts) == 4 and parts[1:3] == ["api", "devices"]:
            device_id = parts[3]
            self._handle_device_detail(device_id)
        elif len(parts) == 5 and parts[1:3] == ["api", "devices"]:
            device_id = parts[3]
            sub = parts[4]
            if sub == "settings":
                self._handle_device_settings(device_id)
            elif sub == "battery":
                self._handle_device_battery(device_id)
            elif sub == "dfu":
                self._handle_device_dfu(device_id)
            else:
                self._send_error(404, f"Unknown sub-resource: {sub}")
        elif path == "/api/settings/profiles":
            self._handle_settings_profiles()
        elif path == "/api/native-bridge":
            self._handle_native_bridge()
        elif path == "/api" or path == "/":
            self._handle_index()
        else:
            self._send_error(404, f"Not found: {path}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        parts = path.split("/")

        if len(parts) == 5 and parts[1:3] == ["api", "devices"] and parts[4] == "settings":
            device_id = parts[3]
            self._handle_set_setting(device_id)
        else:
            self._send_error(404, f"Not found: {path}")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        parts = path.split("/")

        if len(parts) == 4 and parts[1:3] == ["api", "devices"]:
            device_id = parts[3]
            self._handle_remove_device(device_id)
        else:
            self._send_error(404, f"Not found: {path}")

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── Handlers ──────────────────────────────────────────────────────────

    def _handle_index(self):
        """API index — list available endpoints."""
        self._send_json({
            "name": "PolyTool LensServer API",
            "version": "1.0",
            "endpoints": [
                {"method": "GET", "path": "/api/health", "description": "Server status and uptime"},
                {"method": "GET", "path": "/api/devices", "description": "List all connected devices"},
                {"method": "GET", "path": "/api/devices/:id", "description": "Single device details"},
                {"method": "GET", "path": "/api/devices/:id/settings", "description": "Device settings with values"},
                {"method": "POST", "path": "/api/devices/:id/settings", "description": "Write a setting {name, value}"},
                {"method": "GET", "path": "/api/devices/:id/battery", "description": "Battery status"},
                {"method": "GET", "path": "/api/devices/:id/dfu", "description": "Firmware update status"},
                {"method": "DELETE", "path": "/api/devices/:id", "description": "Remove/forget a device"},
                {"method": "GET", "path": "/api/settings/profiles", "description": "Settings profiles info"},
                {"method": "GET", "path": "/api/native-bridge", "description": "Native bridge status"},
            ],
        })

    def _handle_health(self):
        """Server health check."""
        server = self._get_server()
        with server._lock:
            device_count = len(server.devices)
            client_count = len(server.clients)
        self._send_json({
            "status": "ok",
            "uptime_seconds": round(time.time() - self.server.start_time, 1),
            "tcp_port": server.port,
            "http_port": self.server.server_address[1],
            "devices": device_count,
            "clients": client_count,
            "native_bridge": server._native_bridge is not None,
        })

    def _handle_devices_list(self):
        """List all connected devices."""
        server = self._get_server()
        with server._lock:
            devices = []
            for did, dev in server.devices.items():
                clean = self._clean_device(dev)
                # Add settings count
                cache = server._device_settings_cache.get(did, {})
                profile = server._dynamic_profiles.get(did, [])
                clean["_settingsCount"] = max(len(cache), len(profile))
                clean["_hasCachedSettings"] = len(cache) > 0
                devices.append(clean)
        self._send_json({"devices": devices, "count": len(devices)})

    def _handle_device_detail(self, device_id):
        """Single device details."""
        did, dev = self._find_device(device_id)
        if not dev:
            self._send_error(404, f"Device not found: {device_id}")
            return
        clean = self._clean_device(dev)
        server = self._get_server()
        with server._lock:
            cache = server._device_settings_cache.get(did, {})
            clean["_cachedSettings"] = dict(cache)
        self._send_json(clean)

    def _handle_device_settings(self, device_id):
        """Get device settings with metadata and values."""
        did, dev = self._find_device(device_id)
        if not dev:
            self._send_error(404, f"Device not found: {device_id}")
            return

        server = self._get_server()
        try:
            metadata, values = server._get_device_settings_formatted(did)
            self._send_json({
                "deviceId": did,
                "productName": dev.get("productName", ""),
                "settingsCount": len(values),
                "metadata": metadata,
                "settings": values,
            })
        except Exception as e:
            self._send_error(500, f"Error reading settings: {e}")

    def _handle_set_setting(self, device_id):
        """Write a setting to a device."""
        did, dev = self._find_device(device_id)
        if not dev:
            self._send_error(404, f"Device not found: {device_id}")
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_error(400, "Invalid JSON body. Expected {name, value}")
            return

        name = data.get("name")
        value = data.get("value")
        if not name:
            self._send_error(400, "Missing 'name' field")
            return

        server = self._get_server()
        # Update cache
        with server._lock:
            if did not in server._device_settings_cache:
                server._device_settings_cache[did] = {}
            server._device_settings_cache[did][name] = value

        # Write to device
        success = server.write_device_setting(did, name, value)
        self._send_json({
            "deviceId": did,
            "name": name,
            "value": value,
            "written": success,
        })

    def _handle_device_battery(self, device_id):
        """Get battery status for a device."""
        did, dev = self._find_device(device_id)
        if not dev:
            self._send_error(404, f"Device not found: {device_id}")
            return

        # Check native bridge for battery
        server = self._get_server()
        battery = {"level": -1, "charging": False, "available": False}

        if server._native_bridge:
            ptd = dev.get("_polytool_dev", {})
            native_id = ptd.get("_native_id", "")
            if not native_id:
                # Try matching by PID
                pid = ptd.get("pid", 0)
                for nid, ndev in server._native_bridge.get_devices().items():
                    if ndev.get("pid") == pid:
                        native_id = nid
                        break
            if native_id:
                batt = server._native_bridge.get_battery(native_id)
                if batt and batt.get("level", -1) >= 0:
                    level = batt["level"]
                    battery = {
                        "level": min(100, level * 20) if level <= 5 else level,
                        "charging": batt.get("charging", False),
                        "available": True,
                    }

        self._send_json({
            "deviceId": did,
            "productName": dev.get("productName", ""),
            "battery": battery,
        })

    def _handle_device_dfu(self, device_id):
        """Get firmware update status."""
        did, dev = self._find_device(device_id)
        if not dev:
            self._send_error(404, f"Device not found: {device_id}")
            return

        server = self._get_server()
        cached = server._dfu_cache.get(did, {})
        self._send_json({
            "deviceId": did,
            "productName": dev.get("productName", ""),
            "currentVersion": dev.get("firmwareVersion", ""),
            "dfu": cached if cached else {"status": "not_checked"},
        })

    def _handle_remove_device(self, device_id):
        """Remove/forget a device."""
        did, dev = self._find_device(device_id)
        if not dev:
            self._send_error(404, f"Device not found: {device_id}")
            return

        server = self._get_server()
        name = dev.get("productName", did)
        with server._lock:
            server.devices.pop(did, None)
            server._device_settings_cache.pop(did, None)
            server._dynamic_profiles.pop(did, None)
        server.broadcast({
            "type": "DeviceDetached",
            "apiVersion": "1.14.1",
            "deviceId": did,
        })
        self._send_json({"removed": did, "productName": name})

    def _handle_settings_profiles(self):
        """Info about available settings profiles."""
        from lens_settings import (DECT_SETTINGS, CX2070X_SETTINGS,
                                   BLADERUNNER_SETTINGS, VOYAGER_BT_SETTINGS,
                                   VOYAGER_BASE_SETTINGS, PREFER_OFFICIAL_SETTINGS)
        from device_settings_db import SETTINGS_DB, PID_PROFILES

        self._send_json({
            "preferOfficialSettings": PREFER_OFFICIAL_SETTINGS,
            "zipDatabase": {
                "totalSettings": len(SETTINGS_DB),
                "totalPidProfiles": len(PID_PROFILES),
            },
            "hardcodedProfiles": {
                "dect": len(DECT_SETTINGS),
                "cx2070x": len(CX2070X_SETTINGS),
                "bladerunner": len(BLADERUNNER_SETTINGS),
                "voyager_bt": len(VOYAGER_BT_SETTINGS),
                "voyager_base": len(VOYAGER_BASE_SETTINGS),
            },
        })

    def _handle_native_bridge(self):
        """Native bridge status and devices."""
        server = self._get_server()
        bridge = server._native_bridge
        if not bridge:
            self._send_json({"status": "unavailable", "devices": []})
            return

        devices = []
        for nid, ndev in bridge.get_devices().items():
            batt = bridge.get_battery(nid)
            devices.append({
                "nativeId": nid,
                "name": ndev.get("name", ""),
                "pid": ndev.get("pid", 0),
                "pidHex": f"0x{ndev.get('pid', 0):04X}",
                "battery": batt if batt else None,
                "settingValues": bridge.get_setting_values(nid),
            })
        self._send_json({
            "status": "active",
            "deviceCount": len(devices),
            "devices": devices,
        })


def start_http_api(lens_server, port=8080):
    """Start the HTTP API server in a background thread.

    Args:
        lens_server: LensServer instance to expose via HTTP
        port: HTTP port (default 8080)

    Returns:
        (HTTPServer, thread) tuple
    """
    httpd = HTTPServer(("127.0.0.1", port), APIHandler)
    httpd.lens_server = lens_server
    httpd.start_time = time.time()

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread
