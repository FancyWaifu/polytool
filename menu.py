#!/usr/bin/env python3
"""
PolyTool Interactive Menu — Single entry point for all Poly device tools.

Usage: python3 menu.py
"""

import os
import sys
import subprocess
import shutil
import time
from pathlib import Path

# ── ANSI helpers ──────────────────────────────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RED = "\033[31m"
MAGENTA = "\033[35m"
RESET = "\033[0m"
CLEAR = "\033[2J\033[H"


def _termwidth():
    return shutil.get_terminal_size((60, 24)).columns


def clear():
    print(CLEAR, end="")


def banner():
    print(f"""
{CYAN}{BOLD}  ╔══════════════════════════════════════════════════════════╗
  ║                  PolyTool  v2.0                        ║
  ║        Poly/Plantronics Headset Toolkit                ║
  ╚══════════════════════════════════════════════════════════╝{RESET}
""")


def ruler():
    w = min(_termwidth(), 60)
    print(f"\n  {DIM}{'━' * w}{RESET}\n")


def section(title):
    print(f"  {BOLD}{title}{RESET}")
    print(f"  {DIM}{'─' * 54}{RESET}")


def menu_item(key, label, desc=""):
    k = f"{YELLOW}{BOLD}{key:>4s}{RESET}"
    d = f"  {DIM}{desc}{RESET}" if desc else ""
    print(f"  {k})  {label}{d}")


def info(msg):
    print(f"\n  {GREEN}{BOLD}>{RESET} {msg}")


def warn(msg):
    print(f"\n  {YELLOW}{BOLD}!{RESET} {msg}")


def error(msg):
    print(f"\n  {RED}{BOLD}x{RESET} {msg}")


def prompt(msg="Select"):
    try:
        return input(f"\n  {CYAN}{msg}>{RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        return None


def pause():
    """Wait for Enter. The output stays on screen so user can scroll up."""
    ruler()
    try:
        input(f"  {DIM}Press Enter to return to menu...{RESET}")
    except (EOFError, KeyboardInterrupt):
        pass


# ── Tool runners ──────────────────────────────────────────────────────────────

TOOL_DIR = Path(__file__).parent


def run_polytool(*args):
    """Run polytool.py with the given arguments."""
    cmd = [sys.executable, str(TOOL_DIR / "polytool.py")] + list(args)
    subprocess.run(cmd)


def run_probe(*args):
    """Run the FWU probe interactively."""
    cmd = [sys.executable, "-m", "probes.fwu_probe"] + list(args)
    subprocess.run(cmd, cwd=str(TOOL_DIR))


def run_monitor():
    """Run monitor_legacyhost.py."""
    cmd = [sys.executable, str(TOOL_DIR / "monitor_legacyhost.py")]
    subprocess.run(cmd)


# ── Menus ─────────────────────────────────────────────────────────────────────

def menu_devices():
    """Device management submenu."""
    while True:
        clear()
        banner()
        section("Devices")
        print()
        menu_item("1", "Scan devices",       "Find all connected Poly devices")
        menu_item("2", "Device info",         "Detailed info for a specific device")
        menu_item("3", "Battery status",      "Show battery levels")
        menu_item("4", "Live monitor",        "Auto-refreshing device dashboard")
        print()
        menu_item("b", "Back")

        choice = prompt()
        if choice is None or choice in ('b', 'q'):
            return

        print()
        if choice == '1':
            run_polytool("scan")
        elif choice == '2':
            dev = prompt("Device # or name (Enter=all)")
            if dev is None:
                continue
            run_polytool("info", dev or "all")
        elif choice == '3':
            run_polytool("battery")
        elif choice == '4':
            info("Starting live monitor (Ctrl+C to stop)...")
            run_polytool("monitor")
        else:
            warn(f"Invalid choice: {choice}")
            time.sleep(1)
            continue

        pause()


def menu_firmware():
    """Firmware management submenu."""
    while True:
        clear()
        banner()
        section("Firmware")
        print()
        menu_item("1", "Check for updates",   "See if newer firmware is available")
        menu_item("2", "Download & flash",     "Update device firmware")
        menu_item("3", "Force update",         "Flash even if version matches")
        menu_item("4", "Search catalog",       "Browse all Poly firmware online")
        menu_item("5", "Analyze firmware",     "Parse a downloaded firmware file")
        print()
        menu_item("b", "Back")

        choice = prompt()
        if choice is None or choice in ('b', 'q'):
            return

        print()
        if choice == '1':
            dev = prompt("Device # or name (Enter=all)")
            if dev is None:
                continue
            run_polytool("updates", dev or "all")
        elif choice == '2':
            dev = prompt("Device # or name (Enter=all)")
            if dev is None:
                continue
            run_polytool("update", dev or "all")
        elif choice == '3':
            dev = prompt("Device # or name (Enter=all)")
            if dev is None:
                continue
            run_polytool("update", "--force", dev or "all")
        elif choice == '4':
            search = prompt("Search term (Enter=show all)")
            if search is None:
                continue
            if search:
                run_polytool("catalog", search)
            else:
                run_polytool("catalog")
        elif choice == '5':
            path = prompt("Path to firmware file/zip")
            if not path:
                continue
            run_polytool("fwinfo", path)
        else:
            warn(f"Invalid choice: {choice}")
            time.sleep(1)
            continue

        pause()


def menu_debug():
    """Debug/probe tools submenu."""
    while True:
        clear()
        banner()
        section("Debug & Protocol Probes")
        print()
        menu_item("1", "Protocol probe menu",  "Interactive FWU/HID probe tool")
        menu_item("2", "Quick scan",           "Read-only feature report scan")
        menu_item("3", "Log monitor",          "Tail Poly Lens logs for HID/IPC")
        print()
        section("Quick Probes")
        print()
        menu_item("4", "Sign-on test",         "RID 13 sign-on protocol")
        menu_item("5", "FWU enable test",      "Correct LE byte-order FWU")
        menu_item("6", "BladeRunner probe",    "BR protocol handshake")
        menu_item("7", "Enumerate interfaces",  "List all HID interfaces")
        menu_item("8", "Multi-interface test",  "Listen on all interfaces at once")
        print()
        menu_item("b", "Back")

        choice = prompt()
        if choice is None or choice in ('b', 'q'):
            return

        print()
        if choice == '1':
            run_probe()
        elif choice == '2':
            run_probe("--test", "passive")
        elif choice == '3':
            info("Starting log monitor (Ctrl+C to stop)...")
            run_monitor()
        elif choice == '4':
            run_probe("--test", "signon", "--kill")
        elif choice == '5':
            run_probe("--test", "correct", "--kill")
        elif choice == '6':
            run_probe("--test", "bladerunner")
        elif choice == '7':
            run_probe("--test", "enumerate")
        elif choice == '8':
            run_probe("--test", "multi")
        else:
            warn(f"Invalid choice: {choice}")
            time.sleep(1)
            continue

        pause()


def main_menu():
    """Top-level menu."""
    while True:
        clear()
        banner()

        section("Main Menu")
        print()
        menu_item("1", "Devices",              "Scan, info, battery, monitor")
        menu_item("2", "Firmware",              "Check updates, flash, catalog")
        menu_item("3", "Debug & Probes",        "HID protocol tools")
        print()

        section("Quick Actions")
        print()
        menu_item("s", "Quick scan",           "Find connected devices now")
        menu_item("b", "Battery",              "Check battery levels")
        menu_item("u", "Check updates",        "See if firmware is available")
        print()
        menu_item("q", "Quit")

        choice = prompt()
        if choice is None or choice == 'q':
            clear()
            print(f"\n  {DIM}Goodbye!{RESET}\n")
            return

        if choice == '1':
            menu_devices()
        elif choice == '2':
            menu_firmware()
        elif choice == '3':
            menu_debug()
        elif choice == 's':
            print()
            run_polytool("scan")
            pause()
        elif choice == 'b':
            print()
            run_polytool("battery")
            pause()
        elif choice == 'u':
            print()
            run_polytool("updates")
            pause()
        else:
            warn(f"Invalid choice: {choice}")
            time.sleep(1)


if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print(f"\n\n  {DIM}Goodbye!{RESET}\n")
