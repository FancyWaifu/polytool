#!/usr/bin/env python3
"""
Clockwork Client — Connect to Poly Lens' local service to discover
and modify headset settings.

Poly Lens runs Clockwork (LensService) which communicates via a Unix
domain socket. By connecting as a client, we can query and change
ANY setting on ANY connected device — using Poly's own protocol
translation layer.

This works alongside Poly Lens without modifying it.

Usage:
  python3 clockwork_client.py discover        # Find devices and all their settings
  python3 clockwork_client.py get <setting>   # Read a setting
  python3 clockwork_client.py set <setting> <value>  # Write a setting
  python3 clockwork_client.py dump            # Dump all settings for all devices
  python3 clockwork_client.py monitor         # Watch for device events

Protocol (reverse-engineered from Poly Lens):
  Socket: ~/Library/Application Support/Poly/ClockworkLegacyServer
  Framing: [4-byte LE uint32 length] [JSON payload]
  Flow: Connect → INIT → READY → DEVICE_REQUEST/DEVICE_RESPONSE
"""

import json
import os
import sys
import struct
import socket
import time
import argparse
from pathlib import Path

# ── Socket Path ──────────────────────────────────────────────────────────────

def get_socket_path():
    """Find the Clockwork socket."""
    home = Path.home()
    # macOS
    mac_path = home / "Library/Application Support/Poly/ClockworkLegacyServer"
    if mac_path.exists():
        return str(mac_path)
    # Check if running under sudo
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        alt = Path(f"/Users/{sudo_user}/Library/Application Support/Poly/ClockworkLegacyServer")
        if alt.exists():
            return str(alt)
    return None


# ── IPC Protocol ─────────────────────────────────────────────────────────────

class ClockworkConnection:
    """Client connection to Clockwork (LensService)."""

    def __init__(self):
        self.sock = None
        self.devices = {}       # device_id → device info from ATTACH
        self.request_id = 1000  # start high to avoid collisions

    def connect(self, socket_path):
        """Connect to Clockwork socket."""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(socket_path)
        self.sock.settimeout(10)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None

    def send_msg(self, msg):
        """Send a length-prefixed JSON message."""
        data = json.dumps(msg).encode('utf-8')
        header = struct.pack('<I', len(data))
        self.sock.sendall(header + data)

    def recv_msg(self, timeout=5):
        """Receive a length-prefixed JSON message."""
        self.sock.settimeout(timeout)
        try:
            # Read 4-byte length
            header = b''
            while len(header) < 4:
                chunk = self.sock.recv(4 - len(header))
                if not chunk:
                    return None
                header += chunk
            length = struct.unpack('<I', header)[0]

            # Read payload
            data = b''
            while len(data) < length:
                chunk = self.sock.recv(min(length - len(data), 65536))
                if not chunk:
                    return None
                data += chunk

            return json.loads(data.decode('utf-8'))
        except socket.timeout:
            return None
        except Exception:
            return None

    def handshake(self):
        """Perform INIT/READY handshake with Clockwork."""
        # Wait for INIT
        msg = self.recv_msg(timeout=10)
        if not msg or msg.get("command") != "INIT":
            raise RuntimeError(f"Expected INIT, got: {msg}")

        service_version = msg.get("service_version", "unknown")

        # Send READY
        self.send_msg({
            "client_version": "1.0.0-polytool",
            "command": "READY",
            "error_desc": "",
            "successful": True,
        })

        return service_version

    def collect_devices(self, timeout=5):
        """Collect ATTACH messages to learn about connected devices."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.recv_msg(timeout=max(0.5, deadline - time.time()))
            if not msg:
                continue
            cmd = msg.get("command", "")
            if cmd == "ATTACH":
                dev = msg.get("device", {})
                device_id = dev.get("device_id", "")
                if device_id:
                    self.devices[device_id] = dev
            elif cmd == "DETACH":
                device_id = msg.get("device_id", "")
                self.devices.pop(device_id, None)

    def request_setting(self, device_id, setting_name):
        """Send a DEVICE_REQUEST to read a setting. Returns value or None."""
        self.request_id += 1
        req_id = self.request_id

        self.send_msg({
            "command": "DEVICE_REQUEST",
            "device_id": device_id,
            "request_id": req_id,
            "params": {
                "name": setting_name,
            },
        })

        # Wait for DEVICE_RESPONSE with matching request_id
        deadline = time.time() + 3
        while time.time() < deadline:
            msg = self.recv_msg(timeout=max(0.5, deadline - time.time()))
            if not msg:
                continue
            if msg.get("command") == "DEVICE_RESPONSE" and msg.get("request_id") == req_id:
                result = msg.get("result", -1)
                if result == 0:
                    reply = msg.get("reply", {})
                    setting = reply.get("setting", {})
                    return setting.get("value")
                return None
            # Might get other messages — ignore them
        return None

    def write_setting(self, device_id, setting_name, value):
        """Send a DEVICE_REQUEST to write a setting. Returns True on success."""
        self.request_id += 1
        req_id = self.request_id

        self.send_msg({
            "command": "DEVICE_REQUEST",
            "device_id": device_id,
            "request_id": req_id,
            "params": {
                "name": setting_name,
                "value": value,
            },
        })

        deadline = time.time() + 3
        while time.time() < deadline:
            msg = self.recv_msg(timeout=max(0.5, deadline - time.time()))
            if not msg:
                continue
            if msg.get("command") == "DEVICE_RESPONSE" and msg.get("request_id") == req_id:
                return msg.get("result", -1) == 0
        return False


# ── Known Settings ────────────────────────────────────────────────────────────
# These are the setting names used by Poly Lens (from Clockwork IPC captures).

ALL_KNOWN_SETTINGS = [
    # Audio
    "Sidetone Level",
    "Sidetone On/Off",
    "EQ Preset",
    "Volume Level",
    "Microphone Level",
    "HD Voice",
    "Ringtone Volume",
    "Mute On/Off",
    "Mute Reminder Tone",

    # Protection
    "Anti-Startle Protection",
    "Noise Limiting",
    "G616 Limiting",

    # Behavior
    "Auto-Answer",
    "Auto-Disconnect",
    "Wearing Sensor On/Off",
    "IntelliStand On/Off",
    "Audio Sensing",
    "Online Indicator",
    "Second Incoming Call",

    # Wireless
    "Range",
    "Over-The-Air Subscription",
    "Wireless Link",

    # DECT
    "Base Ringer Volume",
    "Base Ringer Tone",
    "Auto-Dial",
    "Default Line",

    # System
    "Language Selection",
    "Custom Name",
    "Restore Defaults",

    # Info (read-only)
    "Device Info Version SW",
    "Device Info Main MAC Address",
    "Set ID",
    "Product Parent ID",
    "device_type",
    "Bladerunner DFU Protocol Type",
]


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_discover(cw):
    """Discover all devices and probe their supported settings."""
    print(f"\nDevices found: {len(cw.devices)}\n")

    for device_id, dev in cw.devices.items():
        name = dev.get("product_name", "Unknown")
        pid = dev.get("product_id", "?")
        fw = dev.get("firmware_version", "?")
        serial = dev.get("serial_number", "?")

        print(f"{'='*60}")
        print(f"{name} (PID: {pid})")
        print(f"  Firmware: {fw}")
        print(f"  Serial:   {serial}")
        print(f"  ID:       {device_id}")
        print(f"{'='*60}")

        print(f"\n  Probing {len(ALL_KNOWN_SETTINGS)} known settings...")
        supported = []
        unsupported = []

        for setting_name in ALL_KNOWN_SETTINGS:
            value = cw.request_setting(device_id, setting_name)
            if value is not None:
                supported.append((setting_name, value))
                print(f"    {setting_name:<35} = {value}")
            else:
                unsupported.append(setting_name)

        print(f"\n  Supported: {len(supported)}/{len(ALL_KNOWN_SETTINGS)}")
        print(f"  Unsupported: {', '.join(unsupported[:5])}{'...' if len(unsupported) > 5 else ''}")
        print()


def cmd_dump(cw, args):
    """Dump all settings as JSON."""
    all_devices = []
    for device_id, dev in cw.devices.items():
        settings = {}
        for name in ALL_KNOWN_SETTINGS:
            val = cw.request_setting(device_id, name)
            if val is not None:
                settings[name] = val
        all_devices.append({
            "device_id": device_id,
            "product_name": dev.get("product_name", ""),
            "product_id": dev.get("product_id", ""),
            "firmware_version": dev.get("firmware_version", ""),
            "serial_number": dev.get("serial_number", ""),
            "settings": settings,
        })

    output = {"devices": all_devices, "count": len(all_devices)}
    print(json.dumps(output, indent=2))


def cmd_get(cw, args):
    """Read a setting from all devices."""
    for device_id, dev in cw.devices.items():
        name = dev.get("product_name", "Unknown")
        value = cw.request_setting(device_id, args.setting)
        if value is not None:
            print(f"  {name:<35} {args.setting} = {value}")
        else:
            print(f"  {name:<35} {args.setting} = (not supported)")


def cmd_set(cw, args):
    """Write a setting to all devices (or --device-id target)."""
    value = args.value
    # Auto-convert types
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

    for device_id, dev in cw.devices.items():
        if args.device_id and args.device_id != device_id:
            continue
        name = dev.get("product_name", "Unknown")
        ok = cw.write_setting(device_id, args.setting, value)
        status = "OK" if ok else "FAILED"
        print(f"  {name:<35} {args.setting} = {value} [{status}]")


def cmd_monitor(cw):
    """Monitor device events from Clockwork."""
    print("Monitoring Clockwork events (Ctrl+C to stop)...\n")
    try:
        while True:
            msg = cw.recv_msg(timeout=30)
            if msg:
                cmd = msg.get("command", "?")
                ts = time.strftime("%H:%M:%S")
                if cmd == "ATTACH":
                    dev = msg.get("device", {})
                    print(f"  [{ts}] ATTACH: {dev.get('product_name', '?')} (ID: {dev.get('device_id', '?')})")
                    cw.devices[dev.get("device_id", "")] = dev
                elif cmd == "DETACH":
                    did = msg.get("device_id", "?")
                    print(f"  [{ts}] DETACH: {did}")
                    cw.devices.pop(did, None)
                elif cmd == "DEVICE_RESPONSE":
                    reply = msg.get("reply", {})
                    setting = reply.get("setting", {})
                    print(f"  [{ts}] RESPONSE: {setting.get('name', '?')} = {setting.get('value', '?')}")
                else:
                    detail = json.dumps(msg)[:100]
                    print(f"  [{ts}] {cmd}: {detail}")
    except KeyboardInterrupt:
        print("\n  Stopped.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Clockwork Client — Discover and modify headset settings via Poly Lens",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  discover      Find all devices and probe supported settings
  dump          Export all settings as JSON
  get <name>    Read a setting from connected devices
  set <name> <value>  Write a setting
  monitor       Watch Clockwork events in real-time
  settings      List all known setting names

Requires Poly Lens to be running.
""")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("discover", help="Discover devices and their settings")
    sub.add_parser("dump", help="Dump all settings as JSON")

    get_p = sub.add_parser("get", help="Read a setting")
    get_p.add_argument("setting", help="Setting name")

    set_p = sub.add_parser("set", help="Write a setting")
    set_p.add_argument("setting", help="Setting name")
    set_p.add_argument("value", help="New value")
    set_p.add_argument("--device-id", help="Target specific device ID")

    sub.add_parser("monitor", help="Monitor Clockwork events")
    sub.add_parser("settings", help="List all known setting names")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "settings":
        print(f"\nKnown Poly Lens settings ({len(ALL_KNOWN_SETTINGS)}):\n")
        for s in ALL_KNOWN_SETTINGS:
            print(f"  {s}")
        return

    # Find socket
    socket_path = get_socket_path()
    if not socket_path:
        print("Clockwork socket not found. Is Poly Lens running?")
        print(f"  Expected: ~/Library/Application Support/Poly/ClockworkLegacyServer")
        sys.exit(1)

    print(f"Connecting to Clockwork at {socket_path}...")

    cw = ClockworkConnection()
    try:
        cw.connect(socket_path)
        version = cw.handshake()
        print(f"Connected! Clockwork v{version}")

        # Collect device ATTACHes
        print("Waiting for device reports (5s)...")
        cw.collect_devices(timeout=5)

        if not cw.devices:
            print("No devices reported by Clockwork.")
            cw.close()
            return

        print(f"Found {len(cw.devices)} device(s)")

        if args.command == "discover":
            cmd_discover(cw)
        elif args.command == "dump":
            cmd_dump(cw, args)
        elif args.command == "get":
            cmd_get(cw, args)
        elif args.command == "set":
            cmd_set(cw, args)
        elif args.command == "monitor":
            cmd_monitor(cw)

    except Exception as e:
        print(f"Error: {e}")
    finally:
        cw.close()


if __name__ == "__main__":
    main()
