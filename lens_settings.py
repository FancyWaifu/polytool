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
    """Get choice keys from Poly's settings database (internal values)."""
    vals = _poly_settings.get(name, {})
    if isinstance(vals, dict):
        return list(vals.keys())
    return []


def _choice_default(name, display_default):
    """Convert a display value to its internal key."""
    vals = _poly_settings.get(name, {})
    if isinstance(vals, dict):
        for key, display in vals.items():
            if display == display_default:
                return key
    return display_default


# ── Setting Definitions Per Device Family ─────────────────────────────────

# Setting names MUST match the IDs in settingsCategories (from Poly Studio renderer)
# possible_values uses INTERNAL KEYS (not display values) — from DeviceSetting.json
DECT_SETTINGS = [
    # ── General ──
    {"name": "Answering Call", "type": "bool", "default": False},
    {"name": "Active Call Audio", "type": "bool", "default": False},
    {"name": "Auto-Answer", "type": "bool", "default": False},
    {"name": "Online Indicator", "type": "bool", "default": False},
    {"name": "Smart Audio Transfer", "type": "bool", "default": False},
    {"name": "Default Line Type", "type": "enum", "choices": _choices("Default Line Type") or ["pstn", "voip", "mobile"], "default": _choice_default("Default Line Type", "Computer")},
    {"name": "Second Incoming Call", "type": "enum", "choices": _choices("Second Incoming Call") or ["ignore", "once", "continuous"], "default": _choice_default("Second Incoming Call", "Ignore")},
    # ── Ringtones & Volume ──
    {"name": "Sidetone", "type": "enum", "choices": _choices("Sidetone") or ["low", "medium", "high"], "default": _choice_default("Sidetone", "Medium")},
    {"name": "Base Ringer Volume", "type": "enum", "choices": _choices("Base Ringer Volume") or ["off", "low", "medium", "high"], "default": _choice_default("Base Ringer Volume", "Medium")},
    {"name": "Computer Volume", "type": "enum", "choices": ["off", "low", "standard"], "default": "standard"},
    {"name": "Desk Phone Volume", "type": "enum", "choices": ["off", "low", "standard"], "default": "standard"},
    {"name": "Desk Phone", "type": "enum", "choices": _choices("Desk Phone") or ["sound1", "sound2", "sound3", "off"], "default": _choice_default("Desk Phone", "Sound 1")},
    {"name": "VoIP Interface Ringtone", "type": "enum", "choices": ["sound1", "sound2", "sound3"], "default": "sound1"},
    {"name": "Volume Level Tones", "type": "enum", "choices": _choices("Volume Level Tones") or ["atEveryLevel", "minMaxOnly"], "default": _choice_default("Volume Level Tones", "At Every Level")},
    {"name": "Mute On/Off Alerts", "type": "enum", "choices": ["singleTone", "doubleTone", "voice"], "default": "voice"},
    {"name": "System Tone Volume", "type": "enum", "choices": ["off", "low", "standard"], "default": "standard"},
    {"name": "Mute Reminder Time", "type": "enum", "choices": ["off", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"], "default": "15"},
    # ── Wireless ──
    {"name": "DECT Density", "type": "enum", "choices": _choices("DECT Density") or ["homeMode", "enterpriseMode", "mono"], "default": _choice_default("DECT Density", "Conversation")},
    {"name": "OTA Subscription", "type": "bool", "default": True},
    {"name": "Power Level", "type": "enum", "choices": _choices("Power Level") or ["low", "medium", "high"], "default": _choice_default("Power Level", "High")},
    {"name": "Keep Link Up", "type": "enum", "choices": _choices("Keep Link Up") or ["activeonlyduringcall", "alwaysactive"], "default": _choice_default("Keep Link Up", "Active Only During Call")},
    # ── Sensors & Presence ──
    {"name": "Wearing Sensor", "type": "bool", "default": False},
    {"name": "Enable Audio Sensing", "type": "bool", "default": False},
    {"name": "Dialtone On/Off", "type": "bool", "default": True},
    # ── Advanced / Audio ──
    {"name": "Anti-Startle", "type": "bool", "default": False},
    {"name": "Anti Startle 2", "type": "bool", "default": True},
    {"name": "Noise Exposure", "type": "enum", "choices": _choices("Noise Exposure") or ["off", "85db", "80db"], "default": _choice_default("Noise Exposure", "No Limiting")},
    {"name": "Hours on Phone Per Day", "type": "enum", "choices": _choices("Hours on Phone Per Day") or ["2", "4", "6", "8", "off"], "default": _choice_default("Hours on Phone Per Day", "Off")},
    {"name": "Multiband Expander", "type": "enum", "choices": ["no", "moderate", "agressive"], "default": "moderate"},
    # ── Language ──
    {"name": "Language Selection", "type": "enum", "choices": ["en", "fr", "de", "es", "it", "pt", "nl", "sv", "no", "da", "fi", "ja", "ko", "zh", "yue", "ru"], "default": "en"},
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

# Device family → (settings profile, read_only flag)
# DECT settings are read-only: the DECT base station write protocol
# has not been reverse-engineered yet. CX2070x and BladeRunner support writes.
DEVICE_PROFILES = {
    "dect": DECT_SETTINGS,
    "cx2070x": CX2070X_SETTINGS,
    "bladerunner": BLADERUNNER_SETTINGS,
}

WRITABLE_FAMILIES = {"cx2070x", "bladerunner"}


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


def settings_to_api_format(settings_defs, current_values=None, family=None,
                           force_writable=False):
    """Convert settings definitions to LensServiceApi format.

    Returns (metadata_list, settings_list) tuple.
    family: device family string — DECT settings are marked read-only unless
            force_writable=True (used when LCS proxy is available for writes).
    """
    if current_values is None:
        current_values = {}

    read_only = (family not in WRITABLE_FAMILIES and not force_writable) if family else False

    metadata = []
    values = []

    for s in settings_defs:
        name = s["name"]
        stype = s["type"]
        default = s.get("default")
        value = current_values.get(name, default)

        # Metadata entry — matches exact format from real LensService
        meta_obj = {
            "type": stype,
            "visible": True,
            "enabled": not read_only,
            "read_only": read_only,
            "auto_supported": False,
            "default_value": default,
            "possible_values": s.get("choices", []),
            "statuses": [],
        }

        if stype == "bool":
            meta_obj["default_bool_value"] = bool(default) if default is not None else False
            meta_obj["possible_values"] = ["false", "true"]
        elif stype == "int":
            meta_obj["default_int_value"] = int(default) if default is not None else 0
            meta_obj["range_min"] = s.get("min", 0)
            meta_obj["range_max"] = s.get("max", 10)
            meta_obj["range_step"] = 1

        meta = {
            "name": name,
            "meta": meta_obj,
        }

        metadata.append(meta)

        # Value entry (for GetDeviceSettings)
        # MUST include 'meta' — the renderer's selectMemoizedDeviceSetting
        # calls translateBaseSetting(upsertCompoundMetadata(settingValue, ...))
        # which checks o.meta to produce {readable, writable, storeType}.
        # Without meta, SettingItem's render check fails:
        #   !(S.meta?.readable || S.meta?.writable) → true → returns null
        val_entry = {"name": name, "value": value, "meta": meta_obj}
        if stype == "bool":
            val_entry["valueBool"] = bool(value) if value is not None else False
        elif stype == "int":
            val_entry["valueInt"] = int(value) if value is not None else 0
        elif stype == "enum":
            val_entry["valueEnum"] = str(value) if value is not None else ""
        values.append(val_entry)

    return metadata, values
