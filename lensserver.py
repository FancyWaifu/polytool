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

    _original_port = None  # saved port from real LensService

    def start(self):
        """Start the TCP server."""
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("127.0.0.1", self.port))
        self.server_sock.listen(5)
        self.port = self.server_sock.getsockname()[1]
        self.running = True

        # Save original port file (so we can restore it on exit)
        try:
            if PORT_FILE.exists():
                self._original_port = PORT_FILE.read_text().strip()
        except Exception:
            pass

        # Write our port file so Poly Studio can find us
        PORT_FILE_DIR.mkdir(parents=True, exist_ok=True)
        PORT_FILE.write_text(str(self.port))

        # Register cleanup for any exit path (Ctrl+C, kill, crash)
        import atexit
        atexit.register(self._cleanup_port_file)
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda s, f: self._signal_shutdown())

        print(f"  Listening on port {self.port}")
        print(f"  Port file: {PORT_FILE}")
        if self._original_port:
            print(f"  Original LCS port saved: {self._original_port}")

    def _signal_shutdown(self):
        """Handle SIGTERM/SIGINT — stop cleanly."""
        self.running = False

    def _cleanup_port_file(self):
        """Restore original port file or remove ours."""
        try:
            if self._original_port:
                PORT_FILE.write_text(self._original_port)
                print(f"  Restored original LCS port: {self._original_port}")
            else:
                PORT_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def stop(self):
        """Stop the server."""
        self.running = False
        # Stop native bridge first (before closing sockets)
        if self._native_bridge:
            try:
                self._native_bridge.stop()
            except Exception:
                pass
            self._native_bridge = None
        if self.server_sock:
            self.server_sock.close()
        for client in self.clients:
            try:
                client.close()
            except:
                pass
        self._cleanup_port_file()

    def accept_clients(self):
        """Accept client connections in a loop."""
        self.server_sock.settimeout(1)

        # Start device scanner thread
        scanner = threading.Thread(target=self._device_scanner, daemon=True)
        scanner.start()

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

    def _device_scanner(self):
        """Periodically scan for new/removed devices and push events."""
        while self.running:
            time.sleep(5)
            try:
                old_ids = set(self.devices.keys())
                current_ids = self.discover_devices()
                new_ids = current_ids if current_ids else set(self.devices.keys())

                # New devices
                for did in new_ids - old_ids:
                    dev = self.devices[did]
                    clean_dev = {k: v for k, v in dev.items() if not k.startswith("_")}
                    print(f"  ++ Device added: {dev.get('productName', '?')}")
                    self.broadcast({
                        "type": "DeviceAttached",
                        "apiVersion": API_VERSION,
                        "device": clean_dev,
                    })
                    # Also fetch and push product images
                    image_data = self.get_product_images(did)
                    if image_data:
                        self.broadcast({
                            "type": "DeviceSetting",
                            "apiVersion": API_VERSION,
                            "deviceId": did,
                            "setting": {"name": "Product Images", "value": image_data},
                        })

                # Removed devices (skip native bridge devices — they're not in USB scan)
                for did in old_ids - new_ids:
                    dev = self.devices.get(did, {})
                    ptd = dev.get("_polytool_dev", {})
                    if ptd.get("_native_id"):
                        continue  # BT device from native bridge, not USB-discoverable
                    print(f"  -- Device removed: {did}")
                    self.broadcast({
                        "type": "DeviceDetached",
                        "apiVersion": API_VERSION,
                        "deviceId": did,
                    })
            except Exception as e:
                print(f"  Scanner error: {e}")

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
                            if self.dump_mode and self.dump_file:
                                self.dump_file.write(json.dumps({"dir": "IN", "msg": msg}) + "\n")
                                self.dump_file.flush()

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

    dump_mode = False
    dump_file = None

    def send_msg(self, client_sock, msg):
        """Send a JSON message to a client (SOH-delimited)."""
        try:
            data = json.dumps(msg) + MSG_DELIM
            client_sock.sendall(data.encode("utf-8"))
            if self.dump_mode and self.dump_file:
                self.dump_file.write(json.dumps({"dir": "OUT", "msg": msg}) + "\n")
                self.dump_file.flush()
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

            # Push battery info if available
            self._push_battery(client_sock, did)

        return None

    def _push_battery(self, client_sock, device_id):
        """Push battery info as a compound DeviceSetting."""
        if not self._native_bridge:
            return
        # Match our device ID to native device ID
        dev = self.devices.get(device_id, {})
        ptd = dev.get("_polytool_dev", {})
        native_id = ptd.get("_native_id", "")

        # Also check all battery entries by PID match
        batt = None
        if native_id:
            batt = self._native_bridge.get_battery(native_id)
        if not batt:
            dev_pid = int(dev.get("pid", 0))
            for nid, ndev in self._native_bridge.get_devices().items():
                if ndev.get("pid") == dev_pid:
                    batt = self._native_bridge.get_battery(nid)
                    break
        if not batt or batt.get("level", -1) < 0:
            return

        level = batt["level"]
        # Native bridge uses 0-5 scale, convert to percentage
        level_pct = min(100, level * 20) if level <= 5 else level
        charging = batt.get("charging", False)

        self.send_msg(client_sock, {
            "type": "DeviceSetting",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "setting": {
                "name": "Singular Battery Info",
                "value": None,
                "value_compound": [
                    {"name": "Level", "value": level_pct, "value_int": level_pct},
                    {"name": "Num Levels", "value": 100, "value_int": 100},
                    {"name": "Charging", "value": charging, "value_bool": charging},
                ],
            },
        })
        print(f"  → Battery: {level_pct}%{' (charging)' if charging else ''}")

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
        metadata, values = self._get_device_settings_formatted(device_id)

        # Push metadata FIRST (GUI needs this to know what controls to render)
        if metadata:
            self.send_msg(client_sock, {
                "type": "DeviceSettingsMetadata",
                "apiVersion": API_VERSION,
                "deviceId": device_id,
                "settings": metadata,
            })
            print(f"  → DeviceSettingsMetadata: {len(metadata)} settings")

        # Then send settings values
        self.send_msg(client_sock, {
            "type": "DeviceSettings",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "settings": values,
        })

        return None  # already sent

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
            image_data = self.get_product_images(device_id)
            return make_setting_response(name, image_data)

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
        # Poly Studio sends value in various fields depending on type
        value = msg.get("valueBool",
                msg.get("valueInt",
                msg.get("valueFloat",
                msg.get("valueString",
                msg.get("valueEnum",
                msg.get("value"))))))  # fallback to generic 'value'

        # Store in cache
        if device_id not in self._device_settings_cache:
            self._device_settings_cache[device_id] = {}
        self._device_settings_cache[device_id][name] = value

        # Try writing to device via HID
        success = self.write_device_setting(device_id, name, value)
        print(f"  Set {name} = {value} [{'OK' if success else 'stored'}]")

        # Broadcast the update to all clients
        update_msg = {
            "type": "DeviceSettingUpdated",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "setting": {"name": name, "value": value},
        }
        self.broadcast(update_msg)

        return None  # already broadcast

    # Per-device settings value cache
    _device_settings_cache = {}

    def on_get_settings_metadata(self, msg, client_sock):
        """Handle GetDeviceSettingsMetadata."""
        device_id = msg.get("deviceId", "")
        metadata, _ = self._get_device_settings_formatted(device_id)
        return {
            "type": "DeviceSettingsMetadata",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "settings": metadata,
        }

    # Cache of dynamic settings profiles built from native bridge data
    _dynamic_profiles = {}  # device_id → [setting_defs]

    def _get_device_settings_formatted(self, device_id):
        """Get settings metadata + values in LensServiceApi format."""
        from lens_settings import (get_settings_for_device, settings_to_api_format,
                                   get_device_family)

        dev = self.devices.get(device_id, {})
        ptd = dev.get("_polytool_dev", {})
        usage_page = ptd.get("usage_page", 0)
        dfu_executor = ptd.get("dfu_executor", "")
        pid = ptd.get("pid", 0)
        family = get_device_family(usage_page, dfu_executor, pid=pid)

        # For native bridge devices, use dynamic profile built from actual device capabilities
        if ptd.get("_native_id") and device_id in self._dynamic_profiles:
            settings_defs = self._dynamic_profiles[device_id]
        else:
            settings_defs = get_settings_for_device(usage_page, dfu_executor, pid=pid)

        current_values = self._device_settings_cache.get(device_id, {})

        # DECT/Voyager settings are writable when native bridge is available
        force_writable = False
        if family in ("dect", "voyager_bt", "voyager_base"):
            try:
                from native_bridge import find_components_dir
                force_writable = find_components_dir() is not None
            except Exception:
                pass

        return settings_to_api_format(settings_defs, current_values, family=family,
                                      force_writable=force_writable)

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

    _image_cache = {}  # pid → image JSON string

    def get_product_images(self, device_id):
        """Fetch product images from Poly Cloud for a device."""
        dev = self.devices.get(device_id, {})
        pid = dev.get("productId", "")
        if not pid:
            return None

        # Poly Cloud API wants no leading zeros (e.g. "2ea" not "02ea")
        pid_query = pid.lstrip("0") or pid

        if pid in self._image_cache:
            return self._image_cache[pid]

        try:
            import requests
            query = '''query ($id: ID!) {
              hardwareProduct(id: $id) {
                productImages { edges { node { url } } }
              }
            }'''
            resp = requests.post(
                "https://api.silica-prod01.io.lens.poly.com/graphql",
                json={"query": query, "variables": {"id": pid_query}},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            data = resp.json()
            images = data.get("data", {}).get("hardwareProduct", {}).get("productImages", {}).get("edges", [])

            if images:
                # Return as JSON string — GUI parses it
                url_list = [{"url": img["node"]["url"]} for img in images if img.get("node", {}).get("url")]
                result = json.dumps(url_list)
                self._image_cache[pid] = result
                print(f"  Images for {pid}: {len(url_list)} found")
                return result
        except Exception as e:
            print(f"  Image fetch error: {e}")

        return None

    def discover_devices(self):
        """Scan for Poly devices. Returns set of current device IDs."""
        try:
            from polytool import discover_devices, try_read_device_info, try_read_battery
            raw_devices = discover_devices()
            current_ids = set()
            for dev in raw_devices:
                try_read_device_info(dev)
                try_read_battery(dev)

                device_id = dev.id
                current_ids.add(device_id)

                # Only add if new (don't overwrite existing — preserves state)
                if device_id in self.devices:
                    continue

                self.devices[device_id] = {
                    # camelCase — matches .NET JsonSerializer default naming
                    "deviceId": device_id,
                    "parentId": "",
                    "productName": dev.friendly_name or dev.product_name,
                    "systemName": dev.product_name,
                    "deviceName": dev.product_name,
                    "manufacturerName": dev.manufacturer or "Plantronics",
                    "displaySerialNumber": dev.serial or "",
                    "buildCode": "",
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
                    "firmwareComponents": {
                        "usbVersion": dev.firmware_display,
                        "baseVersion": "",
                        "tuningVersion": "",
                        "picVersion": "",
                        "cameraVersion": "",
                        "headsetVersion": "",
                        "headsetLanguageVersion": "",
                        "bluetoothVersion": "",
                        "setIdVersion": "",
                    },
                    "peerDevices": [],
                    "connectionType": "USB",
                    "connectionDetails": [{"type": "USB", "handledBy": "Legacy Library"}],
                    "hardwareModel": {"supportedByClients": []},
                    "hasChargeCase": False,
                    "deviceEncryption": "Unknown",
                    "dfuMode": False,
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
                        "pid": dev.pid,
                    },
                }

                # Populate settings cache from real HID reads
                self._populate_settings_cache(device_id, dev)

            # Remove USB devices no longer present (preserve native bridge devices)
            removed = set(self.devices.keys()) - current_ids
            for did in removed:
                dev = self.devices.get(did, {})
                ptd = dev.get("_polytool_dev", {})
                if not ptd.get("_native_id"):  # only remove USB devices
                    del self.devices[did]

            return current_ids
        except Exception as e:
            print(f"  Device discovery error: {e}")
            return set()

    def discover_native_devices(self):
        """Discover additional devices via native bridge (BT headsets, etc.)."""
        try:
            from native_bridge import find_components_dir
            if not find_components_dir():
                return
        except Exception:
            return

        bridge = self._get_native_bridge()
        if not bridge:
            return

        native_devs = bridge.get_devices()
        for nid, ndev in native_devs.items():
            # Skip devices we already found via USB
            pid = ndev.get("pid", 0)
            name = ndev.get("name", "")
            model_id = ndev.get("modelId", "")

            # Check if we already have this device (by PID match)
            already_have = any(
                d.get("pid") == str(pid) or d.get("productId") == model_id.lower()
                for d in self.devices.values()
            )
            if already_have:
                continue

            # Generate a stable device ID from native ID
            device_id = f"{int(nid) & 0xFFFFFFFF:08X}"

            fw = ndev.get("firmwareVersion", {})
            fw_display = fw.get("bluetooth", "") or fw.get("usb", "") or fw.get("headset", "")

            # Find serial
            serial = ""
            for sn in ndev.get("serialNumber", []):
                if sn.get("type") == "genes":
                    val = sn.get("value", {})
                    serial = val.get("headset", "") or val.get("base", "")
                    break

            self.devices[device_id] = {
                "deviceId": device_id,
                "parentId": "",
                "productName": name,
                "systemName": name,
                "deviceName": name,
                "manufacturerName": ndev.get("manufacturerName", "Plantronics"),
                "displaySerialNumber": serial,
                "buildCode": "",
                "firmwareVersion": fw_display,
                "serialNumber": serial,
                "tattooSerialNumber": serial,
                "deviceType": "Headset",
                "connected": True,
                "attached": True,
                "pid": str(pid),
                "vid": str(ndev.get("vid", 0)),
                "productId": model_id.lower() if model_id else f"{pid:04x}",
                "macAddress": "",
                "bluetoothAddress": "",
                "hardwareRevision": "",
                "headsetVersion": fw.get("headset", ""),
                "baseVersion": fw.get("base", ""),
                "usbVersion": fw.get("usb", ""),
                "firmwareComponents": fw,
                "peerDevices": [],
                "connectionType": "Bluetooth",
                "connectionDetails": [{"type": "Bluetooth", "handledBy": "Native Bridge"}],
                "hardwareModel": {"supportedByClients": []},
                "hasChargeCase": ndev.get("hasChargeCase", False),
                "deviceEncryption": "Unknown",
                "dfuMode": False,
                "isAbleToBePrimaryForCallControl": ndev.get("canBePrimary", True),
                "isMuted": False,
                "isInCall": False,
                "state": "Online",
                "supportData": {"state": "Supported"},
                "multiComponentState": None,
                "lastAttachedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "_polytool_dev": {
                    "path": b"",
                    "usage_page": 0,
                    "dfu_executor": "btNeoDfu",
                    "pid": pid,
                    "_native_id": nid,
                },
            }
            # Update call/mute state from native bridge
            call_state = bridge.get_call_state()
            self.devices[device_id]["isMuted"] = call_state.get("muted", False)
            self.devices[device_id]["isInCall"] = call_state.get("inCall", False)

            print(f"    {name} (BT via native bridge, fw {fw_display})")

        # Query settings for all native bridge devices and populate cache
        self._populate_native_settings_cache(bridge)

    def _populate_native_settings_cache(self, bridge):
        """Query native bridge for current setting values, populate cache,
        and build dynamic settings profiles for each device."""
        from native_bridge import setting_id_to_name, build_dynamic_profile

        for nid in bridge.get_devices():
            bridge.get_settings(nid)

        # Wait for responses
        time.sleep(3)
        bridge.recv(timeout=1)

        # Map native IDs to our device IDs and populate cache + dynamic profiles
        for did, dev in self.devices.items():
            ptd = dev.get("_polytool_dev", {})
            native_id = ptd.get("_native_id", "")
            if not native_id:
                continue

            native_values = bridge.get_setting_values(native_id)
            if not native_values:
                continue

            # Build dynamic profile from the hex IDs this device actually supports
            hex_ids = list(native_values.keys())
            profile = build_dynamic_profile(hex_ids)
            if profile:
                self._dynamic_profiles[did] = profile

            # Populate settings cache with actual values
            cache = {}
            for hex_id, val in native_values.items():
                setting_name = setting_id_to_name(hex_id)
                if setting_name:
                    if val == "true":
                        val = True
                    elif val == "false":
                        val = False
                    cache[setting_name] = val

            if cache:
                self._device_settings_cache[did] = cache
                print(f"  Read {len(cache)} settings from native bridge for {dev.get('productName', did)}")

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

    def _populate_settings_cache(self, device_id, dev):
        """Read real settings from HID and populate the cache."""
        try:
            from device_settings import read_all_settings
            settings = read_all_settings(
                dev.path, dev.usage_page, dev.dfu_executor,
            )
            if settings:
                cache = {}
                for s in settings:
                    if s.get("value") is not None and s.get("name"):
                        cache[s["name"]] = s["value"]
                if cache:
                    self._device_settings_cache[device_id] = cache
                    print(f"  Read {len(cache)} settings from {device_id} via HID")
        except Exception as e:
            print(f"  HID settings read failed for {device_id}: {e}")

    def write_device_setting(self, device_id, name, value):
        """Write a setting to device. Uses direct HID for CX2070x/BladeRunner,
        native bridge for DECT/Voyager devices."""
        dev = self.devices.get(device_id, {})
        ptd = dev.get("_polytool_dev", {})
        if not ptd:
            return False

        from lens_settings import get_device_family
        family = get_device_family(
            ptd.get("usage_page", 0), ptd.get("dfu_executor", ""),
            pid=ptd.get("pid", 0))

        # DECT / Voyager: write through native bridge
        if family in ("dect", "voyager_bt", "voyager_base"):
            return self._proxy_dect_write(device_id, name, value)

        # CX2070x / BladeRunner: direct HID write
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

    # ── DECT Native Bridge ────────────────────────────────────────────────────
    _native_bridge = None

    def _get_native_bridge(self):
        """Get or initialize the native bridge for DECT settings."""
        if self._native_bridge:
            return self._native_bridge
        try:
            from native_bridge import NativeBridge
            bridge = NativeBridge()
            bridge.start()
            # Wait for device discovery — BT devices take longer than USB.
            # Keep polling until no new devices appear for 3 seconds.
            import time
            last_count = 0
            stable_ticks = 0
            for _ in range(30):  # max 15 seconds
                time.sleep(0.5)
                bridge.recv(timeout=0.1)
                count = len(bridge.get_devices())
                if count > last_count:
                    last_count = count
                    stable_ticks = 0
                else:
                    stable_ticks += 1
                if stable_ticks >= 6 and count > 0:  # 3s of no new devices
                    break
            self._native_bridge = bridge
            devs = bridge.get_devices()
            print(f"  Native bridge: {len(devs)} device(s)")
            for did, dev in devs.items():
                print(f"    native id={did}: {dev.get('name', '?')}")
            return bridge
        except Exception as e:
            print(f"  Native bridge unavailable: {e}")
            return None

    def _proxy_dect_write(self, device_id, name, value):
        """Write a DECT setting via the native bridge (direct dylib call)."""
        bridge = self._get_native_bridge()
        if not bridge:
            print(f"  DECT write failed: native bridge not available")
            return False

        from native_bridge import setting_name_to_id
        setting_id = setting_name_to_id(name)
        if not setting_id:
            print(f"  DECT write: unknown setting '{name}'")
            return False

        # Find the native device ID (int64 from the native library)
        dev = self.devices.get(device_id, {})
        ptd = dev.get("_polytool_dev", {})
        native_dev_id = ptd.get("_native_id")

        # Fallback: match by PID against native bridge devices
        if not native_dev_id:
            dev_pid = int(dev.get("pid", 0))
            for nid, ndev in bridge.get_devices().items():
                if nid and ndev.get("pid") == dev_pid:
                    native_dev_id = nid
                    break

        # Last resort: use first available native device
        if not native_dev_id:
            for nid in bridge.get_devices():
                if nid:
                    native_dev_id = nid
                    break

        if not native_dev_id:
            print(f"  DECT write: no native device ID")
            return False

        result = bridge.set_setting(native_dev_id, setting_id, value)
        if result:
            print(f"  DECT native: {name} ({setting_id}) = {value} [OK]")
            # Wait for confirmation
            bridge.recv(timeout=2.0)
        return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LensServer — Drop-in Poly Lens Control Service replacement",
    )
    parser.add_argument("--port", type=int, default=0, help="TCP port (0=auto)")
    parser.add_argument("--dump", action="store_true", help="Dump all messages to dump.jsonl")
    args = parser.parse_args()

    server = LensServer(port=args.port)
    server.dump_mode = getattr(args, 'dump', False)
    if server.dump_mode:
        server.dump_file = open('dump.jsonl', 'w')
        print(f"  Dumping all messages to dump.jsonl")

    print(f"\n  LensServer — Poly Lens Replacement")
    print(f"  {'='*40}")

    # Discover devices (USB first, then BT via native bridge)
    print(f"\n  Scanning for USB devices...")
    ids = server.discover_devices()
    print(f"  Found {len(ids)} USB device(s)")

    print(f"  Scanning for BT devices via native bridge...")
    server.discover_native_devices()

    print(f"  Total: {len(server.devices)} device(s)")
    for did, dev in server.devices.items():
        conn = "BT" if dev.get("connectionType") == "Bluetooth" else "USB"
        print(f"    {dev['productName']} (fw {dev['firmwareVersion']}) [{conn}]")

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
