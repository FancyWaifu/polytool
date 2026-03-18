#!/usr/bin/env python3
"""
PolyAgent — Workstation agent for centralized headset management.

Runs on each workstation, discovers connected Poly headsets, reports
inventory to the PolyServer, and executes remote commands (settings
changes, firmware updates).

Usage:
  python3 polyagent.py --server http://server:8421
  python3 polyagent.py --server http://server:8421 --interval 30
  python3 polyagent.py --server http://server:8421 --once    # Single report, then exit

Environment:
  POLYSERVER_URL=http://server:8421    # Alternative to --server flag
"""

import json
import os
import sys
import time
import socket
import hashlib
import platform
import argparse
import logging
from pathlib import Path
from datetime import datetime

try:
    import requests
except ImportError:
    print("Error: requests required. Install with: pip install requests")
    sys.exit(1)

try:
    import hid
except ImportError:
    print("Error: hidapi required. Install with: pip install hidapi")
    sys.exit(1)

# ── Setup ─────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
VENDOR_USAGE_PAGES = {0xFFA0, 0xFFA2, 0xFF52, 0xFF58, 0xFF99}

LOG_DIR = Path.home() / ".polytool" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_DIR / "polyagent.log")),
    ]
)
log = logging.getLogger("polyagent")


def get_agent_id():
    """Generate a stable agent ID from machine identity."""
    hostname = socket.gethostname()
    mac = hex(os.getuid()) if hasattr(os, 'getuid') else "0"
    raw = f"{hostname}-{platform.node()}-{mac}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Device Discovery ─────────────────────────────────────────────────────

def discover_devices():
    """Find all connected Poly devices."""
    seen = {}
    for d in hid.enumerate():
        vid = d.get("vendor_id", 0)
        if vid not in POLY_VIDS:
            continue
        pid = d.get("product_id", 0)
        serial = d.get("serial_number", "") or ""
        usage_page = d.get("usage_page", 0)

        key = (pid, serial) if serial else (pid, d.get("interface_number", 0))
        existing = seen.get(key)
        if existing:
            if usage_page in VENDOR_USAGE_PAGES and existing["usage_page"] not in VENDOR_USAGE_PAGES:
                pass
            elif existing["usage_page"] in VENDOR_USAGE_PAGES:
                continue
            else:
                continue

        # BCD firmware decode
        rn = d.get("release_number", 0)
        if rn > 0:
            digits = "".join(str((rn >> s) & 0xF) for s in (12, 8, 4, 0)).lstrip("0") or "0"
            fw = digits[:-2] + "." + digits[-2:] if len(digits) > 2 else "0." + digits.zfill(2)
        else:
            fw = "unknown"

        # Try to determine family/executor
        try:
            from polytool import DFU_EXECUTOR_MAP, PID_CODENAMES, CODENAME_MAP, DEVICE_CATEGORIES
            pid_hex = f"{pid:x}"
            dfu = DFU_EXECUTOR_MAP.get(pid_hex, "")
            codename = PID_CODENAMES.get(pid, "")
            friendly = CODENAME_MAP.get(codename, "") or d.get("product_string", "")

            # Category
            search = f"{friendly} {codename} {d.get('product_string', '')}".lower()
            category = "other"
            for cat, keywords in DEVICE_CATEGORIES.items():
                if any(kw.lower() in search for kw in keywords):
                    category = cat
                    break
        except ImportError:
            dfu = ""
            friendly = d.get("product_string", "")
            category = "unknown"

        # Family
        if usage_page == 0xFFA0:
            family = "cx2070x"
        elif usage_page == 0xFFA2:
            family = "dect"
        elif dfu in ("HidTiDfu", "SyncDfu", "StudioDfu"):
            family = "bladerunner"
        else:
            family = "unknown"

        seen[key] = {
            "pid": f"{pid:04x}",
            "pid_hex": f"0x{pid:04X}",
            "serial": serial,
            "product_name": d.get("product_string", ""),
            "friendly_name": friendly,
            "firmware": fw,
            "category": category,
            "dfu_executor": dfu,
            "family": family,
            "usage_page": usage_page,
            "battery_level": -1,
        }

    # Try native bridge for BT/DECT devices
    try:
        from native_bridge import NativeBridge, find_components_dir
        if find_components_dir():
            bridge = NativeBridge()
            bridge.start()
            import time as _time
            # Wait for devices
            for _ in range(20):
                _time.sleep(0.5)
                bridge.recv(timeout=0.1)
                if bridge.get_devices():
                    break
            # Wait a bit more for BT devices
            _time.sleep(3)
            bridge.recv(timeout=0.5)

            for nid, ndev in bridge.get_devices().items():
                pid = ndev.get("pid", 0)
                # Skip if already found via USB
                if any(int(d["pid"], 16) == pid for d in seen.values()):
                    continue
                name = ndev.get("name", "")
                fw = ndev.get("firmwareVersion", {})
                fw_str = fw.get("bluetooth", "") or fw.get("usb", "") or fw.get("headset", "")
                # Get battery
                batt = bridge.get_battery(nid)
                batt_level = -1
                if batt and batt.get("level", -1) >= 0:
                    level = batt["level"]
                    batt_level = min(100, level * 20) if level <= 5 else level

                # Get settings
                bridge.get_settings(nid)
                _time.sleep(2)
                bridge.recv(timeout=0.5)
                native_vals = bridge.get_setting_values(nid)

                from native_bridge import setting_id_to_name
                settings = {}
                for hex_id, val in native_vals.items():
                    sname = setting_id_to_name(hex_id)
                    if sname:
                        settings[sname] = val

                key = (pid, nid)
                seen[key] = {
                    "pid": f"{pid:04x}",
                    "pid_hex": f"0x{pid:04X}",
                    "serial": "",
                    "product_name": name,
                    "friendly_name": name,
                    "firmware": fw_str,
                    "category": "headset",
                    "dfu_executor": "btNeoDfu",
                    "family": "voyager_bt",
                    "usage_page": 0,
                    "battery_level": batt_level,
                    "settings": settings,
                }

            bridge.stop()
    except Exception as e:
        log.debug(f"Native bridge discovery: {e}")

    return list(seen.values())


# ── Settings Read ─────────────────────────────────────────────────────────

def read_device_settings(dev, path):
    """Read settings from a device. Returns dict of {name: value}."""
    try:
        from device_settings import read_all_settings
        settings = read_all_settings(path, dev.get("usage_page", 0),
                                      dev.get("dfu_executor", ""))
        return {s["name"]: s["value"] for s in settings}
    except Exception:
        return {}


# ── Command Execution ─────────────────────────────────────────────────────

def execute_command(cmd):
    """Execute a command from the server."""
    cmd_type = cmd.get("command_type", "")
    cmd_data = cmd.get("command_data", "{}")
    if isinstance(cmd_data, str):
        try:
            cmd_data = json.loads(cmd_data)
        except:
            cmd_data = {}

    log.info(f"Executing command: {cmd_type}")

    if cmd_type == "set_setting":
        return execute_set_setting(cmd, cmd_data)
    elif cmd_type == "native_set_setting":
        return execute_native_set_setting(cmd, cmd_data)
    elif cmd_type == "apply_preset":
        return execute_apply_preset(cmd, cmd_data)
    elif cmd_type == "report":
        return {"status": "done", "result": "Report triggered"}
    else:
        return {"status": "error", "result": f"Unknown command: {cmd_type}"}


def execute_set_setting(cmd, data):
    """Execute a set_setting command."""
    name = data.get("name", "")
    value = data.get("value")
    target_pid = cmd.get("device_pid", "*")

    if not name:
        return {"status": "error", "result": "Setting name required"}

    try:
        from device_settings import write_setting

        # Find matching device
        for d in hid.enumerate():
            if d["vendor_id"] not in POLY_VIDS:
                continue
            pid = f"{d['product_id']:04x}"
            if target_pid != "*" and pid != target_pid.lower().replace("0x", ""):
                continue
            if d["usage_page"] not in VENDOR_USAGE_PAGES:
                continue

            from polytool import DFU_EXECUTOR_MAP
            dfu = DFU_EXECUTOR_MAP.get(pid, "")
            ok = write_setting(d["path"], d["usage_page"], dfu, name, value)
            if ok:
                log.info(f"Set {name}={value} on PID 0x{d['product_id']:04X}")
                return {"status": "done", "result": f"Set {name}={value}"}

        return {"status": "error", "result": "No matching device found"}
    except Exception as e:
        return {"status": "error", "result": str(e)}


def execute_apply_preset(cmd, data):
    """Apply a settings preset."""
    settings = data.get("settings", [])
    results = []
    for s in settings:
        r = execute_set_setting(cmd, s)
        results.append(r)
    ok = sum(1 for r in results if r["status"] == "done")
    return {"status": "done", "result": f"{ok}/{len(settings)} settings applied"}


def execute_native_set_setting(cmd, data):
    """Execute a setting write via native bridge (for DECT/Voyager)."""
    name = data.get("name", "")
    value = data.get("value")
    if not name:
        return {"status": "error", "result": "Setting name required"}
    try:
        from native_bridge import NativeBridge, find_components_dir, setting_name_to_id
        if not find_components_dir():
            return {"status": "error", "result": "Native bridge not available"}

        setting_id = setting_name_to_id(name)
        if not setting_id:
            return {"status": "error", "result": f"Unknown setting: {name}"}

        bridge = NativeBridge()
        bridge.start()
        import time as _time
        for _ in range(20):
            _time.sleep(0.5)
            bridge.recv(timeout=0.1)
            if bridge.get_devices():
                break
        _time.sleep(3)
        bridge.recv(timeout=0.5)

        devs = bridge.get_devices()
        if not devs:
            bridge.stop()
            return {"status": "error", "result": "No native devices found"}

        # Use first device (or match by PID if specified)
        target_pid = cmd.get("device_pid", "*")
        native_id = None
        for nid, ndev in devs.items():
            if target_pid == "*" or f"{ndev.get('pid',0):04x}" == target_pid.lower().replace("0x", ""):
                native_id = nid
                break

        if not native_id:
            native_id = list(devs.keys())[0]

        result = bridge.set_setting(native_id, setting_id, str(value))
        _time.sleep(1)
        bridge.recv(timeout=1)
        bridge.stop()

        if result:
            log.info(f"Native set {name}={value} on {native_id}")
            return {"status": "done", "result": f"Set {name}={value} via native bridge"}
        return {"status": "error", "result": "Native bridge write failed"}
    except Exception as e:
        return {"status": "error", "result": str(e)}


# ── Agent Loop ────────────────────────────────────────────────────────────

_agent_key = ""

def _headers():
    """Auth headers for server requests."""
    h = {"Content-Type": "application/json"}
    if _agent_key:
        h["Authorization"] = f"Bearer {_agent_key}"
    return h


def run_report(server_url, agent_id):
    """Discover devices and report to server."""
    devices = discover_devices()
    log.info(f"Found {len(devices)} device(s)")

    # Read settings for each device (best effort)
    for dev in devices:
        for d in hid.enumerate():
            if d["vendor_id"] in POLY_VIDS and f"{d['product_id']:04x}" == dev["pid"] \
               and d["usage_page"] in VENDOR_USAGE_PAGES:
                dev["settings"] = read_device_settings(dev, d["path"])
                break

    payload = {
        "agent_id": agent_id,
        "hostname": socket.gethostname(),
        "username": os.environ.get("USER", os.environ.get("USERNAME", "")),
        "platform": f"{platform.system()} {platform.release()}",
        "agent_version": VERSION,
        "devices": devices,
    }

    try:
        resp = requests.post(f"{server_url}/api/agent/report",
                             json=payload, headers=_headers(), timeout=10)
        if resp.status_code == 200:
            log.info(f"Reported {len(devices)} device(s) to server")
        else:
            log.warning(f"Server returned {resp.status_code}")
    except Exception as e:
        log.error(f"Report failed: {e}")


def poll_commands(server_url, agent_id):
    """Poll server for pending commands and execute them."""
    try:
        resp = requests.get(f"{server_url}/api/agent/commands",
                            params={"agent_id": agent_id},
                            headers=_headers(), timeout=10)
        if resp.status_code != 200:
            return

        data = resp.json()
        commands = data.get("commands", [])
        if not commands:
            return

        for cmd in commands:
            log.info(f"Command {cmd['id']}: {cmd['command_type']}")
            result = execute_command(cmd)

            # Report result
            requests.post(f"{server_url}/api/agent/result", json={
                "command_id": cmd["id"],
                "agent_id": agent_id,
                "device_serial": cmd.get("device_serial", ""),
                "status": result.get("status", "done"),
                "result": result.get("result", ""),
            }, headers=_headers(), timeout=10)

    except Exception as e:
        log.error(f"Command poll failed: {e}")


def agent_loop(server_url, interval=60):
    """Main agent loop."""
    agent_id = get_agent_id()
    log.info(f"Agent ID: {agent_id}")
    log.info(f"Server:   {server_url}")
    log.info(f"Interval: {interval}s")
    log.info(f"Host:     {socket.gethostname()}")
    log.info(f"Platform: {platform.system()} {platform.release()}")

    while True:
        try:
            run_report(server_url, agent_id)
            poll_commands(server_url, agent_id)
        except KeyboardInterrupt:
            log.info("Stopping agent.")
            break
        except Exception as e:
            log.error(f"Agent loop error: {e}")

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("Stopping agent.")
            break


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PolyAgent — Workstation headset agent")
    parser.add_argument("--server", default=os.environ.get("POLYSERVER_URL", "http://localhost:8421"),
                        help="PolyServer URL (default: http://localhost:8421)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Report interval in seconds (default: 60)")
    parser.add_argument("--once", action="store_true",
                        help="Single report, then exit")
    parser.add_argument("--key", default=os.environ.get("POLYAGENT_KEY", ""),
                        help="Agent API key (from server startup output)")
    args = parser.parse_args()

    if not args.key:
        log.warning("No API key provided. Use --key or set POLYAGENT_KEY env var.")

    agent_id = get_agent_id()

    global _agent_key
    _agent_key = args.key

    if args.once:
        run_report(args.server, agent_id)
        return

    agent_loop(args.server, args.interval)


if __name__ == "__main__":
    main()
