#!/usr/bin/env python3
"""
FWU Sign-On via RID 13 → RID 2 Protocol.

From HID descriptor analysis:
  RID 13 (OUTPUT, 1 byte) — Sign-on control
    Usage 0x8F: 2 bits (min=-2, max=1) — sign-on state
    Usage 0x77: 2 bits (min=1, max=2) — mode selector
    Usage 0xF2: 1 bit  (min=0, max=1) — trigger flag
    + 3 bits padding

  RID 2 (INPUT, 1 byte) — Sign-on response
    Usage 0x8F: 1 bit — sign-on status
    Usage 0xEE: 1 bit
    Usage 0x77: 1 bit
    Usage 0x80: 1 bit
    Usage 0xC1: 1 bit
    + 3 bits padding

  legacyhost calls initSignOnExclusive(true) → probably writes to RID 13
  Device responds with RID 2 → sign-on complete
  Then HIDPipeData (RID 3) becomes active for FWU

Usage:
  python3 fwu_signon2.py
"""

import sys
import time
import signal
import subprocess
import hid

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
TARGET_USAGE_PAGE = 0xFFA2

FRAG_START = 0x20
RID_DATA = 0x03
RID_ACK = 0x05


def hexline(data):
    return ' '.join(f'{b:02X}' for b in data)


def find_device():
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS and d["usage_page"] == TARGET_USAGE_PAGE:
            return d
    # Fallback: any Poly device
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS:
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


def listen(h, timeout_ms=5000, quiet_rids=None):
    """Listen for input reports, return list of (rid, data) tuples."""
    if quiet_rids is None:
        quiet_rids = set()
    h.set_nonblocking(1)
    elapsed = 0
    results = []
    while elapsed < timeout_ms:
        data = h.read(512)
        if data:
            rid = data[0]
            if rid not in quiet_rids:
                print(f"    [{elapsed:5d}ms] RID {rid:3d} (0x{rid:02X}) "
                      f"({len(data)}B): {hexline(data)}")
                # Special decode
                if rid == 0x02:
                    bits = data[1] if len(data) > 1 else 0
                    print(f"             → SignOn: 0x8F={bits&1} 0xEE={(bits>>1)&1} "
                          f"0x77={(bits>>2)&1} 0x80={(bits>>3)&1} 0xC1={(bits>>4)&1}")
                elif rid == RID_DATA and len(data) > 2:
                    payload = data[1:]
                    if payload[0] == FRAG_START and len(payload) >= 2:
                        plen = payload[1]
                        msg = payload[2:2+plen]
                        if msg and msg[0] == 0x4F:
                            FWU_NAMES = {
                                0x00: "ENABLE_REQ", 0x01: "ENABLE_CFM",
                                0x02: "DEVICE_NOTIFY_IND", 0x04: "UPDATE_CFM",
                                0x0C: "STATUS_IND",
                            }
                            name = FWU_NAMES.get(msg[1], f"0x{msg[1]:02X}")
                            print(f"             → *** FWU {name} *** : {hexline(msg)}")
                        else:
                            print(f"             → Data len={plen}: {hexline(msg[:20])}")
                elif rid == 0x0E:
                    print(f"             → Settings report")
            results.append((rid, bytes(data)))
        time.sleep(0.005)
        elapsed += 5
    return results


def kill_poly():
    """Kill Poly Lens processes."""
    for proc in ["legacyhost", "LensService", "PolyLauncher"]:
        subprocess.run(["pkill", "-f", proc], capture_output=True)
    time.sleep(1)
    # Verify
    result = subprocess.run(["pgrep", "-f", "legacyhost"], capture_output=True)
    if result.stdout.strip():
        subprocess.run(["pkill", "-9", "-f", "legacyhost"], capture_output=True)
        time.sleep(1)


def main():
    # Kill Poly Lens first for exclusive access
    print("Killing Poly Lens processes...")
    kill_poly()
    time.sleep(2)

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
        # Step 0: Read current state
        print("=" * 60)
        print("STEP 0: Current state")
        print("=" * 60)
        fr15 = h.get_feature_report(15, 64)
        print(f"  FR15: {hexline(fr15)}")

        # Drain pending
        h.set_nonblocking(1)
        drained = 0
        while h.read(256):
            drained += 1
        if drained:
            print(f"  Drained {drained} pending report(s)")

        # Step 1: Listen for unsolicited RID 2 first
        print(f"\n{'='*60}")
        print("STEP 1: Listen for unsolicited reports (3s)")
        print("=" * 60)
        results = listen(h, timeout_ms=3000)
        if not results:
            print("  No unsolicited reports.")

        # Step 2: Try RID 13 sign-on writes
        # RID 13 output format (1 byte):
        #   bits 0-1: Usage 0x8F (sign-on state, range -2 to 1)
        #   bits 2-3: Usage 0x77 (mode, range 1 to 2)
        #   bit 4:    Usage 0xF2 (trigger, range 0 to 1)
        #   bits 5-7: padding
        print(f"\n{'='*60}")
        print("STEP 2: Sign-On via RID 13 output report")
        print("=" * 60)
        print("  RID 13 bit layout:")
        print("    [1:0] Usage 0x8F — sign-on state (-2 to 1)")
        print("    [3:2] Usage 0x77 — mode (1 to 2)")
        print("    [4]   Usage 0xF2 — trigger (0 to 1)")
        print("    [7:5] padding")

        # Build various RID 13 values
        # Bit field: [pad:3][F2:1][77:2][8F:2]
        signon_attempts = [
            # sign-on=1, mode=1, trigger=1  → 0b000_1_01_01 = 0x15
            ("signon=1, mode=1, trigger=1", 0x15),
            # sign-on=1, mode=1, trigger=0  → 0b000_0_01_01 = 0x05
            ("signon=1, mode=1, trigger=0", 0x05),
            # sign-on=1, mode=2, trigger=1  → 0b000_1_10_01 = 0x19
            ("signon=1, mode=2, trigger=1", 0x19),
            # sign-on=0, mode=1, trigger=1  → 0b000_1_01_00 = 0x14
            ("signon=0, mode=1, trigger=1", 0x14),
            # sign-on=-1 (0xFF=3 in 2-bit), mode=1, trigger=1  → 0b000_1_01_11 = 0x17
            ("signon=-1(0x3), mode=1, trigger=1", 0x17),
            # sign-on=-2 (0xFE=2 in 2-bit), mode=1, trigger=1  → 0b000_1_01_10 = 0x16
            ("signon=-2(0x2), mode=1, trigger=1", 0x16),
            # All ones: 0x1F
            ("all bits set", 0x1F),
            # Just sign-on bit
            ("just signon=1", 0x01),
            # Exclusive mode? mode=2, signon=1
            ("signon=1, mode=2, trigger=0", 0x09),
        ]

        for label, value in signon_attempts:
            byte_val = value & 0xFF
            pkt = bytes([0x0D, byte_val])
            print(f"\n  >>> {label} → [0D {byte_val:02X}] = {byte_val:08b}")
            ok = timed_write(h, pkt)
            if ok:
                print(f"      Write accepted!")
                # Listen for RID 2 response
                results = listen(h, timeout_ms=2000, quiet_rids={0x05})
                rid2_found = any(rid == 2 for rid, _ in results)
                if rid2_found:
                    print(f"      *** GOT RID 2 SIGN-ON RESPONSE! ***")
                    # Now try FWU
                    print(f"\n      Sending FWU_ENABLE_REQ after sign-on...")
                    fwu_pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x01]))
                    ok2 = timed_write(h, fwu_pkt)
                    if ok2:
                        print(f"      FWU write accepted, listening 10s...")
                        fwu_results = listen(h, timeout_ms=10000)
                        rid3_found = any(rid == 3 for rid, _ in fwu_results)
                        if rid3_found:
                            print(f"\n      *** FWU RESPONSE ON RID 3! SUCCESS! ***")
                        # Disable FWU
                        dis_pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x00]))
                        timed_write(h, dis_pkt)
                    break  # Found working sign-on, stop trying
                else:
                    print(f"      No RID 2 response")
            else:
                print(f"      BLOCKED/TIMEOUT")
            time.sleep(0.3)

        # Step 3: Try writing larger RID 13 packets (some devices need padding)
        print(f"\n{'='*60}")
        print("STEP 3: RID 13 with padding variations")
        print("=" * 60)

        for pad_size in [0, 1, 7, 15, 63]:
            pkt = bytes([0x0D, 0x15]) + bytes(pad_size)
            print(f"\n  >>> RID13 value=0x15, total {len(pkt)}B: {hexline(pkt[:8])}")
            ok = timed_write(h, pkt)
            if ok:
                print(f"      Write accepted")
                results = listen(h, timeout_ms=2000, quiet_rids={0x05})
                if any(rid == 2 for rid, _ in results):
                    print(f"      *** RID 2 RESPONSE! ***")
            else:
                print(f"      BLOCKED/TIMEOUT")
            time.sleep(0.3)

        # Step 4: Try FWU without sign-on but with longer listen
        print(f"\n{'='*60}")
        print("STEP 4: FWU_ENABLE with 30s listen (no sign-on)")
        print("=" * 60)
        print("  Maybe DEVICE_NOTIFY_IND takes a long time (DECT wireless delay)?")

        fwu_pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x01]))
        print(f"\n  >>> FWU_ENABLE_REQ: {hexline(fwu_pkt[:6])}")
        ok = timed_write(h, fwu_pkt)
        if ok:
            print(f"      Write accepted, listening 30s...")
            results = listen(h, timeout_ms=30000)
            rid3_found = any(rid == 3 for rid, _ in results)
            if rid3_found:
                print(f"\n  *** FWU DATA ON RID 3! ***")
            else:
                print(f"\n  No RID 3 data in 30 seconds.")

            # Disable
            dis_pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x00]))
            timed_write(h, dis_pkt)
            time.sleep(0.5)
            # Drain
            h.set_nonblocking(1)
            while h.read(256):
                pass

        # Step 5: Try FF58 pipe (RID 0x58)
        print(f"\n{'='*60}")
        print("STEP 5: Try FWU via FF58 pipe (RID 0x58)")
        print("=" * 60)
        print("  RID 0x58 is a 63-byte bidirectional pipe on FF58 usage page.")

        fwu_payload = bytes([0x4F, 0x00, 0x01])
        pkt58 = bytearray(64)
        pkt58[0] = 0x58  # RID 88
        pkt58[1] = FRAG_START
        pkt58[2] = len(fwu_payload)
        pkt58[3] = 0x4F
        pkt58[4] = 0x00
        pkt58[5] = 0x01

        print(f"\n  >>> RID 0x58: {hexline(bytes(pkt58)[:8])}")
        ok = timed_write(h, bytes(pkt58))
        if ok:
            print(f"      Write accepted!")
            results = listen(h, timeout_ms=5000)
            if any(rid == 0x58 for rid, _ in results):
                print(f"      *** RESPONSE ON RID 0x58! ***")
        else:
            print(f"      BLOCKED/TIMEOUT")

    except KeyboardInterrupt:
        print("\n\nInterrupted!")
        # Try to disable FWU
        try:
            dis_pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x00]))
            timed_write(h, dis_pkt)
        except Exception:
            pass
    finally:
        h.close()
        print("\nClosed.")


if __name__ == "__main__":
    main()
