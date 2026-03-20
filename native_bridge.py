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

import platform as _platform

def _build_components_dirs():
    """Build platform-specific list of directories to search for native libraries."""
    dirs = []
    if sys.platform == "win32":
        # Windows Poly Lens install locations
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        pdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        localappdata = os.environ.get("LOCALAPPDATA", "")

        for base in [pf, pf86]:
            # Poly Studio install
            dirs.append(Path(base) / "Poly" / "Poly Studio" / "LegacyHost")
            # Poly Lens Desktop install
            dirs.append(Path(base) / "Poly" / "Poly Lens Desktop" / "LegacyHostApp" / "Components")
            dirs.append(Path(base) / "Plantronics" / "Poly Lens Desktop" / "LegacyHostApp" / "Components")
        dirs.append(Path(pdata) / "Plantronics" / "legacyhost" / "Poly" / "LegacyHostApp" / "Components")
        if localappdata:
            dirs.append(Path(localappdata) / "Programs" / "Poly Studio" / "resources" / "LegacyHostApp" / "Components")
    else:
        # macOS
        dirs = [
            Path("/private/tmp/PolyStudio.app/Contents/Helpers/LegacyHostApp.app/Contents/Components"),
            Path("/Applications/Poly Studio.app/Contents/Helpers/LegacyHostApp.app/Contents/Components"),
            Path("/Applications/Poly Lens.app/Contents/Helpers/LegacyHostApp.app/Contents/Components"),
            Path.home() / "Library/Application Support/Plantronics/legacyhost/Poly/LegacyHostApp/Components",
        ]
    return dirs

COMPONENTS_DIRS = _build_components_dirs()

def _get_lib_names():
    """Return platform-specific library names."""
    if sys.platform == "win32":
        return [
            "PLTDeviceManager.dll", "libPLTDeviceManager.dll",
            "NativeLoader.dll", "libNativeLoader.dll",
        ]
    else:
        return ["libPLTDeviceManager.dylib", "libNativeLoader.dylib"]

def _get_loader_lib_name():
    """Return the NativeLoader library name to probe for in find_components_dir()."""
    if sys.platform == "win32":
        return "NativeLoader.dll"
    else:
        return "libNativeLoader.dylib"

LIB_NAMES = _get_lib_names()


def find_components_dir():
    """Find the directory containing Poly native libraries."""
    loader_name = _get_loader_lib_name()
    for d in COMPONENTS_DIRS:
        if d.exists() and ((d / loader_name).exists() or (d / ("lib" + loader_name)).exists()):
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
        self._battery = {}          # dev_id → {level, charging, docked}
        self._settings_cache = {}   # dev_id → {hex_id: value}
        self._in_call = False
        self._muted = False
        self._primary_device = ""
        self._callback_ref = None   # prevent GC of callback

    def start(self):
        """Initialize the native bridge. Loads libraries, starts USB scanning."""
        print(f"  Loading native libraries from {self._dir}")

        if sys.platform == "win32":
            # Windows: add DLL directory so dependent DLLs can be found
            os.add_dll_directory(str(self._dir))
        else:
            # macOS: set rpath so dylibs can find each other
            os.environ["DYLD_LIBRARY_PATH"] = str(self._dir)

        # Load dependencies first, then NativeLoader
        # On Windows, try both with and without "lib" prefix
        loaded_names = []
        for name in LIB_NAMES:
            path = self._dir / name
            if not path.exists():
                continue
            try:
                if sys.platform == "win32":
                    lib = ctypes.cdll.LoadLibrary(str(path))
                else:
                    lib = ctypes.cdll.LoadLibrary(str(path))
                self._libs.append(lib)
                loaded_names.append(name)
                print(f"  Loaded {name}")
            except OSError as e:
                raise OSError(f"Failed to load {name}: {e}")

        if not self._libs:
            raise OSError(f"No native libraries found in {self._dir}")

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
        # NativeLoader_Exit triggers cleanup threads that may race on shutdown.
        # Suppress the C++ exception by giving threads time to wind down.
        try:
            self._native_loader.NativeLoader_Exit()
        except Exception:
            pass
        import time
        time.sleep(0.5)
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

                # Track devices from DeviceList and DeviceStateChanged
                if msg_type == "DeviceList":
                    payload = msg.get("payload", [])
                    if isinstance(payload, list):
                        for dev in payload:
                            dev_id = str(dev.get("id", ""))
                            if dev_id:
                                self._devices[dev_id] = dev
                elif msg_type == "DeviceStateChanged":
                    payload = msg.get("payload", {})
                    dev_id = str(payload.get("deviceId", "") or payload.get("id", ""))
                    if dev_id:
                        self._devices[dev_id] = payload

                # Track battery state
                elif msg_type == "BatteryState":
                    payload = msg.get("payload", {})
                    dev_id = str(payload.get("deviceId", ""))
                    if dev_id:
                        self._battery[dev_id] = {
                            "level": payload.get("batteryLevel", -1),
                            "charging": payload.get("chargingState", False),
                            "docked": payload.get("docked", False),
                        }

                # Track setting values from DeviceSettings responses
                elif msg_type == "DeviceSettings":
                    payload = msg.get("payload", {})
                    dev_id = str(payload.get("deviceId", ""))
                    for s in payload.get("settings", []):
                        sid = s.get("id", "")
                        val = s.get("value", "")
                        if dev_id and sid:
                            if dev_id not in self._settings_cache:
                                self._settings_cache[dev_id] = {}
                            self._settings_cache[dev_id][sid] = val

                # Track call/mute state
                elif msg_type == "InCall":
                    payload = msg.get("payload", {})
                    self._in_call = payload.get("inCall", False)
                elif msg_type == "PrimaryDevice":
                    payload = msg.get("payload", {})
                    self._muted = payload.get("muted", False)
                    self._primary_device = str(payload.get("deviceId", ""))

            self._message_event.set()
        except Exception as e:
            print(f"  ← Native (decode error): {e}")

    _track_counter = 0

    def send(self, message_type, payload, track_id=None):
        """Send a JSON message to the native library."""
        if track_id is None:
            self._track_counter += 1
            track_id = self._track_counter
        msg = {"messageType": message_type, "payload": payload, "trackId": str(track_id)}
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

    def get_battery(self, device_id=None):
        """Return battery info. If device_id is None, return all."""
        if device_id:
            return self._battery.get(device_id)
        return dict(self._battery)

    def get_setting_values(self, device_id):
        """Return cached setting values {hex_id: value} for a device."""
        return dict(self._settings_cache.get(device_id, {}))

    def get_call_state(self):
        """Return current call/mute state."""
        return {
            "inCall": self._in_call,
            "muted": self._muted,
            "primaryDevice": self._primary_device,
        }

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

# ── Unified Setting ID Map ───────────────────────────────────────────────────
# Master map: hex_id → {name, type, choices, default}
# Covers ALL known Poly setting IDs across all device families.
# Used to dynamically build settings profiles for any device.

ALL_SETTING_DEFS = {
    "0x102": {"name": "Second Incoming Call", "type": "enum", "choices": ["ignore", "once", "continuous"], "default": "ignore"},
    "0x103": {"name": "Computer Volume", "type": "enum", "choices": ["off", "low", "standard"], "default": "standard"},
    "0x104": {"name": "Desk Phone Volume", "type": "enum", "choices": ["off", "low", "standard"], "default": "standard"},
    "0x106": {"name": "VoIP Interface Ringtone", "type": "enum", "choices": ["sound1", "sound2", "sound3"], "default": "sound1"},
    "0x107": {"name": "Desk Phone", "type": "enum", "choices": ["sound1", "sound2", "sound3", "off"], "default": "sound1"},
    "0x109": {"name": "Ringtone", "type": "bool", "default": True},
    "0x10a": {"name": "Base Ringer Volume", "type": "enum", "choices": ["off", "low", "medium", "high"], "default": "medium"},
    "0x300": {"name": "Auto-Answer", "type": "bool", "default": False},
    "0x400": {"name": "Default Line Type", "type": "enum", "choices": ["pstn", "voip", "mobile"], "default": "voip"},
    "0x500": {"name": "Noise Exposure", "type": "enum", "choices": ["off", "85db", "80db"], "default": "off"},
    "0x501": {"name": "Hours on Phone Per Day", "type": "enum", "choices": ["2", "4", "6", "8", "off"], "default": "8"},
    "0x504": {"name": "Anti-Startle", "type": "bool", "default": False},
    "0x505": {"name": "Anti Startle 2", "type": "bool", "default": True},
    "0x601": {"name": "Answering Call", "type": "bool", "default": True},
    "0x603": {"name": "Mute Reminder Time", "type": "enum", "choices": ["off", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"], "default": "15"},
    "0x607": {"name": "Mute On/Off Alerts", "type": "enum", "choices": ["singleTone", "doubleTone", "voice"], "default": "voice"},
    "0x608": {"name": "System Tone Volume", "type": "enum", "choices": ["off", "low", "standard"], "default": "standard"},
    "0x609": {"name": "Volume Level Tones", "type": "enum", "choices": ["atEveryLevel", "minMaxOnly"], "default": "atEveryLevel"},
    "0x60c": {"name": "Active Call Audio", "type": "bool", "default": False},
    "0x60d": {"name": "Mute Off Alert", "type": "enum", "choices": ["off", "timed", "voiceAudible", "voiceVisible", "voiceVisibleAndAudible"], "default": "voiceAudible"},
    "0x700": {"name": "DECT Density", "type": "enum", "choices": ["homeMode", "enterpriseMode", "mono"], "default": "mono"},
    "0x701": {"name": "OTA Subscription", "type": "bool", "default": True},
    "0x702": {"name": "Power Level", "type": "enum", "choices": ["low", "medium", "high"], "default": "medium"},
    "0x708": {"name": "Audio Bandwidth VoIP", "type": "enum", "choices": ["narrowband", "wideband"], "default": "wideband"},
    "0x802": {"name": "Multiband Expander", "type": "enum", "choices": ["no", "moderate", "agressive"], "default": "moderate"},
    "0x803": {"name": "Sidetone", "type": "enum", "choices": ["low", "medium", "high"], "default": "medium"},
    "0x805": {"name": "Notification Tones", "type": "bool", "default": False},
    "0x902": {"name": "Online Indicator", "type": "bool", "default": True},
    "0x90a": {"name": "Smart Audio Transfer", "type": "bool", "default": True},
    "0xa00": {"name": "Enable Audio Sensing", "type": "bool", "default": False},
    "0xa01": {"name": "Dialtone On/Off", "type": "bool", "default": True},
    "0xb05": {"name": "Caller ID", "type": "bool", "default": True},
    "0xb06": {"name": "Tone Control", "type": "enum", "choices": ["tone", "voice"], "default": "voice"},
    "0xfff4": {"name": "Keep Link Up", "type": "enum", "choices": ["activeonlyduringcall", "alwaysactive"], "default": "activeonlyduringcall"},
}

# Build reverse maps: name → hex_id, hex_id → name
_NAME_TO_ID = {v["name"]: k for k, v in ALL_SETTING_DEFS.items()}
_ID_TO_NAME = {k: v["name"] for k, v in ALL_SETTING_DEFS.items()}
# Legacy aliases
_NAME_TO_ID["Wearing Sensor"] = "0xa00"
_NAME_TO_ID["Restore Defaults"] = "0x90a"


def setting_name_to_id(name):
    """Convert a setting name to its native hex ID."""
    return _NAME_TO_ID.get(name)


def setting_id_to_name(hex_id):
    """Convert a native hex ID to its setting name."""
    return _ID_TO_NAME.get(hex_id)


def build_dynamic_profile(hex_ids):
    """Build a settings profile from a list of hex IDs a device reports.

    Returns a list of setting defs compatible with lens_settings.settings_to_api_format().
    Only includes settings that have matching Poly Studio renderer entries.
    """
    import json
    from pathlib import Path

    # Load settingsCategories to check which IDs will render
    try:
        cats = json.loads((Path(__file__).parent / "data" / "settingsCategories.json").read_text())
        renderable = set()
        for cat in cats:
            for s in cat.get("settings", []):
                renderable.add(s["id"])
                for sub in s.get("subsettings", []):
                    renderable.add(sub["id"])
    except Exception:
        renderable = None  # Allow all if can't load

    profile = []
    for hex_id in hex_ids:
        defn = ALL_SETTING_DEFS.get(hex_id)
        if not defn:
            continue
        # Only include if the renderer can display it
        if renderable is not None and defn["name"] not in renderable:
            continue
        entry = {"name": defn["name"], "type": defn["type"], "default": defn["default"]}
        if "choices" in defn:
            entry["choices"] = defn["choices"]
        profile.append(entry)

    return profile


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
