#!/usr/bin/env python3
"""
LensServer — Drop-in replacement for Poly Lens Control Service.

Implements the LensServiceApi TCP protocol so that Poly Studio GUI
can connect to us instead of the real LensService. We handle device
discovery and settings via our own HID code.

This means Poly Studio works with ANY device we support, even ones
not in Poly's whitelist.

Protocol:
  TCP server on a dynamic port, written to SocketPortNumber file.
  Newline-delimited JSON messages with "type" field.
  Client registers with RegisterClient, we respond with ClientRegistered.
  We push DeviceAttached events for each connected device.
  Client sends GetDeviceSettings/SetDeviceSetting, we translate to HID.

Usage:
  python3 lensserver.py                 # Start server
  python3 lensserver.py --port 31415    # Fixed port

Then open Poly Studio — it will connect to us automatically.
"""

import json
import os
import sys
import socket
import time
import threading
import argparse
import platform
from pathlib import Path

# Import our device tools
sys.path.insert(0, str(Path(__file__).parent))

API_VERSION = "1.14.1"

# ── Console output control ──────────────────────────────────────────────────
_verbose = False
_quiet = False

def _log(msg, verbose_only=False):
    if _quiet:
        return
    if verbose_only and not _verbose:
        return
    print(msg)


def _sanitize_firmware_components(fw):
    """Strip 0xFFFF placeholder values from firmware component dicts.

    DECT bases sometimes report SetID (or other sub-component) fields as all
    FFFFs when the underlying NVRAM record was never programmed. Poly Studio
    treats those values as real and chokes on version comparisons, so drop
    any nested block whose every field is "ffff" (case-insensitive).
    """
    if not isinstance(fw, dict):
        return fw
    cleaned = {}
    for k, v in fw.items():
        if isinstance(v, dict):
            if v and all(isinstance(x, str) and x.lower() == "ffff" for x in v.values()):
                continue  # drop fully-FF sub-block (e.g. setId)
            cleaned[k] = v
        elif isinstance(v, str) and v.lower() in ("ffff", "ffffffff"):
            continue  # drop scalar FF placeholder
        else:
            cleaned[k] = v
    return cleaned


def _comp(dev, key):
    """Read a per-component firmware version from the device's
    `firmware_components` dict (populated by polytool's LCS-cache hydrator).
    Returns the dotted form ('10.82', '0.0.2134.3260', ...) or '' when
    nothing is known. Empty string means "don't display this slot."""
    try:
        from devices import _format_component_version
    except ImportError:
        return ""
    raw = (dev.firmware_components or {}).get(key, "")
    if not raw:
        return ""
    return _format_component_version(raw)


MSG_DELIM = "\x01"  # SOH byte — real LensService message separator (NOT newline)

# Port file location (platform-specific)
if sys.platform == "win32":
    _pdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    PORT_FILE_DIR = Path(_pdata) / "Poly" / "Lens Control Service"
else:
    PORT_FILE_DIR = Path.home() / "Library/Application Support/Poly/Lens Control Service"
PORT_FILE = PORT_FILE_DIR / "SocketPortNumber"


# Per-serial display-name overrides. Anything in this dict wins over the
# auto-formatted default. Add entries here to force a specific name for
# a specific physical unit. Empty by default - the canonical format
# (see _format_display_name) handles the standard case.
DEVICE_NAME_OVERRIDES = {
    # Example:
    # "377CF5FD4D5A4E1CA151FF7FAA3A5E8A": "Reception desk",
}


def _format_display_name(*, friendly_name: str, model_description: str,
                         tattoo: str, lcs_product_name: str = "",
                         serial: str = "") -> str:
    """Build the canonical display name for a Poly device.

    Format:  Poly <model name> <model number> - <S/N>

    Examples:
        Poly Savi 7320 - S/N2UGHYA
        Poly Savi 8220 - S/N506C1E
        Poly Blackwire 3220 - S/N1D8C82

    Sources, in order of preference for the model portion:
      friendly_name      - PolyDevice.friendly_name, which has CODENAME_MAP
                           translations applied (e.g. "Yen Stereo" -> "Savi 8220")
      model_description  - raw dfu.config DeviceDescription
      lcs_product_name   - last-resort fallback when nothing else is known

    For the serial portion: prefer the human tattoo (S/Nxxxx printed on
    the device label). When unavailable, synthesize one from the last 6
    chars of the GUID-style serial so two units of the same model still
    look distinct.
    """
    # Prefer friendly_name (which has CODENAME_MAP applied via PID_CODENAMES),
    # but if it just echoes a dfu.config codename ("Chickadee", "Heron
    # Stereo Teams"), translate via CODENAME_MAP directly.
    from devices import CODENAME_MAP
    base = (friendly_name or model_description or "").strip()
    # Check both the friendly_name string AND its "Poly "-stripped form
    # against CODENAME_MAP - dfu.config descriptions sometimes get
    # _polyize'd into "Poly Chickadee" which then doesn't match the
    # CODENAME_MAP key "Chickadee".
    base_unprefixed = base
    for prefix in ("Poly ", "Plantronics ", "HP "):
        if base_unprefixed.startswith(prefix):
            base_unprefixed = base_unprefixed[len(prefix):]
            break
    if base_unprefixed in CODENAME_MAP:
        base = CODENAME_MAP[base_unprefixed]
    elif (model_description or "").strip() in CODENAME_MAP:
        base = CODENAME_MAP[model_description.strip()]
    if not base:
        base = (lcs_product_name or "").strip()
        if base.endswith(" Series"):
            base = base[:-len(" Series")]
    if not base:
        return lcs_product_name or "Poly Device"

    # Normalize a few variant spellings that appear in dfu.config but
    # aren't how Poly markets the products:
    #   "Black Wire" -> "Blackwire"   (one word in product naming)
    #   "Yen Stereo / W8220T" left to CODENAME_MAP - shouldn't reach here
    base = base.replace("Black Wire", "Blackwire")

    # Prefix with "Poly " unless the model already has a brand prefix.
    if not base.startswith(("Poly ", "Plantronics ", "HP ")):
        base = f"Poly {base}"

    # Tattoo should look like S/Nxxxxxxx - if it's actually a 32-char
    # GUID (some LCS records put the genes-id under tattooSerialNumber
    # when there's no real tattoo), fall back to a S/N-prefixed short form.
    sn = (tattoo or "").strip()
    if sn and (len(sn) >= 24 and sn.isalnum()):
        # Looks like a GUID, not a real tattoo
        sn = ""
    if not sn and serial:
        sn = "S/N" + serial[-6:].upper()
    if sn:
        return f"{base} - {sn}"
    return base


from typing import Optional  # for the helpers below


def _find_lcs_listening_port() -> Optional[int]:
    """Locate the actual TCP port LCS is listening on, ignoring whatever
    SocketPortNumber says (the file can be stale - a crashed lensserver
    leaves its dead port in there).

    Strategy: find the LensService.exe PID via tasklist, then query
    netstat for any LISTENING TCP socket owned by that PID on 127.0.0.1.
    Returns None if LCS isn't running or no listener is found.
    """
    if sys.platform != "win32":
        return None
    try:
        import subprocess
        r = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq LensService.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=0x08000000,
        )
        line = (r.stdout or "").strip().splitlines()[0] if r.stdout else ""
        if "LensService.exe" not in line:
            return None
        # CSV: "name","pid","session","sessNum","memUsage"
        pid = line.split('","')[1]
    except Exception:
        return None
    try:
        r = subprocess.run(
            ["netstat.exe", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=5,
            creationflags=0x08000000,
        )
    except Exception:
        return None
    for raw in (r.stdout or "").splitlines():
        parts = raw.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        if parts[-1] != pid:
            continue
        local = parts[1]  # 127.0.0.1:53431
        if not local.startswith("127.0.0.1:"):
            continue
        try:
            return int(local.rsplit(":", 1)[1])
        except ValueError:
            continue
    return None


def _detect_lcs_reachable() -> Optional[int]:
    """Returns the actual LCS port if proxy mode is appropriate, else None.

    Used by --proxy=auto to decide between proxy and standalone modes
    at startup. Caller can pass the returned port directly to
    LCSClient.start() instead of re-reading SocketPortNumber (which may
    be stale from a previous lensserver run)."""
    return _find_lcs_listening_port()


class LensServer:
    """TCP server implementing the LensServiceApi protocol."""

    def __init__(self, port=0, *, proxy_lcs=False, lcs_port=None):
        self.port = port
        self.server_sock = None
        self.clients = []  # list of client sockets
        self.devices = {}  # deviceId → device info dict
        self.running = False
        self._lock = threading.Lock()
        # Proxy mode: connect to LCS as a client, augment its data with
        # our overrides (display_name, FFFF strip, firmware version,
        # update statuses), then serve the augmented view to Studio.
        # When False, we behave as the standalone replacement we always
        # have - native_bridge is the data source.
        self.proxy_lcs = proxy_lcs
        # Caller-supplied LCS port (from _find_lcs_listening_port). When
        # None, start() falls back to whatever SocketPortNumber says,
        # which may be a stale value from a previous lensserver crash.
        self._lcs_port_override = lcs_port
        self._lcs_client = None
        # In proxy mode, when we forward a Studio request to LCS we
        # remember which client to route LCS's response back to.
        # Keyed by PLT_<serial> deviceId since that's what LCS echoes.
        self._proxy_pending_responses = {}

    _original_port = None  # saved port from real LensService

    def start(self):
        """Start the TCP server."""
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("127.0.0.1", self.port))
        self.server_sock.listen(5)
        self.port = self.server_sock.getsockname()[1]
        self.running = True

        # Save original port file (so we can restore it on exit)
        # In proxy mode this is ALSO the port LCS is listening on, which
        # is what we'll use for the LCSClient connection.
        try:
            if PORT_FILE.exists():
                self._original_port = PORT_FILE.read_text().strip()
        except Exception:
            pass

        # Proxy mode: connect to LCS using either the caller-supplied
        # port (from _find_lcs_listening_port) or whatever
        # SocketPortNumber says. The override is preferred because the
        # port file can be stale (a crashed previous lensserver leaves
        # its dead port in there).
        if self.proxy_lcs:
            lcs_port = self._lcs_port_override
            if not lcs_port and self._original_port:
                try:
                    lcs_port = int(self._original_port)
                except (TypeError, ValueError):
                    lcs_port = None
            if lcs_port:
                try:
                    self._start_lcs_proxy(lcs_port)
                except Exception as e:
                    _log(f"  proxy: startup failed ({e}) - falling back to standalone mode")
                    self._lcs_client = None
            else:
                _log("  proxy: no LCS port available - falling back to standalone mode")

        # Write our port file so Poly Studio can find us
        PORT_FILE_DIR.mkdir(parents=True, exist_ok=True)
        PORT_FILE.write_text(str(self.port))

        # Register cleanup for any exit path (Ctrl+C, kill, crash)
        import atexit
        atexit.register(self._cleanup_port_file)
        import signal
        sigs = [signal.SIGINT]
        if hasattr(signal, 'SIGTERM'):
            sigs.append(signal.SIGTERM)
        for sig in sigs:
            signal.signal(sig, lambda s, f: self._signal_shutdown())

        _log(f"  Listening on port {self.port}")
        _log(f"  Port file: {PORT_FILE}", verbose_only=True)
        if self._original_port:
            _log(f"  Original LCS port saved: {self._original_port}", verbose_only=True)

        # Start the port-file watcher so we keep ownership even when LCS
        # rewrites the file during its own startup or fix-setid's restart.
        self._start_port_file_watcher()

        # Battery poller: refreshes battery state for every device every
        # 30s and broadcasts DeviceUpdated when a value changes. Without
        # this, Studio's battery icon stays at the level it was when the
        # device first attached and never updates as charge changes.
        import threading
        threading.Thread(target=self._battery_poller, daemon=True,
                         name="battery-poller").start()

    def _signal_shutdown(self):
        """Handle SIGTERM/SIGINT — stop cleanly."""
        self.running = False

    def _cleanup_port_file(self):
        """Restore original port file or remove ours."""
        try:
            if self._original_port:
                PORT_FILE.write_text(self._original_port)
                _log(f"  Restored original LCS port: {self._original_port}", verbose_only=True)
            else:
                PORT_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def reclaim_port_file(self):
        """Re-write our port to the SocketPortNumber file so Poly Studio
        keeps connecting to us.

        Needed after any operation that restarts LCS (e.g. fix-setid's
        device_isolate): LCS overwrites the port file with its own port
        on startup, which would silently route Studio to LCS instead of
        us on Studio's next reconnect.

        In proxy mode, we also use the value LCS wrote as the trigger
        to reconnect our LCSClient: if the port changed, our existing
        connection is dead and the LCSClient needs to dial the new port.
        """
        try:
            current = PORT_FILE.read_text().strip() if PORT_FILE.exists() else ""
        except Exception:
            current = ""
        if current == str(self.port):
            return  # already ours
        # Save what LCS reclaimed (so cleanup can still restore it later)
        if current and current != str(self.port):
            old_lcs_port = self._original_port
            self._original_port = current
            # Proxy mode: LCS just bounced and got a new port. Reconnect
            # our LCSClient on the new value before reclaiming the file
            # so consumers don't see a missing-LCS gap.
            if (self.proxy_lcs and self._lcs_client
                    and current != str(old_lcs_port)):
                _log(f"  proxy: LCS port changed {old_lcs_port}->{current}, "
                     "reconnecting client", verbose_only=True)
                try:
                    self._lcs_client.stop()
                except Exception:
                    pass
                try:
                    self._lcs_client.start(port=int(current),
                                           wait_for_devices=True,
                                           wait_timeout=4.0)
                    # Re-import LCS's catalog after reconnect
                    for lcs_dev in self._lcs_client.list_devices():
                        self._ingest_lcs_device(lcs_dev)
                except Exception as e:
                    _log(f"  proxy: reconnect failed: {e}")
        try:
            PORT_FILE.write_text(str(self.port))
            _log(f"  Reclaimed port file: {self.port} (was {current!r})",
                 verbose_only=True)
        except Exception as e:
            _log(f"  Failed to reclaim port file: {e}")

    def _start_port_file_watcher(self):
        """Background thread that polls SocketPortNumber every 2s and
        reclaims it if LCS (or anyone else) overwrote our value.

        LCS doesn't just write the port file at startup — it appears to
        rewrite periodically as part of its own bookkeeping. A one-shot
        reclaim after fix-setid isn't enough; without continuous
        reclaiming, Studio's next reconnect lands on stock LCS within
        seconds and our MITM stops mattering."""
        import threading
        def _watcher():
            while self.running:
                try:
                    self.reclaim_port_file()
                except Exception:
                    pass
                time.sleep(2)
        t = threading.Thread(target=_watcher, daemon=True,
                             name="port-file-watcher")
        t.start()

    def stop(self):
        """Stop the server."""
        self.running = False
        # Stop native bridge first (before closing sockets)
        if self._native_bridge:
            try:
                self._native_bridge.stop()
            except Exception:
                pass
            self._native_bridge = None
        if self.server_sock:
            self.server_sock.close()
        for client in self.clients:
            try:
                client.close()
            except:
                pass
        self._cleanup_port_file()

    def accept_clients(self):
        """Accept client connections in a loop."""
        self.server_sock.settimeout(1)

        # Start device scanner thread
        scanner = threading.Thread(target=self._device_scanner, daemon=True)
        scanner.start()

        while self.running:
            try:
                client_sock, addr = self.server_sock.accept()
                _log(f"  Client connected from {addr}")
                t = threading.Thread(target=self.handle_client,
                                     args=(client_sock,), daemon=True)
                t.start()
                with self._lock:
                    self.clients.append(client_sock)
            except socket.timeout:
                continue
            except OSError:
                break

    # ── Proxy-mode plumbing ─────────────────────────────────────────────

    def _start_lcs_proxy(self, lcs_port: int):
        """Connect to LCS as a client and wire its events into our data
        flow. lcs_port is the port we captured BEFORE overwriting the
        SocketPortNumber file with our own port."""
        from lcs_client import LCSClient

        self._lcs_client = LCSClient(name="polytool-lensserver-proxy",
                                     log=lambda s: _log(f"  lcs-client: {s}",
                                                        verbose_only=True))
        ok = self._lcs_client.start(port=lcs_port,
                                    wait_for_devices=True, wait_timeout=8.0)
        if not ok:
            _log("  proxy: LCS client failed to start - falling back to native_bridge")
            self._lcs_client = None
            return False

        # Initial population of our device table from LCS's catalog.
        # The canonical name format already includes the tattoo serial
        # so collisions can't happen - no separate disambiguation pass
        # needed.
        for lcs_dev in self._lcs_client.list_devices():
            self._ingest_lcs_device(lcs_dev)
        _log(f"  proxy: imported {len(self.devices)} device(s) from LCS")

        # Subscribe to event-stream changes
        self._lcs_client.on_device_attached(self._on_lcs_device_attached)
        self._lcs_client.on_device_updated(self._on_lcs_device_updated)
        self._lcs_client.on_device_detached(self._on_lcs_device_detached)
        # Catch-all for settings responses we forwarded to LCS
        self._lcs_client.on_raw_message(self._on_lcs_raw_message)
        return True

    def _our_id_from_lcs_dev(self, lcs_dev: dict) -> str:
        """Compute the 8-char polytool-internal device id from an LCS
        device record. Centralized so we don't repeat the slicing logic
        and so the PLT_ prefix is correctly stripped before slicing.

        Strategy:
          1. Use the first 8 chars of serialNumber when available
          2. Strip "PLT_" prefix from deviceId before slicing as fallback
          3. Pad short serials so we don't return collisions
        """
        serial = (lcs_dev.get("serialNumber") or "").strip()
        if serial:
            return serial[:8].upper() if len(serial) >= 8 else serial.upper().ljust(8, "0")
        did = lcs_dev.get("deviceId", "")
        if did.startswith("PLT_"):
            did = did[4:]
        return did[:8].upper() if len(did) >= 8 else did.upper().ljust(8, "0")

    def _ingest_lcs_device(self, lcs_dev: dict):
        """Take a device record from LCS, apply our overrides (display
        name, FFFF strip, synthesized firmware version, update statuses)
        and store it in our self.devices catalog under our 8-char id."""
        # Map LCS's deviceId (PLT_<32-char-serial>) to our 8-char id so
        # downstream code (settings cache, DFU cache, the HTTP API) keeps
        # using the same key it always has.
        serial = lcs_dev.get("serialNumber", "") or ""
        our_id = self._our_id_from_lcs_dev(lcs_dev)
        # Apply per-device overrides. Use polytool's PolyDevice classifier
        # to get model_description/display_name/firmware_components.
        from devices import PolyDevice, classify_device, _hydrate_from_lcs_cache
        from polytool import _normalize_version  # noqa: F401
        # LCS always sends VID/PID as hex strings, even when the value
        # happens to be all digits (e.g. "167" is hex 0x167 = 359 decimal).
        # Always parse as base 16 - parsing as decimal would land on the
        # wrong PID for any device whose hex value is all-numeric digits
        # (Voyager 4320 = 0x167, Voyager Focus 2 = 0x162, etc.).
        try:
            pid = int(str(lcs_dev.get("pid", "0")), 16)
        except (TypeError, ValueError):
            pid = 0
        try:
            vid = int(str(lcs_dev.get("vid", "0")), 16)
        except (TypeError, ValueError):
            vid = 0x047F
        ptd = PolyDevice(vid=vid, pid=pid, serial=serial,
                         tattoo_serial=lcs_dev.get("tattooSerialNumber", "") or "")
        classify_device(ptd)
        _hydrate_from_lcs_cache([ptd])  # picks up tattoo + firmware_components

        # Canonical name format: "Poly <model> - <S/Ntattoo>"
        # User-specified override (DEVICE_NAME_OVERRIDES) wins over the
        # canonical format if present.
        override = DEVICE_NAME_OVERRIDES.get(serial)
        if override:
            disp_name = override
        else:
            disp_name = _format_display_name(
                friendly_name=ptd.friendly_name,
                model_description=ptd.model_description,
                tattoo=ptd.tattoo_serial,
                lcs_product_name=lcs_dev.get("productName", ""),
                serial=serial,
            )
        # Preserve our existing record if any (so we don't lose
        # _polytool_dev / _native_id), then merge LCS's fields on top.
        with self._lock:
            existing = self.devices.get(our_id, {})
        # Build the augmented record
        augmented = dict(lcs_dev)  # start from LCS's full payload (battery!)
        augmented["deviceId"] = our_id  # our 8-char id, not PLT_<serial>
        augmented["productName"] = disp_name
        augmented["systemName"] = disp_name
        augmented["deviceName"] = disp_name
        augmented["displaySerialNumber"] = ptd.tattoo_serial or serial or ""
        augmented["tattooSerialNumber"] = ptd.tattoo_serial or serial or ""
        # Prefer LCS's firmwareVersion (it knows the real value via the
        # device cache) over PolyDevice.firmware_display (which derives
        # from USB bcdDevice and is "unknown" without release_number,
        # which we don't have when constructing PolyDevice from the LCS
        # payload). Only fall back to PolyDevice when LCS gave us nothing.
        lcs_fw = (lcs_dev.get("firmwareVersion") or "").strip()
        derived_fw = ptd.firmware_display if ptd.firmware_display != "unknown" else ""
        augmented["firmwareVersion"] = lcs_fw or derived_fw or "unknown"
        augmented["firmwareComponents"] = {
            "usbVersion": _comp(ptd, "usb") or ptd.firmware_display,
            "baseVersion": _comp(ptd, "base"),
            "tuningVersion": _comp(ptd, "tuning"),
            "picVersion": _comp(ptd, "pic"),
            "cameraVersion": _comp(ptd, "camera"),
            "headsetVersion": _comp(ptd, "headset"),
            "headsetLanguageVersion": "",
            "bluetoothVersion": _comp(ptd, "bluetooth"),
            "setIdVersion": _comp(ptd, "setid"),
        }
        # Strip FFFF placeholder values from the firmware-version dict
        # if LCS sent any (some Studio versions choke on them).
        augmented["firmwareVersion"] = _sanitize_firmware_components(
            augmented.get("firmwareVersion"))
        # Preserve internal fields we set elsewhere
        for k in list(existing.keys()):
            if k.startswith("_"):
                augmented[k] = existing[k]
        # Cache a polytool-side summary so other code paths can read it
        augmented["_polytool_dev"] = {
            "serial": serial, "vid": vid, "pid": pid,
            "firmware_display": ptd.firmware_display,
        }
        with self._lock:
            self.devices[our_id] = augmented

    def _redisambiguate_all_devices(self):
        """After ingesting a device, walk the full device list and append
        tattoo suffixes to any name that collides with another device's
        name. Mirrors the standalone-mode `_disambiguate_names` but works
        against our cached self.devices instead of a fresh discovery."""
        with self._lock:
            devs = list(self.devices.items())
        # Strip any existing parenthetical suffix so we work from base names
        from re import sub as _resub
        base_names = {}
        for did, dev in devs:
            base = _resub(r"\s*\([^)]*\)\s*$", "", dev.get("productName", ""))
            base_names[did] = base
        # Count base names
        counts = {}
        for n in base_names.values():
            counts[n] = counts.get(n, 0) + 1
        # Apply suffix where there's a collision. Skip devices that have
        # an explicit user override - those are the user's choice and
        # shouldn't get auto-mangled.
        with self._lock:
            for did, dev in self.devices.items():
                serial = dev.get("serialNumber", "") or ""
                if serial in DEVICE_NAME_OVERRIDES:
                    continue  # user-set name wins, leave it alone
                base = base_names.get(did, "")
                if counts.get(base, 0) > 1:
                    tattoo = dev.get("tattooSerialNumber") or dev.get("displaySerialNumber") or ""
                    if tattoo and not tattoo.startswith(dev.get("serialNumber", "")[:6]):
                        # Tattoo is a human-friendly value (S/Nxxxxx), not the GUID
                        new_name = f"{base} ({tattoo})"
                        dev["productName"] = new_name
                        dev["systemName"] = new_name
                        dev["deviceName"] = new_name
                else:
                    # No collision — strip any tattoo we previously appended
                    dev["productName"] = base
                    dev["systemName"] = base
                    dev["deviceName"] = base

    # ── Proxy request/response forwarding ──────────────────────────────

    def _proxy_forward_to_lcs(self, msg: dict, client_sock):
        """Send a Studio-originated message to LCS after rewriting our
        8-char deviceId back to LCS's PLT_<32-char> form.

        Records the originating client in self._proxy_pending_responses
        so when LCS's response comes back through the raw handler, we
        know who to forward it to.
        """
        our_id = msg.get("deviceId", "")
        plt_id = self._our_id_to_plt(our_id)
        forwarded = dict(msg)
        if plt_id:
            forwarded["deviceId"] = plt_id
        # Track which client to send the response to. LCS doesn't echo
        # our request_id back reliably, so we just remember "the most
        # recent client to ask about this deviceId" - in practice it's
        # always Studio, and we only have one Studio at a time.
        self._proxy_pending_responses[plt_id] = client_sock
        self._lcs_client.send(forwarded)
        _log(f"  proxy -> LCS: {msg.get('type')} for {plt_id}", verbose_only=True)

    def _our_id_to_plt(self, our_id: str) -> str:
        """8-char our-id back to PLT_<32-char> for LCS."""
        with self._lock:
            dev = self.devices.get(our_id, {})
        serial = dev.get("serialNumber") or dev.get("_polytool_dev", {}).get("serial", "")
        if serial:
            return f"PLT_{serial}"
        return our_id  # Best-effort - LCS will fail the lookup

    def _plt_to_our_id(self, plt_id: str) -> str:
        """PLT_<32-char> back to our 8-char id."""
        if plt_id.startswith("PLT_"):
            return plt_id[4:12]
        return plt_id[:8]

    def _on_lcs_raw_message(self, lcs_msg: dict):
        """Catch-all handler for messages from LCS. In proxy mode we
        forward settings-related responses back to whichever Studio
        client originated the request (tracked in
        self._proxy_pending_responses). DeviceList / DeviceAttached /
        DeviceUpdated / DeviceDetached are still handled by the typed
        handlers in lcs_client; we don't double-forward those.

        Special case: when LCS replies with DeviceSettings, Studio also
        needs a DeviceSettingsMetadata event before it'll render the
        settings page. LCS doesn't send that separately - the meta info
        is embedded in each setting's `meta` field. Extract those into
        a synthesized DeviceSettingsMetadata message and send it
        BEFORE the DeviceSettings, mirroring what our standalone-mode
        on_get_device_settings handler does.
        """
        t = lcs_msg.get("type", "")
        # Only forward response types we explicitly proxy. Skip event
        # types our typed handlers already process.
        if t not in ("DeviceSettings", "DeviceSettingsMetadata",
                     "DeviceSetting", "DeviceSettingChanged",
                     "DeviceSettingChangedConfirmed", "Error"):
            return
        plt_id = lcs_msg.get("deviceId", "")
        our_id = self._plt_to_our_id(plt_id) if plt_id else ""
        translated = dict(lcs_msg)
        if plt_id:
            translated["deviceId"] = our_id

        # If this is a DeviceSettings response, synthesize and send a
        # DeviceSettingsMetadata first - Studio's UI binds to the
        # metadata event to know what controls to render. Without it
        # the settings page comes up blank even though the data is here.
        if t == "DeviceSettings":
            settings = translated.get("settings") or []
            metadata_settings = []
            for s in settings:
                meta = s.get("meta")
                if not meta:
                    continue
                metadata_settings.append({
                    "name": s.get("name", ""),
                    "meta": meta,
                })
            metadata_msg = {
                "type": "DeviceSettingsMetadata",
                "apiVersion": API_VERSION,
                "deviceId": our_id,
                "settings": metadata_settings,
            }
            try:
                # Broadcast metadata to all clients - real LCS does the
                # same, so Studio + CallControlApp + watchdog stay in sync.
                # The previous "track originating client" approach lost
                # responses when multiple clients raced.
                self.broadcast(metadata_msg)
                _log(f"  proxy <- LCS: synthesized DeviceSettingsMetadata "
                     f"({len(metadata_settings)} settings) for {our_id}",
                     verbose_only=True)
            except Exception as e:
                _log(f"  proxy: failed to send synthesized metadata: {e}",
                     verbose_only=True)

        try:
            # Broadcast to all clients (matches real LCS behavior - settings
            # changes propagate to every connected client, not just the one
            # that asked).
            self.broadcast(translated)
            _log(f"  proxy <- LCS: {t} for {our_id}", verbose_only=True)
        except Exception as e:
            _log(f"  proxy: failed to forward {t}: {e}", verbose_only=True)

    def _on_lcs_device_attached(self, lcs_dev: dict):
        self._ingest_lcs_device(lcs_dev)
        our_id = self._our_id_from_lcs_dev(lcs_dev)
        with self._lock:
            dev = self.devices.get(our_id)
        if not dev:
            return
        clean_dev = {k: v for k, v in dev.items() if not k.startswith("_")}
        self.broadcast({
            "type": "DeviceAttached",
            "apiVersion": API_VERSION,
            "device": clean_dev,
        })
        self._prewarm_dfu_cache(our_id)

    def _on_lcs_device_updated(self, lcs_dev: dict):
        self._ingest_lcs_device(lcs_dev)
        our_id = self._our_id_from_lcs_dev(lcs_dev)
        with self._lock:
            dev = self.devices.get(our_id)
        if not dev:
            return
        clean_dev = {k: v for k, v in dev.items() if not k.startswith("_")}
        self.broadcast({
            "type": "DeviceUpdated",
            "apiVersion": API_VERSION,
            "device": clean_dev,
        })

    def _on_lcs_device_detached(self, lcs_device_id: str):
        # LCS uses PLT_<serial>; our id is the 8-char prefix
        serial_prefix = lcs_device_id[4:12] if lcs_device_id.startswith("PLT_") else lcs_device_id[:8]
        with self._lock:
            self.devices.pop(serial_prefix, None)
        self.broadcast({
            "type": "DeviceDetached",
            "apiVersion": API_VERSION,
            "deviceId": serial_prefix,
        })

    def _device_scanner(self):
        """Periodically scan for new/removed devices and push events."""
        while self.running:
            time.sleep(5)
            try:
                with self._lock:
                    old_ids = set(self.devices.keys())
                current_ids = self.discover_devices()
                with self._lock:
                    new_ids = current_ids if current_ids else set(self.devices.keys())

                # New devices
                for did in new_ids - old_ids:
                    with self._lock:
                        dev = self.devices.get(did)
                    if not dev:
                        continue
                    clean_dev = {k: v for k, v in dev.items() if not k.startswith("_")}
                    _log(f"  ++ Device added: {dev.get('productName', '?')}")
                    self._maybe_warn_ff_setid(dev)
                    # Async cloud check so the cache is warm by the time
                    # Poly Studio queries GetDeviceDFUStatus moments later.
                    self._prewarm_dfu_cache(did)
                    self.broadcast({
                        "type": "DeviceAttached",
                        "apiVersion": API_VERSION,
                        "device": clean_dev,
                    })
                    # Also fetch and push product images
                    image_data = self.get_product_images(did)
                    if image_data:
                        self.broadcast({
                            "type": "DeviceSetting",
                            "apiVersion": API_VERSION,
                            "deviceId": did,
                            "setting": {"name": "Product Images", "value": image_data},
                        })

                # Removed devices (skip native bridge devices — they're not in USB scan)
                for did in old_ids - new_ids:
                    with self._lock:
                        dev = self.devices.get(did, {})
                    ptd = dev.get("_polytool_dev", {})
                    if ptd.get("_native_id"):
                        continue  # BT device from native bridge, not USB-discoverable
                    _log(f"  -- Device removed: {did}")
                    self.broadcast({
                        "type": "DeviceDetached",
                        "apiVersion": API_VERSION,
                        "deviceId": did,
                    })
            except Exception as e:
                _log(f"  Scanner error: {e}", verbose_only=True)

    def handle_client(self, client_sock):
        """Handle one client connection."""
        buffer = ""
        client_sock.settimeout(1)
        registered = False


        while self.running:
            try:
                data = client_sock.recv(65536)
                if not data:
                    break
                buffer += data.decode("utf-8", errors="ignore")

                while MSG_DELIM in buffer:
                    line, buffer = buffer.split(MSG_DELIM, 1)
                    if line.strip():
                        try:
                            msg = json.loads(line)
                            msg_type = msg.get("type", "?")
                            _log(f"  <- {msg_type}: {json.dumps(msg)[:150]}", verbose_only=True)
                            if self.dump_mode and self.dump_file:
                                self.dump_file.write(json.dumps({"dir": "IN", "msg": msg}) + "\n")
                                self.dump_file.flush()

                            if msg_type in ("RegisterClient", "RegisterEndUserApplication") and not registered:
                                registered = True
                                self.on_register(msg, client_sock)
                                # After registration, push system info
                                self.send_msg(client_sock, {
                                    "type": "SystemInformation",
                                    "apiVersion": API_VERSION,
                                    "systemName": platform.node(),
                                })
                                self.send_msg(client_sock, {
                                    "type": "LcsConfigurationInformation",
                                    "apiVersion": API_VERSION,
                                    "configurationFlavor": "Lens Desktop",
                                })
                                continue

                            response = self.handle_message(msg, client_sock)
                            if response:
                                self.send_msg(client_sock, response)
                        except json.JSONDecodeError as e:
                            _log(f"  JSON error: {e} — raw: {line[:100]}", verbose_only=True)
                            pass

            except socket.timeout:
                continue
            except (OSError, ConnectionResetError):
                break

        _log(f"  Client disconnected")
        with self._lock:
            if client_sock in self.clients:
                self.clients.remove(client_sock)
        try:
            client_sock.close()
        except:
            pass

    dump_mode = False
    dump_file = None

    def send_msg(self, client_sock, msg):
        """Send a JSON message to a client (SOH-delimited)."""
        try:
            data = json.dumps(msg) + MSG_DELIM
            client_sock.sendall(data.encode("utf-8"))
            if self.dump_mode and self.dump_file:
                self.dump_file.write(json.dumps({"dir": "OUT", "msg": msg}) + "\n")
                self.dump_file.flush()
        except:
            pass

    def broadcast(self, msg):
        """Send a message to all connected clients."""
        with self._lock:
            for client in self.clients[:]:
                self.send_msg(client, msg)

    def handle_message(self, msg, client_sock):
        """Route an incoming message to the appropriate handler."""
        msg_type = msg.get("type", "")

        handlers = {
            "RegisterClient": self.on_register,
            "RegisterEndUserApplication": self.on_register,
            "GetDeviceList": self.on_get_device_list,
            "GetDeviceSettings": self.on_get_device_settings,
            "GetDeviceSetting": self.on_get_device_setting,
            "SetDeviceSetting": self.on_set_device_setting,
            "GetDeviceSettingsMetadata": self.on_get_settings_metadata,
            "GetDeviceDFUStatus": self.on_get_dfu_status,
            "GetDeviceLibraryVersion": self.on_get_library_version,
            "GetSoftphonesList": self.on_get_softphones,
            "GetPrimaryDevice": self.on_get_primary_device,
            "RegisterSoftphones": self.on_register_softphones,
            "GetAvailableSoftwareUpdate": self.on_get_software_update,
            "SlewDeviceSetting": self.on_slew_device_setting,
            "ScheduleDfuExecution": self.on_schedule_dfu,
            "PostponeDFU": self.on_postpone_dfu,
            "RemoveDevice": self.on_remove_device,
            "SoftphoneControl": self.on_softphone_control,
            "LogsPrepared": self.on_logs_prepared,
            "GAnalyticsSent": self.on_analytics,
        }

        # Proxy mode: forward settings/setting queries to LCS instead of
        # answering them ourselves. LCS has the full per-device settings
        # tree (it queries the device over PolyBus); our standalone
        # settings cache is only populated via _populate_settings_cache,
        # which never runs in proxy mode. Forwarding gives Studio access
        # to settings for every device LCS knows about - including
        # Voyager / Focus 2 / Sync devices we don't have HID profiles for.
        if (self.proxy_lcs and self._lcs_client
                and msg_type in ("GetDeviceSettings",
                                 "GetDeviceSettingsMetadata",
                                 "GetDeviceSetting",
                                 "GetSingleDeviceSetting",
                                 "SetDeviceSetting",
                                 "SetSingleDeviceSetting")):
            self._proxy_forward_to_lcs(msg, client_sock)
            return None  # response will arrive via the raw LCS handler

        handler = handlers.get(msg_type)
        if handler:
            result = handler(msg, client_sock)
            if result:
                _log(f"  -> {result.get('type', '?')}", verbose_only=True)
            return result

        # Catch-all — respond to any unknown message with an empty ack
        # This prevents the GUI from hanging waiting for a response
        _log(f"  Unknown: {msg_type} — {json.dumps(msg)[:150]}", verbose_only=True)
        return {
            "type": msg_type.replace("Get", "").replace("Set", "") if msg_type.startswith(("Get", "Set")) else "Error",
            "apiVersion": API_VERSION,
            "error": f"Not implemented: {msg_type}",
        }

    # ── Message Handlers ──────────────────────────────────────────────

    def on_register(self, msg, client_sock):
        """Handle RegisterClient."""
        name = msg.get("name", "unknown")
        _log(f"  Client registered: {name}")

        # Send ClientRegistered — tell client not to use encryption
        self.send_msg(client_sock, {
            "type": "ClientRegistered",
            "apiVersion": API_VERSION,
            "serviceVersion": "1.14.1",
            "serviceProductName": "Poly Lens Control Service",
            "configurationFlavor": "Lens Desktop",
            "displayName": "Poly Lens Control Service",
            "useEncryption": False,
        })

        # Push DeviceAttached for each known device
        with self._lock:
            devices_snapshot = list(self.devices.items())
        for did, dev in devices_snapshot:
            clean_dev = {k: v for k, v in dev.items() if not k.startswith("_")}
            self.send_msg(client_sock, {
                "type": "DeviceAttached",
                "apiVersion": API_VERSION,
                "device": clean_dev,
            })
            _log(f"  -> DeviceAttached: {dev.get('productName', '?')}", verbose_only=True)

            # Push battery info if available
            self._push_battery(client_sock, did)

            # Push firmware-update status. Studio doesn't poll for this -
            # it relies on LCS to notify proactively. Without this push
            # the Update Available button never renders even when our
            # DFU cache correctly knows a newer version exists.
            self._push_dfu_status_to_client(client_sock, did)

        return None

    def _push_dfu_status_to_client(self, client_sock, device_id):
        """Send DeviceDFUStatus + AvailableSoftwareUpdate for one device
        to one client. Used in the on_register loop so newly-connecting
        Studio instances get update availability without having to ask.

        If the DFU cache hasn't been populated yet (rare race - device
        attached but cloud check still in flight), skip silently. The
        broadcast in _prewarm_dfu_cache will catch this client too once
        the cache populates."""
        cached = self._dfu_cache.get(device_id)
        if not cached:
            return
        with self._lock:
            dev = self.devices.get(device_id, {})
        current = dev.get("firmwareVersion", "")
        latest = cached.get("version", "")
        statuses = cached.get("statuses", [])

        try:
            self.send_msg(client_sock, {
                "type": "DeviceDFUStatus",
                "apiVersion": API_VERSION,
                "deviceId": device_id,
                "version": latest,
                "statuses": statuses,
                "releaseNoteUrl": cached.get("releaseNoteUrl", ""),
            })
            self.send_msg(client_sock, {
                "type": "AvailableSoftwareUpdate",
                "apiVersion": API_VERSION,
                "deviceId": device_id,
                "availableVersion": latest,
                "currentVersion": current,
                "statuses": statuses,
                "canPostpone": True,
                "component": "firmware",
                "releaseNoteUrl": cached.get("releaseNoteUrl", ""),
            })
            _log(f"  -> DFU status to {dev.get('productName', '?')}: "
                 f"{', '.join(statuses) if statuses else 'no updates'}",
                 verbose_only=True)
        except Exception as e:
            _log(f"  push DFU to client failed: {e}", verbose_only=True)

    def _refresh_device_battery(self, device_id, broadcast=True):
        """Read the current battery state from native bridge, write it
        into the device record, and (optionally) broadcast a DeviceUpdated
        event so connected Poly Studio cards re-render their battery icon.

        Studio reads battery from the per-device record's `battery`
        sub-block (matching stock LCS's wire format). Updating that field
        and pushing DeviceUpdated is the path Studio's UI hooks listen on.
        """
        if not self._native_bridge:
            return False
        with self._lock:
            dev = self.devices.get(device_id, {})
        if not dev:
            return False

        ptd = dev.get("_polytool_dev", {}) or {}
        native_id = ptd.get("_native_id", "")
        batt = None
        if native_id:
            batt = self._native_bridge.get_battery(native_id)
        # Fallback: scan native devices for a PID match
        if not batt:
            try:
                dev_pid = int(dev.get("pid", 0))
            except (TypeError, ValueError):
                dev_pid = 0
            if dev_pid:
                for nid, ndev in (self._native_bridge.get_devices() or {}).items():
                    if ndev.get("pid") == dev_pid:
                        batt = self._native_bridge.get_battery(nid)
                        if batt and batt.get("level", -1) >= 0:
                            # Cache the native_id so future lookups skip the scan
                            ptd["_native_id"] = nid
                            break
        if not batt:
            return False

        # Forward the native-bridge battery dict verbatim - same wire
        # format real LCS uses, which is what current Studio expects.
        # Fields we've observed real LCS populate (from LegacyHostApp.log):
        #   chargeLevel       0-5 scale on DECT (NOT a percentage!)
        #   level             often equal to chargeLevel - 1
        #   isChargeLevelValid bool flag - false suppresses the icon entirely
        #   charging          bool
        #   docked            bool
        #   realLevel         raw battery counts when device exposes them
        #   realMaxLevel      max raw count for the percentage calc
        #
        # Important: we used to coerce chargeLevel to a percentage (level*20),
        # which made Studio render NOTHING because Studio expects the 0-5
        # raw scale and computes its own display from there. Pass through
        # the native values unchanged.
        def _clean(v):
            return v if v is not None else -1
        new_battery = {
            "chargeLevel": _clean(batt.get("chargeLevel", batt.get("level", -1))),
            "level": _clean(batt.get("level", -1)),
            "charging": bool(batt.get("charging", False)),
            "docked": bool(batt.get("docked", False)),
            "isChargeLevelValid": bool(batt.get("isChargeLevelValid",
                                                batt.get("level", -1) >= 0)),
            "realLevel": _clean(batt.get("realLevel", -1)),
            "realMaxLevel": _clean(batt.get("realMaxLevel", -1)),
        }
        # Synthesize a percentage too, in case some Studio path reads it.
        # Use chargeLevel (0-5 scale) -> percentage. Falls back to a
        # straight pass-through for devices that already report percentage.
        cl = new_battery["chargeLevel"]
        if 0 <= cl <= 5:
            new_battery["chargeLevelPercentage"] = cl * 20
        elif cl > 5:
            new_battery["chargeLevelPercentage"] = min(100, cl)
        else:
            new_battery["chargeLevelPercentage"] = -1

        with self._lock:
            old = dev.get("battery", {}) or {}
            dev["battery"] = new_battery
            changed = (old.get("chargeLevel") != new_battery["chargeLevel"]
                       or old.get("charging") != new_battery["charging"]
                       or old.get("docked") != new_battery["docked"])

        if broadcast and changed:
            clean_dev = {k: v for k, v in dev.items() if not k.startswith("_")}
            self.broadcast({
                "type": "DeviceUpdated",
                "apiVersion": API_VERSION,
                "device": clean_dev,
            })
            _log(f"  Battery {device_id}: chargeLevel={new_battery['chargeLevel']} "
                 f"({new_battery.get('chargeLevelPercentage', '?')}%) "
                 f"charging={new_battery['charging']} "
                 f"docked={new_battery['docked']}", verbose_only=True)
        return True

    def _battery_poller(self):
        """Background thread that refreshes battery for every connected
        device every 30 seconds. Battery state changes regularly (charge,
        dock, removal) and Studio expects continuous updates."""
        while self.running:
            try:
                with self._lock:
                    device_ids = list(self.devices.keys())
                for did in device_ids:
                    self._refresh_device_battery(did, broadcast=True)
            except Exception as e:
                _log(f"  battery poller error: {e}", verbose_only=True)
            # Sleep in 1s chunks so server.stop() responds quickly
            for _ in range(30):
                if not self.running:
                    return
                time.sleep(1)

    def _push_battery(self, client_sock, device_id):
        """Push battery info as a compound DeviceSetting."""
        if not self._native_bridge:
            return
        # Match our device ID to native device ID
        with self._lock:
            dev = self.devices.get(device_id, {})
        ptd = dev.get("_polytool_dev", {})
        native_id = ptd.get("_native_id", "")

        # Also check all battery entries by PID match
        batt = None
        if native_id:
            batt = self._native_bridge.get_battery(native_id)
        if not batt:
            dev_pid = int(dev.get("pid", 0))
            for nid, ndev in self._native_bridge.get_devices().items():
                if ndev.get("pid") == dev_pid:
                    batt = self._native_bridge.get_battery(nid)
                    break
        if not batt or batt.get("level", -1) < 0:
            return

        level = batt["level"]
        # Native bridge uses 0-5 scale, convert to percentage
        level_pct = min(100, level * 20) if level <= 5 else level
        charging = batt.get("charging", False)

        self.send_msg(client_sock, {
            "type": "DeviceSetting",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "setting": {
                "name": "Singular Battery Info",
                "value": None,
                "value_compound": [
                    {"name": "Level", "value": level_pct, "value_int": level_pct},
                    {"name": "Num Levels", "value": 100, "value_int": 100},
                    {"name": "Charging", "value": charging, "value_bool": charging},
                ],
            },
        })
        _log(f"  -> Battery: {level_pct}%{' (charging)' if charging else ''}", verbose_only=True)

    def on_get_device_list(self, msg, client_sock):
        """Handle GetDeviceList."""
        with self._lock:
            devices_values = list(self.devices.values())
        clean_devices = []
        for dev in devices_values:
            clean_devices.append({k: v for k, v in dev.items() if not k.startswith("_")})
        return {
            "type": "DeviceList",
            "apiVersion": API_VERSION,
            "devices": clean_devices,
        }

    def on_get_device_settings(self, msg, client_sock):
        """Handle GetDeviceSettings."""
        device_id = msg.get("deviceId", "")
        metadata, values = self._get_device_settings_formatted(device_id)

        # Push metadata FIRST (GUI needs this to know what controls to render)
        if metadata:
            self.send_msg(client_sock, {
                "type": "DeviceSettingsMetadata",
                "apiVersion": API_VERSION,
                "deviceId": device_id,
                "settings": metadata,
            })
            _log(f"  -> DeviceSettingsMetadata: {len(metadata)} settings", verbose_only=True)

        # Then send settings values
        self.send_msg(client_sock, {
            "type": "DeviceSettings",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "settings": values,
        })

        return None  # already sent

    def on_get_device_setting(self, msg, client_sock):
        """Handle GetDeviceSetting."""
        device_id = msg.get("deviceId", "")
        name = msg.get("name", "")

        # Handle special built-in settings
        with self._lock:
            dev = self.devices.get(device_id, {})

        # Build a proper DeviceSetting response
        # The GUI destructures: const {name} = setting
        # So we need: {type: "DeviceSetting", setting: {name, value, ...}}
        def make_setting_response(sname, svalue, **extra):
            return {
                "type": "DeviceSetting",
                "apiVersion": API_VERSION,
                "deviceId": device_id,
                "setting": {
                    "name": sname,
                    "value": svalue,
                    **extra,
                },
            }

        if name == "Product Images":
            image_data = self.get_product_images(device_id)
            return make_setting_response(name, image_data)

        if name == "Ear Cushion Type":
            return make_setting_response(name, None)

        if name == "Device Info":
            return make_setting_response(name, dev.get("firmwareVersion", ""),
                valueCompound=[
                    {"name": "sw_version", "value": dev.get("firmwareVersion", "")},
                    {"name": "serial_number", "value": dev.get("serialNumber", "")},
                    {"name": "product_name", "value": dev.get("productName", "")},
                ])

        # Try reading from HID
        settings = self.read_device_settings(device_id)
        for s in settings:
            if s.get("name") == name:
                return make_setting_response(name, s.get("value"))

        return make_setting_response(name, None)

    def on_set_device_setting(self, msg, client_sock):
        """Handle SetDeviceSetting."""
        device_id = msg.get("deviceId", "")
        name = msg.get("name", "")
        # Poly Studio sends value in various fields depending on type
        value = msg.get("valueBool",
                msg.get("valueInt",
                msg.get("valueFloat",
                msg.get("valueString",
                msg.get("valueEnum",
                msg.get("value"))))))  # fallback to generic 'value'

        # Admin password bypass — always accept, unlocking admin settings
        if name == "Login Password":
            _log(f"  Admin password accepted (bypass) for {device_id}")
            # Renderer waits for DeviceSettingUpdated with status=true
            self.send_msg(client_sock, {
                "type": "DeviceSettingUpdated",
                "apiVersion": API_VERSION,
                "deviceId": device_id,
                "settingName": name,
                "status": True,
                "errorDesc": "",
            })
            return None  # already sent

        # Store in cache
        with self._lock:
            if device_id not in self._device_settings_cache:
                self._device_settings_cache[device_id] = {}
            self._device_settings_cache[device_id][name] = value

        # Try writing to device via HID
        success = self.write_device_setting(device_id, name, value)
        _log(f"  Set {name} = {value} [{'OK' if success else 'stored'}]")

        # Broadcast the update to all clients
        update_msg = {
            "type": "DeviceSettingUpdated",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "setting": {"name": name, "value": value},
        }
        self.broadcast(update_msg)

        return None  # already broadcast

    # Per-device settings value cache
    _device_settings_cache = {}

    def on_get_settings_metadata(self, msg, client_sock):
        """Handle GetDeviceSettingsMetadata."""
        device_id = msg.get("deviceId", "")
        metadata, _ = self._get_device_settings_formatted(device_id)
        return {
            "type": "DeviceSettingsMetadata",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "settings": metadata,
        }

    # Cache of dynamic settings profiles built from native bridge data
    _dynamic_profiles = {}  # device_id → [setting_defs]

    def _get_device_settings_formatted(self, device_id):
        """Get settings metadata + values in LensServiceApi format."""
        from lens_settings import (get_settings_for_device, settings_to_api_format,
                                   get_device_family)

        with self._lock:
            dev = self.devices.get(device_id, {})
            dynamic_defs = self._dynamic_profiles.get(device_id, [])
            current_values = self._device_settings_cache.get(device_id, {}).copy()
        ptd = dev.get("_polytool_dev", {})
        usage_page = ptd.get("usage_page", 0)
        dfu_executor = ptd.get("dfu_executor", "")
        pid = ptd.get("pid", 0)
        family = get_device_family(usage_page, dfu_executor, pid=pid)

        # Pick settings profile: dynamic (from native bridge) vs static (from
        # lens_settings, which itself checks PREFER_OFFICIAL_SETTINGS)
        from lens_settings import PREFER_OFFICIAL_SETTINGS
        hardcoded_defs = get_settings_for_device(usage_page, dfu_executor, pid=pid)
        if PREFER_OFFICIAL_SETTINGS:
            settings_defs = dynamic_defs if dynamic_defs else hardcoded_defs
        else:
            settings_defs = dynamic_defs if len(dynamic_defs) > len(hardcoded_defs) else hardcoded_defs

        # DECT/Voyager settings are writable when native bridge is available
        force_writable = False
        if family in ("dect", "voyager_bt", "voyager_base"):
            try:
                from native_bridge import find_components_dir
                force_writable = find_components_dir() is not None
            except Exception:
                pass

        return settings_to_api_format(settings_defs, current_values, family=family,
                                      force_writable=force_writable)

    # Cache: device_id → {version, statuses, releaseNoteUrl}
    _dfu_cache = {}

    def on_get_dfu_status(self, msg, client_sock):
        """Handle GetDeviceDFUStatus - check Poly Cloud for firmware updates.

        Returns the real cloud answer synchronously the first time, then
        caches it. Earlier this method returned an 'Equal' placeholder and
        broadcast the real result later, but Poly Studio doesn't always
        re-render its UI when the followup arrives - the 'Update Available'
        button stays hidden because Studio decided 'no update' on the
        first response. The cloud query is ~200ms so blocking is fine.
        """
        device_id = msg.get("deviceId", "")
        if device_id not in self._dfu_cache:
            self._dfu_cache[device_id] = self._check_firmware_update(device_id)
            status_str = ", ".join(self._dfu_cache[device_id].get("statuses", []))
            _log(f"  Firmware check {device_id}: {status_str}", verbose_only=True)
        cached = self._dfu_cache[device_id]
        return {
            "type": "DeviceDFUStatus",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            **cached,
        }

    def _prewarm_dfu_cache(self, device_id):
        """Populate the DFU cache for a freshly-attached device AND push
        the result to all connected clients.

        Poly Studio doesn't poll GetDeviceDFUStatus — it relies on LCS
        to proactively notify it via DeviceDFUStatus events whenever
        firmware-update availability changes. So we have to broadcast
        the status, not just cache it. Done in a background thread
        because the cloud query takes ~200ms and we don't want to delay
        the DeviceAttached broadcast.

        Also broadcasts AvailableSoftwareUpdate (the message type Studio
        uses to render the per-device Update Available button) since
        different Studio versions key off different message names.
        """
        import threading
        def _go():
            try:
                if device_id not in self._dfu_cache:
                    self._dfu_cache[device_id] = self._check_firmware_update(device_id)
                cached = self._dfu_cache[device_id]
                with self._lock:
                    dev = self.devices.get(device_id, {})
                current = dev.get("firmwareVersion", "")
                latest = cached.get("version", "")
                statuses = cached.get("statuses", [])

                self.broadcast({
                    "type": "DeviceDFUStatus",
                    "apiVersion": API_VERSION,
                    "deviceId": device_id,
                    "version": latest,
                    "statuses": statuses,
                    "releaseNoteUrl": cached.get("releaseNoteUrl", ""),
                })
                self.broadcast({
                    "type": "AvailableSoftwareUpdate",
                    "apiVersion": API_VERSION,
                    "deviceId": device_id,
                    "availableVersion": latest,
                    "currentVersion": current,
                    "statuses": statuses,
                    "canPostpone": True,
                    "component": "firmware",
                    "releaseNoteUrl": cached.get("releaseNoteUrl", ""),
                })
                _log(f"  Pushed update status for {device_id}: "
                     f"{', '.join(statuses) if statuses else 'no updates'}",
                     verbose_only=True)
            except Exception as e:
                _log(f"  prewarm_dfu_cache failed for {device_id}: {e}",
                     verbose_only=True)
        threading.Thread(target=_go, daemon=True).start()

    def _check_firmware_update(self, device_id):
        """Check Poly Cloud for available firmware update."""
        with self._lock:
            dev = self.devices.get(device_id, {})
        current_fw = dev.get("firmwareVersion", "")
        pid = dev.get("productId", "")
        pid_query = pid.lstrip("0") or pid

        if not pid_query:
            return {"version": current_fw, "statuses": ["AvailableFirmwareVersionEqual"], "releaseNoteUrl": ""}

        try:
            import requests
            # The Poly cloud schema requires `pid: ID!`. Using `String!`
            # parses successfully but always returns null for the bundle —
            # which used to suppress the "Update Available" button in Poly
            # Studio for every device, since lensserver would silently
            # report AvailableFirmwareVersionEqual on the empty response.
            query = '''query ($pid: ID!) {
              availableProductSoftwareByPid(pid: $pid) {
                version
                publishDate
                releaseChannel
                latest
                productBuild { archiveUrl }
                product { name }
              }
            }'''
            resp = requests.post(
                "https://api.silica-prod01.io.lens.poly.com/graphql",
                json={"query": query, "variables": {"pid": pid_query}},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            data = resp.json()
            sw = (data.get("data") or {}).get("availableProductSoftwareByPid")
            if not sw:
                return {"version": "", "statuses": ["AvailableFirmwareVersionEqual"], "releaseNoteUrl": ""}

            cloud_version = sw.get("version", "")

            # Compare versions
            from polytool import _normalize_version
            current_norm = _normalize_version(current_fw)
            cloud_norm = _normalize_version(cloud_version)

            if cloud_norm > current_norm:
                statuses = ["AvailableFirmwareVersionHigher"]
            elif cloud_norm < current_norm:
                statuses = ["AvailableFirmwareVersionLower"]
            else:
                statuses = ["AvailableFirmwareVersionEqual"]

            return {
                "version": cloud_version,
                "statuses": statuses,
                "releaseNoteUrl": "",
            }

        except Exception as e:
            _log(f"  Firmware check error for {device_id}: {e}", verbose_only=True)
            return {"version": "", "statuses": ["AvailableFirmwareVersionEqual"], "releaseNoteUrl": ""}

    def on_get_library_version(self, msg, client_sock):
        return {
            "type": "DeviceLibraryVersion",
            "apiVersion": API_VERSION,
            "version": "1.0.0-polytool",
        }

    def on_get_softphones(self, msg, client_sock):
        return {
            "type": "SoftphonesList",
            "apiVersion": API_VERSION,
            "softphones": [],
        }

    def on_get_primary_device(self, msg, client_sock):
        # Return first device as primary
        with self._lock:
            first_id = next(iter(self.devices), "")
        return {
            "type": "PrimaryDevice",
            "apiVersion": API_VERSION,
            "primaryDeviceInfo": {"deviceId": first_id} if first_id else None,
        }

    def on_register_softphones(self, msg, client_sock):
        return {
            "type": "SoftphonesRegistered",
            "apiVersion": API_VERSION,
        }

    def on_get_software_update(self, msg, client_sock):
        """Handle GetAvailableSoftwareUpdate - Poly Studio uses this to
        decide whether to render the 'Update Available' button on a
        device card AND for app self-update checks.

        Two callers, distinguished by which fields are present:
          - App self-update:    component='lens-desktop-windows' (no deviceId)
          - Device firmware:    deviceId set, component may be 'firmware'

        We always return empty for app updates (we're not Poly Lens, no
        self-update). For device firmware we route through the same
        cloud check on_get_dfu_status uses, so the button appears when
        a real update exists in Poly's catalog.
        """
        device_id = msg.get("deviceId", "")
        component = msg.get("component", "")
        _log(f"  GetAvailableSoftwareUpdate device={device_id!r} "
             f"component={component!r}", verbose_only=True)

        # App self-update query - always say "no update" since we're not
        # the Lens desktop app.
        if not device_id:
            return {
                "type": "AvailableSoftwareUpdate",
                "apiVersion": API_VERSION,
                "availableVersion": "",
                "currentVersion": "",
                "statuses": [],
                "canPostpone": False,
                "component": component,
            }

        # Device firmware query - reuse the cached DFU status from
        # on_get_dfu_status. Pre-warm if we haven't checked yet.
        if device_id not in self._dfu_cache:
            self._dfu_cache[device_id] = self._check_firmware_update(device_id)
        cached = self._dfu_cache[device_id]
        statuses = cached.get("statuses", [])
        latest = cached.get("version", "")

        # Pull current firmware version from the device record so Studio
        # can show "v1065 -> v1082" in the update prompt.
        with self._lock:
            dev = self.devices.get(device_id, {})
        current = dev.get("firmwareVersion", "")

        return {
            "type": "AvailableSoftwareUpdate",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "availableVersion": latest,
            "currentVersion": current,
            "statuses": statuses,
            "canPostpone": True,
            "component": component or "firmware",
            "releaseNoteUrl": cached.get("releaseNoteUrl", ""),
        }

    def on_slew_device_setting(self, msg, client_sock):
        """Handle SlewDeviceSetting — gradual setting change (sliders)."""
        device_id = msg.get("deviceId", "")
        name = msg.get("settingName", msg.get("name", ""))
        value = msg.get("value")
        active = msg.get("active", True)

        if active and value is not None:
            # Update cache with current slider value
            with self._lock:
                if device_id not in self._device_settings_cache:
                    self._device_settings_cache[device_id] = {}
                self._device_settings_cache[device_id][name] = value

            # Try writing to device
            self.write_device_setting(device_id, name, value)
            _log(f"  Slew {name} = {value} (active={active})", verbose_only=True)

        # Notify clients of the slew update
        self.broadcast({
            "type": "DeviceSettingSlewUpdated",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "settingName": name,
            "value": value,
            "active": active,
        })
        return None  # already broadcast

    def _maybe_warn_ff_setid(self, dev):
        """If a newly-attached device has FFFF SetID, log a one-line hint.

        Reads LCS's own device cache files for the diagnosis; falls back
        silently if the cache isn't available (Poly Lens not installed yet,
        or device hasn't been seen by LCS before).
        """
        try:
            from setid_fix import diagnose_setid
        except Exception:
            return
        serial = dev.get("serialNumber") or dev.get("displaySerialNumber") or ""
        if not serial:
            return
        try:
            info = diagnose_setid(serial)
        except Exception:
            return
        if info.get("state") != "ff":
            return
        name = dev.get("productName", "device")
        _log(f"  ! {name}: unprogrammed SetID NVRAM (FirmwareVersion={info['firmware_version']!r})")
        _log(f"  ! Fix:   polytool fix-setid       (fast, ~10 sec)")
        _log(f"  !       polytool update-legacy   (full firmware update, ~10 min)")

    def on_schedule_dfu(self, msg, client_sock):
        """Handle ScheduleDfuExecution — firmware update request.

        Spawns a background thread that:
          1. Looks up the latest cloud bundle for this device's PID
          2. Downloads it to the firmware cache
          3. Pauses our native bridge so LegacyHost can take over USB
          4. Runs install_bundle() (LegacyDfu + LegacyHost pipeline)
          5. Restarts our native bridge

        Sends DfuExecutionStatus events back to the requesting client at
        the major phase transitions (PreparingDevice → InProgress → Succeeded/Failed).
        """
        device_id = msg.get("deviceId", "")
        request_id = msg.get("id", "")

        with self._lock:
            dev = self.devices.get(device_id)
        if not dev:
            return {
                "type": "DfuExecutionStatus",
                "apiVersion": API_VERSION,
                "deviceId": device_id,
                "dfuRequestId": request_id,
                "status": "Failed",
                "progress": 0,
                "errorReason": f"Unknown device: {device_id}",
            }

        ptd = dev.get("_polytool_dev", {})
        serial = dev.get("serialNumber") or dev.get("displaySerialNumber") or ""
        pid_int = ptd.get("pid", 0)
        vid_int = 0x047F  # Plantronics — only vendor we care about
        if not serial or not pid_int:
            return {
                "type": "DfuExecutionStatus",
                "apiVersion": API_VERSION,
                "deviceId": device_id,
                "dfuRequestId": request_id,
                "status": "Failed",
                "progress": 0,
                "errorReason": "Device missing serial/pid metadata",
            }

        threading.Thread(
            target=self._run_dfu_async,
            args=(client_sock, device_id, request_id, vid_int, pid_int, serial),
            daemon=True,
        ).start()

        # Initial response — Poly Studio shows "preparing" while we download
        return {
            "type": "DfuExecutionStatus",
            "apiVersion": API_VERSION,
            "deviceId": device_id,
            "dfuRequestId": request_id,
            "status": "PreparingDevice",
            "progress": 0,
            "firmwareDownloadStatus": "Pending",
        }

    def _push_dfu_status(self, client_sock, device_id, request_id,
                         status, progress=0, dl_status="", err=""):
        """Send a DfuExecutionStatus event to a single client (best-effort)."""
        try:
            self.send_msg(client_sock, {
                "type": "DfuExecutionStatus",
                "apiVersion": API_VERSION,
                "deviceId": device_id,
                "dfuRequestId": request_id,
                "status": status,
                "progress": progress,
                "firmwareDownloadStatus": dl_status or "Pending",
                "errorReason": err,
            })
        except Exception as e:
            _log(f"  DFU status push failed: {e}")

    def _run_dfu_async(self, client_sock, device_id, request_id, vid, pid, serial):
        """Background DFU worker. Releases native bridge for LegacyHost access."""
        try:
            from firmware import PolyCloudAPI
            from setid_fix import install_bundle
            from devices import PolyDevice

            _log(f"  DFU starting: {serial} (PID 0x{pid:04X})")

            cloud = PolyCloudAPI()
            stub = PolyDevice(vid=vid, pid=pid, lens_product_id=f"{pid:04x}")
            info = cloud.check_firmware(stub)
            if not info or not info.get("download_url"):
                _log(f"  DFU: no firmware available from Poly Cloud for PID 0x{pid:04X}")
                self._push_dfu_status(client_sock, device_id, request_id,
                                       "Failed", err="No firmware available")
                return

            url = info["download_url"]
            _log(f"  DFU: downloading {info.get('latest', '?')} from cloud")
            self._push_dfu_status(client_sock, device_id, request_id,
                                   "PreparingDevice", dl_status="InProgress")
            path = cloud.download_firmware(url)
            if not path:
                _log("  DFU: download failed")
                self._push_dfu_status(client_sock, device_id, request_id,
                                       "Failed", err="Bundle download failed")
                return
            _log(f"  DFU: bundle ready: {path}")

            self._push_dfu_status(client_sock, device_id, request_id,
                                   "InProgress", progress=0, dl_status="Completed")

            # Release native bridge so LegacyHost can take over USB handles
            paused = self._pause_native_bridge_for_dfu()

            try:
                _log("  DFU: running LegacyDfu (this can take several minutes)")
                # Throttle pushes - LegacyDfu emits progress every ~2s but
                # also fires NOTIFY_PROGRESS many times per second during
                # some phases. Push to Studio at most once per percent so
                # we don't flood the SOH socket.
                last_pct = [-1]
                def _on_progress(pct, phase, message):
                    if pct == last_pct[0]:
                        return
                    last_pct[0] = pct
                    self._push_dfu_status(
                        client_sock, device_id, request_id,
                        "InProgress", progress=pct,
                        dl_status="Completed",
                    )
                result = install_bundle(
                    zip_path=path, vid=vid, pid=pid, serial=serial, log=_log,
                    progress_cb=_on_progress,
                )
            finally:
                if paused:
                    self._resume_native_bridge_after_dfu()

            if result["success"]:
                _log("  DFU: success")
                self._push_dfu_status(client_sock, device_id, request_id,
                                       "Succeeded", progress=100, dl_status="Completed")
            else:
                _log(f"  DFU: failed — {result.get('message', 'unknown')}")
                self._push_dfu_status(client_sock, device_id, request_id,
                                       "Failed", err=result.get("message", "DFU failed"))
        except Exception as e:
            _log(f"  DFU thread crashed: {e}")
            try:
                self._push_dfu_status(client_sock, device_id, request_id,
                                       "Failed", err=str(e))
            except Exception:
                pass

    def _pause_native_bridge_for_dfu(self):
        """Stop the native bridge so LegacyHost (spawned by install_bundle) can
        get exclusive USB access. Returns True if a bridge was running, so the
        caller knows to resume it after."""
        if not self._native_bridge:
            return False
        try:
            self._native_bridge.stop()
        except Exception as e:
            _log(f"  Could not stop native bridge cleanly: {e}")
        self._native_bridge = None
        return True

    def _resume_native_bridge_after_dfu(self):
        """Re-initialize native bridge after a DFU. Best-effort; if it fails
        the next discovery cycle will try again."""
        try:
            self._get_native_bridge()
        except Exception as e:
            _log(f"  Native bridge restart failed: {e} (will retry on next scan)")

    def on_postpone_dfu(self, msg, client_sock):
        """Handle PostponeDFU — defer firmware update."""
        _log(f"  DFU postponed for {msg.get('deviceId', '')}", verbose_only=True)
        return None

    def on_remove_device(self, msg, client_sock):
        """Handle RemoveDevice — forget a detached device."""
        device_id = msg.get("deviceId", "")
        with self._lock:
            dev = self.devices.pop(device_id, None)
            self._device_settings_cache.pop(device_id, None)
            self._dynamic_profiles.pop(device_id, None)
        if dev is not None:
            _log(f"  Removed device: {dev.get('productName', device_id)}")
            self.broadcast({
                "type": "DeviceDetached",
                "apiVersion": API_VERSION,
                "deviceId": device_id,
            })
        return None

    def on_softphone_control(self, msg, client_sock):
        """Handle SoftphoneControl — enable/disable a softphone."""
        _log(f"  Softphone control: id={msg.get('id')} enabled={msg.get('enabled')}", verbose_only=True)
        return {
            "type": "SoftphoneStatus",
            "apiVersion": API_VERSION,
            "id": msg.get("id", ""),
            "enabled": msg.get("enabled", True),
            "connected": False,
        }

    def on_logs_prepared(self, msg, client_sock):
        """Handle LogsPrepared — client responding to PrepareLogs request."""
        _log(f"  Logs prepared: {msg.get('filePath', '')}", verbose_only=True)
        return None

    def on_analytics(self, msg, client_sock):
        """Handle GAnalyticsSent — analytics telemetry (ignored)."""
        return None

    # ── Device I/O (delegated to our HID code) ───────────────────────

    _image_cache = {}  # pid → image JSON string

    def get_product_images(self, device_id):
        """Fetch product images from Poly Cloud for a device."""
        with self._lock:
            dev = self.devices.get(device_id, {})
        pid = dev.get("productId", "")
        if not pid:
            return None

        # Poly Cloud API wants no leading zeros (e.g. "2ea" not "02ea")
        pid_query = pid.lstrip("0") or pid

        if pid in self._image_cache:
            return self._image_cache[pid]

        try:
            import requests
            query = '''query ($id: ID!) {
              hardwareProduct(id: $id) {
                productImages { edges { node { url } } }
              }
            }'''
            resp = requests.post(
                "https://api.silica-prod01.io.lens.poly.com/graphql",
                json={"query": query, "variables": {"id": pid_query}},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            data = resp.json()
            images = data.get("data", {}).get("hardwareProduct", {}).get("productImages", {}).get("edges", [])

            if images:
                # Return as JSON string — GUI parses it
                url_list = [{"url": img["node"]["url"]} for img in images if img.get("node", {}).get("url")]
                result = json.dumps(url_list)
                self._image_cache[pid] = result
                _log(f"  Images for {pid}: {len(url_list)} found", verbose_only=True)
                return result
        except Exception as e:
            _log(f"  Image fetch error: {e}", verbose_only=True)

        return None

    def discover_devices(self):
        """Scan for Poly devices. Returns set of current device IDs.

        In proxy mode the LCS event stream (DeviceAttached/Updated/Detached)
        is the source of truth, so this method just returns the current
        snapshot without re-scanning USB. Standalone mode still does the
        full HID discovery + native_bridge enumeration."""
        if self.proxy_lcs and self._lcs_client:
            with self._lock:
                return set(self.devices.keys())
        try:
            from polytool import discover_devices, try_read_device_info, try_read_battery
            raw_devices = discover_devices()
            current_ids = set()
            for dev in raw_devices:
                try_read_device_info(dev)
                try_read_battery(dev)

                device_id = dev.id
                current_ids.add(device_id)

                # Only add if new (don't overwrite existing — preserves state)
                with self._lock:
                    already_exists = device_id in self.devices
                if already_exists:
                    continue

                # Use display_name (which appends the tattoo serial when two
                # devices share a product name) so Poly Studio shows them as
                # distinguishable cards instead of identical "Poly Savi 7300
                # Office Series" twins.
                disp_name = dev.display_name or dev.friendly_name or dev.product_name
                new_device = {
                    # camelCase — matches .NET JsonSerializer default naming
                    "deviceId": device_id,
                    "parentId": "",
                    "productName": disp_name,
                    "systemName": disp_name,
                    "deviceName": disp_name,
                    "manufacturerName": dev.manufacturer or "Plantronics",
                    "displaySerialNumber": dev.tattoo_serial or dev.serial or "",
                    "buildCode": "",
                    "firmwareVersion": dev.firmware_display,
                    "serialNumber": dev.serial or "",
                    "tattooSerialNumber": dev.tattoo_serial or dev.serial or "",
                    "deviceType": (dev.category or "headset").capitalize(),
                    "connected": True,
                    "attached": True,
                    "pid": str(dev.pid),
                    "vid": str(dev.vid),
                    "productId": f"{dev.pid:04x}",
                    "macAddress": "",
                    "bluetoothAddress": "",
                    "hardwareRevision": "",
                    # Per-component versions: prefer the LCS-cache values
                    # (DECT base + headset have different firmware so usb !=
                    # headset). Fall back to firmware_display when nothing
                    # better is known.
                    "headsetVersion": _comp(dev, "headset") or dev.firmware_display,
                    "baseVersion": _comp(dev, "base"),
                    "usbVersion": _comp(dev, "usb") or dev.firmware_display,
                    "firmwareComponents": {
                        "usbVersion": _comp(dev, "usb") or dev.firmware_display,
                        "baseVersion": _comp(dev, "base"),
                        "tuningVersion": _comp(dev, "tuning"),
                        "picVersion": _comp(dev, "pic"),
                        "cameraVersion": _comp(dev, "camera"),
                        "headsetVersion": _comp(dev, "headset"),
                        "headsetLanguageVersion": "",
                        "bluetoothVersion": _comp(dev, "bluetooth"),
                        "setIdVersion": _comp(dev, "setid"),
                    },
                    "peerDevices": [],
                    "connectionType": "USB",
                    "connectionDetails": [{"type": "USB", "handledBy": "Legacy Library"}],
                    "hardwareModel": {"supportedByClients": []},
                    "hasChargeCase": False,
                    "deviceEncryption": "Unknown",
                    "dfuMode": False,
                    "isAbleToBePrimaryForCallControl": True,
                    "isMuted": False,
                    "isInCall": False,
                    # Battery sub-block in the device record itself - this
                    # is what Studio actually reads to render the battery
                    # icon on the device card. Field set matches stock
                    # LCS exactly (chargeLevel is 0-5 raw, NOT a percent).
                    # Populated by _refresh_device_battery right after attach.
                    "battery": {
                        "chargeLevel": -1,
                        "level": -1,
                        "charging": False,
                        "docked": False,
                        "isChargeLevelValid": False,
                        "realLevel": -1,
                        "realMaxLevel": -1,
                        "chargeLevelPercentage": -1,
                    },
                    "state": "Online",
                    "supportData": {"state": "Supported"},
                    "multiComponentState": None,
                    "lastAttachedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    # Internal — not sent to clients
                    "_polytool_dev": {
                        "path": dev.path,
                        "usage_page": dev.usage_page,
                        "dfu_executor": dev.dfu_executor,
                        "pid": dev.pid,
                    },
                }

                with self._lock:
                    self.devices[device_id] = new_device

                # Populate settings cache from real HID reads
                self._populate_settings_cache(device_id, dev)

                # Pre-warm the cloud DFU check so Studio's first
                # GetDeviceDFUStatus query returns the real answer instead of
                # a stale Equal placeholder. Devices added by the periodic
                # _device_scanner already get this through its own pre-warm
                # call; this covers the very-first discovery at startup.
                self._prewarm_dfu_cache(device_id)

                # Read battery from native bridge so the device record
                # has real battery data the first time Studio renders it
                # (no waiting for the next 30s poll cycle).
                self._refresh_device_battery(device_id, broadcast=False)

            # Remove USB devices no longer present (preserve native bridge devices)
            with self._lock:
                removed = set(self.devices.keys()) - current_ids
                for did in removed:
                    dev_entry = self.devices.get(did, {})
                    ptd = dev_entry.get("_polytool_dev", {})
                    if not ptd.get("_native_id"):  # only remove USB devices
                        del self.devices[did]

            return current_ids
        except Exception as e:
            _log(f"  Device discovery error: {e}")
            return set()

    def discover_native_devices(self):
        """Discover additional devices via native bridge (BT headsets, etc.)."""
        try:
            from native_bridge import find_components_dir
            if not find_components_dir():
                return
        except Exception:
            return

        bridge = self._get_native_bridge()
        if not bridge:
            return

        native_devs = bridge.get_devices()
        for nid, ndev in native_devs.items():
            # Skip devices we already found via USB
            pid = ndev.get("pid", 0)
            name = ndev.get("name", "")
            model_id = ndev.get("modelId", "")

            # Check if we already have this device (by PID match)
            with self._lock:
                already_have = any(
                    d.get("pid") == str(pid) or d.get("productId") == model_id.lower()
                    for d in self.devices.values()
                )
            if already_have:
                continue

            # Generate a stable device ID from native ID
            device_id = f"{int(nid) & 0xFFFFFFFF:08X}"

            fw = _sanitize_firmware_components(ndev.get("firmwareVersion", {}))
            fw_display = fw.get("bluetooth", "") or fw.get("usb", "") or fw.get("headset", "")

            # Find serial
            serial = ""
            for sn in ndev.get("serialNumber", []):
                if sn.get("type") == "genes":
                    val = sn.get("value", {})
                    serial = val.get("headset", "") or val.get("base", "")
                    break

            new_device = {
                "deviceId": device_id,
                "parentId": "",
                "productName": name,
                "systemName": name,
                "deviceName": name,
                "manufacturerName": ndev.get("manufacturerName", "Plantronics"),
                "displaySerialNumber": serial,
                "buildCode": "",
                "firmwareVersion": fw_display,
                "serialNumber": serial,
                "tattooSerialNumber": serial,
                "deviceType": "Headset",
                "connected": True,
                "attached": True,
                "pid": str(pid),
                "vid": str(ndev.get("vid", 0)),
                "productId": model_id.lower() if model_id else f"{pid:04x}",
                "macAddress": "",
                "bluetoothAddress": "",
                "hardwareRevision": "",
                "headsetVersion": fw.get("headset", ""),
                "baseVersion": fw.get("base", ""),
                "usbVersion": fw.get("usb", ""),
                "firmwareComponents": fw,
                "peerDevices": [],
                "connectionType": "Bluetooth",
                "connectionDetails": [{"type": "Bluetooth", "handledBy": "Native Bridge"}],
                "hardwareModel": {"supportedByClients": []},
                "hasChargeCase": ndev.get("hasChargeCase", False),
                "deviceEncryption": "Unknown",
                "dfuMode": False,
                "isAbleToBePrimaryForCallControl": ndev.get("canBePrimary", True),
                "isMuted": False,
                "isInCall": False,
                "state": "Online",
                "supportData": {"state": "Supported"},
                "multiComponentState": None,
                "lastAttachedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "_polytool_dev": {
                    "path": b"",
                    "usage_page": 0,
                    "dfu_executor": "btNeoDfu",
                    "pid": pid,
                    "_native_id": nid,
                },
            }
            # Update call/mute state from native bridge
            call_state = bridge.get_call_state()
            new_device["isMuted"] = call_state.get("muted", False)
            new_device["isInCall"] = call_state.get("inCall", False)

            with self._lock:
                self.devices[device_id] = new_device

            _log(f"    {name} (BT via native bridge, fw {fw_display})", verbose_only=True)

        # Query settings for all native bridge devices and populate cache
        self._populate_native_settings_cache(bridge)

    def _populate_native_settings_cache(self, bridge):
        """Query native bridge for current setting values, populate cache,
        and build dynamic settings profiles for each device."""
        from native_bridge import setting_id_to_name, build_dynamic_profile

        for nid in bridge.get_devices():
            bridge.get_settings(nid)

        # Wait for responses
        time.sleep(3)
        bridge.recv(timeout=1)

        # Map native bridge devices to ALL our devices (including USB-discovered ones)
        # by matching PID. This gives USB devices dynamic profiles too.
        native_devs = bridge.get_devices()
        with self._lock:
            devices_snapshot = list(self.devices.items())
        for did, dev in devices_snapshot:
            ptd = dev.get("_polytool_dev", {})
            native_id = ptd.get("_native_id", "")

            # If no native_id, try matching by PID
            if not native_id:
                dev_pid = ptd.get("pid", 0)
                for nid, ndev in native_devs.items():
                    if ndev.get("pid") == dev_pid:
                        native_id = nid
                        # Store for future use
                        ptd["_native_id"] = nid
                        break

            if not native_id:
                continue

            native_values = bridge.get_setting_values(native_id)
            if not native_values:
                continue

            # Build dynamic profile from the hex IDs this device actually supports
            hex_ids = list(native_values.keys())
            profile = build_dynamic_profile(hex_ids)
            if profile:
                with self._lock:
                    self._dynamic_profiles[did] = profile

            # Populate settings cache with actual values
            # Translate HID-level values to UI-level values where needed
            try:
                from device_settings_db import translate_value
            except ImportError:
                translate_value = lambda hex_id, val: val

            cache = {}
            for hex_id, val in native_values.items():
                setting_name = setting_id_to_name(hex_id)
                if setting_name:
                    val = translate_value(hex_id, val)
                    if val == "true":
                        val = True
                    elif val == "false":
                        val = False
                    cache[setting_name] = val

            if cache:
                # Merge with existing cache (USB HID reads) rather than replacing
                with self._lock:
                    existing = self._device_settings_cache.get(did, {})
                    existing.update(cache)
                    self._device_settings_cache[did] = existing
                _log(f"  Read {len(cache)} settings from native bridge for {dev.get('productName', did)}")

    def read_device_settings(self, device_id):
        """Read settings for a device via our HID code."""
        with self._lock:
            dev = self.devices.get(device_id, {})
        ptd = dev.get("_polytool_dev", {})
        if not ptd:
            return []

        try:
            from device_settings import read_all_settings
            settings = read_all_settings(
                ptd.get("path", b""),
                ptd.get("usage_page", 0),
                ptd.get("dfu_executor", ""),
            )
            return settings
        except Exception as e:
            _log(f"  Settings read error: {e}", verbose_only=True)
            return []

    def _populate_settings_cache(self, device_id, dev):
        """Read real settings from HID and populate the cache."""
        try:
            from device_settings import read_all_settings
            settings = read_all_settings(
                dev.path, dev.usage_page, dev.dfu_executor,
            )
            if settings:
                cache = {}
                for s in settings:
                    if s.get("value") is not None and s.get("name"):
                        cache[s["name"]] = s["value"]
                if cache:
                    with self._lock:
                        self._device_settings_cache[device_id] = cache
                    _log(f"  Read {len(cache)} settings from {device_id} via HID")
        except Exception as e:
            _log(f"  HID settings read failed for {device_id}: {e}", verbose_only=True)

    def write_device_setting(self, device_id, name, value):
        """Write a setting to device. Uses direct HID for CX2070x/BladeRunner,
        native bridge for DECT/Voyager devices."""
        with self._lock:
            dev = self.devices.get(device_id, {})
        ptd = dev.get("_polytool_dev", {})
        if not ptd:
            return False

        # If device has a native bridge ID, prefer native bridge for writes
        if ptd.get("_native_id"):
            return self._proxy_dect_write(device_id, name, value)

        from lens_settings import get_device_family
        family = get_device_family(
            ptd.get("usage_page", 0), ptd.get("dfu_executor", ""),
            pid=ptd.get("pid", 0))

        # DECT / Voyager without native ID: try native bridge anyway
        if family in ("dect", "voyager_bt", "voyager_base"):
            return self._proxy_dect_write(device_id, name, value)

        # CX2070x / BladeRunner: direct HID write
        try:
            from device_settings import write_setting
            return write_setting(
                ptd.get("path", b""),
                ptd.get("usage_page", 0),
                ptd.get("dfu_executor", ""),
                name, value,
            )
        except Exception as e:
            _log(f"  Settings write error: {e}")
            return False

    # ── DECT Native Bridge ────────────────────────────────────────────────────
    _native_bridge = None

    def _get_native_bridge(self):
        """Get or initialize the native bridge for DECT settings."""
        if self._native_bridge:
            return self._native_bridge
        try:
            from native_bridge import NativeBridge
            bridge = NativeBridge()
            bridge.start()
            # Wait for device discovery — BT devices take longer than USB.
            # Keep polling until no new devices appear for 3 seconds.
            import time
            last_count = 0
            stable_ticks = 0
            for _ in range(30):  # max 15 seconds
                time.sleep(0.5)
                bridge.recv(timeout=0.1)
                count = len(bridge.get_devices())
                if count > last_count:
                    last_count = count
                    stable_ticks = 0
                else:
                    stable_ticks += 1
                if stable_ticks >= 6 and count > 0:  # 3s of no new devices
                    break
            self._native_bridge = bridge
            devs = bridge.get_devices()
            _log(f"  Native bridge: {len(devs)} device(s)", verbose_only=True)
            for did, dev in devs.items():
                _log(f"    native id={did}: {dev.get('name', '?')}", verbose_only=True)
            return bridge
        except Exception as e:
            _log(f"  Native bridge unavailable: {e}")
            return None

    def _proxy_dect_write(self, device_id, name, value):
        """Write a DECT setting via the native bridge (direct dylib call)."""
        bridge = self._get_native_bridge()
        if not bridge:
            _log(f"  DECT write failed: native bridge not available")
            return False

        from native_bridge import setting_name_to_id
        setting_id = setting_name_to_id(name)
        if not setting_id:
            _log(f"  DECT write: unknown setting '{name}'")
            return False

        # Find the native device ID (int64 from the native library)
        with self._lock:
            dev = self.devices.get(device_id, {})
        ptd = dev.get("_polytool_dev", {})
        native_dev_id = ptd.get("_native_id")

        # Fallback: match by PID against native bridge devices
        if not native_dev_id:
            dev_pid = int(dev.get("pid", 0))
            for nid, ndev in bridge.get_devices().items():
                if nid and ndev.get("pid") == dev_pid:
                    native_dev_id = nid
                    break

        # Last resort: use first available native device
        if not native_dev_id:
            for nid in bridge.get_devices():
                if nid:
                    native_dev_id = nid
                    break

        if not native_dev_id:
            _log(f"  DECT write: no native device ID")
            return False

        result = bridge.set_setting(native_dev_id, setting_id, value)
        if result:
            _log(f"  DECT native: {name} ({setting_id}) = {value} [OK]")
            # Wait for confirmation
            bridge.recv(timeout=2.0)
        return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _verbose, _quiet

    parser = argparse.ArgumentParser(
        description="LensServer — Drop-in Poly Lens Control Service replacement",
    )
    parser.add_argument("--port", type=int, default=0, help="TCP port (0=auto)")
    parser.add_argument("--dump", action="store_true", help="Dump all messages to dump.jsonl")
    parser.add_argument("--verbose", action="store_true", help="Show all protocol messages and debug output")
    parser.add_argument("--quiet", action="store_true", help="Suppress all output except errors")
    parser.add_argument("--proxy", choices=["auto", "on", "off"], default="auto",
                        help="LCS proxy mode: 'on' to require it (fail if LCS "
                             "isn't running), 'off' for standalone replacement "
                             "(historical behavior), 'auto' (default) to use "
                             "proxy when LCS is reachable on the port file's "
                             "port and fall back to standalone otherwise.")
    parser.add_argument("--http", type=int, default=8080, metavar="PORT",
                        help="HTTP API port (default: 8080, 0 to disable)")
    args = parser.parse_args()

    _verbose = args.verbose
    _quiet = args.quiet

    # Resolve --proxy {auto, on, off} into a boolean:
    #   on   - always proxy. If LCS isn't reachable, _start_lcs_proxy
    #          falls back internally to standalone
    #   off  - never proxy
    #   auto - detect by trying to read SocketPortNumber and confirming
    #          something is listening there
    proxy_lcs = False
    lcs_port = None
    if args.proxy == "on":
        proxy_lcs = True
        lcs_port = _find_lcs_listening_port()
    elif args.proxy == "auto":
        lcs_port = _find_lcs_listening_port()
        proxy_lcs = lcs_port is not None
        _log(f"  proxy auto-detect: {'enabled' if proxy_lcs else 'disabled'} "
             f"(LCS {'on port ' + str(lcs_port) if lcs_port else 'not running'})",
             verbose_only=True)
    server = LensServer(port=args.port, proxy_lcs=proxy_lcs, lcs_port=lcs_port)
    server.dump_mode = getattr(args, 'dump', False)
    if server.dump_mode:
        server.dump_file = open('dump.jsonl', 'w')
        _log(f"  Dumping all messages to dump.jsonl")

    # Proxy mode: connect to LCS first (start() does the actual connect),
    # then we DON'T scan USB / native_bridge ourselves - LCS is the source
    # of truth and pushes us its catalog after we register.
    # Standalone mode: scan USB + BT first, then start the server.
    if proxy_lcs:
        _log(f"  Starting TCP server (proxy mode)...", verbose_only=True)
        server.start()
        nb_count = len(server.devices)
        _log(f"  Imported {nb_count} device(s) from LCS proxy", verbose_only=True)
    else:
        _log(f"  Scanning for USB devices...", verbose_only=True)
        ids = server.discover_devices()
        _log(f"  Found {len(ids)} USB device(s)", verbose_only=True)

        _log(f"  Scanning for BT devices via native bridge...", verbose_only=True)
        server.discover_native_devices()

        _log(f"  Starting TCP server...", verbose_only=True)
        server.start()

    # Count settings per device
    def _settings_count(did):
        n = len(server._device_settings_cache.get(did, {}))
        dp = server._dynamic_profiles.get(did, [])
        return max(n, len(dp))

    # Determine native bridge status
    nb = server._native_bridge
    nb_devs = nb.get_devices() if nb else {}
    nb_status = f"active ({len(nb_devs)} device{'s' if len(nb_devs) != 1 else ''})" if nb else "unavailable"

    # Print startup banner
    print(f"\n  PolyTool LensServer v1.0")
    print(f"  {'=' * 30}")
    print(f"  Devices: {len(server.devices)}")
    for did, dev in server.devices.items():
        conn = "BT" if dev.get("connectionType") == "Bluetooth" else "USB"
        sc = _settings_count(did)
        sc_str = f" — {sc} settings" if sc else ""
        print(f"    {dev['productName']} (fw {dev['firmwareVersion']}) [{conn}]{sc_str}")
    print(f"  Native bridge: {nb_status}")
    print(f"  Server: 127.0.0.1:{server.port}")
    print(f"  Port file: {PORT_FILE}")

    # Start HTTP API server
    http_port = args.http
    httpd = None
    if http_port:
        try:
            from http_api import start_http_api
            httpd, _ = start_http_api(server, port=http_port)
            print(f"  HTTP API: http://127.0.0.1:{http_port}/api")
        except Exception as e:
            print(f"  HTTP API failed: {e}")

    print(f"\n  Waiting for Poly Studio to connect...\n")

    try:
        server.accept_clients()
    except KeyboardInterrupt:
        _log(f"\n  Shutting down...")
    finally:
        server.stop()
        _log(f"  Stopped.")


if __name__ == "__main__":
    main()
