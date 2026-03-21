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
import sys

# ── Re-export all public names for backward compatibility ────────────────────
# Every `from polytool import X` that existed before the split MUST still work.

from devices import (
    # Constants
    VERSION, POLY_VIDS, VENDOR_USAGE_PAGES, CLOUD_GRAPHQL, FIRMWARE_CDN,
    CONFIG_DIR, FIRMWARE_CACHE,
    # Product database
    CODENAME_MAP, PID_CODENAMES, DFU_EXECUTOR_MAP, DFU_TRANSPORT_INFO,
    DEVICE_CATEGORIES,
    # Functions
    _normalize_version, classify_device, discover_devices,
    _bus_type_str, _deduplicate_devices,
    _open_hid, try_read_battery, try_read_device_info, _try_cx2070x_serial,
    check_dependencies,
    # Classes & singletons
    PolyDevice, Output, out,
    # Optional dep flags
    HAS_RICH,
)

from firmware import (
    # Cloud API
    PolyCloudAPI,
    # Firmware parsing
    FIRMWARE_FORMATS, detect_firmware_format,
    parse_fwu_header, parse_firmware_container, parse_csr_dfu, parse_appuhdr5,
    parse_firmware_file, parse_firmware_package,
    # BladeRunner DFU
    BRMessageType, BRFTResponse, BladeRunnerDFU,
    # Firmware updater
    FirmwareUpdater,
    # Helpers
    _format_size, _display_fw_file_info,
)

from scanner import (
    cmd_scan, cmd_info, cmd_battery, cmd_updates, cmd_update,
    cmd_monitor, cmd_catalog, cmd_fwinfo,
    _select_devices,
)


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
