"""
TCP client to the Poly Lens Control Service.

Background: lensserver historically replaced LCS by overwriting its
SocketPortNumber and answering everything itself. That works for the
fields we override (display name, FFFF strip, firmware version, update
button) but means we lose access to everything LCS already does well -
notably battery telemetry, settings caching, and call-control state.

This module lets lensserver run in a "proxy mode" where:
  - LCS is left running as normal
  - lensserver connects to LCS as a client (just like Poly Studio does)
  - we read the device catalog from LCS, apply our overrides on top, and
    serve the augmented view to Studio

Protocol notes:
  - Same SOH-delimited JSON wire format Studio speaks to LCS
  - LCS accepts useEncryption=false for local clients (verified via
    probes/lcs_client_probe.py), so no keypair handshake required
  - LCS pushes DeviceList, DeviceAttached/Updated/Detached, DeviceSetting
    events to all registered clients without an explicit subscribe array
  - Some events (battery telemetry) only fire when the underlying
    headset is actually sending data - asleep/off-dock devices have
    battery=null, same as we already see in native_bridge
"""

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional


SOH = "\x01"

if sys.platform == "win32":
    _PDATA = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    PORT_FILE = Path(_PDATA) / "Poly" / "Lens Control Service" / "SocketPortNumber"
else:
    PORT_FILE = (Path.home() / "Library/Application Support/Poly/"
                 "Lens Control Service/SocketPortNumber")


def _log(msg, *, verbose=False):
    """Lightweight logger - lensserver overrides this with its own."""
    if not verbose:
        print(f"[lcs_client] {msg}")


class LCSClient:
    """Long-lived TCP client to the Poly Lens Control Service.

    Maintains a snapshot of LCS's device catalog (`self.devices`) and an
    event listener API. Reconnects automatically when LCS bounces (which
    happens during fix-setid via device_isolate.isolate's stop/start
    cycle) so consumers don't have to track the connection state.

    Usage:
        client = LCSClient()
        client.start()                       # connects + waits for first DeviceList
        devs = client.devices                # dict of deviceId -> device record
        client.on_device_updated(my_handler) # subscribe to event-stream changes
        client.stop()                        # cleanly disconnect
    """

    def __init__(self, name="polytool-lensserver-proxy", *, log: Callable = _log):
        self._name = name
        self._log = log
        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        self._lcs_port: Optional[int] = None
        # Device catalog as LCS knows it. deviceId (PLT_<serial>) -> dev dict
        self.devices: dict = {}
        self._devices_lock = threading.RLock()
        self._first_device_list = threading.Event()
        # Event subscribers
        self._device_updated_handlers: list = []
        self._device_attached_handlers: list = []
        self._device_detached_handlers: list = []
        self._raw_handlers: list = []  # called with every message

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self, *, port: Optional[int] = None, wait_for_devices: bool = True,
              wait_timeout: float = 10.0) -> bool:
        """Open the connection and start the reader thread.

        port - explicit LCS port. If None, reads from SocketPortNumber.
               Useful for proxy mode where lensserver has already captured
               LCS's port before overwriting the file with its own.
        wait_for_devices - block until the first DeviceList arrives (or
               wait_timeout elapses). Lets the caller assume `devices` is
               populated before returning.

        Returns True if the connection + register handshake succeeded.
        """
        if self._running:
            return True
        self._running = True

        if port is None:
            try:
                port = int(PORT_FILE.read_text().strip())
            except (OSError, ValueError) as e:
                self._log(f"failed to read LCS port from {PORT_FILE}: {e}")
                self._running = False
                return False
        self._lcs_port = port

        if not self._connect_and_register():
            self._running = False
            return False

        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="lcs-client-reader")
        self._reader_thread.start()

        if wait_for_devices:
            self._first_device_list.wait(timeout=wait_timeout)
        return True

    def stop(self):
        """Close the connection and stop the reader thread."""
        self._running = False
        with self._sock_lock:
            if self._sock:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None

    # ── Public API ───────────────────────────────────────────────────────

    def get_device(self, device_id: str) -> Optional[dict]:
        """Look up a device by full PLT_<serial> id OR by 8-char prefix
        OR by raw serial. Returns a deep-ish copy so callers can mutate
        without racing the reader thread."""
        with self._devices_lock:
            # Direct hit
            if device_id in self.devices:
                return dict(self.devices[device_id])
            # PLT_ prefix mismatch
            for did, dev in self.devices.items():
                if did == f"PLT_{device_id}" or did.endswith(device_id):
                    return dict(dev)
            # Match by serial
            for did, dev in self.devices.items():
                serial = dev.get("serialNumber") or ""
                if serial == device_id or serial.startswith(device_id):
                    return dict(dev)
        return None

    def list_devices(self) -> list:
        """Snapshot of all known devices."""
        with self._devices_lock:
            return [dict(d) for d in self.devices.values()]

    def on_device_updated(self, handler: Callable[[dict], None]):
        """Register a callback for DeviceUpdated events from LCS.
        Handler is invoked with the device dict; runs on the reader thread,
        so handlers must not block."""
        self._device_updated_handlers.append(handler)

    def on_device_attached(self, handler: Callable[[dict], None]):
        self._device_attached_handlers.append(handler)

    def on_device_detached(self, handler: Callable[[str], None]):
        """Handler receives the deviceId of the detached device."""
        self._device_detached_handlers.append(handler)

    def on_raw_message(self, handler: Callable[[dict], None]):
        """Catch-all: called for every message LCS sends us. Useful for
        forwarding event types we haven't pulled into typed handlers yet."""
        self._raw_handlers.append(handler)

    def send(self, msg: dict) -> bool:
        """Send a message to LCS. Returns False on socket failure."""
        with self._sock_lock:
            if not self._sock:
                return False
            try:
                self._sock.sendall((json.dumps(msg) + SOH).encode("utf-8"))
                return True
            except Exception as e:
                self._log(f"send failed: {e}")
                return False

    # ── Internal ─────────────────────────────────────────────────────────

    def _connect_and_register(self) -> bool:
        """Open the socket, send RegisterClient, wait for ClientRegistered."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect(("127.0.0.1", self._lcs_port))
            sock.settimeout(None)
        except Exception as e:
            self._log(f"connect to 127.0.0.1:{self._lcs_port} failed: {e}")
            return False

        with self._sock_lock:
            self._sock = sock

        # Register without encryption - LCS accepts this for local clients.
        # Subscribe broadly so we get every event LCS pushes (battery,
        # settings, primary device, etc.).
        reg = {
            "type": "RegisterClient",
            "apiVersion": "1.0.0",
            "name": self._name,
            "useEncryption": False,
            "subscriptions": [
                "DeviceList", "DeviceAttached", "DeviceDetached",
                "DeviceUpdated", "DeviceSetting", "DeviceSettings",
                "DeviceSettingsChanged", "DeviceSettingsMetadata",
                "BatteryStatus", "BatteryUpdated",
                "DeviceDFUStatus", "AvailableSoftwareUpdate",
                "PrimaryDevice", "InCall", "Mute", "OnHead",
                "DfuExecutionStatus", "DfuExecutionStatusUpdated",
            ],
        }
        if not self.send(reg):
            return False
        self._log(f"connected to LCS on 127.0.0.1:{self._lcs_port}")
        return True

    def _reader_loop(self):
        """Read SOH-delimited JSON from LCS forever, dispatch by type.
        Reconnects on disconnect (LCS bouncing during fix-setid is the
        common case)."""
        buf = ""
        backoff = 1.0
        while self._running:
            with self._sock_lock:
                sock = self._sock
            if not sock:
                if self._running:
                    self._log("no socket - attempting reconnect")
                    if self._connect_and_register():
                        backoff = 1.0
                        buf = ""
                        continue
                    time.sleep(min(backoff, 30))
                    backoff *= 2
                    continue
                else:
                    return
            try:
                data = sock.recv(65536)
            except Exception as e:
                if self._running:
                    self._log(f"recv error: {e}")
                with self._sock_lock:
                    self._sock = None
                continue
            if not data:
                if self._running:
                    self._log("LCS disconnected")
                with self._sock_lock:
                    self._sock = None
                continue

            buf += data.decode("utf-8", errors="replace")
            while SOH in buf:
                msg, buf = buf.split(SOH, 1)
                if not msg.strip():
                    continue
                try:
                    obj = json.loads(msg)
                except Exception:
                    continue
                self._handle_message(obj)

    def _handle_message(self, msg: dict):
        """Dispatch one parsed message from LCS."""
        t = msg.get("type", "")

        # Always notify raw handlers first - they get everything
        for h in self._raw_handlers:
            try:
                h(msg)
            except Exception as e:
                self._log(f"raw handler error: {e}", verbose=True)

        if t == "DeviceList":
            with self._devices_lock:
                self.devices.clear()
                for dev in (msg.get("devices") or []):
                    did = dev.get("deviceId", "")
                    if did:
                        self.devices[did] = dev
            self._first_device_list.set()
        elif t == "DeviceAttached":
            dev = msg.get("device") or {}
            did = dev.get("deviceId", "")
            if did:
                with self._devices_lock:
                    self.devices[did] = dev
                for h in self._device_attached_handlers:
                    try: h(dev)
                    except Exception as e: self._log(f"attach handler: {e}", verbose=True)
        elif t == "DeviceDetached":
            did = msg.get("deviceId") or (msg.get("device") or {}).get("deviceId", "")
            if did:
                with self._devices_lock:
                    self.devices.pop(did, None)
                for h in self._device_detached_handlers:
                    try: h(did)
                    except Exception as e: self._log(f"detach handler: {e}", verbose=True)
        elif t == "DeviceUpdated":
            dev = msg.get("device") or {}
            did = dev.get("deviceId", "")
            if did:
                with self._devices_lock:
                    self.devices[did] = dev
                for h in self._device_updated_handlers:
                    try: h(dev)
                    except Exception as e: self._log(f"update handler: {e}", verbose=True)


__all__ = ["LCSClient", "PORT_FILE"]
