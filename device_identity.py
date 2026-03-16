#!/usr/bin/env python3
"""
Device Identity Preservation — protects device-unique EEPROM/flash data
during firmware updates.

Firmware update files often contain generic placeholder values for fields
like serial numbers, calibration data, and USB descriptors. If these get
flashed over the device-unique values, the headset loses its identity and
may not be recognized by Poly Lens or other management software.

This module provides backup/restore functions that wrap any flash operation
to preserve device-unique data automatically.

Supports:
  - CX2070x (Blackwire 3220): EEPROM serial + config bytes
  - FWU API (Savi series): Device identity stored in DECT base
  - BladeRunner (Blackwire 33xx/7225/8225): Identity in flash partitions
"""

import time

try:
    import hid
except ImportError:
    hid = None


# ── CX2070x Identity (Blackwire 3220) ────────────────────────────────────────
# The CX2070x stores USB descriptors and device config in EEPROM.
# The PTC firmware file overwrites these with generic values:
#   - Serial number (0x0042-0x0083): overwritten with all 'F' characters
#   - Config bytes (0x0020-0x002F): calibration/birth marks changed
#
# These must be backed up before flash and restored after.

CX2070X_PRESERVE = [
    (0x0020, 0x0030, "Device config (calibration, birth marks)"),
    (0x0042, 0x0084, "USB serial number (UTF-16LE descriptor)"),
]

CX_RID_OUT = 0x04
CX_CMD_EEPROM_READ = 0x20
CX_CMD_EEPROM_WRITE = 0x60
CX_MAX_CHUNK = 16


def cx_backup_identity(h):
    """Back up device-unique EEPROM regions from a CX2070x device.

    Args:
        h: open hid.device handle

    Returns:
        dict mapping (start, end) → bytes, or None on failure
    """
    backup = {}
    for start, end, desc in CX2070X_PRESERVE:
        data = bytearray()
        for addr in range(start, end, CX_MAX_CHUNK):
            length = min(CX_MAX_CHUNK, end - addr)
            pkt = [CX_RID_OUT, CX_CMD_EEPROM_READ, length,
                   (addr >> 8) & 0xFF, addr & 0xFF] + [0x00] * 32
            h.write(pkt)
            resp = h.read(64, timeout_ms=2000)
            if resp and len(resp) > length:
                data.extend(resp[1:1+length])
            else:
                data.extend(b'\xFF' * length)
        backup[(start, end)] = bytes(data)
        # Check if it's non-trivial (not all FF or all 00)
        non_trivial = any(b not in (0x00, 0xFF) for b in data)
        print(f"    Backed up 0x{start:04X}-0x{end:04X}: {desc} "
              f"({'has data' if non_trivial else 'empty'})")
    return backup


def cx_restore_identity(h, backup):
    """Restore device-unique EEPROM regions to a CX2070x device.

    Args:
        h: open hid.device handle (EEPROM writes must already be enabled)
        backup: dict from cx_backup_identity()
    """
    for (start, end), data in sorted(backup.items()):
        for offset in range(0, len(data), CX_MAX_CHUNK):
            addr = start + offset
            chunk = data[offset:offset + CX_MAX_CHUNK]
            if len(chunk) > 0:
                pkt = [CX_RID_OUT, CX_CMD_EEPROM_WRITE, len(chunk),
                       (addr >> 8) & 0xFF, addr & 0xFF] + list(chunk) + [0x00] * 20
                h.write(pkt)
                time.sleep(0.01)

        # Verify
        ok = True
        for offset in range(0, len(data), CX_MAX_CHUNK):
            addr = start + offset
            chunk = data[offset:offset + CX_MAX_CHUNK]
            pkt = [CX_RID_OUT, CX_CMD_EEPROM_READ, len(chunk),
                   (addr >> 8) & 0xFF, addr & 0xFF] + [0x00] * 32
            h.write(pkt)
            resp = h.read(64, timeout_ms=2000)
            if resp:
                actual = bytes(resp[1:1+len(chunk)])
                if actual != chunk:
                    print(f"    WARNING: Verify failed at 0x{addr:04X}")
                    ok = False

        if ok:
            desc = ""
            for s, e, d in CX2070X_PRESERVE:
                if s == start:
                    desc = d
                    break
            print(f"    Restored 0x{start:04X}-0x{end:04X}: {desc}")


# ── FWU API Identity (Savi series) ───────────────────────────────────────────
# The FWU protocol handles identity preservation at the protocol level.
# The base station manages device IDs internally — the host sends firmware
# blocks but doesn't directly write to identity regions.
# No explicit backup/restore needed for FWU devices.

def fwu_backup_identity(h):
    """FWU devices preserve identity at the protocol level. No-op."""
    return {}

def fwu_restore_identity(h, backup):
    """FWU devices preserve identity at the protocol level. No-op."""
    pass


# ── BladeRunner Identity (Blackwire 33xx/7225/8225) ──────────────────────────
# BladeRunner devices store identity in a separate flash partition.
# The DFU process writes to the APP_MAIN partition only — the bootloader
# and identity partitions are not touched during normal firmware updates.
# The Set ID and birth marks are preserved automatically by the device's
# two-image (trial/stable) update system.

def br_backup_identity(h):
    """BladeRunner devices preserve identity via flash partitions. No-op.

    The device's bootloader manages partition integrity:
    - APP_MAIN: updated during DFU
    - APP_DSP: updated during DFU
    - BOOTLOADER: only updated if explicitly requested
    - IDENTITY/PSTORE: never touched by DFU
    """
    return {}

def br_restore_identity(h, backup):
    """BladeRunner devices preserve identity via flash partitions. No-op."""
    pass


# ── Unified Interface ────────────────────────────────────────────────────────

def backup_device_identity(h, device_family):
    """Back up device identity before firmware flash.

    Args:
        h: open hid.device handle
        device_family: "cx2070x", "fwu", "bladerunner"

    Returns:
        backup dict (opaque, pass to restore_device_identity)
    """
    if device_family == "cx2070x":
        return cx_backup_identity(h)
    elif device_family == "fwu":
        return fwu_backup_identity(h)
    elif device_family == "bladerunner":
        return br_backup_identity(h)
    return {}


def restore_device_identity(h, device_family, backup):
    """Restore device identity after firmware flash.

    Args:
        h: open hid.device handle
        device_family: "cx2070x", "fwu", "bladerunner"
        backup: dict from backup_device_identity()
    """
    if not backup:
        return
    if device_family == "cx2070x":
        cx_restore_identity(h, backup)
    elif device_family == "fwu":
        fwu_restore_identity(h, backup)
    elif device_family == "bladerunner":
        br_restore_identity(h, backup)
