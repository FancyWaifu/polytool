#!/usr/bin/env python3
"""
FWU API Explorer — Map the complete response protocol.

Now that we know:
  TX: [RID=3] [0x20] [len] [payload...] [pad to 64]
  RX: Report ID 5, 1-byte ack payload

This script tries multiple FWU commands and other message codes
to fully map what the device responds to, and checks all possible
response channels (RID 3, 5, 14, 15, and feature reports).

Usage:
  python3 fwu_explore.py [--test N]
"""

import sys
import time
import signal
import argparse
import hid

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
TARGET_USAGE_PAGE = 0xFFA2

FRAG_START = 0x20
RID_DATA = 0x03
RID_ACK = 0x05

FWU_MSG_NAMES = {
    0x00: "ENABLE_REQ", 0x01: "ENABLE_CFM", 0x02: "DEVICE_NOTIFY_IND",
    0x03: "UPDATE_REQ", 0x04: "UPDATE_CFM", 0x05: "UPDATE_IND",
    0x06: "UPDATE_RES", 0x07: "GET_BLOCK_IND", 0x08: "GET_BLOCK_RES",
    0x09: "GET_CRC_IND", 0x0A: "GET_CRC_RES", 0x0B: "COMPLETE_IND",
    0x0C: "STATUS_IND", 0x0D: "MULTI_CRC_IND", 0x0E: "MULTI_CRC_RES",
    0x0F: "CRC32_IND", 0x10: "CRC32_RES", 0x11: "PROGRESS_IND",
    0x12: "PLT_MSG", 0x13: "PLT_IND",
}


def hexdump(data, prefix="    "):
    if not data:
        return
    for i in range(0, len(data), 16):
        hex_part = " ".join(f"{b:02X}" for b in data[i:i+16])
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data[i:i+16])
        print(f"{prefix}{i:04X}: {hex_part:<48s} {asc_part}")


def find_device():
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS and d["usage_page"] == TARGET_USAGE_PAGE:
            return d
    return None


def timed_write(h, data, timeout=3):
    def handler(signum, frame):
        raise TimeoutError()
    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout)
    try:
        h.write(data)
        signal.alarm(0)
        return True
    except TimeoutError:
        return False
    except Exception as e:
        signal.alarm(0)
        print(f"    Write error: {e}")
        return False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def build_pkt(payload, report_size=64):
    pkt = bytearray(report_size)
    pkt[0] = RID_DATA
    pkt[1] = FRAG_START
    pkt[2] = len(payload)
    for i, b in enumerate(payload):
        if 3 + i < report_size:
            pkt[3 + i] = b
    return bytes(pkt)


def send_and_listen(h, payload, label, listen_ms=3000):
    """Send a command and collect all responses."""
    pkt = build_pkt(payload)
    hex_preview = ' '.join(f'{b:02X}' for b in pkt[:3+len(payload)])
    print(f"\n  >>> {label}")
    print(f"      TX: {hex_preview}")

    ok = timed_write(h, pkt)
    if not ok:
        print(f"      TIMEOUT/BLOCKED")
        return []

    h.set_nonblocking(1)
    responses = []
    elapsed = 0
    while elapsed < listen_ms:
        data = h.read(256)
        if data:
            rid = data[0] if data else 0
            payload_data = data[1:] if len(data) > 1 else b''
            hex_str = ' '.join(f'{b:02X}' for b in data)
            print(f"      RX [{elapsed}ms] RID={rid} ({len(data)}B): {hex_str}")

            # Decode based on RID
            if rid == RID_ACK:
                if payload_data:
                    print(f"      → DFUAck: value=0x{payload_data[0]:02X}")
            elif rid == RID_DATA:
                if len(payload_data) >= 2 and payload_data[0] == FRAG_START:
                    plen = payload_data[1]
                    msg = payload_data[2:2+plen]
                    if msg and msg[0] == 0x4F:
                        name = FWU_MSG_NAMES.get(msg[1], f"0x{msg[1]:02X}")
                        print(f"      → FWU {name}: {' '.join(f'{b:02X}' for b in msg)}")
                    else:
                        print(f"      → Data START len={plen}: {' '.join(f'{b:02X}' for b in msg)}")
                else:
                    print(f"      → Data payload: {' '.join(f'{b:02X}' for b in payload_data[:16])}")
            else:
                print(f"      → Other RID payload: {' '.join(f'{b:02X}' for b in payload_data[:20])}")

            responses.append(bytes(data))
        time.sleep(0.01)
        elapsed += 10

    if not responses:
        print(f"      (no response)")
    return responses


def read_all_features(h):
    """Read all feature reports and return as dict."""
    features = {}
    for rid in range(256):
        try:
            data = h.get_feature_report(rid, 512)
            if data and len(data) > 0 and any(b != 0 for b in data):
                features[rid] = bytes(data)
        except Exception:
            pass
    return features


def test_fresh_enable(h):
    """Test 1: Clean enable/disable cycle with full observation."""
    print("\n" + "=" * 60)
    print("TEST 1: Fresh Enable/Disable Cycle")
    print("=" * 60)

    # Read features before
    print("\n  --- Feature reports BEFORE ---")
    features_before = read_all_features(h)
    for rid, data in sorted(features_before.items()):
        print(f"    FR{rid:3d}: {' '.join(f'{b:02X}' for b in data)}")

    # Drain any pending input reports
    h.set_nonblocking(1)
    drained = 0
    while True:
        data = h.read(256)
        if not data:
            break
        drained += 1
    if drained:
        print(f"\n  Drained {drained} pending report(s)")

    # Enable FWU
    responses = send_and_listen(h, bytes([0x4F, 0x00, 0x01]),
                                 "FWU_ENABLE_REQ (enable=1)", listen_ms=5000)

    # Read features after enable
    print("\n  --- Feature reports AFTER ENABLE ---")
    features_after = read_all_features(h)
    for rid, data in sorted(features_after.items()):
        changed = " *** CHANGED" if features_before.get(rid) != data else ""
        print(f"    FR{rid:3d}: {' '.join(f'{b:02X}' for b in data)}{changed}")

    # Listen for more responses (DEVICE_NOTIFY_IND might be delayed)
    print(f"\n  --- Extended listen (10s) ---")
    h.set_nonblocking(1)
    elapsed = 0
    while elapsed < 10000:
        data = h.read(256)
        if data:
            hex_str = ' '.join(f'{b:02X}' for b in data)
            print(f"    [{elapsed}ms] ({len(data)}B): {hex_str}")
        time.sleep(0.01)
        elapsed += 10

    # Disable FWU
    responses = send_and_listen(h, bytes([0x4F, 0x00, 0x00]),
                                 "FWU_ENABLE_REQ (disable)", listen_ms=3000)

    # Read features after disable
    print("\n  --- Feature reports AFTER DISABLE ---")
    features_final = read_all_features(h)
    for rid, data in sorted(features_final.items()):
        changed = " *** CHANGED" if features_after.get(rid) != data else ""
        print(f"    FR{rid:3d}: {' '.join(f'{b:02X}' for b in data)}{changed}")


def test_all_fwu_commands(h):
    """Test 2: Try all FWU API command codes to see which get responses."""
    print("\n" + "=" * 60)
    print("TEST 2: Scan All FWU API Command Codes (0x4F00-0x4F13)")
    print("=" * 60)
    print("  Sending each command code to see what the device responds to.")
    print("  Most will likely be ignored or get error responses.\n")

    for cmd_lo in range(0x14):
        name = FWU_MSG_NAMES.get(cmd_lo, f"0x4F{cmd_lo:02X}")
        # Build minimal payload: [0x4F] [cmd_lo] [0x00] (1 byte param)
        payload = bytes([0x4F, cmd_lo, 0x00])
        send_and_listen(h, payload, f"0x4F{cmd_lo:02X} ({name})", listen_ms=1000)
        time.sleep(0.2)


def test_non_fwu_messages(h):
    """Test 3: Try message codes outside 0x4F range."""
    print("\n" + "=" * 60)
    print("TEST 3: Non-FWU Message Codes")
    print("=" * 60)
    print("  Testing if the device responds to other message families.\n")

    # FWS (Firmware Status) messages: 0x4Exx
    test_codes = [
        (bytes([0x4E, 0x00, 0x00]), "FWS_INIT_REQ (0x4E00)"),
        (bytes([0x4E, 0x02, 0x00]), "FWS_TERMINATE_REQ (0x4E02)"),
        (bytes([0x4E, 0x04, 0x00]), "FWS_STATUS_REQ? (0x4E04)"),
        # ODP/Settings commands
        (bytes([0x00, 0x00, 0x00]), "Null command"),
        (bytes([0x01, 0x00, 0x00]), "Generic cmd 0x01"),
        (bytes([0x80, 0x00, 0x00]), "Generic cmd 0x80"),
        (bytes([0xFF, 0x00, 0x00]), "Generic cmd 0xFF"),
    ]

    for payload, label in test_codes:
        send_and_listen(h, payload, label, listen_ms=1000)
        time.sleep(0.2)


def test_ack_channel(h):
    """Test 4: Investigate the DFUAck response format."""
    print("\n" + "=" * 60)
    print("TEST 4: DFUAck Response Investigation")
    print("=" * 60)
    print("  Sending enable multiple times to see ack pattern.\n")

    for i in range(4):
        enable = i % 2  # alternate enable/disable
        label = f"FWU_ENABLE_REQ (enable={enable}) — iteration {i+1}"
        responses = send_and_listen(h, bytes([0x4F, 0x00, enable]),
                                     label, listen_ms=2000)
        time.sleep(0.5)


def test_different_report_sizes(h):
    """Test 5: Try different total report sizes."""
    print("\n" + "=" * 60)
    print("TEST 5: Different Report Sizes for FWU_ENABLE_REQ")
    print("=" * 60)

    payload = bytes([0x4F, 0x00, 0x01])  # FWU_ENABLE_REQ enable=1

    for size in [5, 8, 16, 32, 63, 64, 65]:
        pkt = bytearray(size)
        pkt[0] = RID_DATA
        pkt[1] = FRAG_START
        pkt[2] = len(payload)
        for i, b in enumerate(payload):
            if 3 + i < size:
                pkt[3 + i] = b

        print(f"\n  >>> Size={size}: {' '.join(f'{b:02X}' for b in pkt[:8])}...")
        ok = timed_write(h, bytes(pkt))
        if ok:
            h.set_nonblocking(1)
            time.sleep(0.05)
            for _ in range(200):
                data = h.read(256)
                if data:
                    print(f"      RX ({len(data)}B): {' '.join(f'{b:02X}' for b in data)}")
                    break
                time.sleep(0.01)
            else:
                print(f"      (no response)")
        else:
            print(f"      BLOCKED/TIMEOUT")

        # Disable after each enable
        time.sleep(0.2)
        disable_pkt = build_pkt(bytes([0x4F, 0x00, 0x00]))
        timed_write(h, disable_pkt)
        time.sleep(0.3)
        # Drain
        while h.read(256):
            pass


def main():
    parser = argparse.ArgumentParser(description="FWU API Explorer")
    parser.add_argument("--test", type=int, default=None,
                        help="Run specific test (1-5). Default: run test 1.")
    args = parser.parse_args()

    info = find_device()
    if not info:
        print("No Poly device on FFA2.")
        sys.exit(1)

    print(f"Device: {info['product_string']} "
          f"(0x{info['vendor_id']:04X}:0x{info['product_id']:04X})")

    h = hid.device()
    h.open_path(info["path"])
    print("Opened.\n")

    try:
        if args.test is None or args.test == 1:
            test_fresh_enable(h)
        if args.test == 2:
            test_all_fwu_commands(h)
        if args.test == 3:
            test_non_fwu_messages(h)
        if args.test == 4:
            test_ack_channel(h)
        if args.test == 5:
            test_different_report_sizes(h)

    except KeyboardInterrupt:
        print("\n\nInterrupted — sending disable...")
        try:
            pkt = build_pkt(bytes([0x4F, 0x00, 0x00]))
            timed_write(h, pkt)
            time.sleep(0.5)
        except Exception:
            pass
    finally:
        h.close()
        print("\nClosed.")


if __name__ == "__main__":
    main()
