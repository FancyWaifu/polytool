#!/usr/bin/env python3
"""
LensServer — Drop-in replacement for Poly Lens Control Service.

Implements the LensServiceApi TCP protocol so that Poly Studio GUI
can connect to us instead of the real LensService. We handle device
discovery and settings via our own HID code.

This means Poly Studio works with ANY device we support, even ones
not in Poly's whitelist.

Protocol:
  TCP server on a dynamic port, written to SocketPortNumber file.
  Newline-delimited JSON messages with "type" field.
  Client registers with RegisterClient, we respond with ClientRegistered.
  We push DeviceAttached events for each connected device.
  Client sends GetDeviceSettings/SetDeviceSetting, we translate to HID.

Usage:
  python3 lensserver.py                 # Start server
  python3 lensserver.py --port 31415    # Fixed port

Then open Poly Studio — it will connect to us automatically.
"""

import json
import os
import sys
import socket
import time
import threading
import argparse
from pathlib import Path

# Import our device tools
sys.path.insert(0, str(Path(__file__).parent))

API_VERSION = "1.14.1"
MSG_DELIM = "\x01"  # SOH byte — real LensService message separator (NOT newline)

# Port file location
PORT_FILE_DIR = Path.home() / "Library/Application Support/Poly/Lens Control Service"
PORT_FILE = PORT_FILE_DIR / "SocketPortNumber"


class LensServer:
    """TCP server implementing the LensServiceApi protocol."""

    def __init__(self, port=0):
        self.port = port
        self.server_sock = None
        self.clients = []  # list of client sockets
        self.devices = {}  # deviceId → device info dict
        self.running = False
        self._lock = threading.Lock()

    def start(self):
        """Start the TCP server."""
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("127.0.0.1", self.port))
        self.server_sock.listen(5)
        self.port = self.server_sock.getsockname()[1]
        self.running = True

        # Write port file so Poly Studio can find us
        PORT_FILE_DIR.mkdir(parents=True, exist_ok=True)
        PORT_FILE.write_text(str(self.port))

        print(f"  Listening on port {self.port}")
        print(f"  Port file: {PORT_FILE}")

    def stop(self):
        """Stop the server."""
        self.running = False
        if self.server_sock:
            self.server_sock.close()
        for client in self.clients:
            try:
                client.close()
            except:
                pass
        # Remove port file
        try:
            PORT_FILE.unlink(missing_ok=True)
        except:
            pass

    def accept_clients(self):
        """Accept client connections in a loop."""
        self.server_sock.settimeout(1)
        while self.running:
            try:
                client_sock, addr = self.server_sock.accept()
                print(f"  Client connected from {addr}")
                t = threading.Thread(target=self.handle_client,
                                     args=(client_sock,), daemon=True)
                t.start()
                with self._lock:
                    self.clients.append(client_sock)
            except socket.timeout:
                continue
            except OSError:
                break

    def handle_client(self, client_sock):
        """Handle one client connection."""
        buffer = ""
        client_sock.settimeout(1)
        registered = False


        while self.running:
            try:
                data = client_sock.recv(65536)
                if not data:
                    break
                buffer += data.decode("utf-8", errors="ignore")

                while MSG_DELIM in buffer:
                    line, buffer = buffer.split(MSG_DELIM, 1)
                    if line.strip():
                        try:
                            msg = json.loads(line)
                            msg_type = msg.get("type", "?")
                            print(f"  ← {msg_type}: {json.dumps(msg)[:150]}")

                            if msg_type in ("RegisterClient", "RegisterEndUserApplication") and not registered:
                                registered = True
                                self.on_register(msg, client_sock)
                                # After registration, push system info
                                self.send_msg(client_sock, {
                                    "type": "SystemInformation",
                                    "apiVersion": API_VERSION,
                                    "systemName": os.uname().nodename,
                                })
                                self.send_msg(client_sock, {
                                    "type": "LcsConfigurationInformation",
                                    "apiVersion": API_VERSION,
                                    "configurationFlavor": "Lens Desktop",
                                })
                                continue

                            response = self.handle_message(msg, client_sock)
                            if response:
                                self.send_msg(client_sock, response)
                        except json.JSONDecodeError as e:
                            print(f"  JSON error: {e} — raw: {line[:100]}")
                            pass

            except socket.timeout:
                continue
            except (OSError, ConnectionResetError):
                break

        print(f"  Client disconnected")
        with self._lock:
            if client_sock in self.clients:
                self.clients.remove(client_sock)
        try:
            client_sock.close()
        except:
            pass

    def send_msg(self, client_sock, msg):
        """Send a JSON message to a client (SOH-delimited)."""
        try:
            data = json.dumps(msg) + MSG_DELIM
            client_sock.sendall(data.encode("utf-8"))
        except:
            pass

    def broadcast(self, msg):
        """Send a message to all connected clients."""
        with self._lock:
            for client in self.clients[:]:
                self.send_msg(client, msg)

    def handle_message(self, msg, client_sock):
        """Route an incoming message to the appropriate handler."""
        msg_type = msg.get("type", "")

        handlers = {
            "RegisterClient": self.on_register,
            "RegisterEndUserApplication": self.on_register,
            "GetDeviceList": self.on_get_device_list,
            "GetDeviceSettings": self.on_get_device_settings,
            "GetDeviceSetting": self.on_get_device_setting,
            "SetDeviceSetting": self.on_set_device_setting,
            "GetDeviceSettingsMetadata": self.on_get_settings_metadata,
            "GetDeviceDFUStatus": self.on_get_dfu_status,
            "GetDeviceLibraryVersion": self.on_get_library_version,
            "GetSoftphonesList": self.on_get_softphones,
            "GetPrimaryDevice": self.on_get_primary_device,
            "RegisterSoftphones": self.on_register_softphones,
            "GetAvailableSoftwareUpdate": self.on_get_software_update,
        }

        handler = handlers.get(msg_type)
        if handler:
            result = handler(msg, client_sock)
            if result:
                print(f"  → {result.get('type', '?')}")
            return result

        # Catch-all — respond to any unknown message with an empty ack
        # This prevents the GUI from hanging waiting for a response
        print(f"  Unknown: {msg_type} — {json.dumps(msg)[:150]}")
        return {
            "type": msg_type.replace("Get", "").replace("Set", "") if msg_type.startswith(("Get", "Set")) else "Error",
            "apiVersion": API_VERSION,
            "error": f"Not implemented: {msg_type}",
        }

    # ── Message Handlers ──────────────────────────────────────────────

    def on_register(self, msg, client_sock):
        """Handle RegisterClient."""
        name = msg.get("name", "unknown")
        print(f"  Client registered: {name}")

        # Send ClientRegistered — tell client not to use encryption
        self.send_msg(client_sock, {
            "type": "ClientRegistered",
            "apiVersion": API_VERSION,
            "serviceVersion": "1.14.1",
            "serviceProductName": "Poly Lens Control Service",
            "configurationFlavor": "Lens Desktop",
            "displayName": "Poly Lens Control Service",
            "useEncryption": False,
        })

        # Push DeviceAttached for each known device
        for did, dev in self.devices.items():
            clean_dev = {k: v for k, v in dev.items() if not k.startswith("_")}
            self.send_msg(client_sock, {
                "type": "DeviceAttached",
                "apiVersion": API_VERSION,
                "device": clean_dev,
            })
            print(f"  → DeviceAttached: {dev.get('productName', '?')}")

        return None

    def on_get_device_list(self, msg, client_sock):
        """Handle GetDeviceList."""
        clean_devices = []
        for dev in self.devices.values():
            clean_devices.append({k: v for k, v in dev.items() if not k.startswith("_")})
        return {
            "type": "DeviceList",
            "apiVersion": API_VERSION,
            "devices": clean_devices,
        }

    def on_get_device_settings(self, msg, client_sock):
        """Handle GetDeviceSettings."""
        device_id = msg.get("deviceId", "")
        settings = self.read_device_settings(device_id)
        return {
            "type": "DeviceSettings",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "settings": settings,
        }

    def on_get_device_setting(self, msg, client_sock):
        """Handle GetDeviceSetting."""
        device_id = msg.get("deviceId", "")
        name = msg.get("name", "")

        # Handle special built-in settings
        dev = self.devices.get(device_id, {})

        # Build a proper DeviceSetting response
        # The GUI destructures: const {name} = setting
        # So we need: {type: "DeviceSetting", setting: {name, value, ...}}
        def make_setting_response(sname, svalue, **extra):
            return {
                "type": "DeviceSetting",
                "apiVersion": API_VERSION,
                "deviceId": device_id,
                "setting": {
                    "name": sname,
                    "value": svalue,
                    **extra,
                },
            }

        if name == "Product Images":
            return make_setting_response(name, None)

        if name == "Ear Cushion Type":
            return make_setting_response(name, None)

        if name == "Device Info":
            return make_setting_response(name, dev.get("firmwareVersion", ""),
                valueCompound=[
                    {"name": "sw_version", "value": dev.get("firmwareVersion", "")},
                    {"name": "serial_number", "value": dev.get("serialNumber", "")},
                    {"name": "product_name", "value": dev.get("productName", "")},
                ])

        # Try reading from HID
        settings = self.read_device_settings(device_id)
        for s in settings:
            if s.get("name") == name:
                return make_setting_response(name, s.get("value"))

        return make_setting_response(name, None)

    def on_set_device_setting(self, msg, client_sock):
        """Handle SetDeviceSetting."""
        device_id = msg.get("deviceId", "")
        name = msg.get("name", "")
        value = msg.get("valueBool", msg.get("valueInt", msg.get("valueFloat",
                msg.get("valueString", msg.get("valueEnum")))))

        success = self.write_device_setting(device_id, name, value)

        return {
            "type": "DeviceSettingUpdated",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "name": name,
            "value": value,
            "success": success,
        }

    def on_get_settings_metadata(self, msg, client_sock):
        """Handle GetDeviceSettingsMetadata."""
        device_id = msg.get("deviceId", "")
        settings = self.read_device_settings(device_id)

        metadata = []
        for s in settings:
            metadata.append({
                "name": s.get("name", ""),
                "dataType": s.get("type", "string"),
                "readable": True,
                "writable": s.get("writable", True),
            })

        return {
            "type": "DeviceSettingsMetadata",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "settings": metadata,
        }

    def on_get_dfu_status(self, msg, client_sock):
        """Handle GetDeviceDFUStatus."""
        device_id = msg.get("deviceId", "")
        dev = self.devices.get(device_id, {})
        return {
            "type": "DeviceDFUStatus",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "version": dev.get("firmwareVersion", "unknown"),
            "status": "UpToDate",
        }

    def on_get_library_version(self, msg, client_sock):
        return {
            "type": "DeviceLibraryVersion",
            "apiVersion": API_VERSION,
            "version": "1.0.0-polytool",
        }

    def on_get_softphones(self, msg, client_sock):
        return {
            "type": "SoftphonesList",
            "apiVersion": API_VERSION,
            "softphones": [],
        }

    def on_get_primary_device(self, msg, client_sock):
        # Return first device as primary
        first_id = next(iter(self.devices), "")
        return {
            "type": "PrimaryDevice",
            "apiVersion": API_VERSION,
            "primaryDeviceInfo": {"deviceId": first_id} if first_id else None,
        }

    def on_register_softphones(self, msg, client_sock):
        return {
            "type": "SoftphonesRegistered",
            "apiVersion": API_VERSION,
        }

    def on_get_software_update(self, msg, client_sock):
        return {
            "type": "AvailableSoftwareUpdate",
            "apiVersion": API_VERSION,
            "availableVersion": "",
            "currentVersion": "",
            "statuses": [],
            "canPostpone": False,
            "component": msg.get("component", ""),
        }

    # ── Device I/O (delegated to our HID code) ───────────────────────

    def discover_devices(self):
        """Scan for Poly devices using our polytool code."""
        try:
            from polytool import discover_devices, try_read_device_info, try_read_battery
            raw_devices = discover_devices()
            for dev in raw_devices:
                try_read_device_info(dev)
                try_read_battery(dev)

                device_id = dev.id
                self.devices[device_id] = {
                    # camelCase — matches .NET JsonSerializer default naming
                    "deviceId": device_id,
                    "productName": dev.friendly_name or dev.product_name,
                    "deviceName": dev.product_name,
                    "firmwareVersion": dev.firmware_display,
                    "serialNumber": dev.serial or "",
                    "tattooSerialNumber": dev.serial or "",
                    "deviceType": (dev.category or "headset").capitalize(),
                    "connected": True,
                    "attached": True,
                    "pid": str(dev.pid),
                    "vid": str(dev.vid),
                    "productId": f"{dev.pid:04x}",
                    "macAddress": "",
                    "bluetoothAddress": "",
                    "hardwareRevision": "",
                    "headsetVersion": dev.firmware_display,
                    "baseVersion": "",
                    "usbVersion": dev.firmware_display,
                    "firmwareComponents": [],
                    "peerDevices": [],
                    "connectionType": "USB",
                    "isAbleToBePrimaryForCallControl": True,
                    "isMuted": False,
                    "isInCall": False,
                    "state": "Online",
                    "supportData": {"state": "Supported"},
                    "multiComponentState": None,
                    "lastAttachedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    # Internal — not sent to clients
                    "_polytool_dev": {
                        "path": dev.path,
                        "usage_page": dev.usage_page,
                        "dfu_executor": dev.dfu_executor,
                    },
                }

            return len(self.devices)
        except Exception as e:
            print(f"  Device discovery error: {e}")
            return 0

    def read_device_settings(self, device_id):
        """Read settings for a device via our HID code."""
        dev = self.devices.get(device_id, {})
        ptd = dev.get("_polytool_dev", {})
        if not ptd:
            return []

        try:
            from device_settings import read_all_settings
            settings = read_all_settings(
                ptd.get("path", b""),
                ptd.get("usage_page", 0),
                ptd.get("dfu_executor", ""),
            )
            return settings
        except Exception as e:
            print(f"  Settings read error: {e}")
            return []

    def write_device_setting(self, device_id, name, value):
        """Write a setting via our HID code."""
        dev = self.devices.get(device_id, {})
        ptd = dev.get("_polytool_dev", {})
        if not ptd:
            return False

        try:
            from device_settings import write_setting
            return write_setting(
                ptd.get("path", b""),
                ptd.get("usage_page", 0),
                ptd.get("dfu_executor", ""),
                name, value,
            )
        except Exception as e:
            print(f"  Settings write error: {e}")
            return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LensServer — Drop-in Poly Lens Control Service replacement",
    )
    parser.add_argument("--port", type=int, default=0, help="TCP port (0=auto)")
    args = parser.parse_args()

    server = LensServer(port=args.port)

    print(f"\n  LensServer — Poly Lens Replacement")
    print(f"  {'='*40}")

    # Discover devices
    print(f"\n  Scanning for devices...")
    count = server.discover_devices()
    print(f"  Found {count} device(s)")
    for did, dev in server.devices.items():
        print(f"    {dev['productName']} (fw {dev['firmwareVersion']})")

    # Start server
    print(f"\n  Starting TCP server...")
    server.start()
    print(f"\n  Open Poly Studio — it will connect automatically.\n")

    try:
        server.accept_clients()
    except KeyboardInterrupt:
        print(f"\n  Shutting down...")
    finally:
        server.stop()
        print(f"  Stopped.")


if __name__ == "__main__":
    main()
