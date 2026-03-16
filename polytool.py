#!/usr/bin/env python3
"""
PolyTool — Poly/Plantronics Headset Management & Firmware Update Utility

Reverse-engineered from Poly Studio 5.0.1.9 (HP Inc.)

Features:
  scan       Discover all connected Poly/HP devices
  info       Show detailed device info (incl. DFU transport & platform support)
  battery    Show battery levels for all devices
  updates    Check for available firmware updates
  update     Download & apply firmware to one or all devices
  monitor    Live device status dashboard
  catalog    Search the Poly cloud firmware catalog
  fwinfo     Analyze a firmware package (parse format, components, rules.json)

Requirements: pip install hidapi requests rich
"""

import argparse
import json
import os
import platform
import re
import struct
import sys
import time
import threading
import hashlib
import tempfile
import warnings
from dataclasses import dataclass, field
from enum import Enum
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
VENDOR_USAGE_PAGES = {0xFFA0, 0xFFA2, 0xFF52, 0xFF58}

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
    "4317": "HidTiDfu", "4315": "HidTiDfu", "c053": "HidTiDfu",
    "c054": "HidTiDfu", "430b": "HidTiDfu", "430d": "HidTiDfu",
    "430a": "HidTiDfu", "430c": "HidTiDfu",
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
        self.console = Console() if HAS_RICH else None

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
        if not devices:
            self.warn("No Poly devices found.")
            return

        if self.console:
            table = Table(title=title, box=box.ROUNDED, show_lines=False)
            table.add_column("#", style="dim", width=2, no_wrap=True)
            table.add_column("Device", style="bold white", no_wrap=True, max_width=28)
            table.add_column("Firmware", style="green", no_wrap=True)
            table.add_column("Battery", style="yellow", no_wrap=True)
            table.add_column("VID:PID", style="dim", no_wrap=True)
            table.add_column("Category", style="magenta", no_wrap=True)

            for i, dev in enumerate(devices, 1):
                name = dev.friendly_name or dev.product_name or dev.codename or "Unknown"
                bat = dev.battery_display
                if dev.battery_level >= 0:
                    if dev.battery_level > 50:
                        bat = f"[green]{bat}[/]"
                    elif dev.battery_level > 20:
                        bat = f"[yellow]{bat}[/]"
                    else:
                        bat = f"[red]{bat}[/]"
                table.add_row(
                    str(i),
                    name,
                    dev.firmware_display,
                    bat,
                    f"{dev.vid:04X}:{dev.pid:04X}",
                    dev.category,
                )
            self.console.print(table)
        else:
            print(f"\n{title}")
            print("-" * 80)
            fmt = "{:<3} {:<28} {:<16} {:<10} {:<15} {:<11}"
            print(fmt.format("#", "Device", "Serial", "FW", "Battery", "VID:PID"))
            print("-" * 80)
            for i, dev in enumerate(devices, 1):
                name = dev.friendly_name or dev.product_name or dev.codename or "Unknown"
                print(fmt.format(
                    i,
                    name[:28],
                    (dev.serial or "n/a")[:16],
                    dev.firmware_display[:10],
                    dev.battery_display[:15],
                    f"{dev.vid:04X}:{dev.pid:04X}",
                ))


out = Output()


# ── Device Discovery ─────────────────────────────────────────────────────────

def classify_device(dev: PolyDevice):
    """Populate codename, friendly_name, category, and dfu_executor."""
    # Codename from PID database
    dev.codename = PID_CODENAMES.get(dev.pid, "")

    # Friendly name: prefer codename-mapped name (more descriptive), then USB string
    mapped_name = CODENAME_MAP.get(dev.codename, "") if dev.codename else ""
    if mapped_name:
        dev.friendly_name = mapped_name
    elif dev.product_name:
        dev.friendly_name = dev.product_name
    else:
        dev.friendly_name = f"Poly Device ({dev.pid_hex})"

    # Category classification — search across all name variants
    search_str = f"{dev.friendly_name} {dev.codename} {dev.product_name} {mapped_name}".lower()
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

    # DFU executor
    dev.dfu_executor = DFU_EXECUTOR_MAP.get(dev.lens_product_id, "")


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
    return sorted(devices, key=lambda d: (d.category, d.friendly_name))


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


# ── Cloud API ────────────────────────────────────────────────────────────────

class PolyCloudAPI:
    """Client for the Poly Lens Cloud GraphQL API.

    The Poly Silica API at api.silica-prod01.io.lens.poly.com exposes a public
    GraphQL endpoint that serves the firmware catalog, product info, and upgrade
    rules without authentication. Firmware binaries are hosted on the public CDN
    at swupdate.lens.poly.com. No login required.
    """

    # Local product catalog cache (mirrors the 7-day TTL from LensService)
    CACHE_TTL = 7 * 24 * 3600

    def __init__(self):
        self._catalog_cache = {}
        self._load_catalog_cache()

    def _cache_path(self) -> Path:
        return CONFIG_DIR / "product_catalog.json"

    def _load_catalog_cache(self):
        cp = self._cache_path()
        if cp.exists():
            try:
                data = json.loads(cp.read_text())
                if data.get("_ts", 0) + self.CACHE_TTL > time.time():
                    self._catalog_cache = data
            except Exception:
                pass

    def _save_catalog_cache(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._catalog_cache["_ts"] = time.time()
        self._cache_path().write_text(json.dumps(self._catalog_cache))

    def _graphql(self, query: str, variables: dict = None) -> Optional[dict]:
        """Execute a GraphQL query (no auth required for catalog queries)."""
        if requests is None:
            out.error("requests library not installed. Run: pip install requests")
            return None

        try:
            resp = requests.post(
                CLOUD_GRAPHQL,
                json={"query": query, "variables": variables or {}},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if "errors" in data:
                    for err in data["errors"]:
                        out.warn(f"GraphQL: {err.get('message', 'unknown error')}")
                    if not data.get("data"):
                        return None
                return data.get("data")
            else:
                out.error(f"GraphQL request failed: {resp.status_code}")
                return None
        except requests.exceptions.RequestException as e:
            out.error(f"Network error: {e}")
            return None

    def check_firmware(self, dev: PolyDevice) -> Optional[dict]:
        """Check for firmware updates for a device.

        Uses the public availableProductSoftwareByPid query.
        Returns dict with firmware info or None.
        """
        pid = dev.lens_product_id

        # Check cache first
        cache_key = f"fw_{pid}"
        if cache_key in self._catalog_cache:
            cached = self._catalog_cache[cache_key]
            if cached.get("_ts", 0) + self.CACHE_TTL > time.time():
                return cached

        query = """query ($pid: ID!) {
  availableProductSoftwareByPid(pid: $pid) {
    id version name latest releaseChannel
    supportsPolicies deprecated blockedDownload
    publishDate
    releaseNotes { id header body }
    productBuild {
      id version build url archiveUrl storagePath category description
    }
    product { id name }
  }
}"""
        data = self._graphql(query, {"pid": pid})

        if not data or not data.get("availableProductSoftwareByPid"):
            return None

        sw = data["availableProductSoftwareByPid"]
        build = sw.get("productBuild") or {}
        product = sw.get("product") or {}

        # Build release notes string from sections
        notes_parts = []
        for section in (sw.get("releaseNotes") or []):
            header = section.get("header", "")
            body = section.get("body", "")
            if header:
                notes_parts.append(header)
            if body:
                notes_parts.append(body)
        release_notes = "\n".join(notes_parts)

        result = {
            "current": dev.firmware_display,
            "latest": sw.get("version", "unknown"),
            "product_name": product.get("name", ""),
            "release_channel": sw.get("releaseChannel", ""),
            "download_url": build.get("archiveUrl") or build.get("url", ""),
            "publish_date": sw.get("publishDate", ""),
            "release_notes": release_notes,
            "is_latest": sw.get("latest", False),
            "blocked_download": sw.get("blockedDownload", False),
            "deprecated": sw.get("deprecated", False),
        }

        # Cache the result
        result["_ts"] = time.time()
        self._catalog_cache[cache_key] = result
        self._save_catalog_cache()

        return result

    def check_stepped_firmware(self, dev: PolyDevice) -> Optional[dict]:
        """Check if a stepped (intermediate) firmware version is needed."""
        query = """query ($q: SteppedQuery!) {
  getSteppedProductSoftwareByDeviceId(query: $q) {
    foundSoftware
    isExplicitTarget
    steppedUpgradeVersionList
    productSoftware {
      version
      productBuild { url archiveUrl }
    }
  }
}"""
        variables = {
            "q": {
                "pid": dev.lens_product_id,
                "currentVersion": dev.firmware_display,
            }
        }
        data = self._graphql(query, variables)

        if not data or not data.get("getSteppedProductSoftwareByDeviceId"):
            return None

        stepped = data["getSteppedProductSoftwareByDeviceId"]
        versions = stepped.get("steppedUpgradeVersionList") or []

        if versions:
            sw = stepped.get("productSoftware") or {}
            build = sw.get("productBuild") or {}
            return {
                "version": sw.get("version", ""),
                "download_url": build.get("archiveUrl") or build.get("url", ""),
                "stepped_versions": versions,
                "is_stepped": True,
            }

        return None

    def get_upgrade_rules(self, dev: PolyDevice) -> Optional[dict]:
        """Get firmware upgrade rules for a device."""
        query = """query ($pid: ID!) {
  productSoftwareUpgradeRule(pid: $pid) {
    pid
    rule
  }
}"""
        data = self._graphql(query, {"pid": dev.lens_product_id})
        if data and data.get("productSoftwareUpgradeRule"):
            return data["productSoftwareUpgradeRule"]
        return None

    def get_product_catalog(self, limit: int = 50, search: str = "") -> list:
        """List products from the cloud catalog."""
        query = """query ($params: CatalogProductConnectionParams!) {
  catalogProducts(params: $params) {
    edges {
      node {
        id name
        metadata { dfuSupport }
        availableProductSoftware { version latest releaseChannel }
      }
    }
    total
  }
}"""
        params = {"limit": limit}
        data = self._graphql(query, {"params": params})
        if not data or not data.get("catalogProducts"):
            return []

        products = []
        for edge in data["catalogProducts"].get("edges", []):
            node = edge["node"]
            sw = node.get("availableProductSoftware") or {}
            products.append({
                "id": node["id"],
                "name": node["name"],
                "version": sw.get("version", ""),
                "latest": sw.get("latest", False),
                "dfu_support": (node.get("metadata") or {}).get("dfuSupport", ""),
                "release_channel": sw.get("releaseChannel", ""),
            })
        return products

    def download_firmware(self, url: str, dest_name: str = "",
                          file_size: int = 0) -> Optional[Path]:
        """Download firmware file from the Poly CDN.

        Returns path to downloaded file, or None on failure.
        """
        if requests is None:
            return None

        FIRMWARE_CACHE.mkdir(parents=True, exist_ok=True)

        # Use filename from URL or hash
        if not dest_name:
            dest_name = url.rsplit("/", 1)[-1] if "/" in url else f"fw_{hashlib.sha256(url.encode()).hexdigest()[:12]}.zip"
        dest = FIRMWARE_CACHE / dest_name

        # Check cache
        if dest.exists() and dest.stat().st_size > 0:
            out.print(f"Using cached firmware: {dest.name}")
            return dest

        try:
            with requests.get(url, stream=True, timeout=300) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0)) or file_size

                size_str = f" ({total / 1024 / 1024:.1f} MB)" if total else ""
                out.print(f"Downloading firmware{size_str}...")

                if HAS_RICH:
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                        BarColumn(),
                        DownloadColumn(),
                    ) as progress:
                        task = progress.add_task("Downloading", total=total or 0)
                        with open(dest, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=65536):
                                f.write(chunk)
                                progress.update(task, advance=len(chunk))
                else:
                    downloaded = 0
                    with open(dest, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=65536):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total:
                                pct = downloaded * 100 // total
                                print(f"\r  [{pct:3d}%] {downloaded // 1024} KB / {total // 1024} KB", end="")
                    print()

            out.success(f"Downloaded: {dest.name} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
            return dest

        except requests.exceptions.RequestException as e:
            out.error(f"Download failed: {e}")
            if dest.exists():
                dest.unlink()
            return None


# ── Firmware Format Parsing ──────────────────────────────────────────────────
# Four distinct firmware formats used by Poly devices, identified by magic bytes.

FIRMWARE_FORMATS = {
    b"FWU\x05":    "FWU",
    b"FIRMWARE":   "FIRMWARE",
    b"CSR-dfu2":   "CSR-dfu2",
    b"APPUHDR5":   "APPUHDR5",
}


def detect_firmware_format(data: bytes) -> str:
    """Detect firmware format by magic bytes. Returns format name or 'unknown'."""
    for magic, name in FIRMWARE_FORMATS.items():
        if data[:len(magic)] == magic:
            return name
    return "unknown"


def parse_fwu_header(data: bytes) -> dict:
    """Parse FWU format header (Plantronics DECT firmware).

    The FWU format is used by DECT devices (Savi 8200 series, etc.).
    Header structure is partially understood: magic(4) + metadata.
    Full component table layout requires more RE work.
    """
    if len(data) < 16 or data[:4] != b"FWU\x05":
        return {"format": "FWU", "error": "Invalid FWU header"}

    result = {"format": "FWU"}
    result["header_version"] = data[3]
    result["total_size"] = len(data)
    # Show raw header bytes for manual analysis
    result["header_hex"] = " ".join(f"{b:02X}" for b in data[4:32])
    return result


def parse_firmware_container(data: bytes) -> dict:
    """Parse FIRMWARE container format (TI/BladeRunner).

    Structure: magic(8) + padding(8) + version(4) + section_count(4) + data_offset(4) +
               total_size(4) + crc32(4) + sections[...]
    Each section: name(16) + version(4) + offset(4) + size(4) + crc32(4) = 32 bytes
    """
    if len(data) < 32 or data[:8] != b"FIRMWARE":
        return {"format": "FIRMWARE", "error": "Invalid FIRMWARE header"}

    result = {"format": "FIRMWARE"}
    # Parse header fields (offsets from RE analysis)
    version_raw = struct.unpack_from("<I", data, 16)[0]
    result["version"] = f"{(version_raw >> 8) & 0xFFFF}.{version_raw & 0xFF}"
    section_count = struct.unpack_from("<I", data, 20)[0]
    data_offset = struct.unpack_from("<I", data, 24)[0]
    total_data_size = struct.unpack_from("<I", data, 28)[0]

    result["section_count"] = section_count
    result["data_offset"] = data_offset
    result["total_size"] = len(data)

    # Parse section headers (start at offset 32)
    # Stop when we hit a zero-filled entry or non-ASCII name
    sections = []
    hdr_offset = 32
    for i in range(min(section_count, 16)):
        if hdr_offset + 32 > len(data):
            break
        raw_name = data[hdr_offset:hdr_offset + 16]
        # Validate: name should be printable ASCII or null-padded
        if not raw_name[0:1] or raw_name[0] == 0:
            break  # Empty entry — end of section table
        # Check if name looks like valid ASCII text
        stripped = raw_name.rstrip(b"\x00")
        if stripped and not all(32 <= b < 127 for b in stripped):
            break  # Not ASCII — we've hit the data region
        name = stripped.decode("ascii", errors="replace")
        sec_version = struct.unpack_from("<I", data, hdr_offset + 16)[0]
        sec_offset = struct.unpack_from("<I", data, hdr_offset + 20)[0]
        sec_size = struct.unpack_from("<I", data, hdr_offset + 24)[0]
        sec_crc = struct.unpack_from("<I", data, hdr_offset + 28)[0]

        sections.append({
            "name": name,
            "version": f"0x{sec_version:08X}",
            "offset": sec_offset,
            "size": sec_size,
            "crc32": f"0x{sec_crc:08X}",
        })
        hdr_offset += 32

    result["sections"] = sections
    return result


def parse_csr_dfu(data: bytes) -> dict:
    """Parse CSR-dfu2 format (Cambridge Silicon Radio DFU).

    Structure: magic(8) + version(2) + payload_size(4) + vendor_ext_size(2) +
               codename(16) + ... + UFD suffix with CRC
    """
    if len(data) < 32 or data[:8] != b"CSR-dfu2":
        return {"format": "CSR-dfu2", "error": "Invalid CSR-dfu2 header"}

    result = {"format": "CSR-dfu2"}
    result["version"] = struct.unpack_from("<H", data, 8)[0]
    result["payload_size"] = struct.unpack_from("<I", data, 10)[0]
    result["vendor_ext_size"] = struct.unpack_from("<H", data, 14)[0]
    result["codename"] = data[16:32].rstrip(b"\x00 ").decode("ascii", errors="replace")
    result["total_size"] = len(data)
    return result


def parse_appuhdr5(data: bytes) -> dict:
    """Parse APPUHDR5 format (Qualcomm QCC5xxx).

    Structure: magic(8) + header_size(4) + platform(16) + ...
    Contains PARTDATA partition sections.
    """
    if len(data) < 32 or data[:8] != b"APPUHDR5":
        return {"format": "APPUHDR5", "error": "Invalid APPUHDR5 header"}

    result = {"format": "APPUHDR5"}
    result["header_size"] = struct.unpack_from(">I", data, 8)[0]
    # Platform string is null-terminated starting at byte 12
    platform_raw = data[12:28]
    null_idx = platform_raw.find(b"\x00")
    if null_idx >= 0:
        platform_raw = platform_raw[:null_idx]
    result["platform"] = platform_raw.decode("ascii", errors="replace")
    result["total_size"] = len(data)

    # Count PARTDATA sections
    partdata_count = 0
    offset = 0
    while True:
        idx = data.find(b"PARTDATA", offset)
        if idx < 0:
            break
        partdata_count += 1
        offset = idx + 8
    result["partition_count"] = partdata_count
    return result


def parse_firmware_file(filepath) -> dict:
    """Auto-detect and parse a firmware file."""
    filepath = Path(filepath)
    if not filepath.exists():
        return {"error": f"File not found: {filepath}"}

    data = filepath.read_bytes()
    fmt = detect_firmware_format(data)

    if fmt == "FWU":
        return parse_fwu_header(data)
    elif fmt == "FIRMWARE":
        return parse_firmware_container(data)
    elif fmt == "CSR-dfu2":
        return parse_csr_dfu(data)
    elif fmt == "APPUHDR5":
        return parse_appuhdr5(data)
    else:
        return {
            "format": "unknown",
            "total_size": len(data),
            "magic_hex": " ".join(f"{b:02X}" for b in data[:8]),
        }


def parse_firmware_package(pkg_path) -> dict:
    """Parse a firmware package directory or zip file.

    Looks for rules.json and analyzes all firmware binaries.
    """
    import zipfile

    pkg_path = Path(pkg_path)
    pkg_dir = None

    if pkg_path.is_file() and pkg_path.suffix == ".zip":
        # Extract to temp location
        extract_dir = Path(tempfile.mkdtemp(prefix="polytool_fw_"))
        with zipfile.ZipFile(pkg_path) as zf:
            zf.extractall(extract_dir)
        # The zip might have a subdirectory
        subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
        if len(subdirs) == 1 and (subdirs[0] / "rules.json").exists():
            pkg_dir = subdirs[0]
        elif (extract_dir / "rules.json").exists():
            pkg_dir = extract_dir
        else:
            pkg_dir = extract_dir
    elif pkg_path.is_dir():
        pkg_dir = pkg_path
    else:
        return {"error": f"Not a directory or zip: {pkg_path}"}

    result = {"path": str(pkg_dir), "components": [], "files": []}

    # Parse rules.json
    rules_path = pkg_dir / "rules.json"
    if rules_path.exists():
        try:
            rules = json.loads(rules_path.read_text())
            result["rules_version"] = rules.get("rulesVersion", rules.get("version", ""))
            result["version"] = rules.get("version", rules.get("setid", ""))
            result["release_date"] = rules.get("releaseDate", "")
            result["release_notes"] = rules.get("releaseNotes", "")

            for comp in rules.get("components", []):
                comp_info = {
                    "type": comp.get("type", ""),
                    "pid": comp.get("pid", ""),
                    "version": comp.get("version", ""),
                    "description": comp.get("description", ""),
                    "filename": comp.get("filename", ""),
                    "transport": comp.get("transport", ""),
                    "max_duration": comp.get("maxDuration", 0),
                    "language_id": comp.get("languageId", ""),
                    "should_replug": comp.get("shouldReplug", False),
                    "headset_id": comp.get("headsetId", ""),
                }
                # Parse the actual firmware file if it exists
                filename = comp.get("filename", "")
                if filename:
                    fw_file = pkg_dir / filename
                    if fw_file.exists() and fw_file.is_file():
                        file_info = parse_firmware_file(fw_file)
                        comp_info["file_format"] = file_info.get("format", "unknown")
                        comp_info["file_size"] = fw_file.stat().st_size
                    else:
                        comp_info["file_format"] = "missing"
                        comp_info["file_size"] = 0
                else:
                    comp_info["file_format"] = "meta"
                    comp_info["file_size"] = 0
                result["components"].append(comp_info)
        except Exception as e:
            result["rules_error"] = str(e)
    else:
        result["rules_error"] = "No rules.json found"
        # Still scan for firmware files
        for f in sorted(pkg_dir.iterdir()):
            if f.is_file() and f.suffix in (".fwu", ".bin", ".dfu"):
                file_info = parse_firmware_file(f)
                file_info["filename"] = f.name
                file_info["file_size"] = f.stat().st_size
                result["files"].append(file_info)

    return result


# ── BladeRunner DFU Protocol ─────────────────────────────────────────────────
# Reverse-engineered from HidTiDfu.exe, btNeoDfu.exe, SyncDfu.exe, DolphinDfu.exe
# Source refs: BREncoder.cpp, BRControl.cpp (from debug symbols)

class BRMessageType:
    """BladeRunner message types (from BRPacket::deserialize string table)."""
    HOST_PROTOCOL_VERSION  = 0   # H->D: Host protocol version
    GET_SETTING            = 1   # H->D: Get setting. ID = {ID}
    PERFORM_COMMAND        = 2   # H->D: Perform command. ID = {ID}
    CLOSE_SESSION          = 3   # Close session
    SETTING_SUCCESS        = 4   # D->H: Setting success. ID = {ID}
    SETTING_EXCEPTION      = 5   # D->H: Setting exception. ID = {ID}
    COMMAND_SUCCESS        = 6   # D->H: Command success. ID = {ID}
    COMMAND_EXCEPTION      = 7   # D->H: Command exception. ID = {ID}
    PROTOCOL_VERSION       = 8   # D->H: Protocol version
    METADATA               = 9   # D->H: Metadata (exactly 4 bytes)
    EVENT                  = 10  # D->H: Event
    PROTOCOL_REJECTION     = 11  # D->H: Protocol rejection


class BRFTResponse:
    """BladeRunner File Transfer response codes."""
    OPEN_FILE_FOR_READ_ACK       = 0
    OPEN_FILE_FOR_WRITE_ACK      = 1
    CLOSE_FILE_ACK               = 2
    DELETE_FILE_ACK               = 3
    MOVE_RENAME_FILE_ACK          = 4
    NEXT_BLOCK_OF_FILE_READ_ACK  = 5
    NEXT_BLOCK_OF_FILE_WRITE_ACK = 6
    CHECKSUM_DATA                = 7
    SET_WRITE_PARTITION_ACK      = 8
    GET_WRITE_PARTITION_ACK      = 9
    MAX_BLOCK_SIZE_ACK           = 10
    FILE_OPEN_ERROR1             = 11
    FILE_OPEN_ERROR2             = 12
    FILE_CLOSE_ERROR             = 13
    FILE_DELETE_ERROR             = 14
    FILE_MOVE_ERROR               = 15
    READ_ERROR                    = 16
    WRITE_ERROR                   = 17
    WRITE_ERROR_NO_ACK           = 18


def _crc32_poly(data: bytes) -> int:
    """Standard CRC32 as used by BladeRunner FT checksum verification.
    From RE: checksum is 32-bit, format '0x%08X', compared file vs device.
    """
    import binascii
    return binascii.crc32(data) & 0xFFFFFFFF


class BladeRunnerDFU:
    """BladeRunner protocol implementation for HID-based firmware update.

    Protocol flow (from RE of BRControl.cpp):
      1. Open HID, detect report sizes
      2. Send HostProtocolVersion → receive DeviceProtocolVersion (or rejection)
      3. GetDFUTransferSize (setting query)
      4. OpenFileForWrite → ACK + MaxBlockSize negotiation
      5. SetWritePartition (optional)
      6. Write firmware blocks → ACK per block
      7. CloseFile → ACK
      8. GetChecksumFile → verify CRC32
      9. FinalizeTransfer (command)
    """

    # Protocol constants
    BR_PROTOCOL_VERSION = 3  # Current BladeRunner protocol version
    DEFAULT_BLOCK_SIZE = 128  # Fallback if negotiation fails
    READ_TIMEOUT_MS = 5000

    def __init__(self, dev: PolyDevice):
        self.dev = dev
        self.h = None
        self.seq = 0  # Sequence number for TI handler
        self.report_size = 64  # Default HID report size
        self.block_size = self.DEFAULT_BLOCK_SIZE
        self.protocol_version = 0

    def _open(self) -> bool:
        """Open HID device."""
        self.h = _open_hid(self.dev.path)
        if not self.h:
            out.error("  Cannot open HID device.")
            return False
        # Determine report sizes from HID descriptor
        # Most Poly devices use 64-byte reports; some use larger
        # We'll detect from first successful read/write
        return True

    def _close(self):
        if self.h:
            try:
                self.h.close()
            except Exception:
                pass
            self.h = None

    def _build_br_packet(self, msg_type: int, cmd_id: int = 0, payload: bytes = b"") -> bytes:
        """Build a BladeRunner packet for HID transmission.

        BRPacket format (from BREncoder.cpp):
          [ReportID=0] [MsgType:1] [ID_hi:1] [ID_lo:1] [PayloadLen_hi:1] [PayloadLen_lo:1] [Payload...]
        Padded to report_size.
        """
        pkt = bytearray(self.report_size)
        pkt[0] = 0x00  # Report ID (0 for most Poly HID)
        pkt[1] = msg_type & 0xFF
        pkt[2] = (cmd_id >> 8) & 0xFF
        pkt[3] = cmd_id & 0xFF
        payload_len = len(payload)
        pkt[4] = (payload_len >> 8) & 0xFF
        pkt[5] = payload_len & 0xFF
        # Copy payload
        for i, b in enumerate(payload):
            if 6 + i < self.report_size:
                pkt[6 + i] = b
        return bytes(pkt)

    def _send(self, pkt: bytes):
        """Send HID output report."""
        self.h.write(pkt)

    def _recv(self, timeout_ms: int = 0) -> Optional[bytes]:
        """Read HID input report with timeout."""
        if timeout_ms <= 0:
            timeout_ms = self.READ_TIMEOUT_MS
        self.h.set_nonblocking(1)
        # Poll with small sleeps
        elapsed = 0
        poll_interval = 10  # ms
        while elapsed < timeout_ms:
            data = self.h.read(self.report_size)
            if data:
                return bytes(data)
            time.sleep(poll_interval / 1000.0)
            elapsed += poll_interval
        return None

    def _parse_br_response(self, data: bytes) -> tuple:
        """Parse a BladeRunner response packet.

        Returns (msg_type, cmd_id, payload_bytes) or (None, None, None) on error.
        """
        if not data or len(data) < 6:
            return None, None, None
        # Some devices prepend report ID, some don't
        offset = 0
        if len(data) > self.report_size - 1:
            offset = 0  # Report ID included
        msg_type = data[offset]
        cmd_id = (data[offset + 1] << 8) | data[offset + 2]
        payload_len = (data[offset + 3] << 8) | data[offset + 4]
        payload = data[offset + 5: offset + 5 + payload_len]
        return msg_type, cmd_id, payload

    def _handshake(self) -> bool:
        """Perform BladeRunner protocol version negotiation.

        H->D: HostProtocolVersion (type=0) with version in payload
        D->H: ProtocolVersion (type=8) or ProtocolRejection (type=11)
        """
        out.print("  BladeRunner protocol handshake...")
        # Payload: protocol version as uint8
        pkt = self._build_br_packet(
            BRMessageType.HOST_PROTOCOL_VERSION,
            cmd_id=0,
            payload=bytes([self.BR_PROTOCOL_VERSION])
        )
        self._send(pkt)

        resp = self._recv(3000)
        if not resp:
            out.error("  No response to protocol handshake.")
            return False

        msg_type, cmd_id, payload = self._parse_br_response(resp)
        if msg_type == BRMessageType.PROTOCOL_VERSION:
            if payload and len(payload) >= 1:
                self.protocol_version = payload[0]
            out.print(f"  Device protocol version: {self.protocol_version}")
            return True
        elif msg_type == BRMessageType.PROTOCOL_REJECTION:
            out.error(f"  Device rejected protocol (type={msg_type}).")
            return False
        else:
            out.warn(f"  Unexpected handshake response type={msg_type}. Continuing...")
            return True

    def _get_dfu_transfer_size(self) -> int:
        """Query DFU transfer size from device (GetSetting)."""
        # Setting ID for DFU transfer size (commonly 0x0001 or device-specific)
        # From RE: "DFU Transfer Size: %d [0x%04X]"
        pkt = self._build_br_packet(BRMessageType.GET_SETTING, cmd_id=0x0001)
        self._send(pkt)

        resp = self._recv(3000)
        if resp:
            msg_type, cmd_id, payload = self._parse_br_response(resp)
            if msg_type == BRMessageType.SETTING_SUCCESS and payload and len(payload) >= 2:
                size = (payload[0] << 8) | payload[1]
                if size > 0:
                    out.print(f"  DFU transfer size: {size} [0x{size:04X}]")
                    return size
        return 0

    def _open_file_for_write(self, file_size: int) -> bool:
        """BladeRunner FT: Open file for write.

        Sends OpenFileForWrite command, expects ACK + MaxBlockSize negotiation.
        """
        # Payload: file size as uint32 big-endian
        payload = struct.pack(">I", file_size)
        pkt = self._build_br_packet(BRMessageType.PERFORM_COMMAND, cmd_id=0x0100, payload=payload)
        self._send(pkt)

        resp = self._recv(5000)
        if not resp:
            out.error("  No response to OpenFileForWrite.")
            return False

        msg_type, cmd_id, payload = self._parse_br_response(resp)
        if msg_type == BRMessageType.COMMAND_SUCCESS:
            out.print("  File opened for write.")
            # Try to get max block size
            self._negotiate_block_size()
            return True
        elif msg_type == BRMessageType.COMMAND_EXCEPTION:
            out.error(f"  OpenFileForWrite rejected (ID=0x{cmd_id:04X}).")
            return False
        else:
            out.warn(f"  Unexpected OpenFileForWrite response type={msg_type}")
            return True  # Try to continue

    def _negotiate_block_size(self):
        """Query and set the max block size for file transfer."""
        # Send max block size request
        pkt = self._build_br_packet(BRMessageType.GET_SETTING, cmd_id=0x0002)
        self._send(pkt)

        resp = self._recv(3000)
        if resp:
            msg_type, cmd_id, payload = self._parse_br_response(resp)
            if msg_type == BRMessageType.SETTING_SUCCESS and payload and len(payload) >= 2:
                negotiated = (payload[0] << 8) | payload[1]
                if negotiated > 0:
                    # Block size must fit in report minus header (6 bytes)
                    max_data = self.report_size - 6
                    self.block_size = min(negotiated, max_data)
                    out.print(f"  Negotiated block size: {self.block_size} bytes")
                    return
        # Use default based on report size
        self.block_size = min(self.DEFAULT_BLOCK_SIZE, self.report_size - 6)
        out.print(f"  Using default block size: {self.block_size} bytes")

    def _write_blocks(self, fw_data: bytes) -> bool:
        """Send firmware data in blocks with ACK tracking.

        From RE: Each block gets NEXT_BLOCK_OF_FILE_WRITE_ACK.
        Error: "Invalid File position in ACK command. Offset:%d NAK:%d"
        """
        total_blocks = (len(fw_data) + self.block_size - 1) // self.block_size
        out.print(f"  Sending {total_blocks} blocks ({len(fw_data)} bytes)...")

        nak_count = 0
        max_naks = 5

        def send_block(block_num: int, data_chunk: bytes) -> bool:
            """Send a single firmware block and wait for ACK."""
            nonlocal nak_count
            # Build block write packet
            # Payload: [offset:4 bytes BE] [data...]
            block_offset = block_num * self.block_size
            offset_bytes = struct.pack(">I", block_offset)
            payload = offset_bytes + data_chunk
            pkt = self._build_br_packet(
                BRMessageType.PERFORM_COMMAND,
                cmd_id=0x0101,  # WriteNextBlock command
                payload=payload
            )
            self._send(pkt)

            # Read ACK (with retries for slow devices)
            resp = self._recv(10000)
            if not resp:
                out.warn(f"  Timeout at block {block_num}/{total_blocks}")
                nak_count += 1
                return nak_count < max_naks

            msg_type, cmd_id, resp_payload = self._parse_br_response(resp)
            if msg_type == BRMessageType.COMMAND_SUCCESS:
                return True
            elif msg_type == BRMessageType.COMMAND_EXCEPTION:
                nak_count += 1
                out.warn(f"  NAK at block {block_num} (NAK #{nak_count})")
                return nak_count < max_naks
            else:
                # Unexpected response — continue cautiously
                return True

        if HAS_RICH:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            ) as progress:
                task = progress.add_task("Flashing firmware", total=total_blocks)
                for i in range(total_blocks):
                    offset = i * self.block_size
                    chunk = fw_data[offset:offset + self.block_size]
                    if not send_block(i, chunk):
                        out.error(f"  Too many errors ({nak_count} NAKs). Aborting.")
                        return False
                    progress.update(task, advance=1)
        else:
            for i in range(total_blocks):
                offset = i * self.block_size
                chunk = fw_data[offset:offset + self.block_size]
                if not send_block(i, chunk):
                    out.error(f"  Too many errors ({nak_count} NAKs). Aborting.")
                    return False
                if i % 50 == 0:
                    pct = i * 100 // total_blocks
                    print(f"\r  [{pct:3d}%] Block {i}/{total_blocks}", end="", flush=True)
            print()

        out.print(f"  All {total_blocks} blocks sent.")
        return True

    def _close_file(self) -> bool:
        """BladeRunner FT: Close file."""
        pkt = self._build_br_packet(BRMessageType.PERFORM_COMMAND, cmd_id=0x0102)
        self._send(pkt)

        resp = self._recv(5000)
        if resp:
            msg_type, _, _ = self._parse_br_response(resp)
            if msg_type == BRMessageType.COMMAND_SUCCESS:
                out.print("  File closed.")
                return True
        out.warn("  No ACK for file close.")
        return True  # Non-fatal, continue

    def _verify_checksum(self, fw_data: bytes) -> bool:
        """Verify CRC32 checksum between local file and device.

        From RE: "Check Sum Incorrect! (from file:0x%08X  from device:0x%08X)"
        """
        local_crc = _crc32_poly(fw_data)
        out.print(f"  Local CRC32: 0x{local_crc:08X}")

        # Request checksum from device
        pkt = self._build_br_packet(BRMessageType.PERFORM_COMMAND, cmd_id=0x0103)
        self._send(pkt)

        resp = self._recv(10000)  # Checksum compute can be slow
        if resp:
            msg_type, cmd_id, payload = self._parse_br_response(resp)
            if msg_type == BRMessageType.COMMAND_SUCCESS and payload and len(payload) >= 4:
                device_crc = struct.unpack(">I", payload[:4])[0]
                out.print(f"  Device CRC32: 0x{device_crc:08X}")
                if local_crc == device_crc:
                    out.success("  Checksum verified!")
                    return True
                else:
                    out.error(f"  Checksum mismatch! file:0x{local_crc:08X} device:0x{device_crc:08X}")
                    return False
        out.warn("  Could not verify checksum (no response). Proceeding...")
        return True  # Non-fatal — some devices don't support this

    def _finalize(self) -> bool:
        """Send finalize/restart command (DFUApp::FinalizeTransfer)."""
        out.print("  Finalizing transfer (device will restart)...")
        pkt = self._build_br_packet(BRMessageType.PERFORM_COMMAND, cmd_id=0x0104)
        self._send(pkt)
        time.sleep(2)
        # Device may disconnect immediately — don't wait for response
        return True

    def flash_firmware(self, fw_path: Path, version: str) -> bool:
        """Execute the full BladeRunner DFU sequence.

        Steps (from BRControl.cpp / DfuExecution.dll):
          1. Open HID transport
          2. Protocol handshake (version negotiation)
          3. Query DFU transfer size
          4. Enter DFU mode
          5. Open file for write + negotiate block size
          6. Write firmware blocks with ACK
          7. Close file
          8. Verify CRC32 checksum
          9. Finalize transfer (restart device)
        """
        if not self._open():
            return False

        try:
            fw_data = fw_path.read_bytes()
            out.print(f"  Firmware: {fw_path.name} ({len(fw_data)} bytes)")

            # Step 1: Protocol handshake
            if not self._handshake():
                out.error("  Protocol handshake failed. Device may not support BladeRunner DFU.")
                return False

            # Step 2: Query transfer size (informational)
            self._get_dfu_transfer_size()

            # Step 3: Enter DFU mode
            out.print("  Entering DFU mode...")
            pkt = self._build_br_packet(BRMessageType.PERFORM_COMMAND, cmd_id=0x0200)
            self._send(pkt)
            time.sleep(2)
            # Read and discard any DFU mode acknowledgment
            self._recv(3000)

            # Step 4: Open file for write
            if not self._open_file_for_write(len(fw_data)):
                out.error("  Failed to open file on device for writing.")
                return False

            # Step 5: Write firmware blocks
            if not self._write_blocks(fw_data):
                out.error("  Firmware transfer failed.")
                return False

            # Step 6: Close file
            self._close_file()

            # Step 7: Verify checksum
            if not self._verify_checksum(fw_data):
                out.error("  Checksum verification failed. Firmware may be corrupt on device.")
                out.warn("  The device may need recovery via Poly Lens Desktop.")
                return False

            # Step 8: Finalize (restart device)
            self._finalize()

            out.success(f"  Firmware update complete! Device should reboot with v{version}.")
            out.print("  If the device doesn't respond within 60s, power cycle it manually.")
            return True

        except Exception as e:
            out.error(f"  BladeRunner DFU error: {e}")
            return False
        finally:
            self._close()


# ── Firmware Updater ────────────────────────────────────────────────────────

class FirmwareUpdater:
    """Handles firmware update orchestration."""

    # Minimum battery level required for DFU (from DfuValidation.dll analysis)
    MIN_BATTERY_PERCENT = 20

    def __init__(self, cloud: PolyCloudAPI):
        self.cloud = cloud

    def validate_device_for_update(self, dev: PolyDevice) -> tuple:
        """Run pre-update validation checks.

        Returns (ok: bool, reason: str)
        """
        # Battery check
        if 0 <= dev.battery_level < self.MIN_BATTERY_PERCENT:
            return False, (f"Battery too low ({dev.battery_level}%). "
                          f"Minimum {self.MIN_BATTERY_PERCENT}% required.")

        if dev.battery_left >= 0 and dev.battery_left < self.MIN_BATTERY_PERCENT:
            return False, (f"Left earbud battery too low ({dev.battery_left}%). "
                          f"Minimum {self.MIN_BATTERY_PERCENT}% required.")

        if dev.battery_right >= 0 and dev.battery_right < self.MIN_BATTERY_PERCENT:
            return False, (f"Right earbud battery too low ({dev.battery_right}%). "
                          f"Minimum {self.MIN_BATTERY_PERCENT}% required.")

        # In-call check
        if dev.is_in_call:
            return False, "Device is currently in a call. Please try again after the call."

        return True, "OK"

    def check_and_update(self, dev: PolyDevice, force: bool = False) -> bool:
        """Check for update and apply if available.

        Returns True if update was applied successfully.
        """
        out.print(f"\nChecking updates for: [bold]{dev.friendly_name}[/]" if HAS_RICH
                  else f"\nChecking updates for: {dev.friendly_name}")

        # Check for stepped upgrade first
        stepped = self.cloud.check_stepped_firmware(dev)
        if stepped and stepped.get("is_stepped"):
            versions = stepped.get("stepped_versions", [])
            out.warn(f"Stepped upgrade required: {' -> '.join(versions)}")
            fw_info = stepped
            fw_info["latest"] = stepped["version"]
            fw_info["current"] = dev.firmware_display
        else:
            fw_info = self.cloud.check_firmware(dev)

        if not fw_info:
            out.print("  No firmware info available in cloud catalog for this product.")
            return False

        current = fw_info.get("current", dev.firmware_display)
        latest = fw_info.get("latest", "unknown")

        if _normalize_version(current) == _normalize_version(latest) and not force:
            out.success(f"  Already up to date (v{current})")
            return True

        if fw_info.get("blocked_download"):
            out.error("  Firmware download is blocked for this product.")
            return False

        out.print(f"  Current: v{current}")
        out.print(f"  Latest:  v{latest}")

        if fw_info.get("release_notes"):
            out.print(f"  Notes:   {fw_info['release_notes'][:200]}")

        # Validate device state
        ok, reason = self.validate_device_for_update(dev)
        if not ok:
            out.error(f"  Cannot update: {reason}")
            return False

        # Download firmware
        download_url = fw_info.get("download_url", "")
        if not download_url:
            out.error("  No download URL available.")
            return False

        fw_path = self.cloud.download_firmware(download_url)

        if not fw_path:
            out.error("  Firmware download failed.")
            return False

        # Apply update
        return self._apply_update(dev, fw_path, latest)

    def _apply_update(self, dev: PolyDevice, fw_path: Path, version: str) -> bool:
        """Apply firmware to device.

        On Windows: attempts to use native DFU executors from Poly Lens install.
        On macOS/Linux: attempts HID-based DFU for supported protocols.
        """
        out.print(f"\n  Applying firmware v{version}...")

        system = platform.system()

        if system == "Windows":
            return self._apply_via_native_executor(dev, fw_path)
        else:
            return self._apply_via_hid_dfu(dev, fw_path, version)

    def _apply_via_native_executor(self, dev: PolyDevice, fw_path: Path) -> bool:
        """Use Poly Lens native DFU executor (Windows only)."""
        import subprocess

        # Find Poly Lens installation
        lens_paths = [
            Path(os.environ.get("ProgramFiles", "")) / "Poly" / "Poly Lens" / "LensControlService",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Poly" / "Poly Lens" / "LensControlService",
        ]

        executor_name = dev.dfu_executor
        if not executor_name:
            out.error(f"  No known DFU executor for this device (PID {dev.pid_hex}).")
            out.print("  Try updating via Poly Lens Desktop application instead.")
            return False

        exe_name = f"{executor_name}.exe"
        exe_path = None

        for base in lens_paths:
            candidate = base / exe_name
            if candidate.exists():
                exe_path = candidate
                break

        if not exe_path:
            out.error(f"  DFU executor not found: {exe_name}")
            out.print("  Ensure Poly Lens Desktop is installed, or update via the Poly Lens app.")
            return False

        out.print(f"  Using executor: {exe_path}")
        out.warn("  DO NOT disconnect the device during update!")

        try:
            # Build command line (reverse-engineered from DfuExecution.dll)
            cmd = [
                str(exe_path),
                "--pid", str(dev.pid),
                "--vid", str(dev.vid),
                "--firmware", str(fw_path),
            ]

            if dev.serial:
                cmd.extend(["--serial", dev.serial])

            if dev.bus_type == "Bluetooth":
                cmd.extend(["--connection", "bluetooth"])
            else:
                cmd.extend(["--connection", "usb"])

            out.print(f"  Running: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minute timeout
            )

            if result.returncode == 0:
                out.success(f"  Firmware update successful! Device updated to v{fw_path.stem}")
                return True
            else:
                out.error(f"  DFU executor failed (exit code {result.returncode})")
                if result.stderr:
                    out.print(f"  stderr: {result.stderr[:500]}")
                return False

        except subprocess.TimeoutExpired:
            out.error("  DFU timed out after 10 minutes.")
            return False
        except Exception as e:
            out.error(f"  DFU execution error: {e}")
            return False

    def _apply_via_hid_dfu(self, dev: PolyDevice, fw_path: Path, version: str) -> bool:
        """Attempt HID-based DFU using the BladeRunner protocol (cross-platform).

        Reverse-engineered from HidTiDfu.exe / btNeoDfu.exe / SyncDfu.exe
        (BREncoder.cpp, BRControl.cpp). Supports devices that use the BladeRunner
        file transfer protocol over USB HID.
        """
        # CX2070x EEPROM flash (Blackwire 3220)
        if dev.dfu_executor == "CxEepromDfu":
            return self._apply_via_cx_eeprom(dev, fw_path, version)

        # FWU API flash (Savi 8220 DECT)
        if dev.dfu_executor == "LegacyDfu":
            return self._apply_via_fwu_api(dev, fw_path, version)

        transport_info = DFU_TRANSPORT_INFO.get(dev.dfu_executor)
        experimental_hid = ("HidTiDfu", "SyncDfu", "StudioDfu", "BrightDfu", "DolphinDfu")

        if dev.dfu_executor not in experimental_hid:
            executor = dev.dfu_executor or "unknown"
            out.warn(f"  Cannot flash this device on {platform.system()}.")
            if executor == "btNeoDfu":
                out.print("  This device uses BladeRunner FTP over Bluetooth, which requires")
                out.print("  the btNeoDfu executor on Windows.")
            elif executor == "usbdfu":
                out.print("  This device uses USB DFU class protocol (standard but untested).")
            else:
                out.print(f"  Executor '{executor}' is not supported for cross-platform flashing.")
            if transport_info:
                out.print(f"  Protocol: {transport_info[0]}, Format: {transport_info[1]}")
            out.print(f"\n  Firmware file saved at: {fw_path}")
            out.print("  To flash: connect to a Windows PC with Poly Lens Desktop installed.")
            return False

        # Extract the correct firmware binary from the zip
        # rules.json tells us which file is the main application firmware
        actual_fw_path = self._extract_bladerunner_fw(dev, fw_path)
        if not actual_fw_path:
            return False

        out.warn("  HID DFU is EXPERIMENTAL — reverse-engineered BladeRunner protocol.")
        out.warn("  DO NOT disconnect the device. Ensure it is charged above 20%.")
        out.print("  Press Ctrl+C within 5 seconds to cancel...")

        try:
            time.sleep(5)
        except KeyboardInterrupt:
            out.print("  Cancelled.")
            return False

        try:
            dfu = BladeRunnerDFU(dev)
            return dfu.flash_firmware(actual_fw_path, version)
        except KeyboardInterrupt:
            out.warn("\n  Update interrupted! Device may need manual recovery.")
            out.print("  Try power cycling the device. If unresponsive, use Poly Lens Recovery.")
            return False
        except Exception as e:
            out.error(f"  DFU error: {e}")
            return False

    def _extract_bladerunner_fw(self, dev: PolyDevice, fw_path: Path) -> Optional[Path]:
        """Extract the main firmware binary from a zip for BladeRunner DFU.

        Reads rules.json to find the component with type 'usb' (application firmware),
        extracts it to a temp file, and returns the path.
        For non-zip files, returns the path directly.
        """
        if fw_path.suffix != ".zip":
            return fw_path

        import zipfile, tempfile

        try:
            with zipfile.ZipFile(fw_path) as z:
                names = z.namelist()

                # Try rules.json first to find the right component
                target_file = None
                for name in names:
                    if name.endswith('rules.json'):
                        try:
                            rules = json.loads(z.read(name))
                            for comp in rules.get("components", []):
                                comp_type = comp.get("type", "")
                                fn = comp.get("fileName", "")
                                # Main firmware is type "usb" or "bt"
                                if comp_type in ("usb", "bt") and fn:
                                    # For HidTiDfu: prefer fw.bin or dfu_image.bin
                                    if fn in names:
                                        target_file = fn
                                        break
                        except Exception:
                            pass
                        break

                # Fallback: find by filename pattern
                if not target_file:
                    for name in names:
                        low = name.lower()
                        # Main firmware files by naming convention
                        if low in ('fw.bin', 'dfu_image.bin'):
                            target_file = name
                            break
                        if '_dfu_image' in low and low.endswith('.bin'):
                            target_file = name
                            break
                        if low.endswith('.bin') and 'firmware' in low:
                            target_file = name
                            break

                # Last fallback: any FIRMWARE-magic .bin file
                if not target_file:
                    for name in names:
                        if name.endswith('.bin'):
                            data = z.read(name)[:8]
                            if data == b'FIRMWARE':
                                target_file = name
                                break
                        elif name.endswith('.dfu'):
                            data = z.read(name)[:8]
                            if data == b'CSR-dfu2':
                                target_file = name
                                break

                # For APPUHDR5 (Sync 20): find the bt component
                if not target_file:
                    for name in names:
                        if name.endswith('.bin'):
                            data = z.read(name)[:8]
                            if data == b'APPUHDR5':
                                target_file = name
                                break

                if not target_file:
                    # Check for nested zip (Studio devices use .zip.dfu)
                    for name in names:
                        if name.endswith('.zip.dfu') or name.endswith('.zip'):
                            out.error(f"  This firmware uses OTA packaging ({name})")
                            out.print(f"  Studio OTA updates are not yet supported via HID DFU.")
                            return None
                    out.error(f"  No flashable firmware found in {fw_path.name}")
                    out.print(f"  Files: {', '.join(names[:10])}")
                    return None

                # Extract to temp file
                fw_data = z.read(target_file)
                tmp = Path(tempfile.mktemp(suffix=Path(target_file).suffix,
                                            prefix="polytool_fw_"))
                tmp.write_bytes(fw_data)
                out.print(f"  Extracted: {target_file} ({len(fw_data)} bytes)")
                return tmp

        except Exception as e:
            out.error(f"  Failed to extract firmware: {e}")
            return None

    def _apply_via_cx_eeprom(self, dev: PolyDevice, fw_path: Path, version: str) -> bool:
        """Flash Blackwire 3220 (CX2070x) via EEPROM over HID.

        The firmware zip contains a .ptc file (S-record format) that maps
        directly to EEPROM addresses. Uses bw_flash.py's BlackwireFlasher.
        """
        import zipfile
        from bw_flash import BlackwireFlasher, parse_srecords

        # Extract .ptc file from the firmware zip
        ptc_data = None
        ptc_name = ""
        if fw_path.suffix == ".zip":
            try:
                with zipfile.ZipFile(fw_path) as z:
                    for name in z.namelist():
                        if name.endswith('.ptc'):
                            ptc_data = z.read(name)
                            ptc_name = name
                            break
            except Exception as e:
                out.error(f"  Failed to extract firmware: {e}")
                return False
        elif fw_path.suffix == ".ptc":
            ptc_data = fw_path.read_bytes()
            ptc_name = fw_path.name
        else:
            out.error(f"  Unsupported firmware format: {fw_path.suffix}")
            return False

        if not ptc_data:
            out.error("  No .ptc file found in firmware package.")
            return False

        records = parse_srecords(ptc_data)
        if not records:
            out.error("  No valid S-records found in firmware file.")
            return False

        total_bytes = sum(len(d) for _, d in records)
        out.print(f"  Firmware: {ptc_name} ({len(records)} records, {total_bytes} bytes)")
        out.warn("  DO NOT disconnect the device during the update!")

        flasher = BlackwireFlasher()
        info = flasher.find_device()
        if not info:
            out.error("  Blackwire 3220 not found on HID bus.")
            return False

        flasher.open(info)
        try:
            ok = flasher.flash(records)
            if ok:
                out.success(f"  Firmware update to v{version} complete!")
                return True
            else:
                out.error("  Flash completed with verification errors.")
                return False
        except KeyboardInterrupt:
            out.warn("\n  Update interrupted! EEPROM may be partially written.")
            out.print("  The device should still boot — re-run the update to complete.")
            return False
        except Exception as e:
            out.error(f"  Flash error: {e}")
            return False
        finally:
            flasher.close()

    def _apply_via_fwu_api(self, dev: PolyDevice, fw_path: Path, version: str) -> bool:
        """Flash Savi 8220 (and other DECT devices) via FWU API over HID.

        Uses fwu_flash.py's FwuFlasher — fully reverse-engineered CVM API
        protocol with LE 16-bit primitive IDs over 0xFFA2 usage page.
        Requires pyusb for USB reset before flashing.
        """
        import zipfile
        from fwu_flash import FwuFlasher, FwuFile, usb_reset

        # Extract .fwu file from the firmware zip
        fwu_data = None
        fwu_name = ""
        if fw_path.suffix == ".zip":
            try:
                with zipfile.ZipFile(fw_path) as z:
                    for name in z.namelist():
                        if name.endswith('.fwu'):
                            fwu_data = z.read(name)
                            fwu_name = name
                            break
            except Exception as e:
                out.error(f"  Failed to extract firmware: {e}")
                return False
        elif fw_path.suffix == ".fwu":
            fwu_data = fw_path.read_bytes()
            fwu_name = fw_path.name
        else:
            out.error(f"  Unsupported firmware format: {fw_path.suffix}")
            return False

        if not fwu_data:
            out.error("  No .fwu file found in firmware package.")
            return False

        try:
            fwu = FwuFile(fwu_data)
        except ValueError as e:
            out.error(f"  Invalid FWU file: {e}")
            return False

        out.print(f"  Firmware: {fwu_name}")
        out.print(f"  Device ID: 0x{fwu.device_id:08X}")
        out.print(f"  Flash range: 0x{fwu.range_start:X}..0x{fwu.range_start+fwu.range_size:X} "
                   f"({fwu.range_size} bytes)")
        out.warn("  DO NOT disconnect the device during the update!")

        # USB reset is required on macOS to get exclusive HID access
        out.print("  USB reset...")
        usb_reset()

        flasher = FwuFlasher()
        info = flasher.find_device()
        if not info:
            out.error("  Savi 8220 not found on HID bus after USB reset.")
            return False

        flasher.open(info)
        try:
            flasher.flash(fwu)
            out.success(f"  Firmware update to v{version} complete!")
            return True
        except KeyboardInterrupt:
            out.warn("\n  Update interrupted! Device may need recovery.")
            return False
        except Exception as e:
            out.error(f"  FWU flash error: {e}")
            return False
        finally:
            flasher.close()


# ── CLI Commands ─────────────────────────────────────────────────────────────

def cmd_scan(args):
    """Discover all connected Poly devices."""
    out.header("PolyTool - Device Scanner")
    devices = discover_devices()
    out.device_table(devices)
    return devices


def cmd_info(args):
    """Show detailed info for a specific device or all devices."""
    devices = discover_devices()
    if not devices:
        out.warn("No Poly devices found.")
        return

    targets = _select_devices(devices, args.device)

    for dev in targets:
        # Attempt to read extended info
        try_read_device_info(dev)
        try_read_battery(dev)

        transport_info = DFU_TRANSPORT_INFO.get(dev.dfu_executor, None)
        transport_str = transport_info[0] if transport_info else "n/a"
        fw_format_str = transport_info[1] if transport_info else "n/a"
        platform_str = transport_info[2] if transport_info else "n/a"

        if HAS_RICH:
            info_lines = [
                f"[bold]Product:[/]        {dev.friendly_name}",
                f"[bold]Manufacturer:[/]   {dev.manufacturer}",
                f"[bold]Serial:[/]         {dev.serial or 'n/a'}",
                f"[bold]Firmware:[/]       {dev.firmware_display}",
                f"[bold]Category:[/]       {dev.category}",
                f"[bold]VID:PID:[/]        {dev.vid_hex}:{dev.pid_hex}",
                f"[bold]USB/BT:[/]         {dev.bus_type}",
                f"[bold]Usage Page:[/]     0x{dev.usage_page:04X}",
                f"[bold]Battery:[/]        {dev.battery_display}",
                f"[bold]Codename:[/]       {dev.codename or 'n/a'}",
                f"[bold]LensProductID:[/]  {dev.lens_product_id}",
                f"[bold]DFU Executor:[/]   {dev.dfu_executor or 'n/a'}",
                f"[bold]DFU Transport:[/]  {transport_str}",
                f"[bold]FW Format:[/]      {fw_format_str}",
                f"[bold]Update Support:[/] {platform_str}",
            ]
            if dev.is_muted:
                info_lines.append("[bold]Muted:[/]          Yes")
            if dev.is_on_head:
                info_lines.append("[bold]On Head:[/]        Yes")

            out.console.print(Panel(
                "\n".join(info_lines),
                title=dev.friendly_name,
                border_style="cyan",
                expand=False,
            ))
        else:
            print(f"\n{'='*50}")
            print(f"  {dev.friendly_name}")
            print(f"{'='*50}")
            print(f"  Manufacturer:  {dev.manufacturer}")
            print(f"  Serial:        {dev.serial or 'n/a'}")
            print(f"  Firmware:      {dev.firmware_display}")
            print(f"  Category:      {dev.category}")
            print(f"  VID:PID:       {dev.vid_hex}:{dev.pid_hex}")
            print(f"  USB/BT:        {dev.bus_type}")
            print(f"  Battery:       {dev.battery_display}")
            print(f"  Codename:      {dev.codename or 'n/a'}")
            print(f"  DFU Executor:  {dev.dfu_executor or 'n/a'}")
            print(f"  DFU Transport: {transport_str}")
            print(f"  FW Format:     {fw_format_str}")
            print(f"  Update Support:{platform_str}")


def cmd_battery(args):
    """Show battery levels for all devices."""
    out.header("PolyTool - Battery Status")
    devices = discover_devices()
    if not devices:
        out.warn("No Poly devices found.")
        return

    for dev in devices:
        try_read_battery(dev)

    if HAS_RICH:
        table = Table(title="Battery Levels", box=box.ROUNDED)
        table.add_column("Device", style="bold white", no_wrap=True, max_width=28)
        table.add_column("Battery", no_wrap=True)
        table.add_column("Status", no_wrap=True)

        for dev in devices:
            bat = dev.battery_display
            if dev.battery_level >= 0:
                if dev.battery_level > 50:
                    level_bar = "[green]" + "#" * (dev.battery_level // 10) + "[/]"
                    level_bar += "[dim]" + "-" * (10 - dev.battery_level // 10) + "[/]"
                elif dev.battery_level > 20:
                    level_bar = "[yellow]" + "#" * (dev.battery_level // 10) + "[/]"
                    level_bar += "[dim]" + "-" * (10 - dev.battery_level // 10) + "[/]"
                else:
                    level_bar = "[red]" + "#" * (dev.battery_level // 10) + "[/]"
                    level_bar += "[dim]" + "-" * (10 - dev.battery_level // 10) + "[/]"
                bat_str = f"{level_bar} {dev.battery_level}%"
            else:
                bat_str = "[dim]n/a[/]"

            status = ""
            if dev.battery_charging:
                status = "[yellow]Charging[/]"
            elif dev.battery_level >= 0:
                status = "[green]OK[/]" if dev.battery_level > 20 else "[red]LOW[/]"

            table.add_row(dev.friendly_name, bat_str, status)

        out.console.print(table)
    else:
        for dev in devices:
            level = dev.battery_display
            print(f"  {dev.friendly_name:30s} {level}")


def cmd_updates(args):
    """Check for firmware updates."""
    out.header("PolyTool - Firmware Update Check")

    devices = discover_devices()
    cloud = PolyCloudAPI()
    device_selector = getattr(args, "device", None)

    if not devices:
        out.warn("No Poly devices connected. Searching cloud catalog...")
        # Show catalog search results instead
        products = cloud.get_product_catalog(limit=200)
        search = (device_selector or "").lower()
        if search and search != "all":
            products = [p for p in products if search in p["name"].lower() or search in p["id"].lower()]
        # Only show products with firmware
        products = [p for p in products if p["version"]]

        if products:
            if HAS_RICH:
                table = Table(title="Available Firmware (no device connected)", box=box.ROUNDED)
                table.add_column("PID", style="dim", no_wrap=True)
                table.add_column("Product", style="bold", no_wrap=True, max_width=30)
                table.add_column("Latest FW", style="green", no_wrap=True)
                table.add_column("DFU", style="cyan", no_wrap=True)
                for p in products[:30]:
                    table.add_row(p["id"], p["name"], p["version"], p["dfu_support"] or "n/a")
                out.console.print(table)
            else:
                for p in products[:30]:
                    print(f"  {p['id']:>6s}: {p['name']:40s} v{p['version']}")
            out.print("\nConnect a device to check if it needs updating.")
        else:
            out.print("No matching products found." if search else "No products in catalog.")
        return

    targets = _select_devices(devices, device_selector)
    update_available = []

    for dev in targets:
        try_read_device_info(dev)
        out.print(f"\nChecking: {dev.friendly_name} (v{dev.firmware_display})...")

        fw_info = cloud.check_firmware(dev)
        if fw_info:
            current = fw_info.get("current", dev.firmware_display)
            latest = fw_info.get("latest", "unknown")
            product_name = fw_info.get("product_name", "")

            if product_name and product_name != dev.friendly_name:
                out.print(f"  Cloud product: {product_name}")

            if fw_info.get("blocked_download"):
                out.warn(f"  Firmware download is blocked for this product.")
                continue

            # Compare normalized versions — cloud returns "0225_0_0", device shows "2.25"
            if _normalize_version(current) != _normalize_version(latest):
                out.print(f"  [bold yellow]Update available![/]  v{current} -> v{latest}" if HAS_RICH
                          else f"  Update available!  v{current} -> v{latest}")
                if fw_info.get("release_notes"):
                    notes = fw_info["release_notes"][:300].replace("\n", "\n    ")
                    out.print(f"    {notes}")
                if fw_info.get("download_url"):
                    out.print(f"  Download: {fw_info['download_url']}")
                # Show transport/platform compatibility
                transport_info = DFU_TRANSPORT_INFO.get(dev.dfu_executor, None)
                if transport_info:
                    out.print(f"  Transport: {transport_info[0]} ({transport_info[2]})")
                elif dev.dfu_executor:
                    out.print(f"  Transport: {dev.dfu_executor}")
                else:
                    out.print("  Transport: unknown (update may require Poly Lens Desktop)")
                update_available.append((dev, fw_info))
            else:
                out.success(f"  Up to date (v{current})")
        else:
            out.print("  No firmware info available in cloud catalog for this product.")

    if update_available:
        out.print(f"\n{len(update_available)} update(s) available.")
        out.print("Run 'polytool.py update' to apply updates.")
    else:
        out.success("\nAll devices are up to date!")


def cmd_update(args):
    """Download and apply firmware updates."""
    out.header("PolyTool - Firmware Updater")

    devices = discover_devices()
    if not devices:
        out.warn("No Poly devices found.")
        return

    cloud = PolyCloudAPI()
    updater = FirmwareUpdater(cloud)
    targets = _select_devices(devices, args.device)

    for dev in targets:
        try_read_device_info(dev)
        try_read_battery(dev)
        updater.check_and_update(dev, force=args.force)


def cmd_monitor(args):
    """Live device monitoring dashboard."""
    out.header("PolyTool - Live Monitor (Ctrl+C to exit)")

    interval = args.interval if hasattr(args, "interval") else 5

    try:
        while True:
            devices = discover_devices()

            if HAS_RICH:
                # Read battery for all devices
                for dev in devices:
                    try_read_battery(dev)

                os.system("clear" if os.name != "nt" else "cls")
                out.header(f"PolyTool Monitor - {time.strftime('%H:%M:%S')}")
                out.device_table(devices, "Live Device Status")
                out.print(f"\n[dim]Refreshing every {interval}s. Press Ctrl+C to exit.[/]")
            else:
                for dev in devices:
                    try_read_battery(dev)
                os.system("clear" if os.name != "nt" else "cls")
                print(f"\nPolyTool Monitor - {time.strftime('%H:%M:%S')}")
                for dev in devices:
                    print(f"  {dev.friendly_name:30s} FW:{dev.firmware_display:10s} Bat:{dev.battery_display}")
                print(f"\nRefreshing every {interval}s. Press Ctrl+C to exit.")

            time.sleep(interval)

    except KeyboardInterrupt:
        out.print("\nMonitor stopped.")


def cmd_catalog(args):
    """Search the Poly cloud firmware catalog."""
    out.header("PolyTool - Firmware Catalog")

    cloud = PolyCloudAPI()
    products = cloud.get_product_catalog(limit=200)

    if not products:
        out.error("Could not fetch product catalog.")
        return

    # Filter by search term
    search = (args.search or "").lower()
    if search:
        products = [p for p in products if search in p["name"].lower() or search in p["id"].lower()]

    # Filter to only show products with firmware
    if not args.all:
        products = [p for p in products if p["version"]]

    if not products:
        out.warn(f"No products found{' matching: ' + search if search else ''}.")
        out.print("Use --all to include products without firmware.")
        return

    if HAS_RICH:
        table = Table(title=f"Poly Firmware Catalog ({len(products)} products)", box=box.ROUNDED)
        table.add_column("PID", style="dim", no_wrap=True)
        table.add_column("Product", style="bold white", no_wrap=True, max_width=28)
        table.add_column("Latest FW", style="green", no_wrap=True)
        table.add_column("DFU", style="cyan", no_wrap=True)

        for p in products:
            table.add_row(
                p["id"],
                p["name"],
                p["version"] or "[dim]n/a[/]",
                p["dfu_support"] or "n/a",
            )
        out.console.print(table)
    else:
        print(f"\nPoly Firmware Catalog ({len(products)} products)")
        print("-" * 90)
        fmt = "{:<8} {:<35} {:<22} {:<10}"
        print(fmt.format("PID", "Product", "Latest FW", "DFU"))
        print("-" * 90)
        for p in products:
            print(fmt.format(p["id"][:8], p["name"][:35], p["version"][:22], p["dfu_support"] or "n/a"))

    out.print(f"\nTo check a specific product: polytool.py updates <product name>")


def cmd_fwinfo(args):
    """Analyze a downloaded firmware package."""
    out.header("PolyTool - Firmware Package Analyzer")

    target = args.path
    target_path = Path(target)

    # If it's a cached firmware, look in the cache dir
    if not target_path.exists():
        cached = FIRMWARE_CACHE / target
        if cached.exists():
            target_path = cached
        else:
            out.error(f"Path not found: {target}")
            out.print(f"  Check your firmware cache: {FIRMWARE_CACHE}")
            return

    # Single file or package?
    if target_path.is_file() and target_path.suffix != ".zip":
        # Analyze single firmware file
        info = parse_firmware_file(target_path)
        _display_fw_file_info(target_path.name, info)
        return

    # Full package
    pkg = parse_firmware_package(target_path)

    if "error" in pkg:
        out.error(pkg["error"])
        return

    # Display package summary
    out.print(f"\nPackage: {pkg.get('path', target)}")
    if pkg.get("version"):
        out.print(f"Version: {pkg['version']}")
    if pkg.get("release_date"):
        out.print(f"Release: {pkg['release_date']}")
    if pkg.get("release_notes"):
        out.print(f"Notes:   {pkg['release_notes'][:200]}")

    if pkg.get("rules_error"):
        out.warn(f"Rules: {pkg['rules_error']}")

    # Display components from rules.json
    components = pkg.get("components", [])
    if components:
        if HAS_RICH:
            table = Table(title="Firmware Components", box=box.ROUNDED)
            table.add_column("Type", style="cyan", no_wrap=True)
            table.add_column("Description", style="bold white", no_wrap=True, max_width=24)
            table.add_column("Version", style="green", no_wrap=True)
            table.add_column("Format", style="yellow", no_wrap=True)
            table.add_column("Size", style="dim", no_wrap=True)
            table.add_column("Transport", style="magenta", no_wrap=True)

            for comp in components:
                size_str = _format_size(comp.get("file_size", 0))
                table.add_row(
                    comp.get("type", ""),
                    comp.get("description", ""),
                    comp.get("version", ""),
                    comp.get("file_format", ""),
                    size_str,
                    comp.get("transport", "") or "default",
                    comp.get("pid", ""),
                )
            out.console.print(table)
        else:
            print(f"\nComponents ({len(components)}):")
            print("-" * 90)
            fmt = "  {:<10} {:<30} {:<8} {:<10} {:<10} {:<8}"
            print(fmt.format("Type", "Description", "Version", "Format", "Size", "PID"))
            print("-" * 90)
            for comp in components:
                size_str = _format_size(comp.get("file_size", 0))
                print(fmt.format(
                    comp.get("type", "")[:10],
                    comp.get("description", "")[:30],
                    comp.get("version", "")[:8],
                    comp.get("file_format", "")[:10],
                    size_str[:10],
                    comp.get("pid", "")[:8],
                ))

        # Show unique formats and transports
        formats = set(c.get("file_format", "") for c in components if c.get("file_format"))
        transports = set(c.get("transport", "") for c in components if c.get("transport"))
        if formats:
            out.print(f"\nFirmware formats: {', '.join(sorted(formats))}")
        if transports:
            out.print(f"Transport protocols: {', '.join(sorted(transports))}")

        # Count by type
        type_counts = {}
        for comp in components:
            t = comp.get("type", "other")
            type_counts[t] = type_counts.get(t, 0) + 1
        summary = ", ".join(f"{count} {t}" for t, count in sorted(type_counts.items()))
        out.print(f"Summary: {summary}")

    # Display standalone files (when no rules.json)
    files = pkg.get("files", [])
    if files:
        out.print(f"\nFirmware files ({len(files)}):")
        for f in files:
            _display_fw_file_info(f.get("filename", ""), f)


def _display_fw_file_info(filename: str, info: dict):
    """Display parsed firmware file info."""
    fmt = info.get("format", "unknown")
    size = info.get("total_size", info.get("file_size", 0))
    out.print(f"\n  {filename} [{fmt}, {_format_size(size)}]")

    if fmt == "FWU":
        if info.get("header_hex"):
            out.print(f"    Header: {info['header_hex']}")
    elif fmt == "FIRMWARE":
        sections = info.get("sections", [])
        for s in sections:
            out.print(f"    Section: {s['name']:16s} v={s['version']} "
                      f"size={s['size']} crc={s['crc32']}")
    elif fmt == "CSR-dfu2":
        out.print(f"    Codename: {info.get('codename', 'unknown')}")
        out.print(f"    Payload: {info.get('payload_size', 0)} bytes")
    elif fmt == "APPUHDR5":
        out.print(f"    Platform: {info.get('platform', 'unknown')}")
        out.print(f"    Partitions: {info.get('partition_count', 0)}")
    elif info.get("error"):
        out.print(f"    Error: {info['error']}")


def _format_size(size: int) -> str:
    """Format byte size to human readable."""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    elif size >= 1024:
        return f"{size / 1024:.1f} KB"
    elif size > 0:
        return f"{size} B"
    return "n/a"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _select_devices(devices: list, selector: str = None) -> list:
    """Select device(s) by number, serial prefix, or 'all'."""
    if not selector or selector.lower() == "all":
        return devices

    # Try as number (1-indexed)
    try:
        idx = int(selector) - 1
        if 0 <= idx < len(devices):
            return [devices[idx]]
        out.error(f"Device #{selector} not found. Use 1-{len(devices)}.")
        return []
    except ValueError:
        pass

    # Try as serial prefix
    matches = [d for d in devices if d.serial and d.serial.lower().startswith(selector.lower())]
    if matches:
        return matches

    # Try as product name substring
    matches = [d for d in devices if selector.lower() in d.friendly_name.lower()]
    if matches:
        return matches

    out.error(f"No device matching '{selector}'. Use a number, serial prefix, or device name.")
    return []


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


# ── Main Entry Point ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="polytool",
        description="PolyTool - Poly/Plantronics Headset Management & Firmware Updater",
        epilog="Reverse-engineered from Poly Studio 5.0.1.9 (HP Inc.)",
    )
    parser.add_argument("--version", action="version", version=f"PolyTool {VERSION}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # scan
    subparsers.add_parser("scan", help="Discover all connected Poly devices")

    # info
    info_parser = subparsers.add_parser("info", help="Show detailed device info")
    info_parser.add_argument("device", nargs="?", default="all",
                             help="Device # / serial / name / 'all' (default: all)")

    # battery
    subparsers.add_parser("battery", help="Show battery levels for all devices")

    # updates
    updates_parser = subparsers.add_parser("updates", help="Check for firmware updates")
    updates_parser.add_argument("device", nargs="?", default="all",
                                help="Device # / serial / name / 'all'")

    # update
    update_parser = subparsers.add_parser("update", help="Download and apply firmware updates")
    update_parser.add_argument("device", nargs="?", default="all",
                               help="Device # / serial / name / 'all'")
    update_parser.add_argument("--force", action="store_true",
                               help="Force update even if current version matches")

    # monitor
    monitor_parser = subparsers.add_parser("monitor", help="Live device status dashboard")
    monitor_parser.add_argument("--interval", type=int, default=5,
                                help="Refresh interval in seconds (default: 5)")

    # catalog
    catalog_parser = subparsers.add_parser("catalog", help="Search the Poly cloud firmware catalog")
    catalog_parser.add_argument("search", nargs="?", default="",
                                help="Search term (e.g., 'voyager', 'blackwire', 'sync')")
    catalog_parser.add_argument("--all", action="store_true",
                                help="Include products without firmware")

    # fwinfo
    fwinfo_parser = subparsers.add_parser("fwinfo", help="Analyze a firmware package (zip or directory)")
    fwinfo_parser.add_argument("path", help="Path to firmware zip, directory, or single .fwu/.bin/.dfu file")

    args = parser.parse_args()

    if not args.command:
        # Default to scan if no command given
        args.command = "scan"

    if not check_dependencies():
        sys.exit(1)

    commands = {
        "scan": cmd_scan,
        "info": cmd_info,
        "battery": cmd_battery,
        "updates": cmd_updates,
        "update": cmd_update,
        "monitor": cmd_monitor,
        "catalog": cmd_catalog,
        "fwinfo": cmd_fwinfo,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
