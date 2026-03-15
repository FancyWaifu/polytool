#!/usr/bin/env python3
"""
PolyLens Web — Lightweight web dashboard for Poly/Plantronics devices.

Replaces the bloated Poly Studio Electron app with a simple Flask server
that provides device management, firmware updates, and monitoring via
your browser.

Usage:
  python3 polylens.py              # Start on http://localhost:8420
  python3 polylens.py --port 9000  # Custom port

Requirements: pip install flask hidapi requests
"""

import json
import sys
import os
import time
import threading
import webbrowser
from pathlib import Path

# Import polytool functions
sys.path.insert(0, str(Path(__file__).parent))
from polytool import (
    discover_devices, classify_device, try_read_battery, try_read_device_info,
    PolyCloudAPI, PolyDevice, DFU_TRANSPORT_INFO, POLY_VIDS,
    FIRMWARE_CACHE, CONFIG_DIR, FirmwareUpdater, _normalize_version,
)

try:
    from flask import Flask, jsonify, request, send_from_directory
except ImportError:
    print("Flask not installed. Run: pip install flask")
    sys.exit(1)

# ── Flask App ────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=None)
WEB_DIR = Path(__file__).parent / "web"

# Shared state
_cloud = PolyCloudAPI()
_updater = FirmwareUpdater(_cloud)
_device_cache = {"devices": [], "ts": 0}
_cache_lock = threading.Lock()
_update_jobs = {}  # device_id → {status, progress, message, ...}
_update_lock = threading.Lock()
SCAN_INTERVAL = 5  # seconds


def _scan_devices():
    """Scan for devices and populate battery/firmware info."""
    devices = discover_devices()
    for dev in devices:
        try_read_device_info(dev)
        try_read_battery(dev)
    return devices


def _device_to_dict(dev):
    """Serialize a PolyDevice to JSON-friendly dict."""
    transport_info = DFU_TRANSPORT_INFO.get(dev.dfu_executor)
    return {
        "id": dev.id,
        "name": dev.friendly_name or dev.product_name or "Unknown",
        "product_name": dev.product_name,
        "manufacturer": dev.manufacturer,
        "serial": dev.serial or "n/a",
        "firmware": dev.firmware_display,
        "category": dev.category,
        "codename": dev.codename or "",
        "vid": f"0x{dev.vid:04X}",
        "pid": f"0x{dev.pid:04X}",
        "vid_pid": f"{dev.vid:04X}:{dev.pid:04X}",
        "bus_type": dev.bus_type,
        "battery_level": dev.battery_level,
        "battery_charging": dev.battery_charging,
        "battery_display": dev.battery_display,
        "battery_left": dev.battery_left,
        "battery_right": dev.battery_right,
        "battery_case": dev.battery_case,
        "dfu_executor": dev.dfu_executor or "n/a",
        "dfu_transport": transport_info[0] if transport_info else "n/a",
        "fw_format": transport_info[1] if transport_info else "n/a",
        "platform_support": transport_info[2] if transport_info else "n/a",
        "lens_product_id": dev.lens_product_id,
    }


def _get_cached_devices(max_age=SCAN_INTERVAL):
    """Return cached devices, rescanning if stale."""
    with _cache_lock:
        if time.time() - _device_cache["ts"] > max_age:
            _device_cache["devices"] = _scan_devices()
            _device_cache["ts"] = time.time()
        return _device_cache["devices"]


# ── Static Files ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/style.css")
def style():
    return send_from_directory(WEB_DIR, "style.css")


@app.route("/app.js")
def appjs():
    return send_from_directory(WEB_DIR, "app.js")


# ── API Endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/devices")
def api_devices():
    """Get all connected devices with battery/firmware info."""
    devices = _get_cached_devices()
    return jsonify({
        "devices": [_device_to_dict(d) for d in devices],
        "count": len(devices),
        "ts": int(time.time()),
    })


@app.route("/api/devices/refresh", methods=["POST"])
def api_devices_refresh():
    """Force a fresh scan (bypass cache)."""
    with _cache_lock:
        _device_cache["devices"] = _scan_devices()
        _device_cache["ts"] = time.time()
    devices = _device_cache["devices"]
    return jsonify({
        "devices": [_device_to_dict(d) for d in devices],
        "count": len(devices),
    })


@app.route("/api/updates")
def api_updates():
    """Check for firmware updates for all connected devices."""
    devices = _get_cached_devices()
    results = []
    for dev in devices:
        fw_info = _cloud.check_firmware(dev)
        entry = {
            "device": _device_to_dict(dev),
            "update_available": False,
        }
        if fw_info:
            current = fw_info.get("current", dev.firmware_display)
            latest = fw_info.get("latest", "unknown")
            entry["current"] = current
            entry["latest"] = latest
            entry["product_name"] = fw_info.get("product_name", "")
            entry["download_url"] = fw_info.get("download_url", "")
            entry["release_notes"] = fw_info.get("release_notes", "")
            entry["publish_date"] = fw_info.get("publish_date", "")
            entry["update_available"] = _normalize_version(current) != _normalize_version(latest)
            entry["blocked"] = fw_info.get("blocked_download", False)
        results.append(entry)
    return jsonify({"updates": results})


@app.route("/api/catalog")
def api_catalog():
    """Search the Poly Cloud firmware catalog."""
    search = request.args.get("q", "").lower()
    products = _cloud.get_product_catalog(limit=200)
    if search:
        products = [p for p in products
                    if search in p["name"].lower() or search in p["id"].lower()]
    # Only products with firmware
    products = [p for p in products if p.get("version")]
    return jsonify({
        "products": products,
        "count": len(products),
    })


# ── Firmware Update API ───────────────────────────────────────────────────────

def _find_device_by_id(dev_id):
    """Find a device by its short ID from the cached device list."""
    devices = _get_cached_devices()
    for dev in devices:
        if dev.id == dev_id:
            return dev
    return None


def _run_update(dev_id, force=False):
    """Background thread: download + flash firmware for a device."""
    with _update_lock:
        _update_jobs[dev_id] = {
            "status": "checking",
            "progress": 0,
            "message": "Checking for updates...",
            "error": None,
        }

    def set_status(status, progress, message, error=None):
        with _update_lock:
            _update_jobs[dev_id] = {
                "status": status,
                "progress": progress,
                "message": message,
                "error": error,
            }

    try:
        dev = _find_device_by_id(dev_id)
        if not dev:
            set_status("error", 0, "Device not found", "Device disconnected?")
            return

        try_read_device_info(dev)
        try_read_battery(dev)

        # Validate
        ok, reason = _updater.validate_device_for_update(dev)
        if not ok:
            set_status("error", 0, f"Cannot update: {reason}", reason)
            return

        # Check for update
        set_status("checking", 10, "Querying Poly Cloud...")
        fw_info = _cloud.check_firmware(dev)
        if not fw_info:
            set_status("error", 10, "No firmware info available", "Not in cloud catalog")
            return

        current = fw_info.get("current", dev.firmware_display)
        latest = fw_info.get("latest", "unknown")

        if _normalize_version(current) == _normalize_version(latest) and not force:
            set_status("up_to_date", 100, f"Already up to date (v{current})")
            return

        if fw_info.get("blocked_download"):
            set_status("error", 10, "Download blocked", "Firmware download blocked by Poly")
            return

        download_url = fw_info.get("download_url", "")
        if not download_url:
            set_status("error", 10, "No download URL", "No download URL available")
            return

        # Download
        set_status("downloading", 30, f"Downloading v{latest}...")
        fw_path = _cloud.download_firmware(download_url)
        if not fw_path:
            set_status("error", 30, "Download failed", "Failed to download firmware")
            return

        # Flash
        set_status("flashing", 60, f"Flashing v{latest}... DO NOT DISCONNECT!")
        success = _updater._apply_update(dev, fw_path, latest)

        if success:
            set_status("done", 100, f"Updated to v{latest}! Device is rebooting.")
        else:
            transport = DFU_TRANSPORT_INFO.get(dev.dfu_executor)
            if dev.dfu_executor == "LegacyDfu":
                msg = "FWU API protocol not yet supported for cross-platform flashing"
            elif dev.dfu_executor == "btNeoDfu":
                msg = "Bluetooth DFU requires Windows with Poly Lens Desktop"
            elif transport:
                msg = f"Flashing via {transport[0]} failed"
            else:
                msg = "Flashing not supported for this device on this platform"
            set_status("error", 60, "Flash failed", msg)

    except Exception as e:
        set_status("error", 0, "Update failed", str(e))


@app.route("/api/update/start", methods=["POST"])
def api_update_start():
    """Start a firmware update for a device."""
    data = request.get_json() or {}
    dev_id = data.get("device_id", "")
    force = data.get("force", False)

    if not dev_id:
        return jsonify({"error": "device_id required"}), 400

    # Check device exists
    dev = _find_device_by_id(dev_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404

    # Check not already updating
    with _update_lock:
        job = _update_jobs.get(dev_id)
        if job and job["status"] in ("checking", "downloading", "flashing"):
            return jsonify({"error": "Update already in progress"}), 409

    # Start background update
    t = threading.Thread(target=_run_update, args=(dev_id, force), daemon=True)
    t.start()

    return jsonify({"status": "started", "device_id": dev_id})


@app.route("/api/update/status/<dev_id>")
def api_update_status(dev_id):
    """Get the status of a firmware update job."""
    with _update_lock:
        job = _update_jobs.get(dev_id)
    if not job:
        return jsonify({"status": "idle", "progress": 0, "message": ""})
    return jsonify(job)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="PolyLens Web Dashboard")
    parser.add_argument("--port", type=int, default=8420,
                        help="Port to listen on (default: 8420)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open browser")
    args = parser.parse_args()

    url = f"http://localhost:{args.port}"
    print(f"\n  PolyLens Web Dashboard")
    print(f"  {url}\n")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
