#!/usr/bin/env python3
"""
Device Settings Database — Loads canonical per-device settings from DeviceSettings.zip.

DeviceSettings.zip ships with Poly Studio's LegacyHost and contains one JSON file
per device PID, each defining the exact settings, HID metadata, and value encodings
for that device.

This module parses the zip at import time and provides:
  - SETTINGS_DB: unified map of hex_id -> {name, type, choices, default, internal_name}
  - PID_PROFILES: per-PID settings profiles (pid_hex -> [setting_defs])
  - HID_METADATA: per-PID per-setting HID read/write metadata
  - translate_value(): maps HID-level values to UI-level values
"""

import json
import zipfile
from pathlib import Path

_ZIP_PATH = Path(__file__).parent / "data" / "DeviceSettings.zip"

# ── Global Setting ID → Renderer Name ────────────────────────────────────────
# Maps globalSettingID to the name used in settingsCategories.json (what Poly
# Studio's renderer expects). Built from cross-referencing the zip's internal
# settingName with the renderer's setting IDs.

_ID_TO_RENDERER_NAME = {
    "0x102": "Second Incoming Call",
    "0x103": "Computer Volume",
    "0x104": "Desk Phone Volume",
    "0x105": "Ring Tone Volume Mobile",
    "0x106": "VoIP Interface Ringtone",
    "0x107": "Desk Phone",
    "0x108": "Mobile Interface Ringtone",
    "0x109": "Ringtone",
    "0x10a": "Base Ringer Volume",
    "0x111": "Bluetooth Enable",
    "0x200": "Wearing Sensor",
    "0x201": "Auto-Answer",
    "0x202": "Smart Audio Transfer",
    "0x203": "Auto-Lock Call Button Mode",
    "0x204": "Auto-Pause Music",
    "0x206": "Active Call Audio",
    "0x300": "Auto-Answer",  # intellistand — mapped to Auto-Answer for DECT
    "0x301": "Auto Connect To Mobile",
    "0x302": "Auto Disconnect Bluetooth",
    "0x400": "Default Line Type",
    "0x401": "Bluetooth Enabled",
    "0x402": "Mobile Voice Commands",
    "0x500": "Noise Exposure",
    "0x501": "Hours on Phone Per Day",
    "0x504": "Anti-Startle",
    "0x505": "Anti Startle 2",
    "0x600": "Answer/Ignore",
    "0x601": "Answering Call",
    "0x603": "Mute Reminder Time",
    "0x604": "Mute Tone Volume",
    "0x607": "Mute On/Off Alerts",
    "0x608": "System Tone Volume",
    "0x609": "Volume Level Tones",
    "0x60a": "Caller ID",
    "0x60c": "Active Audio Tone",
    "0x60d": "Mute Off Alert",
    "0x60e": "Battery Status On/Off",
    "0x60f": "Connection Indication",
    "0x610": "Mute Alerts",
    "0x61f": "Connection Indication",
    "0x700": "DECT Density",
    "0x701": "OTA Subscription",
    "0x702": "Power Level",
    "0x704": "A2DP Mode On/Off",
    "0x705": "Extended Range Mode (PC)",
    "0x706": "Audio Bandwidth VoIP",
    "0x707": "Audio Bandwidth PSTN",
    "0x708": "Audio Bandwidth VoIP",
    "0x800": "Select Headset Type",
    "0x802": "Multiband Expander",
    "0x803": "Sidetone",
    "0x804": "ANC Timeout",
    "0x805": "Notification Tones",
    "0x806": "Independent Volume Control",
    "0x808": "Equalizer",
    "0x810": "ANC Mode",
    "0x848": "Clear Trusted Device List",
    "0x902": "Online Indicator",
    "0x903": "Volume Control Orientation",
    "0x908": "Custom Button",
    "0x909": "Clear Trusted Device List",
    "0x90a": "Restore Defaults",
    "0x913": "Hold Reminder",
    "0xa00": "Enable Audio Sensing",
    "0xa01": "Dialtone On/Off",
    "0xb05": "Tone Control",
    "0xb06": "Volume Min/Max Alerts",
    "0xfaa": "ANC Transparency State",
    "0x1300": "Manage All",
    "0x1500": "Transparency Mode",
    "0xfff4": "Keep Link Up",
    "0xfff7": "Quick Disconnect",
}

# ── Value Translation Maps ────────────────────────────────────────────────────
# For settings where the native bridge / HID returns values that differ from
# what the UI expects. Maps (hex_id, hid_value) -> ui_value.
# Built from comparing DeviceSetting.json display values with zip possibleValues.

_VALUE_TRANSLATIONS = {
    # Keep Link Up: HID returns false/true, UI expects activeonlyduringcall/alwaysactive
    "0xfff4": {"false": "activeonlyduringcall", "true": "alwaysactive"},
    # Quick Disconnect: HID returns named values that differ
    "0xfff7": {
        "doNothing": "doNothing",
        "holdActiveCall": "placeCallOnHold",
        "lockScreen": "lockComputerScreen",
        "lockScreenAndHoldCall": "lockScreenAndPlaceCallOnHold",
    },
}


def _normalize_hex(hex_str):
    """Normalize hex ID to lowercase with 0x prefix."""
    if not hex_str:
        return ""
    s = hex_str.strip().lower()
    if not s.startswith("0x"):
        s = "0x" + s
    return s


def _determine_type(possible_values):
    """Determine setting type from possibleValues list."""
    names = [pv.get("name", "") for pv in possible_values]
    if set(names) == {"false", "true"} or set(names) == {"true", "false"}:
        return "bool"
    return "enum"


def _extract_choices(possible_values):
    """Extract choice names from possibleValues, preserving order."""
    return [pv["name"] for pv in possible_values if "name" in pv]


def _extract_default(setting):
    """Extract default value from a setting entry."""
    raw = setting.get("defaultValue", "")
    if raw == "":
        return None
    # Try to match by numeric value in possibleValues
    try:
        raw_int = int(raw)
        get_vals = setting.get("get", {}).get("possibleValues", [])
        for pv in get_vals:
            if pv.get("value") == raw_int:
                name = pv.get("name", "")
                if name in ("true", "false"):
                    return name == "true"
                return name
    except (ValueError, TypeError):
        pass
    # Return raw string
    if raw in ("true", "false"):
        return raw == "true"
    return raw


def load():
    """Load DeviceSettings.zip and build settings database.

    Returns:
        (settings_db, pid_profiles, hid_metadata)
        - settings_db: dict hex_id -> {name, type, choices, default, internal_name}
        - pid_profiles: dict pid_hex -> [setting_defs compatible with lens_settings]
        - hid_metadata: dict pid_hex -> {hex_id -> {get: {...}, set: {...}}}
    """
    settings_db = {}
    pid_profiles = {}
    hid_metadata = {}

    if not _ZIP_PATH.exists():
        return settings_db, pid_profiles, hid_metadata

    with zipfile.ZipFile(_ZIP_PATH, "r") as zf:
        for name in zf.namelist():
            if not name.endswith(".json"):
                continue
            try:
                data = json.loads(zf.read(name))
            except (json.JSONDecodeError, KeyError):
                continue

            pid_hex = _normalize_hex(data.get("pid", name.replace(".json", "")))
            settings = data.get("settings", [])
            profile = []
            pid_hid = {}

            for s in settings:
                gid = _normalize_hex(s.get("globalSettingID", ""))
                if not gid:
                    continue

                messaging = s.get("messaging", "")
                # Skip video/camera settings (0xc00-0xcff range)
                try:
                    gid_int = int(gid, 16)
                    if 0xc00 <= gid_int <= 0xcff:
                        continue
                except ValueError:
                    continue

                # Get renderer name
                renderer_name = _ID_TO_RENDERER_NAME.get(gid)
                if not renderer_name:
                    continue  # Unknown setting, skip

                # Determine type and choices from possibleValues
                # HID settings use get.possibleValues with "value" fields;
                # Deckard settings often only have set.possibleValues with "payload" fields
                get_info = s.get("get", {})
                set_info = s.get("set", {})
                get_vals = get_info.get("possibleValues", [])
                set_vals = set_info.get("possibleValues", [])
                # Use whichever has values; prefer get (read-side)
                effective_vals = get_vals if get_vals else set_vals

                stype = _determine_type(effective_vals) if effective_vals else "bool"

                # Override type for settings that the UI renders as enum dropdowns
                # despite having bool HID values (translated via _VALUE_TRANSLATIONS)
                if gid in _VALUE_TRANSLATIONS:
                    stype = "enum"

                choices = _extract_choices(effective_vals) if stype == "enum" else []

                # For settings with value translations, use UI-level choices
                if gid in _VALUE_TRANSLATIONS and stype == "enum":
                    choices = list(_VALUE_TRANSLATIONS[gid].values())

                default = _extract_default(s)
                # Translate default value too if applicable
                if gid in _VALUE_TRANSLATIONS and default is not None:
                    default = _VALUE_TRANSLATIONS[gid].get(str(default).lower(), default)

                internal_name = s.get("settingName", "")

                # Build unified entry
                entry = {
                    "name": renderer_name,
                    "type": stype,
                    "default": default,
                    "internal_name": internal_name,
                    "messaging": messaging,
                }
                if stype == "enum":
                    entry["choices"] = choices

                # Store in global DB (first occurrence wins, or prefer deckard)
                if gid not in settings_db or messaging == "deckard":
                    settings_db[gid] = entry

                # Build per-PID profile entry (format compatible with lens_settings)
                profile_entry = {
                    "name": renderer_name,
                    "type": stype,
                    "default": default,
                }
                if stype == "enum":
                    profile_entry["choices"] = choices
                profile.append(profile_entry)

                # Store HID metadata for direct read/write
                if messaging == "hid":
                    pid_hid[gid] = {
                        "get": get_info.get("hidMetadata", {}),
                        "get_values": get_vals,
                        "set": set_info.get("hidMetadata", {}),
                        "set_values": set_vals,
                    }

            if profile:
                pid_profiles[pid_hex] = profile
            if pid_hid:
                hid_metadata[pid_hex] = pid_hid

    return settings_db, pid_profiles, hid_metadata


def translate_value(hex_id, raw_value):
    """Translate a HID-level value to its UI-level equivalent.

    Returns the translated value, or the original if no translation exists.
    """
    hex_id = _normalize_hex(hex_id)
    mapping = _VALUE_TRANSLATIONS.get(hex_id)
    if mapping:
        translated = mapping.get(str(raw_value))
        if translated is not None:
            return translated
    return raw_value


# ── Module-level loading ──────────────────────────────────────────────────────
# Load once at import time. If the zip is missing, all maps are empty dicts
# and callers fall back to hardcoded definitions.

try:
    SETTINGS_DB, PID_PROFILES, HID_METADATA = load()
except Exception:
    SETTINGS_DB, PID_PROFILES, HID_METADATA = {}, {}, {}


def get_pid_profile(pid_int):
    """Get the settings profile for a device by its integer PID.

    Returns a list of setting defs compatible with lens_settings.settings_to_api_format(),
    or None if no profile exists for this PID.
    """
    pid_hex = f"0x{pid_int:x}"
    return PID_PROFILES.get(pid_hex)


def get_pid_hid_metadata(pid_int):
    """Get HID read/write metadata for a device by its integer PID.

    Returns a dict of hex_id -> {get, get_values, set, set_values}, or None.
    """
    pid_hex = f"0x{pid_int:x}"
    return HID_METADATA.get(pid_hex)
