#!/usr/bin/env python3
"""
DECT Settings Write Protocol Probe

Systematically tests methods to write settings to Savi 7300/8200 DECT base.
The 0x4002 CVM command returns device info with 14 settings encoded as nibbles
in bytes 15-21. This probe tries to find the corresponding write mechanism.

Usage:
  python3 probes/dect_settings_probe.py --test read       # Read current settings
  python3 probes/dect_settings_probe.py --test write_cvm   # Try CVM write commands
  python3 probes/dect_settings_probe.py --test write_rid   # Try RID-based writes
  python3 probes/dect_settings_probe.py --test scan_cmds   # Scan CVM command families
  python3 probes/dect_settings_probe.py --test all         # Run all tests
"""

import sys
import os
import time
import struct
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from probes.hid_helpers import (
    hexline, find_device, do_signon, do_signoff,
    listen, timed_write, FRAG_START, RID_DATA, RID_ACK,
)

try:
    import hid
except ImportError:
    print("Error: hidapi not installed. Run: pip install hidapi")
    sys.exit(1)


# ── CVM Message Framing ─────────────────────────────────────────────────────

def send_cvm(h, prim_id, params=b"", report_size=64):
    """Send a CVM command via RID 3 with START framing."""
    payload = struct.pack("<H", prim_id) + params
    pkt = bytearray(report_size)
    pkt[0] = RID_DATA      # Report ID 3
    pkt[1] = FRAG_START     # 0x20 = START fragment
    pkt[2] = len(payload)   # payload length
    pkt[3:3+len(payload)] = payload
    timed_write(h, bytes(pkt))


def recv_cvm(h, timeout_ms=2000):
    """Receive CVM responses. Returns list of (prim_id, params) tuples."""
    results = []
    h.set_nonblocking(1)
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        data = h.read(256)
        if data:
            data = bytes(data)
            rid = data[0]
            if rid == RID_DATA and len(data) >= 5:
                frag = data[1]
                plen = data[2]
                if frag == FRAG_START and plen >= 2:
                    prim_id = struct.unpack_from("<H", data, 3)[0]
                    params = bytes(data[5:3+plen])
                    results.append((prim_id, params))
            elif rid == RID_ACK:
                pass  # DFU ACK, ignore
            elif rid == 0x0E:
                print(f"  RID 14 (settings): {hexline(data)}")
            elif rid == 0x02:
                print(f"  RID 2 (sign-on ack): {hexline(data)}")
        time.sleep(0.005)
    return results


# ── Settings Nibble Decoding ─────────────────────────────────────────────────

SETTING_NAMES = [
    "Unknown_0", "Base Ringer Vol", "Unknown_2", "Sidetone Level",
    "DECT Density", "Unknown_5", "Unknown_6", "Unknown_7",
    "Unknown_8", "Unknown_9", "Unknown_10", "Unknown_11",
    "Unknown_12", "Unknown_13",
]


def decode_settings_nibbles(data_bytes):
    """Decode 7 bytes into 14 nibble settings."""
    nibbles = []
    for b in data_bytes:
        nibbles.append((b >> 4) & 0x0F)
        nibbles.append(b & 0x0F)
    return nibbles


def encode_settings_nibbles(nibbles):
    """Encode 14 nibble settings into 7 bytes."""
    result = bytearray(7)
    for i in range(0, min(14, len(nibbles)), 2):
        hi = nibbles[i] & 0x0F
        lo = nibbles[i+1] & 0x0F if i+1 < len(nibbles) else 0
        result[i // 2] = (hi << 4) | lo
    return bytes(result)


def read_device_info(h):
    """Read device info via CVM 0x4002. Returns (full_response, settings_nibbles) or None."""
    print("\n── Reading device info (CVM 0x4002) ──")
    send_cvm(h, 0x4002)
    responses = recv_cvm(h, timeout_ms=2000)

    for prim_id, params in responses:
        if prim_id == 0x4003:
            print(f"  Response 0x4003 ({len(params)} bytes): {hexline(params)}")
            if len(params) >= 22:
                fw = struct.unpack_from("<H", params, 1)[0]
                settings_count = params[13]
                settings_bytes = params[15:22]
                nibbles = decode_settings_nibbles(settings_bytes)

                print(f"  Firmware: {fw >> 8}.{fw & 0xFF}")
                print(f"  Settings count: {settings_count}")
                print(f"  Settings nibbles: {nibbles}")
                for i, val in enumerate(nibbles):
                    name = SETTING_NAMES[i] if i < len(SETTING_NAMES) else f"Setting_{i}"
                    print(f"    [{i:2d}] {name:20s} = {val}")
                return params, nibbles
            else:
                print(f"  Response too short: {len(params)} bytes")
                return params, None

    print("  No 0x4003 response received!")
    return None, None


# ── Test: Read Settings ──────────────────────────────────────────────────────

def test_read(h):
    """Read and display current DECT settings."""
    print("\n" + "=" * 60)
    print("TEST: Read DECT Settings")
    print("=" * 60)

    do_signon(h)
    time.sleep(0.5)

    params, nibbles = read_device_info(h)

    # Also read feature reports for additional info
    print("\n── Feature Reports ──")
    for fr_id in [15, 72, 73, 154]:
        try:
            data = h.get_feature_report(fr_id, 64)
            if data and any(b != 0 for b in data[1:]):
                print(f"  FR {fr_id}: {hexline(data[:20])}")
        except Exception:
            pass

    # Try reading RID 14 settings report
    print("\n── Listening for settings report (RID 14) ──")
    results = listen(h, timeout_ms=3000)
    for rid, data in results:
        if rid == 0x0E:
            print(f"  Settings report: {hexline(data)}")

    do_signoff(h)
    return nibbles


# ── Test: CVM Write Commands ─────────────────────────────────────────────────

def test_write_cvm(h):
    """Try writing settings via CVM commands."""
    print("\n" + "=" * 60)
    print("TEST: CVM Settings Write")
    print("=" * 60)

    do_signon(h)
    time.sleep(0.5)

    # Read current settings first
    orig_params, orig_nibbles = read_device_info(h)
    if not orig_nibbles:
        print("  Cannot read current settings, aborting.")
        do_signoff(h)
        return

    # Pick a safe setting to modify: Base Ringer Volume (nibble 1)
    # Current value is orig_nibbles[1], try changing it by 1
    test_nibble_idx = 1
    old_val = orig_nibbles[test_nibble_idx]
    new_val = (old_val + 1) % 11  # Keep in 0-10 range
    print(f"\n  Target: nibble[{test_nibble_idx}] ({SETTING_NAMES[test_nibble_idx]})")
    print(f"  Current: {old_val} → Attempting: {new_val}")

    modified_nibbles = list(orig_nibbles)
    modified_nibbles[test_nibble_idx] = new_val
    modified_bytes = encode_settings_nibbles(modified_nibbles)

    # ── Method 1: Send 0x4001 with full device info blob ──
    print("\n── Method 1: CVM 0x4001 with modified device info ──")
    if orig_params and len(orig_params) >= 22:
        modified_params = bytearray(orig_params)
        modified_params[15:22] = modified_bytes
        send_cvm(h, 0x4001, bytes(modified_params))
        responses = recv_cvm(h, timeout_ms=2000)
        print(f"  Responses: {len(responses)}")
        for prim_id, params in responses:
            print(f"    0x{prim_id:04X}: {hexline(params[:20])}")

        # Re-read to check
        time.sleep(0.5)
        _, check = read_device_info(h)
        if check and check[test_nibble_idx] == new_val:
            print(f"  *** SUCCESS! Setting changed to {new_val} ***")
            # Restore original
            restore_bytes = encode_settings_nibbles(orig_nibbles)
            restore_params = bytearray(orig_params)
            restore_params[15:22] = restore_bytes
            send_cvm(h, 0x4001, bytes(restore_params))
            recv_cvm(h, timeout_ms=1000)
            do_signoff(h)
            return True
        else:
            print(f"  No change detected.")

    # ── Method 2: Send 0x4001 with just settings bytes ──
    print("\n── Method 2: CVM 0x4001 with settings bytes only ──")
    send_cvm(h, 0x4001, modified_bytes)
    responses = recv_cvm(h, timeout_ms=2000)
    print(f"  Responses: {len(responses)}")
    for prim_id, params in responses:
        print(f"    0x{prim_id:04X}: {hexline(params[:20])}")
    time.sleep(0.5)
    _, check = read_device_info(h)
    if check and check[test_nibble_idx] == new_val:
        print(f"  *** SUCCESS! ***")
        do_signoff(h)
        return True

    # ── Method 3: Try 0x4010 (hypothetical settings write) ──
    print("\n── Method 3: CVM 0x4010 with settings bytes ──")
    send_cvm(h, 0x4010, modified_bytes)
    responses = recv_cvm(h, timeout_ms=2000)
    print(f"  Responses: {len(responses)}")
    for prim_id, params in responses:
        print(f"    0x{prim_id:04X}: {hexline(params[:20])}")
    time.sleep(0.5)
    _, check = read_device_info(h)
    if check and check[test_nibble_idx] == new_val:
        print(f"  *** SUCCESS! ***")
        do_signoff(h)
        return True

    # ── Method 4: Try 0x4012 (settings write with device nr prefix) ──
    print("\n── Method 4: CVM 0x4012 with device_nr + settings ──")
    device_nr = orig_params[0] if orig_params else 0
    send_cvm(h, 0x4012, bytes([device_nr]) + modified_bytes)
    responses = recv_cvm(h, timeout_ms=2000)
    print(f"  Responses: {len(responses)}")
    for prim_id, params in responses:
        print(f"    0x{prim_id:04X}: {hexline(params[:20])}")
    time.sleep(0.5)
    _, check = read_device_info(h)
    if check and check[test_nibble_idx] == new_val:
        print(f"  *** SUCCESS! ***")
        do_signoff(h)
        return True

    # ── Method 5: Try 0x4200 family (hypothetical device settings) ──
    for cmd in [0x4200, 0x4202, 0x4204, 0x4210]:
        print(f"\n── Method: CVM 0x{cmd:04X} with settings ──")
        send_cvm(h, cmd, bytes([device_nr]) + modified_bytes)
        responses = recv_cvm(h, timeout_ms=1500)
        print(f"  Responses: {len(responses)}")
        for prim_id, params in responses:
            print(f"    0x{prim_id:04X}: {hexline(params[:20])}")
        if responses:
            time.sleep(0.5)
            _, check = read_device_info(h)
            if check and check[test_nibble_idx] == new_val:
                print(f"  *** SUCCESS with 0x{cmd:04X}! ***")
                do_signoff(h)
                return True

    print("\n  All CVM write methods failed.")
    do_signoff(h)
    return False


# ── Test: RID-Based Writes ───────────────────────────────────────────────────

def test_write_rid(h):
    """Try writing settings via various Report ID approaches."""
    print("\n" + "=" * 60)
    print("TEST: RID-Based Settings Write")
    print("=" * 60)

    do_signon(h)
    time.sleep(0.5)

    orig_params, orig_nibbles = read_device_info(h)
    if not orig_nibbles:
        print("  Cannot read current settings.")
        do_signoff(h)
        return

    test_nibble_idx = 1
    old_val = orig_nibbles[test_nibble_idx]
    new_val = (old_val + 1) % 11
    modified_nibbles = list(orig_nibbles)
    modified_nibbles[test_nibble_idx] = new_val
    modified_bytes = encode_settings_nibbles(modified_nibbles)
    print(f"  Target: nibble[{test_nibble_idx}] = {old_val} → {new_val}")

    # ── Method 1: Write RID 14 (0x0E) output report with settings ──
    print("\n── Method 1: RID 14 output with settings blob ──")
    # Mirror the format we read: [0E fw_lo fw_hi ... settings ...]
    pkt = bytearray(64)
    pkt[0] = 0x0E
    # Copy the settings report format from what we've seen
    if orig_params and len(orig_params) >= 2:
        pkt[1] = orig_params[1] & 0xFF  # fw lo
        pkt[2] = (orig_params[1] >> 8) & 0xFF if len(orig_params) > 2 else 0
    pkt[15:22] = modified_bytes
    timed_write(h, bytes(pkt))
    time.sleep(0.5)
    responses = recv_cvm(h, timeout_ms=1500)
    _, check = read_device_info(h)
    if check and check[test_nibble_idx] == new_val:
        print(f"  *** SUCCESS! ***")
        do_signoff(h)
        return True
    print(f"  No change.")

    # ── Method 2: RID 13 with different mode bits ──
    # Standard sign-on is 0x15. Try other values that might trigger settings mode.
    print("\n── Method 2: RID 13 with settings mode bits ──")
    for val in [0x16, 0x17, 0x19, 0x1D, 0x25, 0x35, 0x05, 0x09]:
        pkt = bytes([0x0D, val])
        print(f"  TX RID 13: [{val:02X}]...", end=" ")
        timed_write(h, pkt)
        time.sleep(0.3)
        results = listen(h, timeout_ms=1000)
        rids = [rid for rid, _ in results]
        print(f"responses: {rids}")
        if any(rid not in (2, 5) for rid, _ in results):
            print(f"    *** New response type! ***")
            for rid, data in results:
                print(f"    RID {rid}: {hexline(data[:20])}")

    # ── Method 3: Feature report write ──
    print("\n── Method 3: Feature report 0x0E write ──")
    try:
        fr_pkt = bytearray(64)
        fr_pkt[0] = 0x0E
        fr_pkt[1:8] = modified_bytes
        h.send_feature_report(bytes(fr_pkt))
        print("  Sent OK")
        time.sleep(0.5)
        _, check = read_device_info(h)
        if check and check[test_nibble_idx] == new_val:
            print(f"  *** SUCCESS! ***")
            do_signoff(h)
            return True
        print(f"  No change.")
    except Exception as e:
        print(f"  Error: {e}")

    # ── Method 4: Feature report 154 write (was all zeros) ──
    print("\n── Method 4: Feature report 154 write ──")
    try:
        fr_pkt = bytearray(64)
        fr_pkt[0] = 154
        fr_pkt[1:8] = modified_bytes
        h.send_feature_report(bytes(fr_pkt))
        print("  Sent OK")
        time.sleep(0.5)
        _, check = read_device_info(h)
        if check and check[test_nibble_idx] == new_val:
            print(f"  *** SUCCESS! ***")
            do_signoff(h)
            return True
        print(f"  No change.")
    except Exception as e:
        print(f"  Error: {e}")

    print("\n  All RID write methods failed.")
    do_signoff(h)
    return False


# ── Test: Scan CVM Command Families ──────────────────────────────────────────

def test_scan_cmds(h):
    """Scan CVM command families for any that respond — especially settings-related ones."""
    print("\n" + "=" * 60)
    print("TEST: Scan CVM Command Families")
    print("=" * 60)
    print("  Scanning for responsive commands outside FWU API (0x4Fxx)")
    print("  WARNING: Skipping 0x4000 (crashes device)")

    do_signon(h)
    time.sleep(0.5)

    # Read current settings for comparison
    _, orig_nibbles = read_device_info(h)

    responsive = []

    # Scan families: 0x40xx, 0x41xx, 0x42xx, ..., 0x4Exx (skip 0x4Fxx = FWU)
    # Also include 0x50xx-0x52xx in case settings are in a higher range
    test_ranges = [
        # (start, end, skip_list)
        (0x4002, 0x4020, [0x4000]),  # Device info family (skip crash cmd)
        (0x4100, 0x4120, []),         # Headset info family
        (0x4200, 0x4220, []),         # Unknown — might be settings
        (0x4300, 0x4320, []),
        (0x4400, 0x4420, []),
        (0x4500, 0x4520, []),
        (0x4600, 0x4620, []),
        (0x4700, 0x4720, []),
        (0x4800, 0x4820, []),         # Connection status family
        (0x4A00, 0x4A20, []),
        (0x4C00, 0x4C20, []),
        (0x4D00, 0x4D20, []),
        (0x4E00, 0x4E20, []),
        (0x5000, 0x5020, []),         # Extended range
        (0x5100, 0x5120, []),
    ]

    for start, end, skip in test_ranges:
        for cmd in range(start, end, 2):  # Even = requests
            if cmd in skip:
                continue
            send_cvm(h, cmd)
            responses = recv_cvm(h, timeout_ms=300)
            if responses:
                for prim_id, params in responses:
                    label = f"0x{cmd:04X} → 0x{prim_id:04X}"
                    print(f"  {label}: {hexline(params[:20])}")
                    responsive.append((cmd, prim_id, params))

    print(f"\n  Found {len(responsive)} responsive commands")

    # Check if any command changed settings
    _, new_nibbles = read_device_info(h)
    if orig_nibbles and new_nibbles and orig_nibbles != new_nibbles:
        print("  *** SETTINGS CHANGED DURING SCAN! ***")
        for i, (o, n) in enumerate(zip(orig_nibbles, new_nibbles)):
            if o != n:
                print(f"    nibble[{i}]: {o} → {n}")

    do_signoff(h)
    return responsive


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="DECT Settings Write Protocol Probe")
    parser.add_argument("--test", default="read",
                        choices=["read", "write_cvm", "write_rid", "scan_cmds", "all"],
                        help="Test to run")
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=None,
                        help="Target device PID (hex, e.g. 0xAC28)")
    parser.add_argument("--reset", action="store_true", help="USB reset before test")
    args = parser.parse_args()

    print("DECT Settings Write Protocol Probe")
    print("=" * 60)

    # Find device
    from probes.hid_helpers import find_device
    dev_info = find_device(usage_page=0xFFA2)
    if not dev_info:
        print("No DECT device found on usage page 0xFFA2!")
        sys.exit(1)

    path = dev_info["path"]
    vid = dev_info["vendor_id"]
    pid = dev_info["product_id"]
    print(f"  Device: {dev_info.get('product_string', '?')} "
          f"VID=0x{vid:04X} PID=0x{pid:04X}")

    if args.reset:
        print("  Performing USB reset...")
        from probes.hid_helpers import usb_reset
        usb_reset(vid, pid)
        time.sleep(2)

    h = hid.device()
    try:
        h.open_path(path)
        h.set_nonblocking(0)
    except Exception as e:
        print(f"  Cannot open device: {e}")
        print("  Try: --reset flag, or kill Poly Lens processes")
        sys.exit(1)

    try:
        if args.test == "read":
            test_read(h)
        elif args.test == "write_cvm":
            test_write_cvm(h)
        elif args.test == "write_rid":
            test_write_rid(h)
        elif args.test == "scan_cmds":
            test_scan_cmds(h)
        elif args.test == "all":
            test_read(h)
            time.sleep(1)
            test_scan_cmds(h)
            time.sleep(1)
            test_write_cvm(h)
            time.sleep(1)
            test_write_rid(h)
    finally:
        try:
            do_signoff(h)
        except Exception:
            pass
        h.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
