#!/usr/bin/env python3
"""
PolyTool — Device discovery, constants, and HID communication.

Split from polytool.py for modularity. All public names are re-exported
by polytool.py for backward compatibility.
"""

import os
import re
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Suppress urllib3 SSL warnings on older macOS
warnings.filterwarnings("ignore", message=".*urllib3.*OpenSSL.*")

# ── Optional dependency imports ──────────────────────────────────────────────

try:
    import hid
except ImportError:
    hid = None

try:
    import requests
except ImportError:
    requests = None

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn
    from rich.live import Live
    from rich.text import Text
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# ── Constants ────────────────────────────────────────────────────────────────

VERSION = "2.0.0"

# Poly / Plantronics / HP Vendor IDs
POLY_VIDS = {
    0x047F: "Plantronics",
    0x0965: "Poly",
    0x03F0: "HP Inc.",
    0x1BD7: "Poly (Alt)",
}

# Vendor-specific HID usage pages used by Poly devices
VENDOR_USAGE_PAGES = {0xFFA0, 0xFFA2, 0xFF52, 0xFF58, 0xFF99}

# Cloud API endpoints (reverse-engineered from appsettings.json)
CLOUD_GRAPHQL = "https://api.silica-prod01.io.lens.poly.com/graphql"
FIRMWARE_CDN = "https://swupdate.lens.poly.com"

# Local config directory
CONFIG_DIR = Path.home() / ".polytool"
FIRMWARE_CACHE = CONFIG_DIR / "firmware_cache"

# ── Product Database ─────────────────────────────────────────────────────────
# Codename → Product Name mapping (from Devices.config + public product info)

CODENAME_MAP = {
    "Nirvana": "Voyager Focus 2",
    "Sublime": "Voyager 4200 UC",
    "Flamingo": "Voyager Free 60",
    "FlamingoCC": "Voyager Free 60+ UC",
    "FlamingoProsumerSKU": "Voyager Free 60",
    "Falcon_Mono": "Voyager Surround 80 UC Mono",
    "Falcon_Grey_Stereo": "Voyager Surround 80 UC Stereo",
    "Falcon_Boom": "Voyager Surround 85 UC Boom",
    "Falcon_Fold": "Voyager Surround 85 UC Fold",
    "Falcon_CS_Basic": "Voyager Surround 80 CS",
    "Falcon_CS_Premium": "Voyager Surround 85 CS",
    "OspreyCC": "Voyager 5200 UC",
    "Stork": "Voyager 4300 UC",
    "Owl": "Voyager 4400 UC",
    "Hydra": "Savi 7300/7400",
    "Poseidon": "Savi 7200",
    "Cinnamon": "Poly Studio Display",
    "Calisto Pro": "Calisto Pro",
    "Calisto 800": "Calisto 800",
    "Calisto 240": "Calisto 240",
    "Calisto 610 Series": "Calisto 610",
    "Calisto 620 Series": "Calisto 620",
    "C700 Series": "Voyager Focus UC / C700",
    "MDA200": "MDA200",
    "Yeti": "Blackwire Yeti",
    "BellRinger": "BellRinger",
    "BlueMax": "BT600",
    "DA45": "DA45",
    "ULCC-Manatee": "Poly ULCC Headset",
    # Savi 8200 series (DECT)
    "Yen Stereo": "Savi 8220",
    "Yen Mono": "Savi 8210",
    "Seirenes": "Savi 8200 Base",
    "Seirenes VOL": "Savi 8200 Base",
    "Paeon": "Savi 8200 Dongle",
    # Legacy / other
    "Madone": "Voyager Focus UC",
    "Moorea": "Voyager Legend",
    "Salmon": "Blackwire 8225",
}

# PID → Codename mapping from Devices.config (VID 0x047F)
PID_CODENAMES = {
    0x0410: "BellRinger", 0x0411: "Hydra", 0x0412: "Hydra",
    0x0620: "Calisto Pro", 0xAB01: "Yeti", 0xAB02: "Yeti",
    0xAB11: "Yeti", 0xAB12: "Yeti", 0xAC01: "Poseidon",
    0xAC11: "Poseidon", 0x4254: "BlueMax", 0x0715: "BT Adapter",
    0x0716: "BT Adapter", 0x0717: "BT Adapter", 0x0718: "BT Adapter",
    0xCA00: "Calisto 800", 0xCA01: "Calisto 800", 0xCA03: "Calisto 620 Series",
    0xAE01: "Calisto 240", 0xAE11: "Calisto 240", 0xAE04: "Calisto 240",
    0xC02F: "Calisto 610 Series", 0xC059: "Calisto 610 Series",
    0xAD03: "MDA200", 0xAD04: "MDA200",
    0x011A: "Nirvana", 0x011B: "Sublime", 0x011C: "Nirvana", 0x011D: "Sublime",
    0x0137: "Nirvana", 0x0138: "Nirvana",
    0x010A: "C700 Series", 0x010B: "C700 Series", 0x010C: "C700 Series",
    0x010D: "C700 Series", 0x0124: "C700 Series", 0x0125: "C700 Series",
    0x0129: "C700 Series", 0x012A: "C700 Series", 0x0139: "C700 Series",
    0x013A: "C700 Series", 0x013B: "C700 Series", 0x013C: "C700 Series",
    0x0172: "OspreyCC", 0x0173: "Stork", 0x0174: "Owl",
    0x017D: "Flamingo", 0x017E: "FlamingoProsumerSKU", 0xD00A: "FlamingoCC",
    0x0BBF: "Falcon_Mono", 0x04C8: "Falcon_Grey_Stereo",
    0x0CBF: "Falcon_Boom", 0x0DBF: "Falcon_Fold",
    0x05C5: "Falcon_CS_Basic", 0x02C0: "Falcon_CS_Premium",
    0x4319: "ULCC-Manatee", 0xAE02: "Cinnamon", 0xAE03: "Cinnamon",
    # Savi 7300/7400 series (base stations + headsets)
    0xAC27: "Savi 7310", 0xAC28: "Savi 7320",
    0xAC34: "Savi 7410", 0xAC35: "Savi 7420",
    0xAC37: "Savi 8410/8445", 0xAC38: "Savi 8420",
    0xAB06: "Savi 7310", 0xAB07: "Savi 7320",
    0xAB09: "Savi 8210", 0xAB0A: "Savi 8220",
    # HidTiDfu headsets (Blackwire 8225 / Salmon TI variants)
    0x4317: "Salmon", 0x4315: "Salmon",
    0xC053: "Salmon", 0xC054: "Salmon",
    0x430B: "Salmon", 0x430C: "Salmon",
    0x430D: "Salmon", 0x430A: "Salmon",
    # Savi 8200 series — DECT base stations + dongles (LegacyDfu / FwuApiDFU)
    0xACFF: "Yen Stereo",   # W8220T
    0xACFE: "Yen Mono",     # W8210T
    0xAC20: "Seirenes",     # Savi 8200 base
    0xAC26: "Seirenes",     # Savi 8200 base (variant)
    0xAC29: "Seirenes VOL", # Savi 8200 base (volume variant)
    0xAC31: "Seirenes",     # Savi 8200 base (variant)
    0xAC39: "Seirenes VOL", # Savi 8200 base (variant)
    0xAB03: "Paeon",        # Savi 8200 USB dongle
    0xAB04: "Paeon",        # Savi 8200 USB dongle (variant)
    # Voyager Focus UC / C700 legacy
    0x0127: "Madone",       # Voyager Focus UC
    # Voyager Legend
    0x0113: "Moorea",       # Voyager Legend
    0x0101: "Moorea",       # Voyager Legend (variant)
    # Sync speakerphones
    0x015C: "Sync 20", 0x0163: "Sync 40", 0x016D: "Sync 60",
    # Blackwire 7225
    0x4304: "Blackwire 7225",
}

# DFU executor mapping: LensProductId (hex) → executor name
DFU_EXECUTOR_MAP = {
    # btNeoDfu - Bluetooth headsets (BladeRunner FTP over BT)
    "17f": "btNeoDfu", "180": "btNeoDfu", "dc2": "btNeoDfu",
    "bbf": "btNeoDfu", "4c8": "btNeoDfu", "cbf": "btNeoDfu",
    "dbf": "btNeoDfu", "5c5": "btNeoDfu", "2c0": "btNeoDfu",
    "17d": "btNeoDfu", "17e": "btNeoDfu", "d00a": "btNeoDfu",
    # HidTiDfu - USB headsets (TI chipset, BladeRunner over HID)
    "4317": "HidTiDfu", "4315": "HidTiDfu",
    "430b": "HidTiDfu", "430d": "HidTiDfu",
    "430a": "HidTiDfu", "430c": "HidTiDfu",
    # CxEepromDfu - Blackwire 5xx (CX2070x EEPROM, same protocol as BW3220)
    "c053": "CxEepromDfu", "c054": "CxEepromDfu",
    # EncorePro USB series (same TI chipset)
    "430e": "HidTiDfu", "430f": "HidTiDfu",  # EncorePro 320/310 USB
    "431d": "HidTiDfu", "431e": "HidTiDfu", "431f": "HidTiDfu",  # EncorePro 515/525/545
    # DA adapters (TI chipset)
    "431b": "HidTiDfu", "431c": "HidTiDfu",  # DA75, DA85
    # Dell variants (same TI chipset)
    "a513": "HidTiDfu",  # Dell Pro Stereo WH3022
    # SyncDfu - Sync speakerphones (BladeRunner FTP over USB HID)
    "15c": "SyncDfu", "163": "SyncDfu", "16d": "SyncDfu",
    # StudioDfu - Studio video bars
    "9217": "StudioDfu", "9290": "StudioDfu", "92b2": "StudioDfu",
    "431a": "HidTiDfu",  # Poly Studio P21
    # LegacyDfu - DECT devices (FwuApiDFU via named pipe to PLTDeviceManager)
    # Savi 8200 series
    "acff": "LegacyDfu", "acfe": "LegacyDfu",  # W8220T, W8210T
    "ac20": "LegacyDfu", "ac26": "LegacyDfu",  # Savi 8200 bases
    "ac29": "LegacyDfu", "ac31": "LegacyDfu",
    "ac39": "LegacyDfu", "ab03": "LegacyDfu",
    "ab04": "LegacyDfu",
    # Savi 7300/7400 series
    "ac27": "LegacyDfu", "ac28": "LegacyDfu",
    "ac34": "LegacyDfu", "ac35": "LegacyDfu",
    "ac37": "LegacyDfu", "ac38": "LegacyDfu",
    # Savi 8200 base variants
    "ac22": "LegacyDfu", "ac2b": "LegacyDfu",  # Savi 8200 alt bases
    "ac21": "LegacyDfu", "ac2a": "LegacyDfu",  # Savi Office Base CDM
    # Other LegacyDfu devices
    "411": "LegacyDfu",   # Hydra / Savi 7x0
    "412": "LegacyDfu",
    "ac01": "LegacyDfu", "ac11": "LegacyDfu",  # Poseidon / Savi 7200
    # Savi USB dongles (headset-side, FWU protocol)
    "ab06": "LegacyDfu", "ab07": "LegacyDfu",  # Savi 7310/7320 dongles
    "ab09": "LegacyDfu", "ab0a": "LegacyDfu",  # Savi 8210/8220 dongles
    # Voyager Base/Dock (BT adapter, firmware via cloud)
    "2ea": "btNeoDfu", "2eb": "btNeoDfu",  # Voyager Base D / Base-M CD
    "2ec": "btNeoDfu", "2e4": "btNeoDfu",  # Voyager Base CD / Base-M CD
    # Voyager Focus UC (CSR-dfu2 via btNeoDfu)
    "127": "btNeoDfu",
    # Voyager Legend (USB DFU class protocol)
    "113": "usbdfu", "101": "usbdfu",
    # Blackwire 7225 (HidTiDfu)
    "4304": "HidTiDfu",
    # Blackwire 3220 (CX2070x EEPROM flash via HID)
    "c056": "CxEepromDfu",
}

# DFU transport info: executor → (transport, firmware format, platform support)
DFU_TRANSPORT_INFO = {
    "btNeoDfu":   ("BladeRunner FTP (BT)", "CSR-dfu2 / APPUHDR5", "Windows only"),
    "HidTiDfu":   ("BladeRunner FTP (USB HID)", "FIRMWARE container", "Cross-platform (experimental)"),
    "SyncDfu":    ("BladeRunner FTP (USB HID)", "APPUHDR5 / QCC5xxx", "Cross-platform (experimental)"),
    "StudioDfu":  ("BladeRunner FTP (USB HID)", "FIRMWARE container", "Cross-platform (experimental)"),
    "LegacyDfu":  ("FWU API (0x4Fxx)", "FWU", "Cross-platform (requires pyusb)"),
    "usbdfu":     ("USB DFU class", "CSR-dfu2", "Cross-platform (untested)"),
    "CxEepromDfu": ("CX2070x EEPROM (USB HID)", "S-record PTC", "Cross-platform"),
}

# Device category classification
DEVICE_CATEGORIES = {
    "headset": ["Voyager", "Blackwire", "Savi", "Focus", "Surround", "Falcon",
                "Nirvana", "Sublime", "Stork", "Owl", "Flamingo", "ULCC"],
    "speakerphone": ["Sync", "Calisto"],
    "camera": ["Studio", "Webcam", "Cam", "Cinnamon"],
    "adapter": ["BT Adapter", "BT600", "DA45", "MDA200", "BlueMax"],
}


# ── Bundled dfu.config (332 device entries) ─────────────────────────────────
# Extracted from C:\Program Files\Poly\Poly Studio\LegacyHost\Components\
# DFUManager\dfu.config — Poly's own master device table. Each entry has:
#   name         e.g. "Savi 7310"     (canonical model description)
#   handlers     {"Base": "FwuApiDFU", "Headset": "FwuApiDFU", ...}
#   supportedOs  ["win", "mac", "linux"]
#   corruptPid   PID the device exposes when in DFU/recovery mode
#   corruptVid   matching VID for recovery
# Re-extract whenever Poly Studio updates: see scripts/extract_dfu_config.py
# (or polytool's extract logic in this commit).

import json as _json

_DFU_DEVICES_PATH = Path(__file__).resolve().parent / "data" / "dfu_devices.json"

def _load_dfu_devices():
    try:
        return _json.loads(_DFU_DEVICES_PATH.read_text())
    except (OSError, ValueError):
        return {}

DFU_DEVICES = _load_dfu_devices()

# Reverse-lookup index: corrupt PID → primary PID (for recognizing devices
# that are stuck in firmware-recovery/DFU mode and report a generic VID/PID
# instead of their real one).
DFU_CORRUPT_INDEX = {}
for _pid_hex, _entry in DFU_DEVICES.items():
    _cpid = (_entry.get("corruptPid") or "").lower()
    if _cpid and _cpid not in ("0000", "ffff"):
        DFU_CORRUPT_INDEX[_cpid] = _pid_hex

# Brand prefixes that indicate a codename is already a human-friendly model
# name (e.g. "Savi 7310", "Blackwire 8225") and just needs "Poly " in front.
# Used as a final fallback when no CODENAME_MAP override exists.
_POLY_MODEL_PREFIXES = (
    "Savi", "Voyager", "Blackwire", "Sync", "Calisto", "EncorePro",
    "Studio", "Edge", "Focus", "MDA", "DA",
)


def _polyize(name: str) -> str:
    """Add the 'Poly ' brand prefix to a bare model name when one is missing.
    Idempotent — names that already start with Poly/Plantronics/HP are
    returned unchanged.
    """
    if not name:
        return name
    if name.startswith(("Poly ", "Plantronics ", "HP ")):
        return name
    if any(name.startswith(p + " ") or name == p for p in _POLY_MODEL_PREFIXES):
        return f"Poly {name}"
    return name


def lookup_device_info(pid: int) -> dict:
    """Return the dfu.config entry for a PID, or {} when unknown.

    Useful for callers that want the model name + supported handlers without
    going through the PolyDevice classification path."""
    return DFU_DEVICES.get(f"{pid:04x}", {}) or DFU_DEVICES.get(f"{pid:04X}", {}) or {}


def _normalize_version(v):
    """Normalize a firmware version string for comparison.

    Cloud returns formats like:
      "0225_0_0"       → 225  (underscores separate sub-components)
      "3861.3039.100"  → 3861 (dots separate sub-components when 3+ parts)
    Device BCD returns:
      "2.25"           → 225  (single dot = BCD decimal, rejoin digits)
      "38.61"          → 3861 (single dot = BCD decimal, rejoin digits)
      "3861"           → 3861 (raw digits)
    """
    if not v:
        return 0
    # Strip sub-components after underscore
    v = v.split("_")[0]
    parts = v.split(".")
    if len(parts) <= 2:
        # BCD format: "2.25" → "225", "38.61" → "3861", "3861" → "3861"
        digits = "".join(parts)
    else:
        # Multi-component cloud format: "3861.3039.100" → take first component "3861"
        digits = parts[0]
    try:
        return int(digits.lstrip("0") or "0")
    except ValueError:
        return 0


def _format_component_version(raw: str) -> str:
    """Convert a Poly per-component version string into human form.

    Poly's FirmwareComponents dict stores the BCD bcdDevice as a flat decimal
    string: "1082" → "10.82", "0315" → "3.15", "3038" → "30.38". A handful of
    components (notably setid) ship as already-dotted strings ("0.0.2134.3260"),
    so leave anything containing a dot alone.
    """
    if not raw or not isinstance(raw, str):
        return raw or ""
    if "." in raw:
        return raw
    digits = "".join(c for c in raw if c.isdigit()) or raw
    if len(digits) <= 2:
        return "0." + digits.zfill(2)
    return digits[:-2].lstrip("0") + "." + digits[-2:] if digits[:-2].lstrip("0") else "0." + digits[-2:]


# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class PolyDevice:
    """Represents a discovered Poly device."""
    vid: int
    pid: int
    serial: str = ""
    product_name: str = ""
    manufacturer: str = ""
    firmware_version: str = ""
    release_number: int = 0
    usage_page: int = 0
    usage: int = 0
    interface_number: int = -1
    path: bytes = b""
    bus_type: str = "unknown"
    # Extended info (populated by reading device)
    battery_level: int = -1
    battery_charging: bool = False
    battery_left: int = -1
    battery_right: int = -1
    battery_case: int = -1
    is_muted: bool = False
    is_on_head: bool = False
    is_in_call: bool = False
    # Computed fields
    codename: str = ""
    friendly_name: str = ""
    category: str = "unknown"
    dfu_executor: str = ""
    lens_product_id: str = ""
    # Per-unit identity / multi-component fw (populated from LCS cache when available)
    tattoo_serial: str = ""              # e.g. "S/N3J6DE4" — unique per physical unit
    firmware_components: dict = field(default_factory=dict)  # {usb:"1082", headset:"1065", ...}
    name_suffix: str = ""                # disambiguator appended when product_name collides
    # Sourced from the bundled dfu.config (Poly's own master device table).
    # model_description is the canonical model name ("Savi 7310"); the rest
    # are useful for diagnostics, recovery detection, and update planning.
    model_description: str = ""
    dfu_handlers: dict = field(default_factory=dict)  # {Base: FwuApiDFU, Headset: FwuApiDFU, ...}
    supported_os: list = field(default_factory=list)  # [win, mac, linux]
    is_in_recovery: bool = False         # True when this PID matches a corruptPid in dfu.config
    recovery_for_pid: str = ""           # If is_in_recovery, the primary PID this corresponds to

    @property
    def id(self) -> str:
        """Short unique ID for this device."""
        s = self.serial[:8] if self.serial else f"{self.vid:04x}{self.pid:04x}"
        return s

    @property
    def vid_hex(self) -> str:
        return f"0x{self.vid:04X}"

    @property
    def pid_hex(self) -> str:
        return f"0x{self.pid:04X}"

    @property
    def firmware_display(self) -> str:
        if self.firmware_version:
            return self.firmware_version
        if self.release_number > 0:
            # USB bcdDevice is BCD-encoded: 0x0225 → "2.25", 0x3861 → "38.61"
            digits = []
            for shift in (12, 8, 4, 0):
                digits.append(str((self.release_number >> shift) & 0xF))
            raw = "".join(digits).lstrip("0") or "0"
            # Insert decimal: last two digits are minor version
            if len(raw) <= 2:
                return "0." + raw.zfill(2)
            return raw[:-2] + "." + raw[-2:]
        return "unknown"

    @property
    def firmware_components_display(self) -> str:
        """Compact multi-component summary, e.g. 'usb=10.82, hs=10.65'.

        Returns empty string when no per-component data is known. Component
        version digits are formatted with the same bcdDevice convention as
        firmware_display (last two digits = minor version)."""
        if not self.firmware_components:
            return ""
        # Short labels for the common Savi DECT slots
        labels = {"usb": "usb", "headset": "hs", "base": "base",
                  "tuning": "tun", "pic": "pic", "bluetooth": "bt",
                  "camera": "cam", "setid": "setid"}
        parts = []
        for k, v in self.firmware_components.items():
            if not v or v.lower() in ("ffff", "ffffffff"):
                continue
            parts.append(f"{labels.get(k, k)}={_format_component_version(v)}")
        return ", ".join(parts)

    @property
    def display_name(self) -> str:
        """Friendly name, with a per-unit suffix appended when there's a
        product-name collision (so two Savi 7300s aren't indistinguishable)."""
        base = self.friendly_name or self.product_name or self.codename or "Unknown"
        if self.name_suffix:
            return f"{base} ({self.name_suffix})"
        return base

    @property
    def battery_display(self) -> str:
        parts = []
        if self.battery_level >= 0:
            parts.append(f"{self.battery_level}%")
        if self.battery_left >= 0:
            parts.append(f"L:{self.battery_left}%")
        if self.battery_right >= 0:
            parts.append(f"R:{self.battery_right}%")
        if self.battery_case >= 0:
            parts.append(f"Case:{self.battery_case}%")
        if self.battery_charging:
            parts.append("(charging)")
        return " ".join(parts) if parts else "n/a"


# ── Console Output ───────────────────────────────────────────────────────────

class Output:
    """Abstraction for rich/plain console output."""

    def __init__(self):
        # force_terminal + UTF-8 stdout so the card-style scan renders
        # correctly under cp1252 Windows consoles (default Console() picks
        # up cp1252, which then crashes on the box-drawing/glyphs rich
        # uses for panel borders).
        if HAS_RICH:
            try:
                if sys.platform == "win32":
                    sys.stdout.reconfigure(encoding="utf-8")
                    sys.stderr.reconfigure(encoding="utf-8")
            except Exception:
                pass
            self.console = Console()
        else:
            self.console = None

    def print(self, *args, **kwargs):
        if self.console:
            self.console.print(*args, **kwargs)
        else:
            print(*args)

    def error(self, msg: str):
        if self.console:
            self.console.print(f"[bold red]Error:[/] {msg}")
        else:
            print(f"Error: {msg}", file=sys.stderr)

    def warn(self, msg: str):
        if self.console:
            self.console.print(f"[bold yellow]Warning:[/] {msg}")
        else:
            print(f"Warning: {msg}", file=sys.stderr)

    def success(self, msg: str):
        if self.console:
            self.console.print(f"[bold green]{msg}[/]")
        else:
            print(msg)

    def header(self, title: str):
        if self.console:
            self.console.print(Panel(title, style="bold cyan", expand=False))
        else:
            print(f"\n{'='*60}")
            print(f"  {title}")
            print(f"{'='*60}")

    def device_table(self, devices: list, title: str = "Connected Poly Devices"):
        """Render every connected device as a Poly-Studio-style card.

        The historical name 'device_table' stays for backward-compat, but
        the layout is now card-per-device, mirroring Studio's device list:

           ╭ Poly Savi 7320 (S/N2UGHYA) ─────╮
           │  ● Connected                     │
           │                                  │
           │    🔋 Battery     100%           │
           │    📦 Firmware    10.82          │
           │                                  │
           │  serial    377CF5FD...           │
           │  VID:PID   047F:AC28             │
           ╰──────────────────────────────────╯

        Pass `--table` on the CLI for the older compact tabular view.
        """
        if not devices:
            self.warn("No Poly devices found.")
            return

        if self.console:
            self._device_cards(devices, title)
        else:
            self._device_table_plain(devices, title)

    def _device_cards(self, devices, title):
        """Rich card-per-device renderer."""
        from rich.table import Table as _T
        from rich.panel import Panel as _P
        from rich.text import Text as _Text
        from rich import box as _box

        self.console.print(f"\n[bold cyan]{title}[/]  ({len(devices)} device{'s' if len(devices) != 1 else ''})\n")

        for dev in devices:
            # Status pill - ASCII-safe (Windows cp1252 console can't render
            # the typical ● glyph and rich crashes on it)
            status = "[bold green]* Connected[/]"

            # Battery cell: bar + number
            bat_pct = dev.battery_level if dev.battery_level >= 0 else None
            if bat_pct is None:
                bat_str = "[dim]n/a[/]"
            else:
                if bat_pct > 50:
                    color = "green"
                elif bat_pct > 20:
                    color = "yellow"
                else:
                    color = "red"
                filled = bat_pct // 10
                empty = 10 - filled
                bat_str = (f"[{color}]{'#' * filled}[/][dim]{'-' * empty}[/]  "
                           f"[bold {color}]{bat_pct}%[/]")
                if dev.battery_charging:
                    bat_str += "  [yellow](charging)[/]"

            # Firmware cell: top-level + per-component
            fw_str = f"[bold green]{dev.firmware_display}[/]"
            if dev.firmware_components_display:
                fw_str += f"  [dim]({dev.firmware_components_display})[/]"

            # Body uses a tiny inner table for two-column "label / value"
            body = _T.grid(padding=(0, 2))
            body.add_column(style="dim", justify="right", width=10)
            body.add_column(no_wrap=False)
            body.add_row("Status", status)
            body.add_row("", "")
            body.add_row("Battery", bat_str)
            body.add_row("Firmware", fw_str)
            body.add_row("", "")
            body.add_row("Serial", dev.serial[:16] + "..." if len(dev.serial or "") > 16 else dev.serial)
            if dev.tattoo_serial:
                body.add_row("Tattoo", dev.tattoo_serial)
            body.add_row("VID:PID", f"{dev.vid:04X}:{dev.pid:04X}")
            body.add_row("Category", dev.category)

            self.console.print(_P(
                body,
                title=f"[bold white]{dev.display_name}[/]",
                title_align="left",
                border_style="cyan",
                box=_box.ROUNDED,
                expand=False,
                padding=(0, 1),
            ))

    def _device_table_plain(self, devices, title):
        """Old compact ASCII-table renderer for plain stdout / --table."""
        print(f"\n{title}")
        print("-" * 130)
        fmt = "{:<3} {:<44} {:<16} {:<13} {:<40} {:<11}"
        print(fmt.format("#", "Device", "Serial", "Battery", "FW", "VID:PID"))
        print("-" * 130)
        for i, dev in enumerate(devices, 1):
            name = dev.display_name
            fw = dev.firmware_components_display or dev.firmware_display
            print(fmt.format(
                i, name[:44],
                (dev.serial or "n/a")[:16],
                dev.battery_display[:13],
                fw[:40],
                f"{dev.vid:04X}:{dev.pid:04X}",
            ))


out = Output()


# ── Device Discovery ─────────────────────────────────────────────────────────

def classify_device(dev: PolyDevice):
    """Populate codename, friendly_name, category, and dfu_executor.

    Friendly-name priority (most-specific to least-specific):
      1. CODENAME_MAP override         — for codenames that need translation
                                          (e.g. internal "Hydra" → "Savi 7300/7400")
      2. dfu.config DeviceDescription  — Poly's own master table, 332 PIDs,
                                          covers SKUs polytool's hand-curated
                                          map doesn't know about
      3. USB descriptor product_name   — the iProduct string on the device
      4. f"Poly Device ({pid_hex})"    — last-resort placeholder
    """
    # Codename from PID database
    dev.codename = PID_CODENAMES.get(dev.pid, "")

    # Pull in everything dfu.config knows about this PID
    dfu_entry = lookup_device_info(dev.pid)
    dev.model_description = dfu_entry.get("name", "") or ""
    dev.dfu_handlers = dfu_entry.get("handlers", {}) or {}
    dev.supported_os = dfu_entry.get("supportedOs", []) or []

    # Recovery-mode detection: some devices expose a generic VID/PID when
    # stuck in DFU mode. dfu.config records that mapping so we can recognize
    # them and tell the user which device is actually bricked.
    pid_hex_lc = f"{dev.pid:04x}"
    if pid_hex_lc in DFU_CORRUPT_INDEX:
        dev.is_in_recovery = True
        dev.recovery_for_pid = DFU_CORRUPT_INDEX[pid_hex_lc]

    # Resolve friendly name through the priority above.
    mapped_name = CODENAME_MAP.get(dev.codename, "") if dev.codename else ""
    if mapped_name:
        dev.friendly_name = mapped_name
    elif dev.model_description:
        dev.friendly_name = _polyize(dev.model_description)
    elif dev.product_name:
        dev.friendly_name = dev.product_name
    else:
        dev.friendly_name = f"Poly Device ({dev.pid_hex})"

    # Category classification — search across all name variants
    search_str = (
        f"{dev.friendly_name} {dev.codename} {dev.product_name} "
        f"{mapped_name} {dev.model_description}"
    ).lower()
    dev.category = "other"
    for cat, keywords in DEVICE_CATEGORIES.items():
        for kw in keywords:
            if kw.lower() in search_str:
                dev.category = cat
                break
        if dev.category != "other":
            break

    # LensProductId (hex of PID)
    dev.lens_product_id = f"{dev.pid:x}"

    # DFU executor — prefer the explicit map, fall back to inferring from
    # the dfu_handlers (so newly added devices work without needing a
    # DFU_EXECUTOR_MAP entry).
    dev.dfu_executor = DFU_EXECUTOR_MAP.get(dev.lens_product_id, "")
    if not dev.dfu_executor and dev.dfu_handlers:
        dev.dfu_executor = _infer_executor_from_handlers(dev.dfu_handlers)


def _infer_executor_from_handlers(handlers: dict) -> str:
    """Map a set of dfu.config DFU handler names to the eDfu executor that
    actually drives them. Useful as a fallback when a PID isn't in
    DFU_EXECUTOR_MAP yet — Poly's handler names are the most reliable hint
    of which executor will be invoked.
    """
    names = {v for v in handlers.values() if isinstance(v, str)}
    if "FwuApiDFU" in names or "SetID" in names:
        return "LegacyDfu"
    if "ConexantDFU" in names:
        return "CxEepromDfu"
    if "NeoDFU" in names:
        return "btNeoDfu"
    if "TIDFU" in names or "TiDFU" in names:
        return "HidTiDfu"
    return ""


def discover_devices() -> list:
    """Enumerate all connected Poly/HP HID devices."""
    if hid is None:
        out.error("hidapi not installed. Run: pip install hidapi")
        return []

    raw_devices = hid.enumerate()
    seen = {}  # serial+pid → device (deduplicate interfaces)

    for info in raw_devices:
        vid = info.get("vendor_id", 0)
        if vid not in POLY_VIDS:
            continue

        pid = info.get("product_id", 0)
        serial = info.get("serial_number", "") or ""
        usage_page = info.get("usage_page", 0)

        # Prefer vendor-specific interface for each device, but track all
        key = f"{serial}_{pid}" if serial else f"noserial_{pid}_{info.get('interface_number', 0)}"

        existing = seen.get(key)
        if existing:
            # Prefer vendor-specific usage page over generic
            if usage_page in VENDOR_USAGE_PAGES and existing.usage_page not in VENDOR_USAGE_PAGES:
                pass  # Replace with this one
            elif existing.usage_page in VENDOR_USAGE_PAGES:
                continue  # Keep existing
            elif usage_page > existing.usage_page:
                pass  # Take higher usage page
            else:
                continue

        dev = PolyDevice(
            vid=vid,
            pid=pid,
            serial=serial,
            product_name=info.get("product_string", "") or "",
            manufacturer=info.get("manufacturer_string", "") or POLY_VIDS.get(vid, ""),
            release_number=info.get("release_number", 0),
            usage_page=usage_page,
            usage=info.get("usage", 0),
            interface_number=info.get("interface_number", -1),
            path=info.get("path", b""),
            bus_type=_bus_type_str(info.get("bus_type", 0)),
        )
        classify_device(dev)
        seen[key] = dev

    # Deduplicate further: group by serial and keep the best entry per physical device
    devices = _deduplicate_devices(list(seen.values()))

    # Hydrate per-unit identity (tattoo serial) and per-component firmware
    # versions from LCS's cache. LCS keeps these up-to-date for every attach;
    # reading them here is far cheaper than a full HID round-trip and avoids
    # contention with Poly Studio. No-op when LCS isn't installed.
    _hydrate_from_lcs_cache(devices)

    # Disambiguate duplicate friendly_names: two AC27/AC28 Savis report the
    # same product string, so users can't tell them apart in scan output.
    # Append the tattoo serial (or last 6 of genes serial) as a suffix.
    _disambiguate_names(devices)

    return sorted(devices, key=lambda d: (d.category, d.friendly_name, d.name_suffix))


def _hydrate_from_lcs_cache(devices: list) -> None:
    """Fill tattoo_serial and firmware_components from LCS's cache files.

    The cache lives at %PROGRAMDATA%\\Poly\\Lens Control Service\\device_PLT_*
    on Windows; each file is a JSON dump LCS keeps current per device attach.
    Silently no-ops when the cache doesn't exist (LCS not installed) or when
    we can't match a discovered device to any cache entry.
    """
    try:
        from setid_fix import read_lcs_device_cache
    except Exception:
        return
    cache = read_lcs_device_cache()
    if not cache:
        return
    for dev in devices:
        if not dev.serial:
            continue
        rec = cache.get(dev.serial)
        if not rec:
            continue
        tattoo = rec.get("TattooSerialNumber") or rec.get("DisplaySerialNumber") or ""
        if tattoo:
            dev.tattoo_serial = tattoo
        comps = rec.get("FirmwareComponents") or {}
        if isinstance(comps, dict):
            # Keep only populated, non-FF components
            dev.firmware_components = {
                k: v for k, v in comps.items()
                if isinstance(v, str) and v and v.lower() not in ("ffff", "ffffffff")
            }


def _disambiguate_names(devices: list) -> None:
    """Set name_suffix on devices whose friendly_name collides with another's.

    Prefers the tattoo serial as the suffix (short, human-friendly,
    user-recognizable from the headset's label). Falls back to the last 6
    characters of the GUID-style serial when no tattoo is available.
    """
    counts = {}
    for d in devices:
        counts[d.friendly_name] = counts.get(d.friendly_name, 0) + 1
    for d in devices:
        if counts.get(d.friendly_name, 0) > 1:
            if d.tattoo_serial:
                d.name_suffix = d.tattoo_serial
            elif d.serial:
                d.name_suffix = d.serial[-6:]


def _bus_type_str(bus: int) -> str:
    return {0: "unknown", 1: "USB", 2: "Bluetooth", 3: "I2C", 4: "SPI"}.get(bus, "unknown")


def _deduplicate_devices(devices: list) -> list:
    """Keep one entry per physical device (same serial + PID)."""
    by_identity = {}
    for dev in devices:
        ident = (dev.serial, dev.pid) if dev.serial else (id(dev),)
        existing = by_identity.get(ident)
        if existing is None:
            by_identity[ident] = dev
        else:
            # Prefer the one on a vendor-specific usage page
            if dev.usage_page in VENDOR_USAGE_PAGES and existing.usage_page not in VENDOR_USAGE_PAGES:
                by_identity[ident] = dev
    return list(by_identity.values())


# ── HID Communication ────────────────────────────────────────────────────────

def _open_hid(path: bytes):
    """Open a HID device by path. Returns hid.device or None."""
    try:
        h = hid.device()
        h.open_path(path)
        return h
    except Exception:
        return None


def try_read_battery(dev: PolyDevice) -> bool:
    """Attempt to read battery level from the device via HID.

    Returns True if battery was successfully read, False otherwise.
    Uses multiple strategies depending on the device protocol.
    """
    if hid is None or not dev.path:
        return False

    h = _open_hid(dev.path)
    if not h:
        return False

    try:
        # Strategy 1: Read feature report 15 (device info report)
        # On Poly devices, this contains device status including battery
        # at known byte positions depending on the protocol version
        try:
            data = h.get_feature_report(15, 64)
            if data and len(data) >= 25:
                # Bytes 24-27 on some devices: [main_batt, left, right, case]
                # Value 0x7F = unknown/unavailable
                for offset in [24, 25, 26, 27]:
                    if offset < len(data) and data[offset] != 0x7F and 0 < data[offset] <= 100:
                        dev.battery_level = data[offset]
                        return True
        except Exception:
            pass

        # Strategy 2: Try standard feature reports (IDs 3-8)
        for report_id in [3, 4, 5, 6, 7, 8]:
            try:
                data = h.get_feature_report(report_id, 64)
                if data and len(data) > 2:
                    for i in range(1, min(len(data), 8)):
                        if 5 <= data[i] <= 100:
                            dev.battery_level = data[i]
                            return True
            except Exception:
                continue

        # Strategy 3: For FFA2 devices (BaseHostCommand2), try sending
        # a battery query via output report and reading the response
        if dev.usage_page in (0xFFA2, 0xFFA0):
            try:
                h.set_nonblocking(1)
                # Read a few input reports — the device may be
                # pushing battery status events periodically
                for _ in range(30):
                    data = h.read(64)
                    if data and len(data) > 3:
                        # BladeRunner input reports can contain battery
                        # events. Look for known patterns.
                        if len(data) >= 6 and data[0] in (0x01, 0x02):
                            for i in range(2, min(len(data), 12)):
                                if 5 <= data[i] <= 100:
                                    dev.battery_level = data[i]
                                    return True
                    time.sleep(0.05)
            except Exception:
                pass

    except Exception:
        pass
    finally:
        try:
            h.close()
        except Exception:
            pass

    return False


def _try_cx2070x_serial(h) -> str:
    """Try reading serial number from CX2070x EEPROM (Blackwire 3220 etc.).
    Protocol: write [RID=4, CMD_EEPROM_READ, len, addr_hi, addr_lo, ...pad]
    then read response on RID 5."""
    CX_RID_OUT = 0x04
    CX_CMD_EEPROM_READ = 0x20
    SERIAL_ADDR = 0x005E  # CX2070x serial number EEPROM location
    SERIAL_LEN = 16

    try:
        pkt = [CX_RID_OUT, CX_CMD_EEPROM_READ, SERIAL_LEN,
               (SERIAL_ADDR >> 8) & 0xFF, SERIAL_ADDR & 0xFF] + [0x00] * 32
        h.write(pkt)
        resp = h.read(64, timeout_ms=2000)
        if resp and len(resp) > SERIAL_LEN:
            raw = bytes(resp[1:1 + SERIAL_LEN])
            # Serial is ASCII, null-terminated
            serial = raw.split(b'\x00')[0].decode('ascii', errors='ignore').strip()
            if serial and len(serial) >= 4:
                return serial
    except Exception:
        pass
    return ""


def try_read_device_info(dev: PolyDevice) -> bool:
    """Attempt to read extended device info (firmware version, serial) via HID."""
    if hid is None or not dev.path:
        return False

    # If we already have firmware from release_number and a serial, skip
    if dev.release_number > 0 and dev.serial:
        return True

    h = _open_hid(dev.path)
    if not h:
        return False

    try:
        # Try CX2070x EEPROM serial read (Blackwire 3220 on FFA0 usage page)
        if not dev.serial and dev.usage_page == 0xFFA0:
            serial = _try_cx2070x_serial(h)
            if serial:
                dev.serial = serial

        # If we already have firmware from release_number, done
        if dev.release_number > 0:
            return True

        # Try feature reports that commonly contain firmware version info
        for report_id in [1, 2, 5, 6, 9, 10, 15]:
            try:
                data = h.get_feature_report(report_id, 256)
                if data and len(data) > 4:
                    # Look for version-like ASCII strings
                    text = bytes(data[1:]).decode("ascii", errors="ignore")
                    match = re.search(r'(\d+\.\d+(?:\.\d+)?(?:\.\d+)?)', text)
                    if match:
                        dev.firmware_version = match.group(1)
                        return True
            except Exception:
                continue
    except Exception:
        pass
    finally:
        try:
            h.close()
        except Exception:
            pass

    return False


def check_dependencies():
    """Check that required dependencies are installed."""
    missing = []
    if hid is None:
        missing.append("hidapi")
    if requests is None:
        missing.append("requests")
    if not HAS_RICH:
        missing.append("rich (optional, for better UI)")

    if "hidapi" in missing or "requests" in missing:
        out.error("Missing required dependencies.")
        out.print(f"  Install with: pip install {' '.join(m.split()[0] for m in missing)}")
        out.print(f"  Or:           pip install -r requirements.txt")
        return False

    if not HAS_RICH:
        print("Note: Install 'rich' for a better UI: pip install rich")

    return True
