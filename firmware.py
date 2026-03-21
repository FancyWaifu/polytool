#!/usr/bin/env python3
"""
PolyTool — Firmware parsing, cloud API, and DFU protocol.

Split from polytool.py for modularity. All public names are re-exported
by polytool.py for backward compatibility.
"""

import hashlib
import json
import os
import platform
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from devices import (
    PolyDevice, Output, out,
    # Constants
    CLOUD_GRAPHQL, FIRMWARE_CDN, CONFIG_DIR, FIRMWARE_CACHE,
    DFU_TRANSPORT_INFO, DFU_EXECUTOR_MAP,
    # Functions
    _open_hid, _normalize_version,
    # Optional deps
    HAS_RICH,
)

# Re-import optional deps that firmware.py uses directly
try:
    import requests
except ImportError:
    requests = None

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn
    from rich import box
except ImportError:
    pass


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


# ── Helper ───────────────────────────────────────────────────────────────────

def _format_size(size: int) -> str:
    """Format byte size to human readable."""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    elif size >= 1024:
        return f"{size / 1024:.1f} KB"
    elif size > 0:
        return f"{size} B"
    return "n/a"


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
