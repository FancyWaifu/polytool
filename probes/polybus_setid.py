#!/usr/bin/env python3
"""Direct PolyBus.dll invocation to read/write SetID.

Loads clockwork/PolyBus.dll via ctypes and uses poly_bus_request to send
JSON commands. The format mirrors what LCS sends (captured via Frida on
LensService.exe):

  GET: {"params":{"name":"Set ID"},"request_id":N,"method":"GET",
        "resource_id":"/device/settings","device_id":"<polybus_id>"}
  SET: {"params":{"name":"Set ID","value":"X.X.X.X"},"request_id":N,
        "method":"SET","resource_id":"/device/settings","device_id":"..."}

PolyBus device IDs (as seen by LCS, NOT NativeLoader IDs):
  AC28 (Savi 7320, the FF problem) = 1125933431
  AC27 (Savi 7310, no setId field) = 2349978044

Usage:
  python3 probes/polybus_setid.py get <device_id>
  python3 probes/polybus_setid.py list-settings <device_id>
  python3 probes/polybus_setid.py set <device_id> <value>     # DESTRUCTIVE
"""
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

DLL_PATH = r"C:\Program Files\Poly\Lens Control Service\clockwork\PolyBus.dll"
DEVICE_AC28 = "1125933431"
DEVICE_AC27 = "2349978044"


# Result enum (PolyBusDllResult) — exact values unknown, but 0 = success
class PolyBusDllResult:
    SUCCESS = 0


# Notification callback signature (educated guess: takes one char*)
NOTIFY_CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p)


def load_dll():
    if not Path(DLL_PATH).exists():
        sys.exit(f"PolyBus.dll not found at {DLL_PATH}")
    lib = ctypes.CDLL(DLL_PATH)

    lib.poly_bus_request.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.poly_bus_request.restype = ctypes.c_int

    lib.poly_bus_get_utf8.argtypes = [ctypes.c_void_p]
    lib.poly_bus_get_utf8.restype = ctypes.c_char_p

    lib.poly_bus_free.argtypes = [ctypes.c_void_p]
    lib.poly_bus_free.restype = ctypes.c_int

    lib.poly_initialize_context_ex2.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.c_char_p,
    ]
    lib.poly_initialize_context_ex2.restype = ctypes.c_int

    lib.poly_cleanup_context.argtypes = [ctypes.c_void_p]
    lib.poly_cleanup_context.restype = ctypes.c_int

    lib.poly_bus_get_devices.argtypes = [
        ctypes.c_char_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.c_int,
    ]
    lib.poly_bus_get_devices.restype = ctypes.c_int

    lib.poly_bus_register_notification_callback.argtypes = [NOTIFY_CB]
    lib.poly_bus_register_notification_callback.restype = ctypes.c_int

    return lib


_notifications = []


def _on_notify(data):
    """Notification callback — store events for inspection."""
    if data:
        try:
            s = data.decode("utf-8", errors="replace")
        except Exception:
            s = repr(data)
        _notifications.append(s)


_NOTIFY_CB_HANDLE = NOTIFY_CB(_on_notify)  # keep ref so it's not GC'd


_request_id = 1


def send_request(lib, request_dict):
    """Send a JSON request via poly_bus_request, return response dict."""
    global _request_id
    request_dict.setdefault("request_id", _request_id)
    _request_id += 1
    body = json.dumps(request_dict).encode("utf-8")
    print(f">> {body.decode('utf-8')}")

    response_handle = ctypes.c_void_p(0)
    result = lib.poly_bus_request(body, ctypes.byref(response_handle))
    print(f"   result={result} response_handle={response_handle.value}")

    if response_handle.value:
        resp_str = lib.poly_bus_get_utf8(response_handle.value)
        if resp_str:
            resp = resp_str.decode("utf-8", errors="replace")
            print(f"<< {resp}")
            try:
                parsed = json.loads(resp)
            except json.JSONDecodeError:
                parsed = {"raw": resp}
        else:
            print("<< (empty utf8)")
            parsed = {}
        lib.poly_bus_free(response_handle.value)
    else:
        print("<< (no response)")
        parsed = {}

    return result, parsed


def cmd_get(lib, device_id):
    """GET the Set ID setting for a device."""
    result, resp = send_request(lib, {
        "params": {"name": "Set ID"},
        "method": "GET",
        "resource_id": "/device/settings",
        "device_id": device_id,
    })
    return result, resp


def cmd_list_settings(lib, device_id):
    """LIST all settings (with metadata) for a device."""
    result, resp = send_request(lib, {
        "params": {"get_values": False},
        "method": "LIST",
        "resource_id": "/device/settings",
        "device_id": device_id,
    })
    return result, resp


def cmd_list_devices(lib):
    """List all PolyBus devices."""
    buf = ctypes.create_string_buffer(8192)
    return_bytes = ctypes.c_uint(0)
    result = lib.poly_bus_get_devices(buf, 8192, ctypes.byref(return_bytes), 0)
    print(f"poly_bus_get_devices: result={result} bytes={return_bytes.value}")
    if return_bytes.value > 0:
        text = buf.raw[:return_bytes.value].decode("utf-8", errors="replace")
        print(f"<< {text}")


def cmd_set(lib, device_id, value):
    """SET the Set ID setting. DESTRUCTIVE — writes to NVRAM."""
    print(f"!!! WRITING SetID = {value!r} to device {device_id} !!!")
    result, resp = send_request(lib, {
        "params": {"name": "Set ID", "value": value},
        "method": "SET",
        "resource_id": "/device/settings",
        "device_id": device_id,
    })
    return result, resp


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]

    lib = load_dll()
    print(f"Loaded {DLL_PATH}")

    # Initialize context — flags=2 matches LCS desktop init (Windows non-room)
    # filterData = empty list of libraries to exclude (UTF-8, null-terminated)
    context = ctypes.c_void_p(0)
    filter_data = b"\0"
    init_result = lib.poly_initialize_context_ex2(ctypes.byref(context), 2, filter_data)
    print(f"poly_initialize_context_ex2: result={init_result} context={context.value}")

    # Register notification callback BEFORE devices come online (LCS does this)
    cb_result = lib.poly_bus_register_notification_callback(_NOTIFY_CB_HANDLE)
    print(f"poly_bus_register_notification_callback: result={cb_result}")

    # Poll until AC27/AC28 specifically reach Online (max 60s)
    import re
    print("Waiting for Savi devices to come Online...")
    for i in range(60):
        time.sleep(1)
        buf = ctypes.create_string_buffer(32768)
        ret_bytes = ctypes.c_uint(0)
        if lib.poly_bus_get_devices(buf, 32768, ctypes.byref(ret_bytes), 0) == 0:
            text = buf.raw[:ret_bytes.value].decode("utf-8", errors="replace")
            states = {}
            for m in re.finditer(r'"device_productId":\s*"(0x[A-Fa-f0-9]+)".*?"device_state":\s*"(\w+)"', text, re.DOTALL):
                pid, state = m.group(1), m.group(2)
                if pid in ("0xAC27", "0xAC28"):
                    states[pid] = state
            if i % 3 == 0:
                print(f"  t+{i+1}s states: {states}, notifications: {len(_notifications)}")
            if states.get("0xAC28") == "Online":
                print(f"  AC28 Online after {i+1}s")
                break
    else:
        print("  WARNING: AC28 never reached Online state in 60s — proceeding anyway")
    if _notifications:
        print(f"  First notification sample: {_notifications[0][:200]}")

    try:
        if cmd == "list-devices":
            cmd_list_devices(lib)
        elif cmd == "get":
            device_id = sys.argv[2] if len(sys.argv) > 2 else DEVICE_AC28
            cmd_get(lib, device_id)
        elif cmd == "list-settings":
            device_id = sys.argv[2] if len(sys.argv) > 2 else DEVICE_AC28
            cmd_list_settings(lib, device_id)
        elif cmd == "set":
            if len(sys.argv) < 4:
                sys.exit("Usage: set <device_id> <value>")
            cmd_set(lib, sys.argv[2], sys.argv[3])
        else:
            sys.exit(f"Unknown command: {cmd}")
    finally:
        lib.poly_cleanup_context(context.value)


if __name__ == "__main__":
    main()
