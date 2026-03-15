#!/usr/bin/env python3
"""
Poly Bridge — Universal device bridge for Poly Lens/Studio on macOS.

Acts as a MITM proxy between Clockwork (LensService) and LegacyHost,
intercepting device property queries for ANY Poly device where LegacyHost
fails to provide required info. Fills in missing firmware version, serial
number, and device properties by reading directly from the device via HID.

This makes devices that are partially supported (detected but stuck in
"Initializing" or missing from the UI) fully functional in Poly Studio.

How it works:
  1. Temporarily disables legacyhost (renames the binary)
  2. Starts Poly Studio — Clockwork creates its socket but nobody connects
  3. Connects to Clockwork as the first client, completes INIT/READY handshake
  4. Restores legacyhost, creates a proxy socket at the original path
  5. Starts legacyhost — it connects to our proxy socket
  6. Proxies all traffic, intercepting and fixing device messages:
     - Fills empty firmware_version/serial in ATTACH messages
     - Answers property queries with data read via HID/EEPROM
     - Provides fallback values for settings that LegacyHost can't handle
     - Passes through everything else unchanged

Usage:
  python3 poly_bridge.py          # run bridge (auto-escalates to sudo)
  python3 poly_bridge.py --stop   # stop bridge

Requires: pip install hidapi
"""

import hashlib
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

try:
    import hid
except ImportError:
    print("Error: hidapi required. Install with: pip install hidapi")
    sys.exit(1)

# ── Constants ────────────────────────────────────────────────────────────────

# Poly / Plantronics / HP Vendor IDs
POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}

# When running under sudo, SUDO_USER gives us the real user
_real_home = Path(os.environ.get("HOME", str(Path.home())))
if os.environ.get("SUDO_USER"):
    _real_home = Path(f"/Users/{os.environ['SUDO_USER']}")

SOCKET_PATH = _real_home / "Library/Application Support/Poly/ClockworkLegacyServer"
PID_FILE = _real_home / ".polytool" / "bridge.pid"

# LaunchAgent label to disable while bridge runs (prevents PolyLauncher
# from respawning legacyhost behind our back).
POLY_LAUNCH_AGENTS_DISABLE = [
    "com.poly.LegacyHostApp",
]

# All Poly LaunchAgents (for reload on shutdown)
POLY_LAUNCH_AGENTS_ALL = [
    "com.poly.LegacyHostApp",
    "com.poly.LensControlService",
    "com.poly.CallControlApp",
]

# Vendor-specific HID usage pages used by Poly devices
VENDOR_USAGE_PAGES = {0xFFA0, 0xFFA2, 0xFF52, 0xFF58}

# ── PID Spoofing ────────────────────────────────────────────────────────────
# Map unsupported PIDs to already-whitelisted equivalents so Poly Studio
# accepts them without needing to patch PolyDolphin.dylib or Devices.config.
# The spoofed PID must use the same chipset/handler family as the real device.
#
# Format: real_pid → (spoofed_pid, description)
PID_SPOOF_MAP = {
    # Blackwire 3220 (CX2070x) → Yeti (CX2070x, same YetiEvent/YetiCommand handlers)
    0xC056: (0xAB01, "Blackwire 3220 → Yeti"),
}

def _spoof_pid(pid):
    """Return the spoofed PID if this device needs spoofing, else the original."""
    entry = PID_SPOOF_MAP.get(pid)
    return entry[0] if entry else pid

# CX2070x HID protocol constants (from bw_flash.py)
CX_RID_OUT = 0x04
CX_RID_IN = 0x05
CX_CMD_REG_READ = 0x00
CX_CMD_REG_WRITE = 0x40
CX_CMD_EEPROM_READ = 0x20
CX_CMD_EEPROM_WRITE = 0x60


def _bcd_version_string(bcd_value):
    """
    Parse a BCD-encoded USB bcdDevice value into a version string.
    Each nibble is a digit: 0x0225 -> "2.25", 0x3861 -> "38.61".
    Strips leading zeros.
    """
    digits = []
    for shift in (12, 8, 4, 0):
        digits.append(str((bcd_value >> shift) & 0xF))
    # Format as X.XX or XX.XX depending on leading digit
    raw = "".join(digits).lstrip("0") or "0"
    if len(raw) <= 2:
        return "0." + raw.zfill(2)
    return raw[:-2] + "." + raw[-2:]


def _cx2070x_read_eeprom(h, addr, length):
    """
    Read bytes from CX2070x EEPROM via HID report protocol.
    Sends [RID_OUT, CMD_EEPROM_READ, length, addr_hi, addr_lo, ...padding]
    and reads the response.
    """
    pkt = [CX_RID_OUT, CX_CMD_EEPROM_READ, length,
           (addr >> 8) & 0xFF, addr & 0xFF] + [0x00] * 32
    h.write(pkt)
    resp = h.read(64, timeout_ms=2000)
    if not resp or len(resp) < 1 + length:
        return None
    return bytes(resp[1:1 + length])


# ── HID Device Info Reader ───────────────────────────────────────────────────

def read_device_info_hid(vid, pid):
    """
    Try to read firmware version and serial from a Poly device via HID.
    Tries multiple strategies:
      1. Feature report scanning for version strings
      2. CX2070x EEPROM read protocol (for Blackwire 3220 etc.)
      3. BCD parsing of USB release_number as last resort
    Returns dict with whatever info we could gather.
    """
    info = {"firmware_version": "", "serial": ""}

    # Find the device's vendor-specific HID interface
    target = None
    all_interfaces = []
    for d in hid.enumerate():
        if d["vendor_id"] == vid and d["product_id"] == pid:
            all_interfaces.append(d)
            if d["usage_page"] in VENDOR_USAGE_PAGES and target is None:
                target = d

    if not target:
        return info

    # Use serial from HID enumeration if available — check all interfaces
    for d in all_interfaces:
        if d.get("serial_number"):
            info["serial"] = d["serial_number"]
            break

    # Remember release_number for BCD fallback
    release_number = target.get("release_number", 0)

    # Try to read firmware version from feature reports
    try:
        h = hid.device()
        h.open_path(target["path"])
        h.set_nonblocking(0)

        # Strategy 1: Scan feature reports for version strings
        for report_id in (15, 5, 6, 9, 10, 1, 2):
            try:
                data = h.get_feature_report(report_id, 256)
                if data and len(data) > 4:
                    text = bytes(data[1:]).decode("ascii", errors="ignore")
                    match = re.search(r'(\d+\.\d+(?:\.\d+)?(?:\.\d+)?)', text)
                    if match:
                        info["firmware_version"] = match.group(1)
                        break
            except Exception:
                continue

        # Strategy 2: CX2070x EEPROM read (Blackwire 3220 and similar)
        if not info["firmware_version"]:
            try:
                # Read first 16 bytes of EEPROM looking for version data
                for eeprom_offset in (0x0000, 0x0004, 0x0008, 0x000C):
                    chunk = _cx2070x_read_eeprom(h, eeprom_offset, 16)
                    if chunk:
                        text = chunk.decode("ascii", errors="ignore")
                        match = re.search(r'(\d+\.\d+(?:\.\d+)?(?:\.\d+)?)', text)
                        if match:
                            info["firmware_version"] = match.group(1)
                            break
            except Exception:
                pass

        h.close()
    except Exception:
        pass

    # Strategy 3: BCD parse of USB release_number (bcdDevice)
    if not info["firmware_version"] and release_number > 0:
        info["firmware_version"] = _bcd_version_string(release_number)

    # Generate a deterministic placeholder serial from device path if still empty
    if not info["serial"] and target.get("path"):
        path_hash = hashlib.md5(target["path"]).hexdigest()[:8].upper()
        info["serial"] = f"POLY-{pid:04X}-{path_hash}"

    return info


def find_poly_devices():
    """Find all connected Poly devices on vendor-specific HID interfaces."""
    seen = set()
    devices = []
    for d in hid.enumerate():
        vid = d["vendor_id"]
        pid = d["product_id"]
        key = (vid, pid)
        if vid in POLY_VIDS and key not in seen and d["usage_page"] in VENDOR_USAGE_PAGES:
            seen.add(key)
            devices.append({
                "vid": vid,
                "pid": pid,
                "name": d.get("product_string") or f"Poly Device {pid:04X}",
                "serial": d.get("serial_number") or "",
                "usage_page": d["usage_page"],
                "path": d["path"],
            })
    return devices


# ── HID Settings Driver ──────────────────────────────────────────────────────

class HIDSettingsDriver:
    """
    Read/write device settings via HID for Poly devices.
    Supports CX2070x (Blackwire 3220) and generic feature report devices.
    """

    def __init__(self):
        # Cache of open HID handles: (vid, pid) → hid.device
        self._handles = {}
        # Cache of device paths: (vid, pid) → path
        self._paths = {}
        # Cached setting values: (vid, pid) → {setting_name: value}
        self._cache = {}

    def register_device(self, vid, pid, path, usage_page):
        """Register a device for HID settings access."""
        self._paths[(vid, pid)] = (path, usage_page)
        self._cache[(vid, pid)] = {}

    def _open(self, vid, pid):
        """Open a temporary HID handle for a device. Caller must close it."""
        key = (vid, pid)
        if key not in self._paths:
            return None
        path, _ = self._paths[key]
        try:
            h = hid.device()
            h.open_path(path)
            h.set_nonblocking(0)
            return h
        except Exception:
            return None

    def close_all(self):
        """Close any cached handles from probe phase."""
        for h in self._handles.values():
            try:
                h.close()
            except Exception:
                pass
        self._handles.clear()

    # ── CX2070x register access ──────────────────────────────────────────

    def _cx_reg_read(self, h, addr, length=1):
        """Read register(s) from CX2070x."""
        pkt = [CX_RID_OUT, CX_CMD_REG_READ, length,
               (addr >> 8) & 0xFF, addr & 0xFF] + [0x00] * 32
        h.write(pkt)
        resp = h.read(64, timeout_ms=1000)
        if not resp or len(resp) < 1 + length:
            return None
        return bytes(resp[1:1 + length])

    def _cx_reg_write(self, h, addr, data):
        """Write register(s) to CX2070x."""
        length = len(data)
        pkt = [CX_RID_OUT, CX_CMD_REG_WRITE, length,
               (addr >> 8) & 0xFF, addr & 0xFF] + list(data)
        pkt += [0x00] * (37 - len(pkt))
        h.write(pkt)
        time.sleep(0.01)

    # ── Setting read/write ───────────────────────────────────────────────

    def probe_device(self, vid, pid):
        """
        Probe a device to discover its settings capabilities.
        Returns a dict of setting_name → current_value.
        Handle is cached in self._handles during probe; caller should
        call close_all() after probing is complete.
        """
        key = (vid, pid)
        if key not in self._paths:
            return {}
        path, usage_page = self._paths[key]
        try:
            h = hid.device()
            h.open_path(path)
            h.set_nonblocking(0)
            self._handles[key] = h
        except Exception:
            return {}

        settings = {}
        if usage_page == 0xFFA0:
            settings = self._probe_cx2070x(h, vid, pid)
        elif usage_page == 0xFFA2:
            settings = self._probe_ffa2(h, vid, pid)

        self._cache[key] = settings
        return settings

    def _probe_cx2070x(self, h, vid, pid):
        """Probe CX2070x registers for settings."""
        settings = {}

        # CX2070x common register map (Conexant CX20707/CX20708):
        # These addresses are based on the CX2070x reference design.
        # The actual layout depends on the firmware loaded into the chip.
        #
        # Probe strategy: try reading known registers and check for
        # valid responses. A non-response or all-zeros/all-FF means
        # the register doesn't exist in this firmware.

        probes = [
            # (register_addr, setting_name, parser_func)
            # Sidetone: CX2070x sidetone gain is typically a codec register
            # that controls the loopback path from ADC to DAC
            (0x191A, "sidetone_gain"),
            (0x1900, "sidetone_ctrl"),
            (0x0800, "dac_vol_l"),
            (0x0801, "dac_vol_r"),
            (0x0808, "dac_mute"),
            (0x0900, "adc_vol"),
            (0x0908, "adc_mute"),
        ]

        for addr, name in probes:
            try:
                data = self._cx_reg_read(h, addr, 2)
                if data and data != b'\xff\xff' and data != b'\x00\x00':
                    settings[name] = int.from_bytes(data, 'big')
                    print(f"    [hid] CX reg 0x{addr:04X} ({name}) = 0x{settings[name]:04X}")
                elif data:
                    settings[name] = int.from_bytes(data, 'big')
            except Exception:
                pass

        return settings

    def _probe_ffa2(self, h, vid, pid):
        """Probe 0xFFA2 device (Savi 8220 etc.) for settings."""
        settings = {}

        # Try reading feature report 0x0E (settings report)
        try:
            data = h.get_feature_report(0x0E, 64)
            if data and len(data) > 2:
                settings["_raw_settings_report"] = data
                print(f"    [hid] Settings report (RID 0x0E): {len(data)} bytes")
                # Parse known fields from the settings report
                # Byte layout is device-specific; log for analysis
                hex_str = ' '.join(f'{b:02X}' for b in data[:32])
                print(f"    [hid] Data: {hex_str}")
        except Exception:
            pass

        # Try reading feature report 0x0F (feature/capability report)
        try:
            data = h.get_feature_report(0x0F, 64)
            if data and len(data) > 2:
                settings["_raw_feature_report"] = data
                print(f"    [hid] Feature report (RID 0x0F): {len(data)} bytes")
                hex_str = ' '.join(f'{b:02X}' for b in data[:32])
                print(f"    [hid] Data: {hex_str}")
        except Exception:
            pass

        return settings

    def read_setting(self, vid, pid, setting_name):
        """Read a setting value from the device. Opens/closes HID per call."""
        h = self._open(vid, pid)
        if not h:
            return None

        key = (vid, pid)
        _, usage_page = self._paths.get(key, (None, None))

        try:
            if usage_page == 0xFFA0:
                return self._cx_read_setting(h, setting_name)
            elif usage_page == 0xFFA2:
                return self._ffa2_read_setting(h, setting_name)
        finally:
            h.close()
        return None

    def write_setting(self, vid, pid, setting_name, value):
        """Write a setting value to the device. Opens/closes HID per call."""
        h = self._open(vid, pid)
        if not h:
            return False

        key = (vid, pid)
        _, usage_page = self._paths.get(key, (None, None))

        try:
            if usage_page == 0xFFA0:
                return self._cx_write_setting(h, setting_name, value)
            elif usage_page == 0xFFA2:
                return self._ffa2_write_setting(h, setting_name, value)
        finally:
            h.close()
        return False

    # ── CX2070x setting implementations ──────────────────────────────────

    # Map of Poly Studio setting names to CX2070x register operations.
    # Each entry: (read_addr, write_addr, value_transform_read, value_transform_write)
    CX_SETTING_MAP = {
        # Sidetone: register 0x191A controls sidetone gain
        # Value range 0x0000 (off) to 0x7FFF (max)
        # Poly Studio sends 0-10 scale
        "Sidetone Level": {
            "addr": 0x191A,
            "to_device": lambda v: int(v * 0x7FFF / 10) if isinstance(v, (int, float)) else 0,
            "from_device": lambda raw: round(raw * 10 / 0x7FFF) if raw else 0,
        },
        "Sidetone On/Off": {
            "addr": 0x1900,
            "to_device": lambda v: 0x0001 if v else 0x0000,
            "from_device": lambda raw: bool(raw & 0x0001) if raw is not None else False,
        },
    }

    def _cx_read_setting(self, h, setting_name):
        """Read a setting from CX2070x registers."""
        spec = self.CX_SETTING_MAP.get(setting_name)
        if not spec:
            return None
        try:
            data = self._cx_reg_read(h, spec["addr"], 2)
            if data:
                raw = int.from_bytes(data, 'big')
                return spec["from_device"](raw)
        except Exception:
            pass
        return None

    def _cx_write_setting(self, h, setting_name, value):
        """Write a setting to CX2070x registers."""
        spec = self.CX_SETTING_MAP.get(setting_name)
        if not spec:
            return False
        try:
            device_val = spec["to_device"](value)
            data = device_val.to_bytes(2, 'big')
            self._cx_reg_write(h, spec["addr"], data)
            print(f"    [hid] Wrote CX 0x{spec['addr']:04X} = 0x{device_val:04X} "
                  f"({setting_name} = {value})")
            return True
        except Exception as e:
            print(f"    [hid] Write failed: {e}")
            return False

    # ── FFA2 (Savi 8220) setting implementations ─────────────────────────

    def _ffa2_read_setting(self, h, setting_name):
        """Read a setting from FFA2 device via feature report."""
        # Settings report is RID 0x0E
        try:
            data = h.get_feature_report(0x0E, 64)
            if not data or len(data) < 4:
                return None
            # Parse based on setting name
            # These byte offsets are device-specific and would need
            # reverse-engineering for each model. For now, return cached value.
            return None
        except Exception:
            return None

    def _ffa2_write_setting(self, h, setting_name, value):
        """Write a setting to FFA2 device via feature report."""
        # Would need to construct and send a settings report
        return False


# ── Socket Proxy ─────────────────────────────────────────────────────────────

class LegacyProxy:
    """Universal MITM proxy between Clockwork and LegacyHost."""

    def __init__(self):
        self.clockwork_sock = None
        self.legacy_sock = None
        self.server_sock = None
        self.running = False
        self.lh_proc = None

        # Tracked devices: device_id → {pid, vid, name, fw_version, serial, ...}
        self.tracked_devices = {}

        # Set of request_ids we already answered (suppress LegacyHost's response)
        self.intercepted_requests = set()

        # Map of request_id → {device_id, prop_name} for pending requests
        # (so we can intercept failed responses and provide fallbacks)
        self.pending_requests = {}

        # After legacyhost restart, suppress its READY (Clockwork already got ours)
        self._suppress_ready = False

        # Connected Poly devices (from HID enumeration)
        self.hid_devices = {}  # (vid, pid) → device info dict

        # HID settings driver for real device access
        self.settings_driver = HIDSettingsDriver()

    def discover_hid_devices(self):
        """Scan HID bus and cache device info."""
        for dev in find_poly_devices():
            key = (dev["vid"], dev["pid"])
            self.hid_devices[key] = dev
            # Register device for HID settings access
            self.settings_driver.register_device(
                dev["vid"], dev["pid"], dev["path"], dev["usage_page"])
            # Also try to read extended info
            hid_info = read_device_info_hid(dev["vid"], dev["pid"])
            dev["hid_fw_version"] = hid_info["firmware_version"]
            dev["hid_serial"] = hid_info["serial"]
            print(f"  HID: {dev['name']} (VID 0x{dev['vid']:04X} PID 0x{dev['pid']:04X}) "
                  f"fw={dev['hid_fw_version'] or '?'} serial={dev['hid_serial'][:16] or '?'}")

    def connect_to_clockwork(self):
        """Connect to Clockwork via the Unix socket."""
        print("Connecting to Clockwork...")
        for attempt in range(20):
            if SOCKET_PATH.exists():
                try:
                    self.clockwork_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.clockwork_sock.connect(str(SOCKET_PATH))
                    self.clockwork_sock.setblocking(False)
                    print("  Connected to Clockwork.")
                    return True
                except OSError:
                    self.clockwork_sock = None
                    if attempt < 19:
                        time.sleep(0.5)
            else:
                time.sleep(0.5)
        print("Error: Could not connect to Clockwork socket.")
        return False

    def setup_fake_server(self):
        """Create a fake socket for legacyhost to connect to."""
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock.bind(str(SOCKET_PATH))
        self.server_sock.listen(1)
        self.server_sock.settimeout(30)
        print(f"  Listening on {SOCKET_PATH}")

    def accept_legacyhost(self):
        """Wait for legacyhost to connect."""
        print("Waiting for legacyhost to connect...")
        try:
            self.legacy_sock, _ = self.server_sock.accept()
            self.legacy_sock.setblocking(False)
            print("  LegacyHost connected.")
            return True
        except socket.timeout:
            print("Error: LegacyHost did not connect.")
            return False

    def recv_all_json(self, sock):
        """Read available data and parse length-prefixed JSON messages.

        Protocol: each message is [4-byte LE uint32 length][JSON payload].
        """
        # First, read all available data into the per-socket buffer
        buf_attr = "_buf_cw" if sock == self.clockwork_sock else "_buf_lh"
        if not hasattr(self, buf_attr):
            setattr(self, buf_attr, b"")
        buf = getattr(self, buf_attr)

        while True:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    return None  # Connection closed
                buf += chunk
            except BlockingIOError:
                break
            except OSError:
                return None

        # Parse complete messages from the buffer
        messages = []
        while len(buf) >= 4:
            msg_len = struct.unpack("<I", buf[:4])[0]
            if len(buf) < 4 + msg_len:
                break  # Incomplete message, wait for more data
            payload = buf[4:4 + msg_len]
            buf = buf[4 + msg_len:]
            try:
                msg = json.loads(payload)
                messages.append(msg)
            except json.JSONDecodeError:
                pass

        setattr(self, buf_attr, buf)

        if not messages and not buf:
            return []
        return messages

    def send_json(self, sock, msg):
        """Send length-prefixed JSON message."""
        payload = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        header = struct.pack("<I", len(payload))
        try:
            sock.sendall(header + payload)
        except OSError:
            pass

    # ── Message interception ─────────────────────────────────────────────

    def _synthesize_attach(self, bricked_dev):
        """Convert a BRICKED_DEVICE_INFO device into an ATTACH message."""
        # Try to match by VID/PID to our HID-discovered devices
        try:
            vid = int(bricked_dev.get("vendor_id", "0"))
            pid = int(bricked_dev.get("product_id", "0"))
        except ValueError:
            return None

        hid_dev = self.hid_devices.get((vid, pid))
        name = bricked_dev.get("product_name", "")
        device_id = bricked_dev.get("device_id", "")

        if not device_id:
            # Generate a stable device_id from VID/PID
            import hashlib
            device_id = str(abs(int(hashlib.md5(
                f"{vid:04X}{pid:04X}".encode()).hexdigest()[:16], 16)))

        # Spoof PID if needed
        spoofed_pid = _spoof_pid(pid)
        if spoofed_pid != pid:
            spoof_desc = PID_SPOOF_MAP[pid][1]
            print(f"  [bridge] Synthesizing ATTACH for {name} "
                  f"(VID 0x{vid:04X} PID 0x{pid:04X} → spoofed 0x{spoofed_pid:04X}, {spoof_desc})")
        else:
            print(f"  [bridge] Synthesizing ATTACH for {name} (VID 0x{vid:04X} PID 0x{pid:04X})")

        # Build an ATTACH message with the info from bricked_dev,
        # handle_attach will fill in missing fw/serial from HID
        attach = {
            "command": "ATTACH",
            "device": {
                **bricked_dev,
                "device_id": device_id,
                "product_id": str(spoofed_pid),
                "is_bricked": False,
            },
        }
        # Remove bricked-specific fields
        attach["device"].pop("bricked", None)
        return attach

    def handle_attach(self, msg):
        """
        Intercept any ATTACH from LegacyHost. If device info is incomplete,
        fill it in from HID data.
        """
        if msg.get("command") != "ATTACH":
            return msg

        dev = msg.get("device", {})
        device_id = dev.get("device_id", "")

        # Parse VID/PID
        try:
            vid = int(dev.get("vendor_id", "0"))
            pid = int(dev.get("product_id", "0"))
        except ValueError:
            return msg

        # Look up HID info
        hid_dev = self.hid_devices.get((vid, pid))
        fw_version = dev.get("firmware_version") or ""
        product_version = dev.get("product_version") or ""
        serial = dev.get("serial_number") or ""
        name = dev.get("product_name") or ""

        # Fill in from HID if missing
        if hid_dev:
            if not fw_version and hid_dev.get("hid_fw_version"):
                fw_version = hid_dev["hid_fw_version"]
            if not serial and hid_dev.get("hid_serial"):
                serial = hid_dev["hid_serial"]
            if not serial and hid_dev.get("serial"):
                serial = hid_dev["serial"]

        # Use product_version as firmware_version fallback, with BCD decoding
        if not fw_version and product_version:
            # If product_version is a raw integer string (e.g. "225"), try BCD decode
            try:
                pv_int = int(product_version)
                if pv_int > 0 and "." not in product_version:
                    fw_version = _bcd_version_string(pv_int)
                else:
                    fw_version = product_version
            except ValueError:
                fw_version = product_version

        # Generate a placeholder serial if still empty
        if not serial:
            serial = f"POLY-{pid:04X}"

        # Spoof PID if this device isn't in Poly Studio's allowlist
        spoofed_pid = _spoof_pid(pid)
        if spoofed_pid != pid:
            spoof_desc = PID_SPOOF_MAP[pid][1]
            print(f"  [bridge] PID spoof: 0x{pid:04X} → 0x{spoofed_pid:04X} ({spoof_desc})")
            dev["product_id"] = str(spoofed_pid)
            changed = True

        # Track this device (store real PID for HID access)
        self.tracked_devices[device_id] = {
            "vid": vid,
            "pid": pid,  # real PID — needed for HID reads
            "spoofed_pid": spoofed_pid,
            "name": name,
            "firmware_version": fw_version,
            "serial": serial,
            "product_version": product_version,
        }

        # Update the ATTACH message
        if dev.get("firmware_version") != fw_version:
            dev["firmware_version"] = fw_version
            changed = True
        if dev.get("serial_number") != serial:
            dev["serial_number"] = serial
            changed = True
        if not dev.get("serial_number_tattoo"):
            dev["serial_number_tattoo"] = serial
            changed = True

        # Fix firmware_versions_list if firmware_version was empty
        if fw_version:
            try:
                fwl = json.loads(dev.get("firmware_versions_list", "{}"))
            except (json.JSONDecodeError, TypeError):
                fwl = {}
            # Set usb version if empty
            if not fwl.get("usb") and fw_version:
                fwl["usb"] = fw_version
                dev["firmware_versions_list"] = json.dumps(fwl)
                changed = True

        msg["device"] = dev

        if changed:
            print(f"  [bridge] Fixed ATTACH: {name} "
                  f"(fw={fw_version}, serial={serial[:16]}, id={device_id})")

        return msg

    def handle_request_from_clockwork(self, msg):
        """
        Intercept DEVICE_REQUEST from Clockwork. If the device is tracked
        and we can answer the property, respond directly and suppress the
        forwarding to LegacyHost.
        Returns True if handled, False to forward.
        """
        if msg.get("command") != "DEVICE_REQUEST":
            return False

        device_id = msg.get("device_id", "")
        dev = self.tracked_devices.get(device_id)
        if not dev:
            return False

        prop_name = msg.get("params", {}).get("name", "")
        request_id = msg.get("request_id", 0)

        # Build property map from tracked device info
        fw = dev["firmware_version"]
        serial = dev["serial"]
        pid = dev["pid"]

        # Properties we answer proactively (before LegacyHost gets a chance)
        known_props = {
            "Device Info Version SW": fw,
            "Device Info Main MAC Address": f"00:04:7F:{(pid >> 8) & 0xFF:02X}:{pid & 0xFF:02X}:01",
            "Set ID": f"{pid:04X}",
            "Bladerunner DFU Protocol Type": "",
            "Product Parent ID": "",
            "Release": "",
            "device_type": "corded_headset",
            "is_bricked": False,
            "recovery_archive_path": "",
            "can_be_primary_for_call_control": True,
        }

        if prop_name in known_props:
            self._send_prop_response(request_id, prop_name, known_props[prop_name])
            self.intercepted_requests.add(request_id)
            print(f"  [bridge] {dev['name']}: {prop_name} = {known_props[prop_name]}")
            return True

        # Check if this is a setting we can read directly from the device
        params = msg.get("params", {})
        set_value = params.get("value")

        if set_value is not None:
            # This is a WRITE request — Poly Studio wants to change a setting
            vid, pid_val = dev["vid"], dev["pid"]
            success = self.settings_driver.write_setting(vid, pid_val, prop_name, set_value)
            if success:
                self._send_prop_response(request_id, prop_name, set_value,
                                         read_only=False)
                self.intercepted_requests.add(request_id)
                print(f"  [bridge] {dev['name']}: SET {prop_name} = {set_value} (HID write)")
                return True
        else:
            # This is a READ request — try reading from device HID
            vid, pid_val = dev["vid"], dev["pid"]
            hid_value = self.settings_driver.read_setting(vid, pid_val, prop_name)
            if hid_value is not None:
                self._send_prop_response(request_id, prop_name, hid_value,
                                         read_only=False)
                self.intercepted_requests.add(request_id)
                print(f"  [bridge] {dev['name']}: {prop_name} = {hid_value} (HID read)")
                return True

        # Track request so we can intercept failed responses from LegacyHost
        self.pending_requests[request_id] = {
            "device_id": device_id,
            "prop_name": prop_name,
        }

        return False

    def _send_prop_response(self, request_id, prop_name, value, read_only=True):
        """Send a property response to Clockwork."""
        resp = {
            "command": "DEVICE_RESPONSE",
            "request_id": request_id,
            "result": 0,
            "error_desc": "",
            "reply": {
                "setting": {
                    "name": prop_name,
                    "value": value,
                    "type": type(value).__name__,
                    "meta": {"name": prop_name, "read_only": read_only},
                }
            },
        }
        self.send_json(self.clockwork_sock, resp)

    # Default values for settings that LegacyHost may fail to read.
    # These provide sensible defaults so Poly Studio can display the settings UI.
    FALLBACK_SETTINGS = {
        "Language Selection": "English",
        "Sidetone Level": 3,
        "Sidetone On/Off": True,
        "Ringtone Volume": 5,
        "Anti-Startle Protection": True,
        "Noise Limiting": True,
        "G616 Limiting": False,
        "Wearing Sensor On/Off": False,
        "Auto-Answer": False,
        "Auto-Disconnect": False,
        "Mute On/Off": False,
        "Mute Reminder Tone": True,
        "Second Incoming Call": "Ignore",
        "Online Indicator": True,
        "Audio Sensing": False,
        "IntelliStand On/Off": False,
        "HD Voice": True,
        "EQ Preset": "Default",
        "Volume Level": 5,
        "Microphone Level": 5,
        "Custom Name": "",
    }

    def handle_response_from_legacy(self, msg):
        """
        Intercept DEVICE_RESPONSE from LegacyHost.
        - If we already answered the request, suppress LegacyHost's response.
        - If LegacyHost failed and we have a fallback, provide it.
        """
        if msg.get("command") != "DEVICE_RESPONSE":
            return msg

        request_id = msg.get("request_id", 0)

        # If we already answered this request, suppress LegacyHost's response
        if request_id in self.intercepted_requests:
            self.intercepted_requests.discard(request_id)
            self.pending_requests.pop(request_id, None)
            return None  # Drop this message

        # Check if this is a failed response for a tracked request
        pending = self.pending_requests.pop(request_id, None)
        if pending and msg.get("result", 0) != 0:
            prop_name = pending["prop_name"]
            device_id = pending["device_id"]
            dev = self.tracked_devices.get(device_id)

            # Check if we have a fallback value for this setting
            if prop_name in self.FALLBACK_SETTINGS:
                value = self.FALLBACK_SETTINGS[prop_name]
                self._send_prop_response(request_id, prop_name, value,
                                         read_only=False)
                dev_name = dev["name"] if dev else device_id[:12]
                print(f"  [bridge] {dev_name}: {prop_name} = {value} (fallback)")
                return None  # Drop LegacyHost's error

        return msg

    def _proxy_loop(self):
        """Inner proxy loop. Returns 'lh_disconnect' or 'done'."""
        sockets = [self.clockwork_sock, self.legacy_sock]

        while self.running:
            try:
                readable, _, errored = select.select(sockets, [], sockets, 1.0)
            except (ValueError, OSError):
                return "done"

            for sock in errored:
                name = "Clockwork" if sock == self.clockwork_sock else "LegacyHost"
                print(f"Socket error: {name}")
                if sock == self.clockwork_sock:
                    return "done"
                return "lh_disconnect"

            for sock in readable:
                if sock == self.clockwork_sock:
                    msgs = self.recv_all_json(sock)
                    if msgs is None:
                        print("Clockwork disconnected.")
                        return "done"
                    for msg in msgs:
                        if not isinstance(msg, dict):
                            continue
                        cmd = msg.get("command", "")
                        if cmd == "DEVICE_REQUEST":
                            prop = msg.get("params", {}).get("name", "")
                            print(f"  CW→LH: {cmd} [{prop}] dev={msg.get('device_id','')[:12]}")
                        elif cmd:
                            print(f"  CW→LH: {cmd}")
                        if not self.handle_request_from_clockwork(msg):
                            self.send_json(self.legacy_sock, msg)

                elif sock == self.legacy_sock:
                    msgs = self.recv_all_json(sock)
                    if msgs is None:
                        return "lh_disconnect"
                    for msg in msgs:
                        if not isinstance(msg, dict):
                            continue
                        cmd = msg.get("command", "")
                        # After restart, suppress duplicate READY/handshake
                        if self._suppress_ready and cmd == "READY":
                            print(f"  LH→CW: READY (suppressed, post-restart)")
                            self._suppress_ready = False
                            continue
                        if cmd == "BRICKED_DEVICE_INFO":
                            # Suppress bricked info and synthesize ATTACH instead
                            bricked_dev = msg.get("device", {})
                            bdev_name = bricked_dev.get("product_name", "unknown")
                            print(f"  LH→CW: BRICKED_DEVICE_INFO {bdev_name} (intercepted)")
                            # Dump for debugging
                            for k, v in msg.items():
                                if k != "command":
                                    print(f"    {k}: {json.dumps(v) if isinstance(v, (dict, list)) else v}")
                            attach_msg = self._synthesize_attach(bricked_dev)
                            if attach_msg:
                                attach_msg = self.handle_attach(attach_msg)
                                if attach_msg:
                                    self.send_json(self.clockwork_sock, attach_msg)
                            continue
                        if cmd == "ATTACH":
                            dev = msg.get("device", {})
                            print(f"  LH→CW: ATTACH {dev.get('product_name','')} "
                                  f"fw={dev.get('firmware_version','')} "
                                  f"serial={dev.get('serial_number','')[:16]}")
                        elif cmd == "DEVICE_RESPONSE":
                            err = msg.get("error_desc", "")
                            res = msg.get("result", 0)
                            if res != 0:
                                print(f"  LH→CW: DEVICE_RESPONSE ERROR: {err}")
                            else:
                                print(f"  LH→CW: DEVICE_RESPONSE OK")
                        elif cmd:
                            print(f"  LH→CW: {cmd}")
                        msg = self.handle_attach(msg)
                        msg = self.handle_response_from_legacy(msg)
                        if msg is not None:
                            self.send_json(self.clockwork_sock, msg)

        return "done"

    def _standalone_loop(self):
        """Handle Clockwork queries directly when LegacyHost is gone."""
        while self.running:
            try:
                readable, _, errored = select.select(
                    [self.clockwork_sock], [], [self.clockwork_sock], 1.0)
            except (ValueError, OSError):
                return

            if errored:
                return

            for sock in readable:
                msgs = self.recv_all_json(sock)
                if msgs is None:
                    print("Clockwork disconnected.")
                    return
                for msg in msgs:
                    if not isinstance(msg, dict):
                        continue
                    if not self.handle_request_from_clockwork(msg):
                        # Can't forward to LH — answer with fallback or error
                        cmd = msg.get("command", "")
                        if cmd == "DEVICE_REQUEST":
                            prop = msg.get("params", {}).get("name", "")
                            request_id = msg.get("request_id", 0)
                            device_id = msg.get("device_id", "")
                            dev = self.tracked_devices.get(device_id)
                            dev_name = dev["name"] if dev else device_id[:12]
                            if prop in self.FALLBACK_SETTINGS:
                                value = self.FALLBACK_SETTINGS[prop]
                                self._send_prop_response(request_id, prop, value,
                                                         read_only=False)
                                print(f"  [standalone] {dev_name}: {prop} = {value}")
                            else:
                                # Send empty success response
                                self._send_prop_response(request_id, prop, "",
                                                         read_only=True)
                                print(f"  [standalone] {dev_name}: {prop} = (empty)")

    def run(self, lh_bin, init_msg):
        """Main proxy loop. Falls back to standalone mode when LH disconnects."""
        self.running = True
        print("\nBridge active. Proxying traffic. Press Ctrl+C to stop.")
        print("Logging all messages...\n")

        result = self._proxy_loop()

        if result == "lh_disconnect" and self.running:
            print("\nLegacyHost disconnected. Switching to standalone mode...")
            print("  (Bridge will answer all queries directly)\n")
            # Clean up LH connection
            if self.legacy_sock:
                try:
                    self.legacy_sock.close()
                except Exception:
                    pass
                self.legacy_sock = None
            if self.lh_proc:
                self.lh_proc.terminate()
                try:
                    self.lh_proc.wait(timeout=5)
                except Exception:
                    pass
            subprocess.run(["pkill", "-9", "-f", "legacyhost"], capture_output=True)
            # Clean up server socket (no longer needed)
            if self.server_sock:
                try:
                    self.server_sock.close()
                except Exception:
                    pass
                self.server_sock = None
            self._standalone_loop()

    def close(self):
        self.running = False
        self.settings_driver.close_all()
        for s in [self.clockwork_sock, self.legacy_sock, self.server_sock]:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        if SOCKET_PATH.exists():
            try:
                SOCKET_PATH.unlink()
            except Exception:
                pass


# ── Main ─────────────────────────────────────────────────────────────────────

def wait_for_init(sock, timeout=10):
    """Wait for INIT message from Clockwork. Returns the message or None.

    Protocol: 4-byte LE uint32 length prefix, then JSON payload.
    """
    sock.setblocking(True)
    sock.settimeout(timeout)
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
            # Try to parse a length-prefixed message
            while len(buf) >= 4:
                msg_len = struct.unpack("<I", buf[:4])[0]
                if len(buf) < 4 + msg_len:
                    break  # Need more data
                payload = buf[4:4 + msg_len]
                buf = buf[4 + msg_len:]
                try:
                    msg = json.loads(payload)
                    if isinstance(msg, dict) and msg.get("command") == "INIT":
                        return msg
                except json.JSONDecodeError:
                    pass
        except socket.timeout:
            return None
        except OSError:
            return None
    return None


def _unload_launch_agents():
    """Unload LegacyHostApp LaunchAgent so PolyLauncher doesn't respawn it."""
    real_uid = os.environ.get("SUDO_UID", str(os.getuid()))
    for label in POLY_LAUNCH_AGENTS_DISABLE:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{real_uid}", f"/Library/LaunchAgents/{label}.plist"],
            capture_output=True,
        )


def _reload_launch_agents():
    """Reload all Poly LaunchAgents so normal auto-start resumes."""
    real_uid = os.environ.get("SUDO_UID", str(os.getuid()))
    for label in POLY_LAUNCH_AGENTS_ALL:
        plist = f"/Library/LaunchAgents/{label}.plist"
        if Path(plist).exists():
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{real_uid}", plist],
                capture_output=True,
            )


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Poly Bridge — Universal device bridge for Poly Studio"
    )
    parser.add_argument("--stop", action="store_true", help="Stop bridge")
    args = parser.parse_args()

    if args.stop:
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                print(f"Stopped bridge (PID {pid})")
                PID_FILE.unlink(missing_ok=True)
            except (ProcessLookupError, ValueError):
                print("Bridge not running")
                PID_FILE.unlink(missing_ok=True)
        else:
            print("Bridge not running")
        subprocess.run(["pkill", "-f", "legacyhost"], capture_output=True)
        return

    # Must run as root to kill processes reliably
    if os.geteuid() != 0:
        print("This tool needs root access. Re-running with sudo...")
        os.execvp("sudo", ["sudo", sys.executable] + sys.argv)

    lh_bin = Path("/Applications/Poly Studio.app/Contents/Helpers/"
                   "LegacyHostApp.app/Contents/MacOS/legacyhost")

    if not lh_bin.exists():
        print(f"Error: legacyhost not found at {lh_bin}")
        print("  Is Poly Studio installed?")
        sys.exit(1)

    proxy = LegacyProxy()
    lh_proc = None
    # (legacyhost LaunchAgent managed via launchctl)

    # Get the real user's UID for launchctl (we're running as root via sudo)
    _sudo_user = os.environ.get("SUDO_USER", "")
    _sudo_uid = ""
    if _sudo_user:
        import pwd
        try:
            _sudo_uid = str(pwd.getpwnam(_sudo_user).pw_uid)
        except KeyError:
            pass

    def _unload_legacyhost_agent():
        """Unload the LegacyHostApp LaunchAgent so launchd stops respawning it.
        Do NOT unload LensControlService — that kills Clockwork."""
        plist = "/Library/LaunchAgents/com.poly.LegacyHostApp.plist"
        if _sudo_uid:
            subprocess.run(
                ["launchctl", "bootout", f"gui/{_sudo_uid}", plist],
                capture_output=True)
        # Also kill any running PolyLauncher/legacyhost
        subprocess.run(["pkill", "-9", "-f", "legacyhost"], capture_output=True)
        subprocess.run(["pkill", "-9", "-f", "PolyLauncher"], capture_output=True)

    def _reload_legacyhost_agent():
        """Reload the LegacyHostApp LaunchAgent on shutdown."""
        plist = "/Library/LaunchAgents/com.poly.LegacyHostApp.plist"
        if _sudo_uid:
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{_sudo_uid}", plist],
                capture_output=True)

    def shutdown(signum=None, frame=None):
        print("\nShutting down...")
        proxy.close()
        PID_FILE.unlink(missing_ok=True)
        if lh_proc:
            lh_proc.terminate()
        subprocess.run(["pkill", "-f", "legacyhost"], capture_output=True)
        _reload_legacyhost_agent()
        print("Stopped. Restart Poly Studio to restore normal operation.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # Step 1: Kill everything and disable legacyhost LaunchAgent
    print("Step 1: Stopping Poly Studio...")
    for proc in ["Poly Studio", "LensService", "legacyhost", "PolyLauncher",
                  "CallControlApp"]:
        subprocess.run(["pkill", "-9", "-f", proc], capture_output=True)
    time.sleep(2)

    # Unload the LaunchAgent so launchd stops respawning legacyhost.
    # Do NOT unload LensControlService — we need Clockwork alive.
    _unload_legacyhost_agent()
    print("  Poly Studio stopped (LegacyHostApp agent unloaded).")

    # Clean up stale sockets
    if SOCKET_PATH.exists():
        try:
            SOCKET_PATH.unlink()
        except OSError:
            pass

    # Step 2: Scan HID bus
    print("Step 2: Scanning for Poly devices...")
    proxy.discover_hid_devices()
    if not proxy.hid_devices:
        print("No Poly devices found on HID bus.")
        _reload_legacyhost_agent()
        PID_FILE.unlink(missing_ok=True)
        sys.exit(1)

    # Step 3: Start Poly Studio (legacyhost gets killed by our killer thread)
    print("Step 3: Starting Poly Studio...")
    subprocess.Popen(
        ["open", "/Applications/Poly Studio.app"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for the Clockwork socket to appear
    print("  Waiting for Clockwork socket...")
    socket_found = False
    for _ in range(60):  # Up to 30 seconds
        if SOCKET_PATH.exists():
            socket_found = True
            break
        time.sleep(0.5)

    if not socket_found:
        print("Error: Clockwork socket never appeared.")
        _reload_legacyhost_agent()
        PID_FILE.unlink(missing_ok=True)
        sys.exit(1)
    print(f"  Socket appeared at {SOCKET_PATH}")

    # Step 4: Connect to Clockwork — we're the FIRST client (legacyhost is disabled)
    print("Step 4: Connecting to Clockwork...")
    time.sleep(1)  # Let Clockwork finish setting up the listener
    for attempt in range(20):
        try:
            proxy.clockwork_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            proxy.clockwork_sock.connect(str(SOCKET_PATH))
            print("  Connected to Clockwork.")
            break
        except OSError as e:
            proxy.clockwork_sock = None
            if attempt < 19:
                time.sleep(0.5)
    else:
        print("Error: Could not connect to Clockwork socket.")
        _reload_legacyhost_agent()
        PID_FILE.unlink(missing_ok=True)
        sys.exit(1)

    # Step 5: Handshake — we should get INIT since we're the first client
    print("Step 5: Handshake with Clockwork...")
    init_msg = wait_for_init(proxy.clockwork_sock, timeout=15)

    if not init_msg:
        print("  ERROR: No INIT received from Clockwork.")
        proxy.close()
        _reload_legacyhost_agent()
        PID_FILE.unlink(missing_ok=True)
        sys.exit(1)

    print(f"  Clockwork version: {init_msg.get('service_version')}")
    proxy.send_json(proxy.clockwork_sock, {
        "client_version": "1.0.0-polybridge",
        "command": "READY",
        "error_desc": "",
        "successful": True,
    })
    print("  Handshake complete!")
    proxy.clockwork_sock.setblocking(False)

    # Step 6: Ready to start legacyhost
    print("Step 6: Preparing legacyhost...")

    # Step 7: Create our listener at the Clockwork socket path
    # Delete the original socket (Clockwork already accepted us, the listener
    # socket is no longer needed) and create our own for legacyhost
    print("Step 7: Setting up proxy socket...")
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    proxy.setup_fake_server()

    # Step 8: Start legacyhost manually (connects to our proxy)
    print("Step 8: Starting legacyhost...")
    lh_log = open("/tmp/polytool-legacyhost.log", "w")
    lh_proc = subprocess.Popen(
        [str(lh_bin)],
        stdout=lh_log,
        stderr=lh_log,
    )
    proxy.lh_proc = lh_proc
    print(f"  Started legacyhost (PID {lh_proc.pid})")

    # Step 9: Wait for legacyhost to connect
    print("Step 9: Waiting for legacyhost...")
    if not proxy.accept_legacyhost():
        proxy.close()
        lh_proc.terminate()
        PID_FILE.unlink(missing_ok=True)
        sys.exit(1)

    # LaunchAgent is still unloaded — PolyLauncher can't respawn legacyhost

    # Forward the real INIT to legacyhost
    proxy.send_json(proxy.legacy_sock, init_msg)
    proxy._suppress_ready = True  # We already sent READY to Clockwork
    print("  Forwarded INIT to legacyhost.")

    # Step 10: Run proxy
    print("Step 10: Proxy active! Logging all messages...\n")
    try:
        proxy.run(lh_bin, init_msg)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        proxy.close()
        _reload_legacyhost_agent()
        PID_FILE.unlink(missing_ok=True)
        if lh_proc:
            lh_proc.terminate()
        subprocess.run(["pkill", "-f", "legacyhost"], capture_output=True)


if __name__ == "__main__":
    main()
