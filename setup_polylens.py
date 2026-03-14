#!/usr/bin/env python3
"""
Poly Lens Patcher — One-command setup for unsupported Poly/Plantronics devices.

Patches the official Poly Studio / Poly Lens app so that devices like the
Blackwire 3220 (VID 0x047F, PID 0xC056) are recognized and manageable in the
GUI, without requiring any manual binary patching or config editing.

What it does:
  1. Copies Poly Studio.app to a temp location (avoids SIP restrictions)
  2. Patches PolyDolphin.dylib to accept non-HP vendor IDs
  3. Adds missing devices to Devices.config with correct handlers
  4. Re-signs the app bundle (ad-hoc)
  5. Replaces the original (requires sudo for the final move)

Usage:
  python3 setup_polylens.py              # patch Poly Studio
  python3 setup_polylens.py --revert     # restore original
  python3 setup_polylens.py --status     # check current patch state
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

APP_NAME = "Poly Studio.app"
DEFAULT_APP = Path("/Applications") / APP_NAME

DYLIB_REL = Path(
    "Contents/Helpers/LensService.app/Contents/MacOS/clockwork/PolyDolphin.dylib"
)
CONFIG_REL = Path(
    "Contents/Helpers/LegacyHostApp.app/Contents/Resources/Devices.config"
)

# ── PolyDolphin binary patches ───────────────────────────────────────────────
# Reverse-engineered from PolyDolphin.dylib in Poly Studio 5.0.x (ARM64).
# These change the VID-check logic so non-HP devices fall through to the
# Deckard HID protocol probe instead of being rejected as NOT_SUPPORTED.

DYLIB_PATCHES = [
    {
        "name": "Gate 1: map-miss → factory probe fallback",
        "offset": 0x217FD8,
        "original": bytes.fromhex("200200b4"),
        "patched": bytes.fromhex("20faffb4"),
    },
    {
        "name": "Gate 2: IsOnSupportedDeviceList → true for unknown VIDs",
        "offset": 0x21A674,
        "original": bytes.fromhex("800200b40a0c42f8"),
        "patched": bytes.fromhex("2000805213000014"),
    },
]

# ── Device config additions ──────────────────────────────────────────────────
# Devices that are missing from the stock Devices.config.
# Each entry maps a PID to the correct LegacyHost handler for its chipset.

EXTRA_DEVICES = [
    {
        "ProductID": "C056",
        "usagePage": "FFA0",
        "Name": "Blackwire 3220",
        "DeviceEventHandler": "YetiEvent",
        "HostCommandHandler": "YetiCommand",
        "DeviceListenerHandler": "",
        "HostCommandDelay": "",
    },
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def die(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd, check=True, **kwargs):
    """Run a shell command, print it, and return the result."""
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kwargs)


def check_dylib_state(dylib_path):
    """Return 'original', 'patched', or 'unknown'."""
    data = dylib_path.read_bytes()
    all_original = all(
        data[p["offset"]:p["offset"] + len(p["original"])] == p["original"]
        for p in DYLIB_PATCHES
    )
    all_patched = all(
        data[p["offset"]:p["offset"] + len(p["patched"])] == p["patched"]
        for p in DYLIB_PATCHES
    )
    if all_original:
        return "original"
    if all_patched:
        return "patched"
    return "unknown"


def check_config_state(config_path):
    """Return list of extra device PIDs that are correctly configured."""
    with open(config_path) as f:
        cfg = json.load(f)

    correct = []
    for extra in EXTRA_DEVICES:
        for dev in cfg.get("devices", []):
            if dev.get("ProductID") == extra["ProductID"]:
                # Check that handlers match, not just that PID exists
                if (dev.get("HostCommandHandler") == extra["HostCommandHandler"]
                        and dev.get("DeviceEventHandler") == extra["DeviceEventHandler"]):
                    correct.append(extra["ProductID"])
                break
    return correct


def patch_dylib(dylib_path):
    """Apply binary patches to PolyDolphin.dylib."""
    data = bytearray(dylib_path.read_bytes())
    for p in DYLIB_PATCHES:
        cur = data[p["offset"]:p["offset"] + len(p["original"])]
        if cur == p["patched"]:
            print(f"  [skip] {p['name']} (already patched)")
            continue
        if cur != p["original"]:
            die(f"Unexpected bytes at 0x{p['offset']:X}: {cur.hex()}\n"
                f"  Expected: {p['original'].hex()}\n"
                f"  This version of Poly Studio may not be compatible.")
        data[p["offset"]:p["offset"] + len(p["patched"])] = p["patched"]
        print(f"  [patch] {p['name']}")
    dylib_path.write_bytes(bytes(data))


def revert_dylib(dylib_path):
    """Revert binary patches in PolyDolphin.dylib."""
    data = bytearray(dylib_path.read_bytes())
    for p in DYLIB_PATCHES:
        cur = data[p["offset"]:p["offset"] + len(p["patched"])]
        if cur == p["original"]:
            print(f"  [skip] {p['name']} (already original)")
            continue
        if cur != p["patched"]:
            die(f"Unexpected bytes at 0x{p['offset']:X}: {cur.hex()}\n"
                f"  Cannot safely revert.")
        data[p["offset"]:p["offset"] + len(p["original"])] = p["original"]
        print(f"  [revert] {p['name']}")
    dylib_path.write_bytes(bytes(data))


def patch_config(config_path):
    """Add missing devices to Devices.config."""
    with open(config_path) as f:
        cfg = json.load(f)

    extra_pids = {d["ProductID"] for d in EXTRA_DEVICES}
    # Remove existing entries for our PIDs to avoid duplicates
    cfg["devices"] = [d for d in cfg["devices"]
                      if d.get("ProductID") not in extra_pids]
    cfg["devices"].extend(EXTRA_DEVICES)

    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)

    for d in EXTRA_DEVICES:
        print(f"  [config] Added {d['Name']} (PID {d['ProductID']})")


def revert_config(config_path):
    """Remove added devices from Devices.config."""
    with open(config_path) as f:
        cfg = json.load(f)

    extra_pids = {d["ProductID"] for d in EXTRA_DEVICES}
    before = len(cfg["devices"])
    cfg["devices"] = [d for d in cfg["devices"]
                      if d.get("ProductID") not in extra_pids]
    after = len(cfg["devices"])

    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)

    print(f"  [config] Removed {before - after} device(s)")


# ── Main commands ────────────────────────────────────────────────────────────

def cmd_status(app_path):
    """Show current patch state."""
    dylib = app_path / DYLIB_REL
    config = app_path / CONFIG_REL

    if not dylib.exists():
        die(f"Poly Studio not found at {app_path}")

    state = check_dylib_state(dylib)
    present = check_config_state(config)

    print(f"Poly Studio: {app_path}")
    print(f"PolyDolphin: {state}")
    print(f"Devices.config: {len(present)}/{len(EXTRA_DEVICES)} extra devices present")
    if present:
        for pid in present:
            name = next(d["Name"] for d in EXTRA_DEVICES if d["ProductID"] == pid)
            print(f"  - {name} ({pid})")

    if state == "patched" and len(present) == len(EXTRA_DEVICES):
        print("\nAll patches applied.")
    elif state == "original" and not present:
        print("\nNo patches applied (stock Poly Studio).")
    else:
        print("\nPartially patched.")


def cmd_patch(app_path):
    """Apply all patches to Poly Studio."""
    dylib = app_path / DYLIB_REL
    config = app_path / CONFIG_REL

    if not dylib.exists():
        die(f"Poly Studio not found at {app_path}")

    # Check if already fully patched
    if check_dylib_state(dylib) == "patched":
        present = check_config_state(config)
        if len(present) == len(EXTRA_DEVICES):
            print("Already fully patched, nothing to do.")
            return

    # On macOS, SIP prevents writing to /Applications directly.
    # Work around by copying to a temp dir, patching there, then moving back.
    needs_copy = str(app_path).startswith("/Applications")

    if needs_copy:
        print("Step 1/5: Copying Poly Studio to temp directory...")
        tmp_dir = Path(tempfile.mkdtemp(prefix="polytool_"))
        tmp_app = tmp_dir / APP_NAME
        shutil.copytree(app_path, tmp_app, symlinks=True)
        work_app = tmp_app
    else:
        work_app = app_path

    work_dylib = work_app / DYLIB_REL
    work_config = work_app / CONFIG_REL

    print("Step 2/5: Patching PolyDolphin.dylib...")
    patch_dylib(work_dylib)

    print("Step 3/5: Updating Devices.config...")
    patch_config(work_config)

    print("Step 4/5: Re-signing app bundle...")
    result = run(["codesign", "--force", "--deep", "--sign", "-", str(work_app)],
                 check=False)
    if result.returncode != 0:
        print(f"  Warning: codesign returned {result.returncode}: {result.stderr.strip()}")
        print("  Continuing anyway (may need xattr fix)...")

    run(["xattr", "-rd", "com.apple.quarantine", str(work_app)], check=False)

    if needs_copy:
        print("Step 5/5: Replacing original (requires sudo)...")
        # Kill Poly Studio processes first
        for proc in ["Poly Studio", "LensService", "legacyhost", "PolyLauncher",
                      "CallControlApp"]:
            subprocess.run(["pkill", "-f", proc], capture_output=True)

        # Move original out, move patched in
        result = run(["sudo", "rm", "-rf", str(app_path)], check=False)
        if result.returncode != 0:
            die(f"Failed to remove original: {result.stderr.strip()}\n"
                f"  Patched copy is at: {work_app}")

        result = run(["sudo", "mv", str(work_app), str(app_path)], check=False)
        if result.returncode != 0:
            die(f"Failed to move patched app: {result.stderr.strip()}\n"
                f"  Patched copy is at: {work_app}")

        # Clean up temp dir
        shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        print("Step 5/5: Done (patched in place).")

    print()
    print("Poly Studio has been patched successfully!")
    print("Open Poly Studio to verify your device is detected.")


def cmd_revert(app_path):
    """Revert all patches."""
    dylib = app_path / DYLIB_REL
    config = app_path / CONFIG_REL

    if not dylib.exists():
        die(f"Poly Studio not found at {app_path}")

    if check_dylib_state(dylib) == "original":
        present = check_config_state(config)
        if not present:
            print("Already at stock state, nothing to revert.")
            return

    needs_copy = str(app_path).startswith("/Applications")

    if needs_copy:
        print("Step 1/4: Copying Poly Studio to temp directory...")
        tmp_dir = Path(tempfile.mkdtemp(prefix="polytool_"))
        tmp_app = tmp_dir / APP_NAME
        shutil.copytree(app_path, tmp_app, symlinks=True)
        work_app = tmp_app
    else:
        work_app = app_path

    work_dylib = work_app / DYLIB_REL
    work_config = work_app / CONFIG_REL

    print("Step 2/4: Reverting PolyDolphin.dylib...")
    revert_dylib(work_dylib)

    print("Step 3/4: Reverting Devices.config...")
    revert_config(work_config)

    print("Step 4/4: Re-signing app bundle...")
    run(["codesign", "--force", "--deep", "--sign", "-", str(work_app)], check=False)
    run(["xattr", "-rd", "com.apple.quarantine", str(work_app)], check=False)

    if needs_copy:
        for proc in ["Poly Studio", "LensService", "legacyhost", "PolyLauncher",
                      "CallControlApp"]:
            subprocess.run(["pkill", "-f", proc], capture_output=True)

        run(["sudo", "rm", "-rf", str(app_path)], check=False)
        result = run(["sudo", "mv", str(work_app), str(app_path)], check=False)
        if result.returncode != 0:
            die(f"Failed to move reverted app: {result.stderr.strip()}\n"
                f"  Reverted copy is at: {work_app}")
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print()
    print("Poly Studio has been reverted to stock.")


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Patch Poly Studio to support additional Poly/Plantronics devices",
        epilog="Run without arguments to apply patches. Use --status to check state.",
    )
    parser.add_argument("--revert", action="store_true",
                        help="Revert all patches to stock")
    parser.add_argument("--status", action="store_true",
                        help="Show current patch state")
    parser.add_argument("--app", type=str, default=str(DEFAULT_APP),
                        help="Path to Poly Studio.app")
    args = parser.parse_args()

    app_path = Path(args.app)

    if not (app_path / DYLIB_REL).exists():
        die(f"Poly Studio not found at {app_path}\n"
            f"  Install it from https://www.poly.com/us/en/support/downloads-apps/studio\n"
            f"  Or specify a custom path with --app")

    if args.status:
        cmd_status(app_path)
    elif args.revert:
        cmd_revert(app_path)
    else:
        cmd_patch(app_path)


if __name__ == "__main__":
    main()
