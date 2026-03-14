#!/usr/bin/env python3
"""
Patch PolyDolphin.dylib to support non-HP USB HID devices (like Blackwire 3220).

PolyDolphin.dylib's IsDeviceSupportedEx only checks a hardcoded map of HP (VID 0x03F0)
PIDs. Any device with a different VID is immediately rejected as NOT_SUPPORTED, causing
PolyBus.dll to classify it as "bricked". This patch changes the map-miss path to fall
through to the Deckard HID protocol probe, which can detect and support Poly/Plantronics
USB HID devices.

Two patches (12 bytes total):
  Patch 1: Gate 1 map-miss → fall through to factory probe instead of NOT_SUPPORTED
  Patch 2: Gate 2 IsOnSupportedDeviceList → return true for non-HP VIDs

Usage:
  sudo python3 patch_polydolphin.py          # apply patch
  sudo python3 patch_polydolphin.py --revert # restore original
"""

import sys
import shutil
import argparse
from pathlib import Path

DEFAULT_APP = Path("/Applications/Poly Studio.app")
DYLIB_REL = Path("Contents/Helpers/LensService.app/Contents/MacOS/clockwork/PolyDolphin.dylib")

PATCHES = [
    {
        "name": "Gate 1: map-miss → factory probe fallback",
        "offset": 0x217FD8,
        # Original: cbz x0, 0x21801C  (VID not in map → return NOT_SUPPORTED=5)
        # Patched:  cbz x0, 0x217F1C  (VID not in map → try Deckard HID probe)
        #   The probe path calls a vtable method to test the device. If it fails,
        #   still returns NOT_SUPPORTED. If it succeeds, returns 0 (supported).
        "original": bytes.fromhex("200200b4"),
        "patched":  bytes.fromhex("20faffb4"),
    },
    {
        "name": "Gate 2: IsOnSupportedDeviceList → true for unknown VIDs",
        "offset": 0x21A674,
        # Original: cbz x0, 0x21A6C4; ldr x10, [x0, #0x20]!
        #   (VID not in map → return false; or dereference map iterator)
        # Patched:  mov w0, #1; b 0x21A6C4
        #   (VID not in map → return true, skip PID set iteration)
        "original": bytes.fromhex("800200b40a0c42f8"),
        "patched":  bytes.fromhex("2000805213000014"),
    },
]


def read_at(data, offset, length):
    return data[offset:offset + length]


def main():
    parser = argparse.ArgumentParser(description="Patch PolyDolphin.dylib for non-HP VID support")
    parser.add_argument("--revert", action="store_true", help="Revert to original bytes")
    parser.add_argument("--app", type=str, default=str(DEFAULT_APP),
                        help="Path to Poly Studio.app (default: /Applications/Poly Studio.app)")
    args = parser.parse_args()

    revert = args.revert
    DYLIB = Path(args.app) / DYLIB_REL

    if not DYLIB.exists():
        print(f"Error: {DYLIB} not found")
        sys.exit(1)

    data = bytearray(DYLIB.read_bytes())
    backup = DYLIB.with_suffix(".dylib.bak")

    # Verify current state
    all_original = True
    all_patched = True
    for p in PATCHES:
        cur = read_at(data, p["offset"], len(p["original"]))
        if cur != p["original"]:
            all_original = False
        if cur != p["patched"]:
            all_patched = False

    if revert:
        if all_original:
            print("Already at original state, nothing to revert.")
            return
        if not all_patched:
            print("Error: binary is in an unexpected state, cannot safely revert.")
            sys.exit(1)
        # Revert
        for p in PATCHES:
            data[p["offset"]:p["offset"] + len(p["original"])] = p["original"]
        DYLIB.write_bytes(bytes(data))
        print("Reverted to original.")
    else:
        if all_patched:
            print("Already patched, nothing to do.")
            return
        if not all_original:
            print("Error: binary is in an unexpected state. Expected original bytes not found.")
            for p in PATCHES:
                cur = read_at(data, p["offset"], len(p["original"]))
                print(f"  {p['name']} @ 0x{p['offset']:X}: {cur.hex()} (expected {p['original'].hex()})")
            sys.exit(1)
        # Backup
        if not backup.exists():
            shutil.copy2(DYLIB, backup)
            print(f"Backup: {backup}")
        # Patch
        for p in PATCHES:
            data[p["offset"]:p["offset"] + len(p["patched"])] = p["patched"]
            print(f"  Patched: {p['name']} @ 0x{p['offset']:X}")
        DYLIB.write_bytes(bytes(data))
        print("Done. Restart Poly Studio for changes to take effect.")


if __name__ == "__main__":
    main()
