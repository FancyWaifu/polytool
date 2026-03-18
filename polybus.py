#!/usr/bin/env python3
"""
PolyBus — Direct native library interface to Poly's HID communication layer.

Calls the PolyBus native library (clockwork/PolyBus.dll, actually a Mach-O
dylib) via ctypes to read/write device properties without going through
LensService or any .NET code.

Exported C API (from nm -gU PolyBus.dll):
  _GetBusLibraryVersion()
  _OpenDevice(???)
  _CloseDevice(???)
  _CloseAllResources()
  _GetDeviceProperty(???)
  _SetDeviceProperty(???)

The function signatures need to be determined by testing. This module
provides a safe exploration interface for mapping the ABI.

Usage:
  python3 polybus.py version    # Get library version
  python3 polybus.py probe      # Probe function signatures
"""

import ctypes
import ctypes.util
import sys
import os
import argparse
from pathlib import Path


def find_polybus():
    """Find the PolyBus native library."""
    candidates = [
        # macOS — installed Poly Studio
        "/Applications/Poly Studio.app/Contents/Helpers/LensService.app/Contents/MacOS/clockwork/PolyBus.dll",
        # macOS — tmp copy
        "/tmp/PolyStudio.app/Contents/Helpers/LensService.app/Contents/MacOS/clockwork/PolyBus.dll",
        # Relative to this script
        str(Path(__file__).parent / "clockwork" / "PolyBus.dll"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def load_polybus(path):
    """Load the PolyBus native library."""
    # On macOS, .dll extension is actually a Mach-O dylib
    lib = ctypes.CDLL(path)
    return lib


def cmd_version(lib):
    """Get library version string."""
    # Try different calling conventions for GetBusLibraryVersion
    # It likely returns a char* or fills a buffer
    try:
        # Try: const char* GetBusLibraryVersion()
        lib.GetBusLibraryVersion.restype = ctypes.c_char_p
        version = lib.GetBusLibraryVersion()
        if version:
            print(f"  PolyBus version: {version.decode('utf-8', errors='ignore')}")
            return
    except Exception as e:
        print(f"  char* return failed: {e}")

    try:
        # Try: int GetBusLibraryVersion(char* buf, int bufsize)
        buf = ctypes.create_string_buffer(256)
        lib.GetBusLibraryVersion.restype = ctypes.c_int
        result = lib.GetBusLibraryVersion(buf, 256)
        if buf.value:
            print(f"  PolyBus version: {buf.value.decode('utf-8', errors='ignore')} (result={result})")
            return
    except Exception as e:
        print(f"  Buffer fill failed: {e}")

    try:
        # Try: int GetBusLibraryVersion()
        lib.GetBusLibraryVersion.restype = ctypes.c_int
        result = lib.GetBusLibraryVersion()
        print(f"  PolyBus version (int): {result}")
    except Exception as e:
        print(f"  Int return failed: {e}")


def cmd_probe(lib):
    """Probe function signatures by trying different calling conventions."""
    print("Probing PolyBus function signatures...\n")

    # GetBusLibraryVersion
    print("--- GetBusLibraryVersion ---")
    cmd_version(lib)

    # CloseAllResources (likely void or int, no args)
    print("\n--- CloseAllResources ---")
    try:
        lib.CloseAllResources.restype = ctypes.c_int
        result = lib.CloseAllResources()
        print(f"  CloseAllResources() = {result}")
    except Exception as e:
        print(f"  Error: {e}")

    # OpenDevice — likely takes a device identifier
    print("\n--- OpenDevice ---")
    print("  Signature unknown — needs RE of callers in DeviceLibraries.dll")
    print("  Likely: int OpenDevice(const char* path_or_id)")

    # GetDeviceProperty / SetDeviceProperty
    print("\n--- GetDeviceProperty ---")
    print("  Signature unknown — needs RE of callers")
    print("  Likely: int GetDeviceProperty(handle, const char* name, char* value, int* valuelen)")

    print("\n--- SetDeviceProperty ---")
    print("  Likely: int SetDeviceProperty(handle, const char* name, const char* value)")

    print("\n  To determine exact signatures, decompile DeviceLibraries.dll")
    print("  and look at PolyBusDll.cs P/Invoke declarations.")


def main():
    parser = argparse.ArgumentParser(description="PolyBus — Native library interface")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("version", help="Get library version")
    sub.add_parser("probe", help="Probe function signatures")
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    path = find_polybus()
    if not path:
        print("PolyBus library not found.")
        print("  Install Poly Studio or copy clockwork/PolyBus.dll to this directory.")
        sys.exit(1)

    print(f"Loading: {path}\n")
    lib = load_polybus(path)

    if args.command == "version":
        cmd_version(lib)
    elif args.command == "probe":
        cmd_probe(lib)


if __name__ == "__main__":
    main()
