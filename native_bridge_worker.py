#!/usr/bin/env python3
"""
Native Bridge Worker — 32-bit subprocess that loads Poly's 32-bit DLLs.

This script runs under 32-bit Python and communicates with the main
64-bit NativeBridge process over stdin/stdout using newline-delimited JSON.

Protocol:
  Parent → Worker (stdin):  {"cmd": "start"|"send"|"stop", ...}\n
  Worker → Parent (stdout): {"type": "ready"|"loaded"|"callback"|"result"|"error", ...}\n

Started automatically by NativeBridge when it detects an architecture mismatch.
Do not run directly.
"""

import ctypes
import json
import os
import sys
import threading
import time

# ── Callback Type ────────────────────────────────────────────────────────────
RECEIVER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p)

# ── Output Lock (stdout must be atomic per-line) ────────────────────────────
_write_lock = threading.Lock()


def _resolve_func(lib, plain_name, mangled_name):
    """Try to resolve a function by plain name first, then mangled."""
    try:
        return getattr(lib, plain_name)
    except AttributeError:
        pass
    if mangled_name:
        try:
            return getattr(lib, mangled_name)
        except AttributeError:
            pass
    return None


def _send(msg):
    """Write a JSON message to stdout (thread-safe)."""
    line = json.dumps(msg, separators=(",", ":")) + "\n"
    with _write_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def _send_error(text):
    _send({"type": "error", "error": str(text)})


# ── Globals ──────────────────────────────────────────────────────────────────
_libs = []
_native_loader = None
_callback_ref = None


def _on_received(data):
    """Callback from native library — forward JSON to parent process."""
    if not data:
        return
    try:
        msg_str = data.decode("utf-8")
        # Parse to validate JSON, then forward
        msg = json.loads(msg_str)
        _send({"type": "callback", "message": msg})
    except Exception as e:
        _send({"type": "callback_error", "error": str(e)})


def cmd_start(components_dir):
    """Load native libraries and initialize the bridge."""
    global _libs, _native_loader, _callback_ref

    components_dir = components_dir.rstrip("/\\")

    # Add DLL directory so dependent DLLs resolve
    try:
        os.add_dll_directory(components_dir)
    except Exception:
        pass
    # Also prepend to PATH as fallback for older DLL search
    os.environ["PATH"] = components_dir + ";" + os.environ.get("PATH", "")

    # Load order: PLTDeviceManager first (dependency), then NativeLoader
    load_order = [
        ("PLTDeviceManager.dll", False),
        ("libPLTDeviceManager.dll", False),
        ("NativeLoader.dll", True),
        ("libNativeLoader.dll", True),
    ]

    for name, is_loader in load_order:
        path = os.path.join(components_dir, name)
        if not os.path.exists(path):
            continue
        try:
            lib = ctypes.cdll.LoadLibrary(path)
            _libs.append(lib)
            if is_loader:
                _native_loader = lib
            _send({"type": "loaded_lib", "name": name})
        except OSError as e:
            _send_error(f"Failed to load {name}: {e}")
            return

    if not _native_loader:
        _send_error("NativeLoader not found in " + components_dir)
        return

    # Resolve function names — Windows uses C++ mangled exports,
    # macOS uses plain C names
    init_func = _resolve_func(_native_loader, "NativeLoader_Init",
                              "?NativeLoader_Init@@YAXP6AXPBD@Z@Z")
    send_func = _resolve_func(_native_loader, "NativeLoader_SendToNative",
                              "?NativeLoader_SendToNative@@YA_NPBD@Z")
    exit_func = _resolve_func(_native_loader, "NativeLoader_Exit",
                              "?NativeLoader_Exit@@YAXXZ")

    if not init_func or not send_func or not exit_func:
        _send_error("Required NativeLoader functions not found")
        return

    init_func.argtypes = [RECEIVER_FUNC]
    init_func.restype = None
    send_func.argtypes = [ctypes.c_char_p]
    send_func.restype = ctypes.c_bool
    exit_func.argtypes = []
    exit_func.restype = None

    _native_loader._init = init_func
    _native_loader._send = send_func
    _native_loader._exit = exit_func

    # Create callback and init
    _callback_ref = RECEIVER_FUNC(_on_received)
    init_func(_callback_ref)

    # Start the native bridge (begins USB device scanning)
    # On macOS this is a separate export; on Windows, Init starts scanning
    start_func = _resolve_func(_native_loader, "StartNativeBridge", None)
    if start_func:
        try:
            start_func()
        except Exception as e:
            _send_error(f"StartNativeBridge failed: {e}")
            return

    _send({"type": "started"})


def cmd_send(message_type, payload, track_id):
    """Forward a SendToNative call."""
    if not _native_loader or not hasattr(_native_loader, '_send'):
        _send_error("Bridge not started")
        return
    msg = {"messageType": message_type, "payload": payload, "trackId": str(track_id)}
    data = json.dumps(msg).encode("utf-8")
    result = _native_loader._send(data)
    _send({"type": "send_result", "ok": bool(result), "trackId": str(track_id)})


def cmd_stop():
    """Shut down the native bridge."""
    if _native_loader and hasattr(_native_loader, '_exit'):
        try:
            _native_loader._exit()
        except Exception:
            pass
        time.sleep(0.3)
    _send({"type": "stopped"})


# ── Main Loop ────────────────────────────────────────────────────────────────

def main():
    # Signal readiness
    _send({"type": "ready", "bits": 32, "pid": os.getpid()})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _send_error(f"Invalid JSON: {e}")
            continue

        cmd = req.get("cmd")
        try:
            if cmd == "start":
                cmd_start(req["components_dir"])
            elif cmd == "send":
                cmd_send(req["message_type"], req["payload"], req.get("track_id", 0))
            elif cmd == "stop":
                cmd_stop()
                break
            elif cmd == "ping":
                _send({"type": "pong"})
            else:
                _send_error(f"Unknown cmd: {cmd}")
        except Exception as e:
            _send_error(f"Exception in {cmd}: {e}")

    # Clean exit
    sys.exit(0)


if __name__ == "__main__":
    main()
