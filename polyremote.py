#!/usr/bin/env python3
"""
PolyRemote — Remote headset configuration tool for managed environments.

Lightweight CLI for making approved setting changes to Poly headsets
without modifying the Poly Lens client or firmware. Designed to be
deployed via RMM tools (SCCM, Intune, Jamf, etc.) for hands-off
remote configuration.

Coexists with Poly Lens — does NOT kill or modify the Poly client.

Usage:
  polyremote list                          # List connected devices
  polyremote get <setting>                 # Read a setting from all devices
  polyremote set <setting> <value>         # Change a setting on all devices
  polyremote set <setting> <value> --pid 0xC056   # Target specific device
  polyremote dump                          # Dump all readable settings
  polyremote batch settings.json           # Apply multiple settings from file
  polyremote identify                      # Flash LEDs / play tone to ID device

Output is JSON when piped (for automation) or human-readable in terminal.

Examples:
  polyremote set "Sidetone Level" 5
  polyremote set "Ringtone Volume" 8 --pid 0xACFF
  polyremote get "Sidetone Level"
  polyremote batch --file office_preset.json
  polyremote list --json
"""

import argparse
import json
import sys
import time
import os
import logging
from datetime import datetime
from pathlib import Path

try:
    import hid
except ImportError:
    print("Error: hidapi required. Install with: pip install hidapi", file=sys.stderr)
    sys.exit(1)

from device_settings import (
    get_device_family, get_settings_for_device, read_all_settings,
    write_setting, SETTINGS_DB, CX_REGISTER_MAP, BR_SETTING_IDS,
)

# ── Constants ────────────────────────────────────────────────────────────────

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
VENDOR_USAGE_PAGES = {0xFFA0, 0xFFA2, 0xFF52, 0xFF58}
VERSION = "1.0.0"

LOG_DIR = Path.home() / ".polytool" / "logs"

# ── Logging ──────────────────────────────────────────────────────────────────

def setup_logging():
    """Set up audit logging to file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "polyremote.log"
    logging.basicConfig(
        filename=str(log_file),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("polyremote")

logger = setup_logging()


# ── Device Discovery ─────────────────────────────────────────────────────────

def discover_devices():
    """Find all connected Poly devices. Does NOT interfere with Poly Lens."""
    seen = {}
    for d in hid.enumerate():
        vid = d.get("vendor_id", 0)
        if vid not in POLY_VIDS:
            continue
        pid = d.get("product_id", 0)
        usage_page = d.get("usage_page", 0)
        serial = d.get("serial_number", "") or ""

        # Prefer vendor-specific usage page per device
        key = (pid, serial) if serial else (pid, d.get("interface_number", 0))
        existing = seen.get(key)
        if existing:
            if usage_page in VENDOR_USAGE_PAGES and existing["usage_page"] not in VENDOR_USAGE_PAGES:
                pass  # replace
            elif existing["usage_page"] in VENDOR_USAGE_PAGES:
                continue
            else:
                continue

        # Determine DFU executor for family detection
        from polytool import DFU_EXECUTOR_MAP, CODENAME_MAP, PID_CODENAMES
        lens_pid = f"{pid:x}"
        dfu_executor = DFU_EXECUTOR_MAP.get(lens_pid, "")
        codename = PID_CODENAMES.get(pid, "")
        friendly = CODENAME_MAP.get(codename, "") if codename else ""

        seen[key] = {
            "vid": vid,
            "pid": pid,
            "pid_hex": f"0x{pid:04X}",
            "serial": serial,
            "product_name": d.get("product_string", "") or f"Poly 0x{pid:04X}",
            "friendly_name": friendly or d.get("product_string", ""),
            "manufacturer": d.get("manufacturer_string", ""),
            "usage_page": usage_page,
            "path": d.get("path", b""),
            "release_number": d.get("release_number", 0),
            "dfu_executor": dfu_executor,
            "family": get_device_family(usage_page, dfu_executor),
        }

    return list(seen.values())


# ── Output Helpers ────────────────────────────────────────────────────────────

def is_tty():
    return sys.stdout.isatty()


def output_json(data):
    print(json.dumps(data, indent=2, default=str))


def output_table(headers, rows):
    """Print a simple aligned table for terminal output."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))

    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*[str(v) for v in row]))


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_list(args):
    """List connected Poly devices."""
    devices = discover_devices()

    if not devices:
        if args.json:
            output_json({"devices": [], "count": 0})
        else:
            print("No Poly devices found.")
        return 0

    if args.json:
        output_json({
            "devices": [{
                "pid": d["pid_hex"],
                "name": d["friendly_name"],
                "serial": d["serial"],
                "firmware": f"0x{d['release_number']:04X}",
                "family": d["family"],
                "dfu_executor": d["dfu_executor"],
            } for d in devices],
            "count": len(devices),
        })
    else:
        rows = []
        for d in devices:
            fw = d["release_number"]
            # BCD decode
            digits = "".join(str((fw >> s) & 0xF) for s in (12, 8, 4, 0)).lstrip("0") or "0"
            fw_str = digits[:-2] + "." + digits[-2:] if len(digits) > 2 else "0." + digits.zfill(2)
            rows.append([
                d["pid_hex"],
                d["friendly_name"][:30],
                d["serial"][:16] or "—",
                fw_str,
                d["family"],
            ])
        output_table(["PID", "Device", "Serial", "Firmware", "Family"], rows)

    return 0


def cmd_get(args):
    """Read a setting from connected devices."""
    devices = discover_devices()
    if args.pid:
        pid_val = int(args.pid, 16) if args.pid.startswith("0x") else int(args.pid)
        devices = [d for d in devices if d["pid"] == pid_val]

    if not devices:
        print("No matching devices found.", file=sys.stderr)
        return 1

    setting_name = args.setting
    results = []

    for dev in devices:
        settings = read_all_settings(dev["path"], dev["usage_page"], dev["dfu_executor"])
        value = None
        for s in settings:
            if s["name"].lower() == setting_name.lower():
                value = s["value"]
                break

        result = {
            "device": dev["friendly_name"],
            "pid": dev["pid_hex"],
            "serial": dev["serial"],
            "setting": setting_name,
            "value": value,
        }
        results.append(result)

        if not args.json:
            status = f"{value}" if value is not None else "not supported"
            print(f"  {dev['friendly_name']:<30} {setting_name} = {status}")

        logger.info(f"GET {dev['pid_hex']} {dev['serial'][:16]} {setting_name}={value}")

    if args.json:
        output_json({"results": results})

    return 0


def cmd_set(args):
    """Write a setting to connected devices."""
    devices = discover_devices()
    if args.pid:
        pid_val = int(args.pid, 16) if args.pid.startswith("0x") else int(args.pid)
        devices = [d for d in devices if d["pid"] == pid_val]

    if not devices:
        print("No matching devices found.", file=sys.stderr)
        return 1

    setting_name = args.setting
    raw_value = args.value

    # Parse value based on setting type
    sdef = None
    for s in SETTINGS_DB:
        if s["name"].lower() == setting_name.lower():
            sdef = s
            setting_name = s["name"]  # normalize case
            break

    if not sdef:
        print(f"Unknown setting: {setting_name}", file=sys.stderr)
        print(f"Available settings:", file=sys.stderr)
        for s in SETTINGS_DB:
            print(f"  {s['name']}", file=sys.stderr)
        return 1

    # Convert value to correct type
    if sdef["type"] == "bool":
        value = raw_value.lower() in ("true", "1", "yes", "on")
    elif sdef["type"] == "range":
        try:
            value = int(raw_value)
        except ValueError:
            value = float(raw_value)
    elif sdef["type"] == "choice":
        # Case-insensitive match
        choices = sdef.get("choices", [])
        matched = None
        for c in choices:
            if c.lower() == raw_value.lower():
                matched = c
                break
        if not matched:
            print(f"Invalid value '{raw_value}' for {setting_name}.", file=sys.stderr)
            print(f"Choices: {', '.join(choices)}", file=sys.stderr)
            return 1
        value = matched
    else:
        value = raw_value

    results = []
    exit_code = 0

    for dev in devices:
        success = write_setting(dev["path"], dev["usage_page"],
                                dev["dfu_executor"], setting_name, value)
        result = {
            "device": dev["friendly_name"],
            "pid": dev["pid_hex"],
            "serial": dev["serial"],
            "setting": setting_name,
            "value": value,
            "success": success,
        }
        results.append(result)

        if not args.json:
            status = "OK" if success else "FAILED"
            print(f"  {dev['friendly_name']:<30} {setting_name} = {value} [{status}]")

        if success:
            logger.info(f"SET {dev['pid_hex']} {dev['serial'][:16]} {setting_name}={value} OK")
        else:
            logger.warning(f"SET {dev['pid_hex']} {dev['serial'][:16]} {setting_name}={value} FAILED")
            exit_code = 1

    if args.json:
        output_json({"results": results, "success": exit_code == 0})

    return exit_code


def cmd_dump(args):
    """Dump all readable settings for connected devices."""
    devices = discover_devices()
    if args.pid:
        pid_val = int(args.pid, 16) if args.pid.startswith("0x") else int(args.pid)
        devices = [d for d in devices if d["pid"] == pid_val]

    if not devices:
        print("No matching devices found.", file=sys.stderr)
        return 1

    all_results = []

    for dev in devices:
        settings = read_all_settings(dev["path"], dev["usage_page"], dev["dfu_executor"])

        if args.json:
            all_results.append({
                "device": dev["friendly_name"],
                "pid": dev["pid_hex"],
                "serial": dev["serial"],
                "family": dev["family"],
                "settings": settings,
            })
        else:
            print(f"\n  {dev['friendly_name']} ({dev['pid_hex']})")
            print(f"  {'─' * 50}")
            if not settings:
                print(f"  No readable settings (family: {dev['family']})")
            for s in settings:
                val = s.get("value", "—")
                writable = "" if s.get("writable", True) else " [read-only]"
                print(f"    {s['name']:<30} = {val}{writable}")

    if args.json:
        output_json({"devices": all_results})

    return 0


def cmd_batch(args):
    """Apply multiple settings from a JSON file.

    File format:
    {
        "settings": [
            {"name": "Sidetone Level", "value": 5},
            {"name": "Ringtone Volume", "value": 8},
            {"name": "Anti-Startle Protection", "value": true}
        ]
    }

    Optional: add "pid": "0xC056" to target specific devices.
    """
    try:
        batch = json.loads(Path(args.file).read_text())
    except Exception as e:
        print(f"Error reading batch file: {e}", file=sys.stderr)
        return 1

    settings_list = batch.get("settings", [])
    if not settings_list:
        print("No settings in batch file.", file=sys.stderr)
        return 1

    target_pid = batch.get("pid")
    devices = discover_devices()
    if target_pid:
        pid_val = int(target_pid, 16) if target_pid.startswith("0x") else int(target_pid)
        devices = [d for d in devices if d["pid"] == pid_val]

    if not devices:
        print("No matching devices found.", file=sys.stderr)
        return 1

    results = []
    total = 0
    success_count = 0

    for dev in devices:
        if not args.json:
            print(f"\n  {dev['friendly_name']} ({dev['pid_hex']})")

        for setting in settings_list:
            name = setting.get("name", "")
            value = setting.get("value")
            if not name:
                continue

            total += 1
            ok = write_setting(dev["path"], dev["usage_page"],
                               dev["dfu_executor"], name, value)

            results.append({
                "device": dev["friendly_name"],
                "pid": dev["pid_hex"],
                "setting": name,
                "value": value,
                "success": ok,
            })

            if ok:
                success_count += 1

            if not args.json:
                status = "OK" if ok else "FAILED"
                print(f"    {name:<30} = {value} [{status}]")

            logger.info(f"BATCH {dev['pid_hex']} {name}={value} {'OK' if ok else 'FAILED'}")

    if args.json:
        output_json({
            "results": results,
            "total": total,
            "success": success_count,
            "failed": total - success_count,
        })
    else:
        print(f"\n  Done: {success_count}/{total} settings applied successfully.")

    return 0 if success_count == total else 1


def cmd_settings(args):
    """List available settings. Shows per-device when devices are connected."""
    devices = discover_devices()
    if args.pid:
        pid_val = int(args.pid, 16) if args.pid.startswith("0x") else int(args.pid)
        devices = [d for d in devices if d["pid"] == pid_val]

    if devices:
        # Show settings per connected device
        all_results = []
        for dev in devices:
            supported = get_settings_for_device(dev["usage_page"], dev["dfu_executor"])
            if args.json:
                all_results.append({
                    "device": dev["friendly_name"],
                    "pid": dev["pid_hex"],
                    "family": dev["family"],
                    "settings": supported,
                })
            else:
                print(f"\n  {dev['friendly_name']} ({dev['pid_hex']}) — {dev['family']}")
                print(f"  {'─' * 50}")
                if not supported:
                    print(f"    No configurable settings for this device family")
                for s in supported:
                    if s["type"] == "range":
                        detail = f"range {s.get('min', 0)}-{s.get('max', 10)}"
                    elif s["type"] == "bool":
                        detail = "true / false"
                    elif s["type"] == "choice":
                        detail = " | ".join(s.get("choices", [])[:4])
                        if len(s.get("choices", [])) > 4:
                            detail += " | ..."
                    else:
                        detail = s["type"]
                    print(f"    {s['name']:<30} {detail}")
        if args.json:
            output_json({"devices": all_results})
    else:
        # No devices — show full settings database
        if args.json:
            output_json({"settings": SETTINGS_DB})
        else:
            print(f"\n  All Settings ({len(SETTINGS_DB)}):")
            print(f"  (No devices connected — showing all families)\n")
            for s in SETTINGS_DB:
                families = ", ".join(s["families"])
                if s["type"] == "range":
                    detail = f"range {s.get('min', 0)}-{s.get('max', 10)}"
                elif s["type"] == "bool":
                    detail = "true / false"
                elif s["type"] == "choice":
                    detail = " | ".join(s.get("choices", [])[:4])
                    if len(s.get("choices", [])) > 4:
                        detail += " | ..."
                else:
                    detail = s["type"]
                print(f"    {s['name']:<30} {detail:<30} [{families}]")
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="polyremote",
        description="PolyRemote — Remote headset configuration for managed environments",
    )
    parser.add_argument("--version", action="version", version=f"PolyRemote {VERSION}")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON (for automation)")
    parser.add_argument("--pid", type=str, default=None,
                        help="Target specific device by PID (e.g. 0xC056)")

    sub = parser.add_subparsers(dest="command")

    # Add --json and --pid to each subcommand too (so order doesn't matter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Output as JSON")
    common.add_argument("--pid", type=str, default=None, help="Target device PID")

    sub.add_parser("list", help="List connected Poly devices", parents=[common])

    get_p = sub.add_parser("get", help="Read a setting", parents=[common])
    get_p.add_argument("setting", help="Setting name (e.g. 'Sidetone Level')")

    set_p = sub.add_parser("set", help="Write a setting", parents=[common])
    set_p.add_argument("setting", help="Setting name")
    set_p.add_argument("value", help="New value")

    sub.add_parser("dump", help="Dump all settings for connected devices", parents=[common])

    batch_p = sub.add_parser("batch", help="Apply settings from JSON file", parents=[common])
    batch_p.add_argument("file", help="Path to settings JSON file")

    sub.add_parser("settings", help="List all available setting names", parents=[common])

    args = parser.parse_args()

    # Auto-detect JSON mode when piped
    if not sys.stdout.isatty():
        args.json = True

    if not args.command:
        parser.print_help()
        return 0

    commands = {
        "list": cmd_list,
        "get": cmd_get,
        "set": cmd_set,
        "dump": cmd_dump,
        "batch": cmd_batch,
        "settings": cmd_settings,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
