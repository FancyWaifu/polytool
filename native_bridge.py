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


# ── Architecture Detection ──────────────────────────────────────────────────

import struct as _struct

def _python_bits():
    """Return the bitness of the running Python (32 or 64)."""
    return _struct.calcsize("P") * 8


def _dll_bits(path):
    """Return the bitness of a PE DLL/EXE (32 or 64), or None on error."""
    try:
        with open(path, "rb") as f:
            f.seek(0x3C)
            pe_offset = _struct.unpack("<I", f.read(4))[0]
            f.seek(pe_offset + 4)  # skip PE signature
            machine = _struct.unpack("<H", f.read(2))[0]
            return {0x14C: 32, 0x8664: 64, 0xAA64: 64}.get(machine)
    except Exception:
        return None


def _needs_proxy(components_dir):
    """Check if we need a 32-bit subprocess proxy to load the DLLs."""
    if sys.platform != "win32":
        return False
    py_bits = _python_bits()
    loader_name = _get_loader_lib_name()
    for name in [loader_name, "lib" + loader_name]:
        p = components_dir / name
        if p.exists():
            dll_arch = _dll_bits(str(p))
            if dll_arch and dll_arch != py_bits:
                return True
    return False


_PYTHON32_SEARCH_PATHS = [
    Path("C:/Python312-32/python.exe"),
    Path("C:/Python311-32/python.exe"),
    Path("C:/Python310-32/python.exe"),
    Path("C:/Python39-32/python.exe"),
]


def _find_python32():
    """Find a 32-bit Python interpreter for the subprocess proxy."""
    # Check known embeddable/install locations
    for p in _PYTHON32_SEARCH_PATHS:
        if p.exists():
            bits = _dll_bits(str(p))
            if bits == 32:
                return str(p)
    # Try py launcher
    import subprocess as _sp
    for ver in ["3.12", "3.11", "3.10", "3.9", "3"]:
        try:
            result = _sp.run(
                ["py", f"-{ver}-32", "-c", "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                exe = result.stdout.strip()
                if exe and Path(exe).exists():
                    return exe
        except Exception:
            continue
    return None


# ── Callback Type ────────────────────────────────────────────────────────────

# receiver_t = void (*)(const char*)
RECEIVER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p)


# ── Native Bridge ────────────────────────────────────────────────────────────

class NativeBridge:
    """Direct interface to Poly's native HID/DECT settings library.

    Automatically detects architecture mismatches (e.g. 32-bit DLLs with
    64-bit Python) and spawns a 32-bit subprocess proxy when needed.
    """

    def __init__(self, components_dir=None):
        self._dir = components_dir or find_components_dir()
        if not self._dir:
            raise FileNotFoundError(
                "Poly native libraries not found. Install Poly Studio or Poly Lens.")
        self._dir = Path(self._dir)

        self._use_proxy = _needs_proxy(self._dir)
        self._proxy_proc = None
        self._proxy_reader = None

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

    # ── Proxy-mode helpers ───────────────────────────────────────────────

    def _proxy_send(self, msg):
        """Send a command to the 32-bit worker process."""
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        self._proxy_proc.stdin.write(line)
        self._proxy_proc.stdin.flush()

    def _proxy_reader_thread(self):
        """Background thread that reads stdout from the 32-bit worker."""
        proc = self._proxy_proc
        while self._running and proc.poll() is None:
            try:
                line = proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type", "")

            if msg_type == "callback":
                # Native library callback — process the same as _on_received
                native_msg = msg.get("message", {})
                self._process_native_message(native_msg)
            elif msg_type == "error":
                print(f"  Worker error: {msg.get('error')}")
            elif msg_type == "loaded_lib":
                print(f"  Loaded {msg.get('name')} (via 32-bit worker)")
            elif msg_type == "started":
                print("  StartNativeBridge done (via 32-bit worker)")
            elif msg_type == "send_result":
                ok = msg.get("ok", False)
                tid = msg.get("trackId", "?")
                print(f"  >> Native: trackId={tid} = {'OK' if ok else 'FAIL'}")
            elif msg_type in ("ready", "pong", "stopped"):
                pass  # internal control messages

    def _start_proxy(self):
        """Launch the 32-bit Python subprocess worker."""
        import subprocess as _sp

        py32 = _find_python32()
        if not py32:
            dll_bits = 32 if _python_bits() == 64 else 64
            raise OSError(
                f"Poly native DLLs are {dll_bits}-bit but Python is {_python_bits()}-bit. "
                f"Install 32-bit Python (embeddable) to C:\\Python312-32\\ or via "
                f"'py -{dll_bits // 8 * 4}-32' to enable the architecture bridge.\n"
                f"Download: https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-win32.zip"
            )

        worker_script = Path(__file__).parent / "native_bridge_worker.py"
        if not worker_script.exists():
            raise FileNotFoundError(f"Worker script not found: {worker_script}")

        print(f"  DLL/Python architecture mismatch detected")
        print(f"  Python is {_python_bits()}-bit, DLLs are {64 if _python_bits() == 32 else 32}-bit")
        print(f"  Launching 32-bit worker: {py32}")

        self._proxy_proc = _sp.Popen(
            [py32, str(worker_script)],
            stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE,
            text=True, bufsize=1,  # line-buffered
        )

        # Wait for ready message
        ready_line = self._proxy_proc.stdout.readline().strip()
        if not ready_line:
            stderr = self._proxy_proc.stderr.read()
            raise OSError(f"32-bit worker failed to start: {stderr}")
        ready = json.loads(ready_line)
        if ready.get("type") != "ready":
            raise OSError(f"Unexpected worker response: {ready}")
        print(f"  32-bit worker ready (PID {ready.get('pid')})")

        # Start background reader thread
        self._running = True
        self._proxy_reader = threading.Thread(
            target=self._proxy_reader_thread, daemon=True)
        self._proxy_reader.start()

        # Tell worker to load the DLLs
        self._proxy_send({"cmd": "start", "components_dir": str(self._dir)})

        # Give the worker time to load DLLs and start scanning
        time.sleep(3)

    def _stop_proxy(self):
        """Shut down the 32-bit worker process."""
        if self._proxy_proc and self._proxy_proc.poll() is None:
            try:
                self._proxy_send({"cmd": "stop"})
                self._proxy_proc.wait(timeout=5)
            except Exception:
                self._proxy_proc.kill()
        self._proxy_proc = None

    # ── Direct-mode (same-arch) start/stop ───────────────────────────────

    @staticmethod
    def _resolve_func(lib, plain_name, mangled_name):
        """Try to resolve a function by plain name first, then C++ mangled."""
        try:
            return getattr(lib, plain_name)
        except AttributeError:
            pass
        if mangled_name:
            try:
                return getattr(lib, mangled_name)
            except AttributeError:
                pass
        return None

    def _start_direct(self):
        """Load native libraries directly (same architecture)."""
        if sys.platform == "win32":
            os.add_dll_directory(str(self._dir))
        else:
            os.environ["DYLD_LIBRARY_PATH"] = str(self._dir)

        loaded_names = []
        for name in LIB_NAMES:
            path = self._dir / name
            if not path.exists():
                continue
            try:
                lib = ctypes.cdll.LoadLibrary(str(path))
                self._libs.append(lib)
                loaded_names.append(name)
                print(f"  Loaded {name}")
            except OSError as e:
                raise OSError(f"Failed to load {name}: {e}")

        if not self._libs:
            raise OSError(f"No native libraries found in {self._dir}")

        self._native_loader = self._libs[-1]

        # Resolve functions — Windows DLLs use C++ mangled names
        self._fn_init = self._resolve_func(
            self._native_loader, "NativeLoader_Init",
            "?NativeLoader_Init@@YAXP6AXPBD@Z@Z")
        self._fn_send = self._resolve_func(
            self._native_loader, "NativeLoader_SendToNative",
            "?NativeLoader_SendToNative@@YA_NPBD@Z")
        self._fn_exit = self._resolve_func(
            self._native_loader, "NativeLoader_Exit",
            "?NativeLoader_Exit@@YAXXZ")

        if not self._fn_init or not self._fn_send or not self._fn_exit:
            raise OSError("Required NativeLoader functions not found in DLL exports")

        self._fn_init.argtypes = [RECEIVER_FUNC]
        self._fn_init.restype = None
        self._fn_send.argtypes = [ctypes.c_char_p]
        self._fn_send.restype = ctypes.c_bool
        self._fn_exit.argtypes = []
        self._fn_exit.restype = None

        self._callback_ref = RECEIVER_FUNC(self._on_received)
        self._fn_init(self._callback_ref)
        print("  NativeLoader_Init done")

        # StartNativeBridge is a macOS-only export; on Windows, Init starts scanning
        fn_start = self._resolve_func(self._native_loader, "StartNativeBridge", None)
        if fn_start:
            try:
                fn_start()
                print("  StartNativeBridge done")
            except Exception as e:
                print(f"  StartNativeBridge: {e}")

        self._running = True
        time.sleep(2)

    def _stop_direct(self):
        """Shut down direct-mode native bridge.

        NativeLoader_Exit() triggers a libc++ mutex crash during cleanup —
        cosmetic only, but noisy. Redirect stderr at both Python and fd level
        to suppress it (native code writes to fd 2 directly).
        """
        old_stderr = sys.stderr
        old_fd = os.dup(2)
        try:
            devnull = open(os.devnull, 'w')
            sys.stderr = devnull
            os.dup2(devnull.fileno(), 2)
            self._fn_exit()
        except Exception:
            pass
        finally:
            os.dup2(old_fd, 2)
            os.close(old_fd)
            sys.stderr = old_stderr
            try:
                devnull.close()
            except Exception:
                pass
        time.sleep(0.5)

    # ── Public API ───────────────────────────────────────────────────────

    def start(self):
        """Initialize the native bridge. Loads libraries, starts USB scanning."""
        print(f"  Loading native libraries from {self._dir}")

        if self._use_proxy:
            self._start_proxy()
        else:
            self._start_direct()

        # Wait a moment for device discovery
        time.sleep(2)

    def stop(self):
        """Shut down the native bridge."""
        if not self._running:
            return
        self._running = False

        if self._use_proxy:
            self._stop_proxy()
        else:
            self._stop_direct()

        print("  Native bridge stopped")

    def _process_native_message(self, msg):
        """Process a parsed JSON message from the native library.

        Used by both direct-mode callback and proxy-mode reader thread.
        """
        with self._lock:
            self._messages.append(msg)
            msg_type = msg.get("messageType", "?")
            msg_str = json.dumps(msg)
            print(f"  << Native: {msg_type}: {msg_str[:150]}")

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
                for s in (payload.get("settings") or []):
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

    def _on_received(self, data):
        """Callback from native library (direct mode) — receives JSON messages."""
        if not data:
            return
        try:
            msg_str = data.decode("utf-8")
            msg = json.loads(msg_str)
            self._process_native_message(msg)
        except Exception as e:
            print(f"  ← Native (decode error): {e}")

    _track_counter = 0

    def send(self, message_type, payload, track_id=None):
        """Send a JSON message to the native library."""
        if track_id is None:
            self._track_counter += 1
            track_id = self._track_counter

        if self._use_proxy:
            self._proxy_send({
                "cmd": "send",
                "message_type": message_type,
                "payload": payload,
                "track_id": track_id,
            })
            print(f"  >> Native: {message_type} (via worker)")
            return True

        msg = {"messageType": message_type, "payload": payload, "trackId": str(track_id)}
        data = json.dumps(msg).encode("utf-8")
        result = self._fn_send(data)
        print(f"  >> Native: {message_type} = {'OK' if result else 'FAIL'}")
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
    "0x60c": {"name": "Active Audio Tone", "type": "bool", "default": False},
    "0x60d": {"name": "Mute Off Alert", "type": "enum", "choices": ["off", "timed", "voiceAudible", "voiceVisible", "voiceVisibleAndAudible"], "default": "voiceAudible"},
    "0x700": {"name": "DECT Density", "type": "enum", "choices": ["homeMode", "enterpriseMode", "mono", "narrowBand"], "default": "mono"},
    "0x701": {"name": "OTA Subscription", "type": "bool", "default": True},
    "0x702": {"name": "Power Level", "type": "enum", "choices": ["low", "medium", "high"], "default": "medium"},
    "0x708": {"name": "Audio Bandwidth VoIP", "type": "enum", "choices": ["narrowband", "wideband"], "default": "wideband"},
    "0x802": {"name": "Multiband Expander", "type": "enum", "choices": ["no", "aimoderate", "agressive"], "default": "aimoderate"},
    "0x803": {"name": "Sidetone", "type": "enum", "choices": ["low", "medium", "high"], "default": "medium"},
    "0x805": {"name": "Notification Tones", "type": "bool", "default": False},
    "0x902": {"name": "Online Indicator", "type": "bool", "default": True},
    "0x90a": {"name": "Restore Defaults", "type": "bool", "default": False},
    "0xa00": {"name": "Enable Audio Sensing", "type": "bool", "default": False},
    "0xa01": {"name": "Dialtone On/Off", "type": "bool", "default": True},
    "0xb05": {"name": "Tone Control", "type": "bool", "default": True},
    "0xb06": {"name": "Volume Min/Max Alerts", "type": "enum", "choices": ["tone", "voice"], "default": "voice"},
    "0xfff4": {"name": "Keep Link Up", "type": "enum", "choices": ["activeonlyduringcall", "alwaysactive"], "default": "activeonlyduringcall"},
}

# ── Merge with canonical DeviceSettings.zip database ──────────────────────────
# The zip contains per-device settings with exact HID metadata. We merge its
# unified settings map into ALL_SETTING_DEFS so any setting the zip knows about
# is available for dynamic profile building, while keeping hardcoded entries as
# fallbacks for settings not in the zip.

try:
    from device_settings_db import SETTINGS_DB as _ZIP_DB
    # Zip entries override hardcoded (they're canonical), then fill gaps
    _merged = {}
    for gid, entry in _ZIP_DB.items():
        _merged[gid] = {
            "name": entry["name"],
            "type": entry["type"],
            "default": entry.get("default"),
        }
        if "choices" in entry:
            _merged[gid]["choices"] = entry["choices"]
    # Add hardcoded entries not in the zip (fallback)
    for gid, entry in ALL_SETTING_DEFS.items():
        if gid not in _merged:
            _merged[gid] = entry
    ALL_SETTING_DEFS = _merged
except ImportError:
    pass

# Build reverse maps: name → hex_id, hex_id → name
_NAME_TO_ID = {v["name"]: k for k, v in ALL_SETTING_DEFS.items()}
_ID_TO_NAME = {k: v["name"] for k, v in ALL_SETTING_DEFS.items()}
# Legacy aliases
_NAME_TO_ID["Wearing Sensor"] = "0xa00"
_NAME_TO_ID["Smart Audio Transfer"] = "0x202"  # Was wrongly at 0x90a


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
