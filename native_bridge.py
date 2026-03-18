#!/usr/bin/env python3
"""
Native Bridge — Direct ctypes interface to Poly's NativeLoader dylib.

Loads libNativeLoader.dylib + libPLTDeviceManager.dylib and calls
NativeLoader_SendToNative() to write DECT settings without needing
the full Poly Lens stack (legacyhost/Clockwork/LCS).

The native library handles all USB HID communication internally,
including the proprietary DECT base station protocol for settings.

Usage:
  from native_bridge import NativeBridge
  bridge = NativeBridge()
  bridge.start()
  bridge.set_setting(device_id, "0x601", "true")  # Answering Call = on
  bridge.stop()

Standalone test:
  python3 native_bridge.py              # Discover devices
  python3 native_bridge.py --set 0x10a medium  # Set Base Ringer Volume
"""

import ctypes
import json
import os
import sys
import time
import threading
from pathlib import Path

# ── Library Paths ────────────────────────────────────────────────────────────

COMPONENTS_DIRS = [
    # Poly Studio bundled legacyhost
    Path("/private/tmp/PolyStudio.app/Contents/Helpers/LegacyHostApp.app/Contents/Components"),
    Path("/Applications/Poly Studio.app/Contents/Helpers/LegacyHostApp.app/Contents/Components"),
    # Standalone Poly Lens install
    Path("/Applications/Poly Lens.app/Contents/Helpers/LegacyHostApp.app/Contents/Components"),
    Path.home() / "Library/Application Support/Plantronics/legacyhost/Poly/LegacyHostApp/Components",
]

LIB_NAMES = ["libPLTDeviceManager.dylib", "libNativeLoader.dylib"]


def find_components_dir():
    """Find the directory containing Poly native libraries."""
    for d in COMPONENTS_DIRS:
        if d.exists() and (d / "libNativeLoader.dylib").exists():
            return d
    return None


# ── Callback Type ────────────────────────────────────────────────────────────

# receiver_t = void (*)(const char*)
RECEIVER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p)


# ── Native Bridge ────────────────────────────────────────────────────────────

class NativeBridge:
    """Direct interface to Poly's native HID/DECT settings library."""

    def __init__(self, components_dir=None):
        self._dir = components_dir or find_components_dir()
        if not self._dir:
            raise FileNotFoundError(
                "Poly native libraries not found. Install Poly Studio or Poly Lens.")

        self._libs = []
        self._native_loader = None
        self._running = False
        self._messages = []
        self._message_event = threading.Event()
        self._lock = threading.Lock()
        self._devices = {}
        self._callback_ref = None  # prevent GC of callback

    def start(self):
        """Initialize the native bridge. Loads libraries, starts USB scanning."""
        print(f"  Loading native libraries from {self._dir}")

        # Set rpath so dylibs can find each other
        os.environ["DYLD_LIBRARY_PATH"] = str(self._dir)

        # Load dependencies first, then NativeLoader
        for name in LIB_NAMES:
            path = str(self._dir / name)
            try:
                lib = ctypes.cdll.LoadLibrary(path)
                self._libs.append(lib)
                print(f"  Loaded {name}")
            except OSError as e:
                raise OSError(f"Failed to load {name}: {e}")

        self._native_loader = self._libs[-1]  # libNativeLoader

        # Set up function signatures
        self._native_loader.NativeLoader_Init.argtypes = [RECEIVER_FUNC]
        self._native_loader.NativeLoader_Init.restype = None

        self._native_loader.NativeLoader_SendToNative.argtypes = [ctypes.c_char_p]
        self._native_loader.NativeLoader_SendToNative.restype = ctypes.c_bool

        self._native_loader.NativeLoader_Exit.argtypes = []
        self._native_loader.NativeLoader_Exit.restype = None

        # Create and register callback (prevent GC!)
        self._callback_ref = RECEIVER_FUNC(self._on_received)
        self._native_loader.NativeLoader_Init(self._callback_ref)
        print("  NativeLoader_Init done")

        # Start the native bridge (begins USB device scanning)
        try:
            self._native_loader.StartNativeBridge()
            print("  StartNativeBridge done")
        except Exception as e:
            print(f"  StartNativeBridge: {e}")

        self._running = True

        # Wait a moment for device discovery
        time.sleep(2)

    def stop(self):
        """Shut down the native bridge."""
        if not self._running:
            return
        self._running = False
        try:
            self._native_loader.StopNativeBridge()
        except Exception:
            pass
        try:
            self._native_loader.NativeLoader_Exit()
        except Exception:
            pass
        print("  Native bridge stopped")

    def _on_received(self, data):
        """Callback from native library — receives JSON messages."""
        if not data:
            return
        try:
            msg_str = data.decode("utf-8")
            msg = json.loads(msg_str)
            with self._lock:
                self._messages.append(msg)
                msg_type = msg.get("messageType", "?")
                print(f"  ← Native: {msg_type}: {msg_str[:150]}")

                # Track devices
                if msg_type == "DeviceStateChanged":
                    payload = msg.get("payload", {})
                    dev_id = str(payload.get("deviceId", ""))
                    if dev_id:
                        self._devices[dev_id] = payload

            self._message_event.set()
        except Exception as e:
            print(f"  ← Native (decode error): {e}")

    def send(self, message_type, payload, track_id=None):
        """Send a JSON message to the native library."""
        msg = {"messageType": message_type, "payload": payload}
        if track_id is not None:
            msg["trackId"] = str(track_id)
        data = json.dumps(msg).encode("utf-8")
        result = self._native_loader.NativeLoader_SendToNative(data)
        print(f"  → Native: {message_type} = {'OK' if result else 'FAIL'}")
        return result

    def recv(self, timeout=2.0):
        """Wait for and return received messages."""
        self._message_event.wait(timeout=timeout)
        self._message_event.clear()
        with self._lock:
            msgs = list(self._messages)
            self._messages.clear()
        return msgs

    def get_devices(self):
        """Return discovered devices."""
        return dict(self._devices)

    def set_setting(self, device_id, setting_id, value, track_id=None):
        """Write a setting to a DECT device.

        Args:
            device_id: Device ID (numeric string from native bridge)
            setting_id: Hex setting ID (e.g. "0x601")
            value: Value string (e.g. "true", "medium", "85db")
            track_id: Optional request tracking ID
        """
        return self.send("SetDeviceSettings", {
            "deviceId": str(device_id),
            "settings": [{"id": setting_id, "value": str(value)}],
        }, track_id=track_id)

    def get_settings(self, device_id, track_id=None):
        """Request all settings for a device."""
        return self.send("GetDeviceSettings", {
            "deviceId": str(device_id),
        }, track_id=track_id)


# ── DECT Setting Name → Hex ID Map ──────────────────────────────────────────

DECT_SETTING_IDS = {
    "Second Incoming Call":   "0x102",
    "Computer Volume":        "0x103",
    "Desk Phone Volume":      "0x104",
    "VoIP Interface Ringtone": "0x106",
    "Desk Phone":             "0x107",
    "Base Ringer Volume":     "0x10a",
    "Auto-Answer":            "0x300",
    "Default Line Type":      "0x400",
    "Noise Exposure":         "0x500",
    "Hours on Phone Per Day": "0x501",
    "Anti-Startle":           "0x504",
    "Anti Startle 2":         "0x505",
    "Answering Call":         "0x601",
    "Mute Reminder Time":     "0x603",
    "Mute On/Off Alerts":     "0x607",
    "System Tone Volume":     "0x608",
    "Volume Level Tones":     "0x609",
    "Active Call Audio":      "0x60c",
    "DECT Density":           "0x700",
    "OTA Subscription":       "0x701",
    "Power Level":            "0x702",
    "Multiband Expander":     "0x802",
    "Online Indicator":       "0x902",
    "Wearing Sensor":         "0xa00",
    "Dialtone On/Off":        "0xa01",
    "Keep Link Up":           "0xfff4",
}


def setting_name_to_id(name):
    """Convert a Poly Studio setting name to its DECT hex ID."""
    return DECT_SETTING_IDS.get(name)


# ── Standalone Test ──────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Poly Native Bridge — Direct DECT settings")
    parser.add_argument("--set", nargs=2, metavar=("ID", "VALUE"),
                        help="Set a setting (e.g. --set 0x10a medium)")
    parser.add_argument("--get", action="store_true", help="Get all settings")
    parser.add_argument("--wait", type=float, default=5,
                        help="Wait time for device discovery (default: 5s)")
    args = parser.parse_args()

    print("Poly Native Bridge")
    print("=" * 50)

    bridge = NativeBridge()
    try:
        bridge.start()

        # Wait for device discovery
        print(f"\n  Waiting {args.wait}s for device discovery...")
        time.sleep(args.wait)

        # Drain any messages
        msgs = bridge.recv(timeout=1)
        print(f"  Received {len(msgs)} messages")

        devices = bridge.get_devices()
        print(f"\n  Devices found: {len(devices)}")
        for dev_id, info in devices.items():
            print(f"    {dev_id}: {json.dumps(info)[:100]}")

        if args.set and devices:
            setting_id, value = args.set
            dev_id = list(devices.keys())[0]
            print(f"\n  Setting {setting_id} = {value} on device {dev_id}")
            bridge.set_setting(dev_id, setting_id, value, track_id=1)
            time.sleep(2)
            msgs = bridge.recv(timeout=2)
            for m in msgs:
                print(f"  Response: {json.dumps(m)[:200]}")

        if args.get and devices:
            dev_id = list(devices.keys())[0]
            print(f"\n  Requesting settings for device {dev_id}")
            bridge.get_settings(dev_id, track_id=2)
            time.sleep(3)
            msgs = bridge.recv(timeout=3)
            for m in msgs:
                print(f"  Response: {json.dumps(m)[:200]}")

    except KeyboardInterrupt:
        print("\n  Interrupted")
    finally:
        bridge.stop()

    print("\nDone.")


if __name__ == "__main__":
    main()
