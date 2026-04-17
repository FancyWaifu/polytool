#!/usr/bin/env python3
"""
PolyTool — CLI commands (scan, info, battery, updates, update, monitor, catalog, fwinfo).

Split from polytool.py for modularity. All public names are re-exported
by polytool.py for backward compatibility.
"""

import os
import time
from pathlib import Path

from devices import (
    PolyDevice, Output, out,
    # Constants
    FIRMWARE_CACHE, DFU_TRANSPORT_INFO,
    # Functions
    discover_devices, try_read_device_info, try_read_battery,
    _normalize_version,
    # Optional deps
    HAS_RICH,
)

from firmware import (
    PolyCloudAPI, FirmwareUpdater,
    parse_firmware_file, parse_firmware_package,
    _format_size, _display_fw_file_info,
)

# Rich imports (used directly in CLI commands for table rendering)
try:
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
except ImportError:
    pass


# ── CLI Commands ─────────────────────────────────────────────────────────────

def cmd_scan(args):
    """Discover all connected Poly devices."""
    out.header("PolyTool - Device Scanner")
    devices = discover_devices()
    out.device_table(devices)
    _warn_on_ff_setid(devices)
    return devices


def _warn_on_ff_setid(devices):
    """Cross-check discovered devices against LCS's cache for FFFF SetID problem.

    LCS keeps device_PLT_<serial> JSON files updated with each device attach.
    If FirmwareVersion or setid in any of those files reads as FFs, the
    headset's NVRAM SetID region is unprogrammed — flag it and suggest a fix.
    """
    try:
        from setid_fix import read_lcs_device_cache, diagnose_setid
    except Exception:
        return  # not on Windows or import failed; silently skip
    cache = read_lcs_device_cache()
    if not cache:
        return  # Poly Lens not installed or no cache yet
    issues = []
    for dev in devices:
        if not dev.serial:
            continue
        d = diagnose_setid(dev.serial, cache=cache)
        if d["state"] == "ff":
            issues.append((dev, d))
    if not issues:
        return
    out.print("")
    out.warn(f"  {len(issues)} device(s) with unprogrammed SetID NVRAM (FFFFs):")
    for dev, d in issues:
        name = dev.friendly_name or dev.product_name or "device"
        out.print(f"    - {name} ({dev.vid_hex}:{dev.pid_hex})  "
                  f"FirmwareVersion={d['firmware_version']!r}")
    out.print("    Fix:  polytool fix-setid       (fast, ~10 sec)")
    out.print("          polytool update-legacy   (full firmware update, ~10 min, also fixes it)")


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
