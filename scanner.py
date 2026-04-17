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
    """Cross-check discovered devices against LCS's cache for two known
    Poly Studio display bugs:

      1. FFFF SetID NVRAM: device shows ffff.ffff.ffff.ffff as firmware
         version. Fixable via `polytool fix-setid`.
      2. Empty FirmwareVersion despite populated FirmwareComponents: a Poly
         LCS bug introduced when newer Savi firmware (10.82+) dropped the
         setId field from firmwareVersion. LCS doesn't roll up per-component
         versions into FirmwareVersion, so Poly Studio shows blank. NOT
         fixable on the device — Poly's Devices.dll needs to compute the
         fallback. Surfaced here so the user understands why Studio is blank.
    """
    try:
        from setid_fix import read_lcs_device_cache, diagnose_setid
    except Exception:
        return
    cache = read_lcs_device_cache()
    if not cache:
        return
    ff_issues = []
    blank_issues = []
    for dev in devices:
        if not dev.serial:
            continue
        d = diagnose_setid(dev.serial, cache=cache)
        if d["state"] == "ff":
            ff_issues.append((dev, d))
            continue
        # New-schema bug: cache has components but no top-level FirmwareVersion
        rec = cache.get(dev.serial, {})
        comps = rec.get("FirmwareComponents") or {}
        has_real_components = any(
            isinstance(v, str) and v and v.lower() not in ("ffff", "ffffffff")
            for v in comps.values()
        )
        if has_real_components and not (rec.get("FirmwareVersion") or "").strip():
            blank_issues.append(dev)

    def _yellow(msg):
        # Use stdout (out.print) instead of out.warn so warning text doesn't
        # leapfrog the device table on systems without rich (stderr is
        # unbuffered, stdout line-buffered).
        if HAS_RICH:
            out.print(f"[bold yellow]Warning:[/] {msg}")
        else:
            out.print(f"Warning: {msg}")

    if ff_issues:
        out.print("")
        _yellow(f"{len(ff_issues)} device(s) with unprogrammed SetID NVRAM (FFFFs):")
        for dev, d in ff_issues:
            out.print(f"    - {dev.display_name} ({dev.vid_hex}:{dev.pid_hex})  "
                      f"FirmwareVersion={d['firmware_version']!r}")
        out.print("    Fix:  polytool fix-setid       (fast, ~10 sec)")
        out.print("          polytool update-legacy   (full firmware update, ~10 min, also fixes it)")

    if blank_issues:
        # If our lensserver service is up, Poly Studio is reading from us
        # (not LCS) and the synthesis is already happening — the cache-file
        # blank is irrelevant. Only warn when LCS is the live source.
        try:
            from service import _probe_lensserver_running
            lensserver_active = _probe_lensserver_running()
        except Exception:
            lensserver_active = False

        if lensserver_active:
            out.print("")
            out.print("  (Note: 2 device(s) have blank LCS FirmwareVersion, but lensserver is")
            out.print("   active and synthesizing it for Poly Studio - display will be correct.)")
        else:
            out.print("")
            _yellow(f"{len(blank_issues)} device(s) where Poly Studio shows blank firmware version:")
            for dev in blank_issues:
                comp = dev.firmware_components_display or "n/a"
                out.print(f"    - {dev.display_name} ({dev.vid_hex}:{dev.pid_hex})  actual: {comp}")
            out.print("    Cause: Poly LCS bug (Devices.dll). New Savi firmware reports per-component")
            out.print("           versions but no top-level FirmwareVersion, and LCS doesn't roll them up.")
            out.print("    Fix:   polytool install-service   (auto-starts lensserver MITM at every logon)")
            out.print("           polytool service-start     (start it now without re-logging in)")


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

        comp_str = dev.firmware_components_display or "n/a"
        handlers_str = (
            ", ".join(f"{k}={v}" for k, v in dev.dfu_handlers.items())
            if dev.dfu_handlers else "n/a"
        )
        os_str = ", ".join(dev.supported_os) if dev.supported_os else "n/a"
        recovery_str = (
            f"YES (recovery PID for 0x{dev.recovery_for_pid.upper()})"
            if dev.is_in_recovery else "no"
        )

        if HAS_RICH:
            info_lines = [
                f"[bold]Product:[/]        {dev.display_name}",
                f"[bold]Model:[/]          {dev.model_description or 'n/a'}",
                f"[bold]Manufacturer:[/]   {dev.manufacturer}",
                f"[bold]Serial:[/]         {dev.serial or 'n/a'}",
                f"[bold]Tattoo:[/]         {dev.tattoo_serial or 'n/a'}",
                f"[bold]Firmware:[/]       {dev.firmware_display}",
                f"[bold]Components:[/]     {comp_str}",
                f"[bold]Category:[/]       {dev.category}",
                f"[bold]VID:PID:[/]        {dev.vid_hex}:{dev.pid_hex}",
                f"[bold]USB/BT:[/]         {dev.bus_type}",
                f"[bold]Usage Page:[/]     0x{dev.usage_page:04X}",
                f"[bold]Battery:[/]        {dev.battery_display}",
                f"[bold]Codename:[/]       {dev.codename or 'n/a'}",
                f"[bold]LensProductID:[/]  {dev.lens_product_id}",
                f"[bold]DFU Executor:[/]   {dev.dfu_executor or 'n/a'}",
                f"[bold]DFU Handlers:[/]   {handlers_str}",
                f"[bold]DFU Transport:[/]  {transport_str}",
                f"[bold]FW Format:[/]      {fw_format_str}",
                f"[bold]Update Support:[/] {platform_str}",
                f"[bold]Supported OS:[/]   {os_str}",
                f"[bold]Recovery Mode:[/]  {recovery_str}",
            ]
            if dev.is_muted:
                info_lines.append("[bold]Muted:[/]          Yes")
            if dev.is_on_head:
                info_lines.append("[bold]On Head:[/]        Yes")

            out.console.print(Panel(
                "\n".join(info_lines),
                title=dev.display_name,
                border_style="cyan",
                expand=False,
            ))
        else:
            print(f"\n{'='*60}")
            print(f"  {dev.display_name}")
            print(f"{'='*60}")
            print(f"  Model:         {dev.model_description or 'n/a'}")
            print(f"  Manufacturer:  {dev.manufacturer}")
            print(f"  Serial:        {dev.serial or 'n/a'}")
            print(f"  Tattoo:        {dev.tattoo_serial or 'n/a'}")
            print(f"  Firmware:      {dev.firmware_display}")
            print(f"  Components:    {comp_str}")
            print(f"  Category:      {dev.category}")
            print(f"  VID:PID:       {dev.vid_hex}:{dev.pid_hex}")
            print(f"  USB/BT:        {dev.bus_type}")
            print(f"  Battery:       {dev.battery_display}")
            print(f"  Codename:      {dev.codename or 'n/a'}")
            print(f"  DFU Executor:  {dev.dfu_executor or 'n/a'}")
            print(f"  DFU Handlers:  {handlers_str}")
            print(f"  DFU Transport: {transport_str}")
            print(f"  FW Format:     {fw_format_str}")
            print(f"  Update Support:{platform_str}")
            print(f"  Supported OS:  {os_str}")
            print(f"  Recovery Mode: {recovery_str}")


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
    """Download and apply firmware updates - same code path Poly Studio uses.

    For each target device:
      1. Query Poly cloud catalog for the latest firmware bundle
      2. Compare bundle version vs device's current usb component
      3. If newer (or --force), prompt for confirmation (unless --yes)
      4. Download the bundle, run install_bundle() -> LegacyDfu pipeline

    install_bundle is the same function lensserver's on_schedule_dfu and
    polytool fix-setid use. Auto-isolates sibling same-PID devices so the
    DFU lands on the unit you specified.
    """
    from setid_fix import install_bundle
    from devices import _normalize_version

    out.header("PolyTool - Firmware Updater")

    devices = discover_devices()
    if not devices:
        out.warn("No Poly devices found.")
        return

    cloud = PolyCloudAPI()
    targets = _select_devices(devices, args.device)
    if not targets:
        return

    # Build the update list first so the user sees the full plan before
    # we start hitting any device.
    plan = []
    for dev in targets:
        try_read_device_info(dev)
        try_read_battery(dev)
        info = cloud.check_firmware(dev)
        if not info or not info.get("download_url"):
            plan.append((dev, None, "no firmware in cloud catalog"))
            continue
        current_n = _normalize_version(dev.firmware_display)
        latest_n = _normalize_version(info.get("latest", ""))
        if current_n >= latest_n and not getattr(args, "force", False):
            plan.append((dev, info, f"already up to date (v{dev.firmware_display})"))
            continue
        plan.append((dev, info, "update available"))

    # Show the plan
    out.print("")
    has_updates = False
    for dev, info, status in plan:
        latest = (info or {}).get("latest", "?")
        if "update available" in status:
            has_updates = True
            out.print(f"  [bold yellow]Update Available[/]  {dev.display_name}  "
                      f"v{dev.firmware_display} -> v{latest}" if HAS_RICH
                      else f"  Update Available  {dev.display_name}  "
                           f"v{dev.firmware_display} -> v{latest}")
        else:
            out.print(f"  -                 {dev.display_name}  ({status})")

    if not has_updates:
        out.success("\nAll selected devices are already up to date.")
        return

    # Apply updates - prompt per device unless --yes
    for dev, info, status in plan:
        if "update available" not in status:
            continue
        out.print(f"\n{'='*60}")
        out.print(f"  Updating: {dev.display_name}")
        out.print(f"  Serial:   {dev.serial}")
        out.print(f"  Current:  v{dev.firmware_display}")
        out.print(f"  Latest:   v{info.get('latest', '?')}")
        if info.get("release_notes"):
            notes = info["release_notes"][:300].replace("\n", "\n    ")
            out.print(f"  Notes:    {notes}")

        if not getattr(args, "yes", False):
            ans = input(f"\n  Press Enter to install (or n to skip): ").strip().lower()
            if ans == "n":
                out.print("  skipped")
                continue

        # Download
        out.print("  Downloading bundle...")
        path = cloud.download_firmware(info["download_url"])
        if not path:
            out.error("  Download failed")
            continue
        out.print(f"  Bundle: {path}")

        # Install via the same pipeline Studio uses
        out.print("  Installing (this can take several minutes; don't unplug)...")
        result = install_bundle(
            zip_path=path,
            vid=dev.vid, pid=dev.pid, serial=dev.serial,
            isolate_siblings=getattr(args, "isolate", True),
            log=lambda s: out.print(f"  {s}"),
        )
        if result["success"]:
            out.success(f"  OK: {result['message']}")
        else:
            out.error(f"  FAILED: {result['message']}")
            if getattr(args, "verbose", False) and result.get("output"):
                out.print(result["output"])


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

    sel_low = selector.lower()

    # Try as serial prefix
    matches = [d for d in devices if d.serial and d.serial.lower().startswith(sel_low)]
    if matches:
        return matches

    # Try as tattoo serial (the value printed on the device label)
    matches = [d for d in devices if d.tattoo_serial and sel_low in d.tattoo_serial.lower()]
    if matches:
        return matches

    # Try as product name substring (also matches the disambiguator suffix)
    matches = [d for d in devices if sel_low in d.display_name.lower()]
    if matches:
        return matches

    out.error(f"No device matching '{selector}'. Use a number, serial prefix, tattoo, or device name.")
    return []
