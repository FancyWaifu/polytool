#!/usr/bin/env python3
"""
Device Settings — HID-based settings read/write for Poly headsets.

Each device family has a different mechanism:
  - CX2070x (Blackwire 3220): vendor HID registers on FFA0
  - BladeRunner (Blackwire 33xx/7225/8225): BR GetSetting/SetSetting messages
  - FFA2/DECT (Savi series): feature report 0x0E (limited, needs base station)

Setting definitions specify name, type, range, and HID mapping per device family.
"""

import struct
import time

try:
    import hid
except ImportError:
    hid = None

# ── Setting Definitions ──────────────────────────────────────────────────────
# Each setting has:
#   name:      Display name (matches Poly Lens UI names)
#   type:      "range", "bool", "choice"
#   min/max:   For range type
#   choices:   For choice type
#   families:  Which device families support this setting
#   readable:  Can we read the current value?
#   writable:  Can we write/change it?

# Settings verified against actual hardware:
#   cx2070x: Only sidetone registers (0x191A, 0x1900) work via vendor HID.
#            Volume/mute use USB Audio class (OS controls).
#   bladerunner: GET_SETTING/SET_SETTING — needs testing with actual BW33xx/7225.
#   dect: Settings report is read-only via direct USB. Needs DECT base.
SETTINGS_DB = [
    {
        "name": "Sidetone On/Off",
        "type": "bool",
        "default": False,
        "families": ["cx2070x", "bladerunner"],
    },
    {
        "name": "Sidetone Level",
        "type": "range",
        "min": 0,
        "max": 10,
        "default": 3,
        "families": ["cx2070x", "bladerunner"],
    },
    {
        "name": "EQ Preset",
        "type": "choice",
        "choices": ["Default", "Bass Boost", "Bright", "Warm"],
        "default": "Default",
        "families": ["bladerunner"],
    },
    {
        "name": "Ringtone Volume",
        "type": "range",
        "min": 0,
        "max": 10,
        "default": 5,
        "families": ["bladerunner"],
    },
    {
        "name": "Anti-Startle Protection",
        "type": "bool",
        "default": True,
        "families": ["bladerunner"],
    },
    {
        "name": "Noise Limiting",
        "type": "bool",
        "default": True,
        "families": ["bladerunner"],
    },
    {
        "name": "G616 Limiting",
        "type": "bool",
        "default": False,
        "families": ["bladerunner"],
    },
    {
        "name": "HD Voice",
        "type": "bool",
        "default": True,
        "families": ["bladerunner"],
    },
    {
        "name": "Wearing Sensor On/Off",
        "type": "bool",
        "default": False,
        "families": ["bladerunner"],
    },
    {
        "name": "Auto-Answer",
        "type": "bool",
        "default": False,
        "families": ["bladerunner"],
    },
    {
        "name": "Mute Reminder Tone",
        "type": "bool",
        "default": True,
        "families": ["bladerunner"],
    },
    {
        "name": "Online Indicator",
        "type": "bool",
        "default": True,
        "families": ["bladerunner"],
    },
    {
        "name": "Audio Sensing",
        "type": "bool",
        "default": False,
        "families": ["bladerunner"],
    },
    {
        "name": "Second Incoming Call",
        "type": "choice",
        "choices": ["Ignore", "Ring", "Last Number Redial"],
        "default": "Ignore",
        "families": ["bladerunner"],
    },
    {
        "name": "Auto-Disconnect",
        "type": "bool",
        "default": False,
        "families": ["bladerunner"],
    },
    {
        "name": "IntelliStand On/Off",
        "type": "bool",
        "default": False,
        "families": ["bladerunner"],
    },
    {
        "name": "Volume Level",
        "type": "range",
        "min": 0,
        "max": 10,
        "default": 5,
        "families": ["bladerunner"],
    },
    {
        "name": "Microphone Level",
        "type": "range",
        "min": 0,
        "max": 10,
        "default": 5,
        "families": ["bladerunner"],
    },
    {
        "name": "Language Selection",
        "type": "choice",
        "choices": ["English", "French", "German", "Spanish", "Italian",
                    "Portuguese", "Dutch", "Swedish", "Norwegian", "Danish",
                    "Finnish", "Japanese", "Korean", "Mandarin", "Cantonese",
                    "Russian"],
        "default": "English",
        "families": ["bladerunner"],
    },
]


# ── Device Family Detection ──────────────────────────────────────────────────

def get_device_family(usage_page, dfu_executor=""):
    """Determine the settings family for a device."""
    if usage_page == 0xFFA0:
        return "cx2070x"
    if usage_page == 0xFFA2:
        return "dect"
    if dfu_executor in ("HidTiDfu", "SyncDfu", "StudioDfu"):
        return "bladerunner"
    return "unknown"


def get_settings_for_device(usage_page, dfu_executor=""):
    """Return the list of settings supported by a device."""
    family = get_device_family(usage_page, dfu_executor)
    if family == "unknown":
        return []
    return [s for s in SETTINGS_DB if family in s["families"]]


# ── CX2070x Settings (Blackwire 3220) ────────────────────────────────────────

CX_RID_OUT = 0x04
CX_CMD_REG_READ = 0x00
CX_CMD_REG_WRITE = 0x40
CX_NOT_SUPPORTED = 0x0202

# Register map: setting_name → (address, read_transform, write_transform)
# Verified via write-readback testing on actual hardware.
CX_REGISTER_MAP = {
    "Sidetone Level": (0x191A, lambda r: round(r * 10 / 0x7FFF),
                                lambda v: int(v * 0x7FFF / 10)),
    "Sidetone On/Off": (0x1900, lambda r: bool(r & 0x0001),
                                 lambda v: 0x0001 if v else 0x0000),
    # Volume/Mute (0x0800, 0x0900, 0x0808, 0x0908) return 0x0202 — not supported.
    # Blackwire 3220 uses standard USB Audio class for volume/mute (OS controls).
    # GPIO Ctrl (0x00A0) is writable but controls hardware pins — DO NOT EXPOSE.
}


def cx_read_setting(h, name):
    """Read a setting from CX2070x register."""
    spec = CX_REGISTER_MAP.get(name)
    if not spec:
        return None
    addr, read_fn, _ = spec
    try:
        pkt = [CX_RID_OUT, CX_CMD_REG_READ, 2,
               (addr >> 8) & 0xFF, addr & 0xFF] + [0x00] * 32
        h.write(pkt)
        resp = h.read(64, timeout_ms=1000)
        if resp and len(resp) >= 3:
            raw = (resp[1] << 8) | resp[2]
            if raw == CX_NOT_SUPPORTED:
                return None
            return read_fn(raw)
    except Exception:
        pass
    return None


def cx_write_setting(h, name, value):
    """Write a setting to CX2070x register."""
    spec = CX_REGISTER_MAP.get(name)
    if not spec:
        return False
    addr, _, write_fn = spec
    try:
        device_val = write_fn(value)
        pkt = [CX_RID_OUT, CX_CMD_REG_WRITE, 2,
               (addr >> 8) & 0xFF, addr & 0xFF,
               (device_val >> 8) & 0xFF, device_val & 0xFF] + [0x00] * 30
        h.write(pkt)
        time.sleep(0.01)
        return True
    except Exception:
        return False


# ── BladeRunner Settings (Blackwire 33xx/7225/8225, Sync) ─────────────────────

# BladeRunner setting IDs (from RE of LegacyHost + HidTiDfu)
# These are queried via GET_SETTING (msg type 1) and written via SET_SETTING
BR_SETTING_IDS = {
    "Sidetone On/Off":        0x0011,
    "Sidetone Level":         0x0010,
    "EQ Preset":              0x0020,
    "Ringtone Volume":        0x0030,
    "Anti-Startle Protection": 0x0040,
    "Noise Limiting":         0x0041,
    "G616 Limiting":          0x0042,
    "HD Voice":               0x0050,
    "Wearing Sensor On/Off":  0x0060,
    "Auto-Answer":            0x0070,
    "Mute Reminder Tone":     0x0080,
    "Online Indicator":       0x0090,
    "Audio Sensing":          0x00A0,
    "Second Incoming Call":   0x00B0,
    "Auto-Disconnect":        0x00C0,
    "IntelliStand On/Off":    0x00D0,
    "Volume Level":           0x00E0,
    "Microphone Level":       0x00F0,
    "Language Selection":     0x0100,
}

# Choice value encoding for BladeRunner
BR_CHOICE_MAP = {
    "EQ Preset": {"Default": 0, "Bass Boost": 1, "Bright": 2, "Warm": 3},
    "Second Incoming Call": {"Ignore": 0, "Ring": 1, "Last Number Redial": 2},
    "Language Selection": {
        "English": 0, "French": 1, "German": 2, "Spanish": 3,
        "Italian": 4, "Portuguese": 5, "Dutch": 6, "Swedish": 7,
        "Norwegian": 8, "Danish": 9, "Finnish": 10, "Japanese": 11,
        "Korean": 12, "Mandarin": 13, "Cantonese": 14, "Russian": 15,
    },
}


def br_read_setting(h, name, report_size=64):
    """Read a setting via BladeRunner GET_SETTING."""
    setting_id = BR_SETTING_IDS.get(name)
    if setting_id is None:
        return None

    try:
        # Build GET_SETTING packet
        pkt = bytearray(report_size)
        pkt[0] = 0x00  # Report ID
        pkt[1] = 0x01  # GET_SETTING
        pkt[2] = (setting_id >> 8) & 0xFF
        pkt[3] = setting_id & 0xFF
        pkt[4] = 0x00  # payload len hi
        pkt[5] = 0x00  # payload len lo
        h.write(bytes(pkt))

        # Read response
        h.set_nonblocking(1)
        deadline = time.time() + 2
        while time.time() < deadline:
            resp = h.read(report_size)
            if resp and len(resp) >= 6:
                msg_type = resp[0]
                resp_id = (resp[1] << 8) | resp[2]
                plen = (resp[3] << 8) | resp[4]

                if msg_type == 4 and resp_id == setting_id:  # SETTING_SUCCESS
                    payload = resp[5:5+plen]
                    return _decode_br_value(name, payload)
                elif msg_type == 5:  # SETTING_EXCEPTION
                    return None
            time.sleep(0.01)
    except Exception:
        pass
    return None


def br_write_setting(h, name, value, report_size=64):
    """Write a setting via BladeRunner SET_SETTING (type 3)."""
    setting_id = BR_SETTING_IDS.get(name)
    if setting_id is None:
        return False

    payload = _encode_br_value(name, value)
    if payload is None:
        return False

    try:
        pkt = bytearray(report_size)
        pkt[0] = 0x00  # Report ID
        pkt[1] = 0x03  # SET_SETTING (PerformCommand repurposed for settings)
        pkt[2] = (setting_id >> 8) & 0xFF
        pkt[3] = setting_id & 0xFF
        pkt[4] = 0x00
        pkt[5] = len(payload)
        pkt[6:6+len(payload)] = payload
        h.write(bytes(pkt))

        # Wait for ACK
        h.set_nonblocking(1)
        deadline = time.time() + 2
        while time.time() < deadline:
            resp = h.read(report_size)
            if resp and len(resp) >= 3:
                msg_type = resp[0]
                if msg_type in (4, 6):  # SETTING_SUCCESS or COMMAND_SUCCESS
                    return True
                elif msg_type in (5, 7):  # Exception
                    return False
            time.sleep(0.01)
        return True  # Assume success if no response (some devices don't ACK)
    except Exception:
        return False


def _read_dect_status(h):
    """Read status from DECT device via sign-on + settings report.
    Settings report (RID 0x0E) is read-only and arrives after sign-on.
    Returns list of read-only status entries."""
    import signal as _signal

    results = []

    # Need sign-on to get the settings report
    def _timeout(signum, frame):
        raise TimeoutError()

    old = _signal.signal(_signal.SIGALRM, _timeout)
    _signal.alarm(3)
    try:
        h.write(bytes([0x0D, 0x15]))  # sign on
        _signal.alarm(0)
    except (TimeoutError, Exception):
        _signal.alarm(0)
        _signal.signal(_signal.SIGALRM, old)
        return results
    _signal.signal(_signal.SIGALRM, old)

    # Read responses for 3 seconds
    h.set_nonblocking(1)
    settings_data = None
    deadline = time.time() + 3
    while time.time() < deadline:
        data = h.read(256)
        if data and data[0] == 0x0E and len(data) >= 11:
            settings_data = bytes(data)
            break
        time.sleep(0.01)

    # Sign off
    try:
        h.write(bytes([0x0D, 0x00]))
    except Exception:
        pass

    if settings_data:
        d = settings_data
        # Decode known fields
        fw = (d[2] << 8) | d[1]  # LE
        fw_str = f"{fw >> 8}.{fw & 0xFF}"
        results.append({"name": "Firmware Version", "value": fw_str,
                        "type": "text", "writable": False})

        if len(d) > 10:
            connected = bool(d[10] & 0x01)
            results.append({"name": "Headset Connected", "value": connected,
                            "type": "bool", "writable": False})

        results.append({"name": "Note", "value": "Settings require DECT base station",
                        "type": "text", "writable": False})

    return results


def _decode_br_value(name, payload):
    """Decode a BladeRunner setting value from payload bytes."""
    if not payload:
        return None

    # Find the setting definition
    sdef = None
    for s in SETTINGS_DB:
        if s["name"] == name:
            sdef = s
            break
    if not sdef:
        return payload[0] if len(payload) == 1 else int.from_bytes(payload[:2], 'big')

    if sdef["type"] == "bool":
        return bool(payload[0])
    elif sdef["type"] == "range":
        return payload[0] if len(payload) == 1 else int.from_bytes(payload[:2], 'big')
    elif sdef["type"] == "choice":
        choices = BR_CHOICE_MAP.get(name, {})
        val = payload[0]
        # Reverse lookup
        for label, code in choices.items():
            if code == val:
                return label
        return sdef.get("default", val)
    return payload[0]


def _encode_br_value(name, value):
    """Encode a setting value for BladeRunner SET_SETTING."""
    sdef = None
    for s in SETTINGS_DB:
        if s["name"] == name:
            sdef = s
            break
    if not sdef:
        return None

    if sdef["type"] == "bool":
        return bytes([1 if value else 0])
    elif sdef["type"] == "range":
        v = int(value) if isinstance(value, (int, float)) else 0
        return bytes([max(sdef.get("min", 0), min(sdef.get("max", 255), v))])
    elif sdef["type"] == "choice":
        choices = BR_CHOICE_MAP.get(name, {})
        code = choices.get(value, 0)
        return bytes([code])
    return bytes([0])


# ── Unified Read/Write ────────────────────────────────────────────────────────

def read_all_settings(path, usage_page, dfu_executor=""):
    """Read all available settings from a device.

    Returns list of {name, value, type, min, max, choices, writable}.
    """
    if hid is None:
        return []

    family = get_device_family(usage_page, dfu_executor)
    defs = get_settings_for_device(usage_page, dfu_executor)
    if not defs:
        return []

    h = None
    try:
        h = hid.device()
        h.open_path(path)
        h.set_nonblocking(0)
    except Exception:
        return []

    results = []

    # For DECT devices connected directly via USB, read what we can
    # from the sign-on settings report (RID 0x0E)
    if family == "dect":
        results = _read_dect_status(h)
        try:
            h.close()
        except Exception:
            pass
        return results

    try:
        for sdef in defs:
            name = sdef["name"]
            value = None

            if family == "cx2070x":
                value = cx_read_setting(h, name)
            elif family == "bladerunner":
                value = br_read_setting(h, name)

            if value is None:
                value = sdef.get("default")

            entry = {
                "name": name,
                "value": value,
                "type": sdef["type"],
                "writable": family in ("cx2070x", "bladerunner"),
            }
            if sdef["type"] == "range":
                entry["min"] = sdef.get("min", 0)
                entry["max"] = sdef.get("max", 10)
            elif sdef["type"] == "choice":
                entry["choices"] = sdef.get("choices", [])

            results.append(entry)
    finally:
        if h:
            try:
                h.close()
            except Exception:
                pass

    return results


def write_setting(path, usage_page, dfu_executor, name, value):
    """Write a single setting to a device. Returns True on success."""
    if hid is None:
        return False

    family = get_device_family(usage_page, dfu_executor)

    h = None
    try:
        h = hid.device()
        h.open_path(path)
        h.set_nonblocking(0)

        if family == "cx2070x":
            return cx_write_setting(h, name, value)
        elif family == "bladerunner":
            return br_write_setting(h, name, value)
        return False
    except Exception:
        return False
    finally:
        if h:
            try:
                h.close()
            except Exception:
                pass
