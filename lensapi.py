#!/usr/bin/env python3
"""
LensAPI — Direct TCP client for Poly Lens Control Service (Clockwork).

Connects to LensService via its TCP socket (port read from file),
speaks the LensServiceApi JSON protocol to discover devices,
read/write settings, and trigger firmware updates.

This is the same protocol Poly Studio GUI uses internally.

Protocol (reverse-engineered from Poly Studio app.asar):
  Port file: ~/Library/Application Support/Poly/Lens Control Service/SocketPortNumber
  Transport: TCP with newline-delimited JSON
  Message format: {"type": "MessageType", "apiVersion": "1.14.1", ...}
  Registration: {"type": "RegisterClient", "name": "PolyTool", "useEncryption": false}

Usage:
  python3 lensapi.py discover        # Find devices + all settings per device
  python3 lensapi.py devices         # List connected devices
  python3 lensapi.py settings <id>   # Read all settings for a device
  python3 lensapi.py get <id> <name> # Read one setting
  python3 lensapi.py set <id> <name> <value>  # Write a setting
  python3 lensapi.py monitor         # Watch real-time events
  python3 lensapi.py dump            # Export everything as JSON
"""

import json
import os
import sys
import socket
import time
import argparse
import threading
from pathlib import Path


# ── Port Discovery ────────────────────────────────────────────────────────────

def find_lcs_port():
    """Read the LensService TCP port from its port file."""
    home = Path.home()
    # macOS
    port_file = home / "Library/Application Support/Poly/Lens Control Service/SocketPortNumber"
    if port_file.exists():
        try:
            return int(port_file.read_text().strip())
        except (ValueError, OSError):
            pass

    # Windows
    prog_data = os.environ.get("PROGRAMDATA", "")
    if prog_data:
        port_file = Path(prog_data) / "Poly/Lens Control Service/SocketPortNumber"
        if port_file.exists():
            try:
                return int(port_file.read_text().strip())
            except (ValueError, OSError):
                pass

    return None


# ── LensServiceApi Client ────────────────────────────────────────────────────

API_VERSION = "1.14.1"
CLIENT_NAME = "PolyTool"
MSG_DELIM = "\x01"  # SOH byte — LensService message separator


class LensAPIClient:
    """TCP client for LensService API."""

    def __init__(self):
        self.sock = None
        self.buffer = ""
        self.handlers = {}
        self.devices = {}      # deviceId → device info
        self.pending = {}      # type → callback
        self.events = []       # collected events
        self._lock = threading.Lock()

    def connect(self, port, host="127.0.0.1"):
        """Connect to LensService TCP socket."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        self.sock.settimeout(5)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None

    def send(self, msg):
        """Send a JSON message (SOH-delimited)."""
        data = json.dumps(msg) + MSG_DELIM
        self.sock.sendall(data.encode("utf-8"))

    def recv(self, timeout=5):
        """Receive and parse one JSON message."""
        self.sock.settimeout(timeout)
        try:
            while MSG_DELIM not in self.buffer:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return None
                self.buffer += chunk.decode("utf-8", errors="ignore")

            line, self.buffer = self.buffer.split(MSG_DELIM, 1)
            if line.strip():
                return json.loads(line)
            return None
        except socket.timeout:
            return None
        except json.JSONDecodeError:
            return None

    def recv_all(self, timeout=3):
        """Receive all pending messages within timeout."""
        messages = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            msg = self.recv(timeout=remaining)
            if msg:
                messages.append(msg)
            else:
                break
        return messages

    def register(self):
        """Register as a client with LensService."""
        self.send({
            "type": "RegisterClient",
            "apiVersion": API_VERSION,
            "name": CLIENT_NAME,
            "useEncryption": False,
        })
        # Wait for ClientRegistered response
        msgs = self.recv_all(timeout=3)
        for msg in msgs:
            if msg.get("type") == "ClientRegistered":
                return True
            self._handle_event(msg)
        return False

    def get_device_list(self):
        """Request device list."""
        self.send({"type": "GetDeviceList", "apiVersion": API_VERSION})
        msgs = self.recv_all(timeout=5)
        for msg in msgs:
            if msg.get("type") == "DeviceList":
                devices = msg.get("devices", [])
                for d in devices:
                    did = d.get("deviceId", "")
                    if did:
                        self.devices[did] = d
                return devices
            self._handle_event(msg)
        return []

    def get_device_settings(self, device_id):
        """Get all settings for a device."""
        self.send({
            "type": "GetDeviceSettings",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
        })
        msgs = self.recv_all(timeout=5)
        for msg in msgs:
            if msg.get("type") == "DeviceSettings":
                return msg.get("settings", [])
            self._handle_event(msg)
        return []

    def get_device_setting(self, device_id, setting_name):
        """Get a single setting value."""
        self.send({
            "type": "GetDeviceSetting",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "name": setting_name,
        })
        msgs = self.recv_all(timeout=3)
        for msg in msgs:
            if msg.get("type") == "DeviceSetting":
                return msg
            self._handle_event(msg)
        return None

    def set_device_setting(self, device_id, setting_name, value):
        """Set a device setting."""
        msg = {
            "type": "SetDeviceSetting",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "name": setting_name,
        }
        # Set the right value field based on type
        if isinstance(value, bool):
            msg["valueBool"] = value
        elif isinstance(value, int):
            msg["valueInt"] = value
        elif isinstance(value, float):
            msg["valueFloat"] = value
        else:
            msg["valueString"] = str(value)

        self.send(msg)
        msgs = self.recv_all(timeout=3)
        for msg in msgs:
            if msg.get("type") == "DeviceSettingUpdated":
                return True
            self._handle_event(msg)
        return False

    def get_settings_metadata(self, device_id):
        """Get settings metadata (what settings are available)."""
        self.send({
            "type": "GetDeviceSettingsMetadata",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
        })
        msgs = self.recv_all(timeout=5)
        for msg in msgs:
            if msg.get("type") == "DeviceSettingsMetadata":
                return msg.get("settings", [])
            self._handle_event(msg)
        return []

    def get_dfu_status(self, device_id):
        """Get firmware update status."""
        self.send({
            "type": "GetDeviceDFUStatus",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
        })
        msgs = self.recv_all(timeout=3)
        for msg in msgs:
            if msg.get("type") == "DeviceDFUStatus":
                return msg
            self._handle_event(msg)
        return None

    def _handle_event(self, msg):
        """Handle asynchronous events."""
        msg_type = msg.get("type", "")
        if msg_type == "DeviceAttached":
            dev = msg.get("device", msg)
            did = dev.get("deviceId", "")
            if did:
                self.devices[did] = dev
        elif msg_type == "DeviceDetached":
            did = msg.get("deviceId", "")
            self.devices.pop(did, None)
        elif msg_type == "DeviceUpdated":
            dev = msg.get("device", msg)
            did = dev.get("deviceId", "")
            if did:
                self.devices[did] = dev

        with self._lock:
            self.events.append(msg)


# ── CLI Commands ──────────────────────────────────────────────────────────────

def connect_client():
    """Connect and register a LensAPI client."""
    port = find_lcs_port()
    if not port:
        print("LensService not running (port file not found).")
        print(f"  Expected: ~/Library/Application Support/Poly/Lens Control Service/SocketPortNumber")
        sys.exit(1)

    print(f"Connecting to LensService on port {port}...")
    client = LensAPIClient()
    client.connect(port)

    if client.register():
        print(f"Registered as '{CLIENT_NAME}'")
    else:
        print("Warning: registration response not received, continuing...")

    # Collect initial device events
    time.sleep(1)
    client.recv_all(timeout=2)

    # Request device list
    devices = client.get_device_list()
    print(f"Found {len(devices)} device(s)\n")

    return client


def cmd_devices(client, args):
    """List connected devices."""
    for did, dev in client.devices.items():
        name = dev.get("productName", dev.get("deviceName", "Unknown"))
        fw = dev.get("firmwareVersion", "?")
        serial = dev.get("serialNumber", "?")
        dtype = dev.get("deviceType", "?")
        print(f"  {name}")
        print(f"    ID:       {did}")
        print(f"    Firmware: {fw}")
        print(f"    Serial:   {serial}")
        print(f"    Type:     {dtype}")
        print()


def cmd_discover(client, args):
    """Discover all devices and their supported settings."""
    for did, dev in client.devices.items():
        name = dev.get("productName", dev.get("deviceName", "Unknown"))
        print(f"{'='*60}")
        print(f"  {name} (ID: {did})")
        print(f"{'='*60}")

        # Get settings metadata first
        metadata = client.get_settings_metadata(did)
        if metadata:
            print(f"\n  Settings Metadata ({len(metadata)} settings):")
            for m in metadata:
                mname = m.get("name", "?")
                mtype = m.get("dataType", "?")
                readable = m.get("readable", False)
                writable = m.get("writable", False)
                rw = "RW" if readable and writable else "R" if readable else "W" if writable else "-"
                print(f"    [{rw}] {mname:<35} ({mtype})")

        # Get current setting values
        settings = client.get_device_settings(did)
        if settings:
            print(f"\n  Current Values ({len(settings)} settings):")
            for s in settings:
                sname = s.get("name", "?")
                # Value can be in different fields
                val = s.get("value", s.get("valueBool", s.get("valueInt",
                      s.get("valueFloat", s.get("valueString",
                      s.get("valueEnum", "?"))))))
                print(f"    {sname:<35} = {val}")

        # DFU status
        dfu = client.get_dfu_status(did)
        if dfu:
            print(f"\n  Firmware Status:")
            print(f"    Version: {dfu.get('version', '?')}")
            print(f"    Update:  {dfu.get('status', '?')}")

        print()


def cmd_settings(client, args):
    """Read all settings for a device."""
    did = args.device_id
    if did not in client.devices:
        # Try matching by partial ID or name
        for d, info in client.devices.items():
            if did in d or did.lower() in info.get("productName", "").lower():
                did = d
                break

    settings = client.get_device_settings(did)
    if not settings:
        print(f"No settings found for device {did}")
        return

    name = client.devices.get(did, {}).get("productName", did)
    print(f"\n  {name} — {len(settings)} settings:\n")
    for s in settings:
        sname = s.get("name", "?")
        val = s.get("value", s.get("valueBool", s.get("valueInt",
              s.get("valueFloat", s.get("valueString",
              s.get("valueEnum", "?"))))))
        print(f"    {sname:<35} = {val}")


def cmd_get(client, args):
    """Read one setting."""
    did = args.device_id
    result = client.get_device_setting(did, args.setting)
    if result:
        val = result.get("value", result.get("valueBool", result.get("valueInt", "?")))
        print(f"  {args.setting} = {val}")
    else:
        print(f"  {args.setting} = (not found)")


def cmd_set(client, args):
    """Write a setting."""
    value = args.value
    if value.lower() == "true":
        value = True
    elif value.lower() == "false":
        value = False
    else:
        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                pass

    ok = client.set_device_setting(args.device_id, args.setting, value)
    print(f"  {args.setting} = {args.value} [{'OK' if ok else 'FAILED'}]")


def cmd_dump(client, args):
    """Export everything as JSON."""
    output = {"devices": []}
    for did, dev in client.devices.items():
        settings = client.get_device_settings(did)
        metadata = client.get_settings_metadata(did)
        output["devices"].append({
            "deviceId": did,
            "info": dev,
            "settings": settings,
            "metadata": metadata,
        })
    print(json.dumps(output, indent=2, default=str))


def cmd_monitor(client, args):
    """Watch real-time events."""
    print("Monitoring LensService events (Ctrl+C to stop)...\n")
    try:
        while True:
            msg = client.recv(timeout=30)
            if msg:
                ts = time.strftime("%H:%M:%S")
                mtype = msg.get("type", "?")
                if mtype == "DeviceSettingUpdated":
                    name = msg.get("name", "?")
                    val = msg.get("value", msg.get("valueBool", msg.get("valueInt", "?")))
                    print(f"  [{ts}] Setting: {name} = {val}")
                elif mtype == "DeviceAttached":
                    dev = msg.get("device", msg)
                    print(f"  [{ts}] Attached: {dev.get('productName', '?')}")
                elif mtype == "DeviceDetached":
                    print(f"  [{ts}] Detached: {msg.get('deviceId', '?')}")
                else:
                    detail = json.dumps(msg)[:100]
                    print(f"  [{ts}] {mtype}: {detail}")
    except KeyboardInterrupt:
        print("\n  Stopped.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LensAPI — Direct TCP client for Poly Lens Control Service",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("devices", help="List connected devices")
    sub.add_parser("discover", help="Discover devices and all settings")

    sp = sub.add_parser("settings", help="Read all settings for a device")
    sp.add_argument("device_id", help="Device ID")

    gp = sub.add_parser("get", help="Read one setting")
    gp.add_argument("device_id")
    gp.add_argument("setting")

    setp = sub.add_parser("set", help="Write a setting")
    setp.add_argument("device_id")
    setp.add_argument("setting")
    setp.add_argument("value")

    sub.add_parser("dump", help="Export everything as JSON")
    sub.add_parser("monitor", help="Watch real-time events")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    client = connect_client()
    try:
        cmds = {
            "devices": cmd_devices,
            "discover": cmd_discover,
            "settings": cmd_settings,
            "get": cmd_get,
            "set": cmd_set,
            "dump": cmd_dump,
            "monitor": cmd_monitor,
        }
        cmds[args.command](client, args)
    finally:
        client.close()


if __name__ == "__main__":
    main()
