#!/usr/bin/env python3
"""
FWU Exclusive Mode Probe — Kill legacyhost, then try FWU protocol.

legacyhost (PID running) has the device open and may be:
  1. Consuming RID 3 input reports before we read them
  2. Holding device state that prevents FWU responses
  3. Maintaining a sign-on/exclusive session that blocks other clients

This script:
  Phase 1: Try FWU while legacyhost is alive (baseline)
  Phase 2: Kill legacyhost, then immediately try FWU
  Phase 3: Sign-on via RID 2 (02 01) then FWU (legacyhost init sequence)

Usage:
  python3 fwu_exclusive.py [--phase N]
"""

import sys
import os
import time
import signal
import subprocess
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
    0x0C: "STATUS_IND",
}


def hexline(data):
    return ' '.join(f'{b:02X}' for b in data)


def find_device(usage_page=None):
    """Find Poly device, optionally filtering by usage page."""
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS:
            if usage_page is None or d["usage_page"] == usage_page:
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


def build_fwu_pkt(payload, report_size=64):
    pkt = bytearray(report_size)
    pkt[0] = RID_DATA
    pkt[1] = FRAG_START
    pkt[2] = len(payload)
    for i, b in enumerate(payload):
        if 3 + i < report_size:
            pkt[3 + i] = b
    return bytes(pkt)


def listen_all(h, timeout_ms=5000, label=""):
    """Listen for ALL input reports and decode them."""
    h.set_nonblocking(1)
    elapsed = 0
    responses = []
    rid3_found = False
    while elapsed < timeout_ms:
        data = h.read(512)
        if data:
            rid = data[0]
            payload = data[1:]
            ts_str = f"[{elapsed}ms]"

            if rid == RID_DATA:
                rid3_found = True
                print(f"    {ts_str} *** RID 3 RESPONSE *** ({len(data)}B): {hexline(data)}")
                # Decode FWU framing
                if len(payload) >= 2 and payload[0] == FRAG_START:
                    plen = payload[1]
                    msg = payload[2:2+plen]
                    if msg and msg[0] == 0x4F:
                        name = FWU_MSG_NAMES.get(msg[1], f"0x{msg[1]:02X}")
                        print(f"         → FWU {name}: {hexline(msg)}")
                    else:
                        print(f"         → Data len={plen}: {hexline(msg[:20])}")
                elif len(payload) >= 1 and payload[0] == 0x80:
                    print(f"         → CONTINUATION: {hexline(payload[1:20])}")
                else:
                    print(f"         → Raw: {hexline(payload[:20])}")
            elif rid == RID_ACK:
                print(f"    {ts_str} RID 5 (ack/button): {hexline(data)}")
            elif rid == 0x02:
                print(f"    {ts_str} *** RID 2 RESPONSE *** : {hexline(data)}")
            elif rid == 0x0E:
                print(f"    {ts_str} RID 14 (settings): {hexline(data)}")
            else:
                print(f"    {ts_str} RID {rid} ({len(data)}B): {hexline(data)}")

            responses.append(bytes(data))
        time.sleep(0.005)
        elapsed += 5

    if not responses:
        print(f"    (no response within {timeout_ms}ms)")
    elif not rid3_found:
        print(f"    ({len(responses)} responses but NO RID 3)")
    return responses


def is_legacyhost_running():
    try:
        result = subprocess.run(["pgrep", "-f", "legacyhost"],
                                capture_output=True, text=True)
        return bool(result.stdout.strip())
    except Exception:
        return False


def kill_legacyhost():
    """Kill legacyhost and wait for it to die."""
    print("  Killing legacyhost...")
    subprocess.run(["pkill", "-f", "legacyhost"], capture_output=True)
    # Also kill LensService which may respawn legacyhost
    subprocess.run(["pkill", "-f", "LensService"], capture_output=True)
    subprocess.run(["pkill", "-f", "PolyLauncher"], capture_output=True)
    time.sleep(1)
    if is_legacyhost_running():
        print("  Still alive, sending SIGKILL...")
        subprocess.run(["pkill", "-9", "-f", "legacyhost"], capture_output=True)
        subprocess.run(["pkill", "-9", "-f", "LensService"], capture_output=True)
        time.sleep(1)
    if is_legacyhost_running():
        print("  WARNING: legacyhost is still running!")
    else:
        print("  legacyhost killed successfully.")


def phase1_baseline(h):
    """Try FWU while legacyhost is alive — establish baseline."""
    print("\n" + "=" * 60)
    print("PHASE 1: Baseline — FWU with legacyhost ALIVE")
    print("=" * 60)

    running = is_legacyhost_running()
    print(f"  legacyhost running: {running}")

    # Drain
    h.set_nonblocking(1)
    while h.read(256):
        pass

    # Send FWU_ENABLE_REQ
    pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x01]))
    print(f"\n  >>> FWU_ENABLE_REQ (enable=1)")
    print(f"      TX: {hexline(pkt[:6])}")
    ok = timed_write(h, pkt)
    if ok:
        print("      Write accepted")
        listen_all(h, timeout_ms=5000)
    else:
        print("      BLOCKED/TIMEOUT")

    # Disable
    pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x00]))
    timed_write(h, pkt)
    time.sleep(0.5)
    # Drain
    h.set_nonblocking(1)
    while h.read(256):
        pass


def phase2_exclusive(h_close_and_reopen=True):
    """Kill legacyhost, then try FWU with exclusive device access."""
    print("\n" + "=" * 60)
    print("PHASE 2: Exclusive — Kill legacyhost, then FWU")
    print("=" * 60)

    if is_legacyhost_running():
        kill_legacyhost()
    else:
        print("  legacyhost not running.")

    # Re-enumerate and open device fresh
    print("\n  Re-enumerating devices after killing legacyhost...")
    time.sleep(2)  # Let macOS release the device handle

    # Try FFA2 first, then any Poly device
    info = find_device(usage_page=TARGET_USAGE_PAGE)
    if not info:
        info = find_device()
    if not info:
        print("  No Poly device found after killing legacyhost!")
        return None

    print(f"  Device: {info['product_string']}")
    print(f"  Usage:  0x{info['usage_page']:04X}:0x{info['usage']:04X}")

    h = hid.device()
    h.open_path(info["path"])
    print("  Opened fresh handle.\n")

    # Drain any pending reports
    h.set_nonblocking(1)
    drained = 0
    while h.read(256):
        drained += 1
    if drained:
        print(f"  Drained {drained} pending report(s)")

    # Read FR15 state
    try:
        fr15 = h.get_feature_report(15, 64)
        print(f"  FR15: {hexline(fr15)}")
    except Exception as e:
        print(f"  FR15 read error: {e}")

    # Send FWU_ENABLE_REQ
    pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x01]))
    print(f"\n  >>> FWU_ENABLE_REQ (enable=1)")
    print(f"      TX: {hexline(pkt[:6])}")
    ok = timed_write(h, pkt)
    if ok:
        print("      Write accepted")
        print(f"\n  Listening 10s for FWU responses (especially RID 3)...")
        listen_all(h, timeout_ms=10000)
    else:
        print("      BLOCKED/TIMEOUT")

    # Read FR15 after
    try:
        fr15 = h.get_feature_report(15, 64)
        print(f"\n  FR15 after enable: {hexline(fr15)}")
    except Exception:
        pass

    # Disable
    print("\n  Sending FWU disable...")
    pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x00]))
    timed_write(h, pkt)
    listen_all(h, timeout_ms=2000)

    return h


def phase3_signon_then_fwu(h):
    """Try legacyhost's sign-on sequence: RID 2 (02 01) then FWU."""
    print("\n" + "=" * 60)
    print("PHASE 3: Sign-On via RID 2, then FWU")
    print("=" * 60)
    print("  legacyhost sends RID 2 (02 01) during initSignOnExclusive().")
    print("  This may be required before FWU responses are routed.\n")

    # Drain
    h.set_nonblocking(1)
    while h.read(256):
        pass

    # Try RID 2 sign-on: [02 01] padded to various sizes
    signon_attempts = [
        ("RID 2: [02 01]", bytes([0x02, 0x01])),
        ("RID 2: [02 01] pad 18B", bytes([0x02, 0x01]) + bytes(16)),
        ("RID 2: [02 01] pad 64B", bytes([0x02, 0x01]) + bytes(62)),
        # legacyhost might also use 0x20 framing on RID 2
        ("RID 2: [02 20 01 01]", bytes([0x02, 0x20, 0x01, 0x01]) + bytes(14)),
    ]

    for label, data in signon_attempts:
        print(f"\n  >>> {label}: {hexline(data[:8])}")
        ok = timed_write(h, data)
        if ok:
            print(f"      Write accepted!")
            responses = listen_all(h, timeout_ms=2000)
        else:
            print(f"      BLOCKED/TIMEOUT")
        time.sleep(0.3)

    # Now try FWU_ENABLE_REQ after sign-on
    print(f"\n  --- Now trying FWU after sign-on ---")

    pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x01]))
    print(f"\n  >>> FWU_ENABLE_REQ (enable=1)")
    print(f"      TX: {hexline(pkt[:6])}")
    ok = timed_write(h, pkt)
    if ok:
        print("      Write accepted")
        print(f"\n  Listening 10s for RID 3 responses...")
        listen_all(h, timeout_ms=10000)
    else:
        print("      BLOCKED/TIMEOUT")

    # Cleanup
    pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x00]))
    timed_write(h, pkt)
    time.sleep(0.5)
    h.set_nonblocking(1)
    while h.read(256):
        pass


def main():
    parser = argparse.ArgumentParser(description="FWU Exclusive Mode Probe")
    parser.add_argument("--phase", type=int, default=None,
                        help="Run specific phase (1=baseline, 2=kill legacyhost, 3=sign-on)")
    args = parser.parse_args()

    if args.phase is None or args.phase == 1:
        info = find_device()
        if not info:
            print("No Poly device found.")
            sys.exit(1)

        print(f"Device: {info['product_string']} "
              f"(0x{info['vendor_id']:04X}:0x{info['product_id']:04X})")
        print(f"  Usage: 0x{info['usage_page']:04X}:0x{info['usage']:04X}")

        h = hid.device()
        h.open_path(info["path"])
        print("  Opened.\n")

        try:
            phase1_baseline(h)
        finally:
            h.close()
            print("\nClosed.")

    if args.phase is None or args.phase == 2:
        try:
            h = phase2_exclusive()
            if h:
                h.close()
                print("\nClosed.")
        except KeyboardInterrupt:
            print("\nInterrupted.")

    if args.phase == 3:
        # Phase 3 requires legacyhost to be dead
        if is_legacyhost_running():
            kill_legacyhost()
            time.sleep(2)

        info = find_device()
        if not info:
            print("No Poly device found.")
            sys.exit(1)

        print(f"Device: {info['product_string']} "
              f"(0x{info['vendor_id']:04X}:0x{info['product_id']:04X})")

        h = hid.device()
        h.open_path(info["path"])
        print("  Opened.\n")

        try:
            phase3_signon_then_fwu(h)
        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            h.close()
            print("\nClosed.")


if __name__ == "__main__":
    main()
