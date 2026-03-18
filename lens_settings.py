#!/usr/bin/env python3
"""
Lens Settings — Per-device settings definitions for LensServer.

Maps device PIDs to their supported settings based on the Poly Lens
DeviceSetting.json database. Each device type (DECT, CX2070x, BladeRunner)
has a different settings profile.

Settings are defined with name, type, choices, default value, and
the Poly Studio GUI metadata format.
"""

import json
from pathlib import Path

# Load Poly's settings value database
_data_dir = Path(__file__).parent / "data"
_poly_settings = {}
try:
    _poly_settings = json.loads((_data_dir / "DeviceSetting.json").read_text())
except:
    pass


def _choices(name):
    """Get choice values from Poly's settings database."""
    vals = _poly_settings.get(name, {})
    if isinstance(vals, dict):
        return list(vals.values())
    return []


# ── Setting Definitions Per Device Family ─────────────────────────────────

# Setting names MUST match the IDs in settingsCategories (from Poly Studio renderer)
# The GUI only renders controls for settings it has a static definition for.
DECT_SETTINGS = [
    # Ringtones & Volume
    {"name": "Sidetone", "type": "enum", "choices": _choices("Sidetone") or ["Low", "Medium", "High"], "default": "Medium"},
    {"name": "Volume Level Tones", "type": "enum", "choices": _choices("Volume Level Tones") or ["At Every Level", "Minimum & Maximum Only"], "default": "At Every Level"},
    {"name": "Base Ringer Volume", "type": "int", "min": 0, "max": 10, "default": 7},
    {"name": "Desk Phone", "type": "enum", "choices": _choices("Desk Phone") or ["Sound 1", "Sound 2", "Sound 3", "Off"], "default": "Sound 1"},
    # Wireless
    {"name": "DECT Density", "type": "enum", "choices": _choices("DECT Density") or ["Music", "Hybrid", "Conversation", "Narrowband"], "default": "Conversation"},
    {"name": "HD Voice", "type": "bool", "default": True},
    {"name": "Power Level", "type": "enum", "choices": _choices("Power Level") or ["Low", "Medium", "High"], "default": "High"},
    {"name": "Keep Link Up", "type": "enum", "choices": _choices("Keep Link Up") or ["Active Only During Call", "Always Active"], "default": "Active Only During Call"},
    {"name": "Default Line Type", "type": "enum", "choices": _choices("Default Line Type") or ["Desk phone", "Computer", "Mobile"], "default": "Computer"},
    {"name": "Second Incoming Call", "type": "enum", "choices": _choices("Second Incoming Call") or ["Ignore", "Ring Once", "Ring Continuous"], "default": "Ignore"},
    # Sensors & Presence
    {"name": "Wearing Sensor", "type": "bool", "default": False},
    {"name": "Auto-Answer", "type": "bool", "default": False},
    {"name": "Active Call Audio", "type": "enum", "choices": _choices("Active Call Audio") or ["Do Nothing", "Transfer Audio to Mobile Phone", "Mute Microphone"], "default": "Do Nothing"},
    # Advanced
    {"name": "Anti-Startle", "type": "bool", "default": True},
    {"name": "Anti Startle 2", "type": "enum", "choices": _choices("Anti Startle 2") or ["Off", "Standard", "Enhanced"], "default": "Standard"},
    {"name": "Noise Exposure", "type": "enum", "choices": _choices("Noise Exposure") or ["Limit at 85 dBA", "Limit at 80 dBA", "No Limiting"], "default": "Limit at 85 dBA"},
    {"name": "Hours on Phone Per Day", "type": "enum", "choices": _choices("Hours on Phone Per Day") or ["2 hrs.", "4 hrs.", "6 hrs.", "8 hrs.", "Off"], "default": "Off"},
    # Language
    {"name": "Language Selection", "type": "enum", "choices": ["English", "French", "German", "Spanish", "Italian", "Portuguese", "Dutch", "Swedish", "Norwegian", "Danish", "Finnish", "Japanese", "Korean", "Mandarin", "Cantonese", "Russian"], "default": "English"},
]

CX2070X_SETTINGS = [
    {"name": "Sidetone", "type": "enum", "choices": ["Low", "Medium", "High"], "default": "Medium"},
    {"name": "Sidetone Level", "type": "int", "min": 0, "max": 10, "default": 3},
]

BLADERUNNER_SETTINGS = [
    {"name": "Sidetone", "type": "enum", "choices": _choices("Sidetone") or ["Low", "Medium", "High"], "default": "Medium"},
    {"name": "EQ Preset", "type": "enum", "choices": ["Default", "Bass Boost", "Bright", "Warm"], "default": "Default"},
    {"name": "Ringtone Volume", "type": "int", "min": 0, "max": 10, "default": 5},
    {"name": "Anti-Startle", "type": "enum", "choices": _choices("Anti Startle 2") or ["Off", "Standard", "Enhanced"], "default": "Standard"},
    {"name": "Noise Exposure", "type": "enum", "choices": _choices("Noise Exposure") or ["Limit at 85 dBA", "Limit at 80 dBA", "No Limiting"], "default": "Limit at 85 dBA"},
    {"name": "HD Voice", "type": "bool", "default": True},
    {"name": "Auto-Answer", "type": "bool", "default": False},
    {"name": "Wearing Sensor", "type": "bool", "default": False},
    {"name": "Online Indicator", "type": "bool", "default": True},
    {"name": "Mute Reminder Volume", "type": "enum", "choices": _choices("Mute Reminder Volume") or ["Off", "Low volume", "Standard volume"], "default": "Standard volume"},
    {"name": "Second Incoming Call", "type": "enum", "choices": _choices("Second Incoming Call") or ["Ignore", "Ring Once", "Ring Continuous"], "default": "Ignore"},
    {"name": "Volume Level Tones", "type": "enum", "choices": _choices("Volume Level Tones") or ["At Every Level", "Minimum & Maximum Only"], "default": "At Every Level"},
    {"name": "Language Selection", "type": "enum", "choices": ["English", "French", "German", "Spanish", "Italian", "Portuguese", "Dutch", "Swedish", "Norwegian", "Danish", "Finnish", "Japanese", "Korean", "Mandarin", "Cantonese", "Russian"], "default": "English"},
]

# Device family → settings profile
DEVICE_PROFILES = {
    "dect": DECT_SETTINGS,
    "cx2070x": CX2070X_SETTINGS,
    "bladerunner": BLADERUNNER_SETTINGS,
}


def get_device_family(usage_page, dfu_executor=""):
    """Determine device family from usage page and DFU executor."""
    if usage_page == 0xFFA0:
        return "cx2070x"
    if usage_page == 0xFFA2:
        return "dect"
    if dfu_executor in ("HidTiDfu", "SyncDfu", "StudioDfu"):
        return "bladerunner"
    return "dect"  # default


def get_settings_for_device(usage_page, dfu_executor=""):
    """Get the settings profile for a device."""
    family = get_device_family(usage_page, dfu_executor)
    return DEVICE_PROFILES.get(family, DECT_SETTINGS)


def settings_to_api_format(settings_defs, current_values=None):
    """Convert settings definitions to LensServiceApi format.

    Returns (metadata_list, settings_list) tuple.
    """
    if current_values is None:
        current_values = {}

    metadata = []
    values = []

    for s in settings_defs:
        name = s["name"]
        stype = s["type"]
        default = s.get("default")
        value = current_values.get(name, default)

        # Metadata entry (for GetDeviceSettingsMetadata)
        meta = {
            "name": name,
            "meta": {
                "name": name,
                "type": stype,
                "visible": True,
                "enabled": True,
                "read_only": False,
                "auto_supported": False,
                "default_value": default,
                "possible_values": s.get("choices"),
                "range_min": s.get("min") if stype == "int" else 0,
                "range_max": s.get("max") if stype == "int" else 0,
                "range_step": 1 if stype == "int" else 0,
            },
            "value": value,
            "value_int": int(value) if stype == "int" and value is not None else None,
            "value_bool": bool(value) if stype == "bool" else None,
            "value_string": None,
            "value_enum": str(value) if stype == "enum" and value is not None else None,
            "value_compound": None,
            "value_struct_array": None,
            "auto_mode": False,
            "default_struct_array_value": None,
        }

        metadata.append(meta)

        # Value entry (for GetDeviceSettings)
        val_entry = {"name": name, "value": value}
        if stype == "bool":
            val_entry["valueBool"] = bool(value) if value is not None else False
        elif stype == "int":
            val_entry["valueInt"] = int(value) if value is not None else 0
        elif stype == "enum":
            val_entry["valueEnum"] = str(value) if value is not None else ""
        values.append(val_entry)

    return metadata, values
