#!/usr/bin/env python3
"""
Clean FWU test — single sign-on, FWU enable, careful listen, proper cleanup.

Lessons learned:
  - RID 13 (0x0D) output = sign-on trigger → RID 2 response
  - Value 0x15 works: signon=1, mode=1, trigger=1
  - Multiple sign-on writes without sign-off WEDGES the device
  - Device needs USB reset to recover from wedged state
  - After sign-on, FWU writes accepted but only DFUAck (RID 5) received
  - Need to sign off (RID 13 with signon=0) to release device

Protocol (from this analysis):
  1. Sign on: write [0D 15] → get RID 2 + RID 14
  2. FWU enable: write [03 20 03 4F 00 01] → get RID 5 DFUAck
  3. Listen for RID 3 data (DEVICE_NOTIFY_IND)
  4. FWU disable: write [03 20 03 4F 00 00]
  5. Sign off: write [0D 00] or [0D 04]
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


def hexline(data):
    return ' '.join(f'{b:02X}' for b in data)


def find_device():
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS and d["usage_page"] == TARGET_USAGE_PAGE:
            return d
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS:
            return d
    return None


def timed_write(h, data, timeout=5):
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


def listen(h, timeout_ms=5000, label=""):
    """Listen and decode ALL input reports."""
    h.set_nonblocking(1)
    elapsed = 0
    results = []
    while elapsed < timeout_ms:
        data = h.read(512)
        if data:
            rid = data[0]
            ts = f"[{elapsed:5d}ms]"
            if rid == RID_DATA:
                print(f"    {ts} *** RID 3 ({len(data)}B): {hexline(data)}")
                payload = data[1:]
                if payload and payload[0] == FRAG_START and len(payload) >= 2:
                    plen = payload[1]
                    msg = payload[2:2+plen]
                    if msg and msg[0] == 0x4F:
                        FWU = {0x01: "ENABLE_CFM", 0x02: "DEVICE_NOTIFY_IND",
                               0x04: "UPDATE_CFM", 0x05: "UPDATE_IND",
                               0x0B: "COMPLETE_IND", 0x0C: "STATUS_IND"}
                        name = FWU.get(msg[1], f"CMD_0x{msg[1]:02X}")
                        print(f"         → FWU {name}: {hexline(msg)}")
            elif rid == 0x02:
                bits = data[1] if len(data) > 1 else 0
                print(f"    {ts} RID 2 (sign-on): {hexline(data)}  "
                      f"8F={bits&1} EE={(bits>>1)&1} 77={(bits>>2)&1} "
                      f"80={(bits>>3)&1} C1={(bits>>4)&1}")
            elif rid == 0x05:
                val = data[1] if len(data) > 1 else 0
                print(f"    {ts} RID 5 (DFUAck): {hexline(data)}  val={val}")
            elif rid == 0x0E:
                print(f"    {ts} RID 14 (settings {len(data)}B): {hexline(data)}")
            elif rid == 0xFE:
                print(f"    {ts} RID 254 ({len(data)}B): {hexline(data)}")
            else:
                print(f"    {ts} RID {rid} ({len(data)}B): {hexline(data)}")
            results.append((rid, bytes(data)))
        time.sleep(0.005)
        elapsed += 5
    return results


def usb_reset():
    """Reset the USB device to clear any wedged state."""
    try:
        import usb.core
        dev = usb.core.find(idVendor=0x047F, idProduct=0xACFF)
        if dev:
            dev.reset()
            time.sleep(3)
            return True
    except Exception:
        pass
    return False


def main():
    # Kill Poly Lens
    print("Killing Poly Lens...")
    for proc in ["legacyhost", "LensService", "PolyLauncher"]:
        subprocess.run(["pkill", "-f", proc], capture_output=True)
    time.sleep(2)

    # USB reset for clean slate
    print("USB reset for clean state...")
    usb_reset()

    info = find_device()
    if not info:
        print("No Poly device found.")
        sys.exit(1)

    print(f"\nDevice: {info['product_string']} "
          f"(0x{info['vendor_id']:04X}:0x{info['product_id']:04X})")
    print(f"  Usage: 0x{info['usage_page']:04X}:0x{info['usage']:04X}")

    h = hid.device()
    h.open_path(info["path"])
    print("  Opened.")

    # Read initial state
    fr15 = h.get_feature_report(15, 64)
    print(f"  FR15: {hexline(fr15)}")

    # Drain
    h.set_nonblocking(1)
    while h.read(256):
        pass

    try:
        # ============================================================
        # STEP 1: Sign on via RID 13
        # ============================================================
        print(f"\n{'='*60}")
        print("STEP 1: Sign On (RID 13 → RID 2)")
        print("=" * 60)

        pkt = bytes([0x0D, 0x15])  # signon=1, mode=1, trigger=1
        print(f"  TX: [0D 15]")
        ok = timed_write(h, pkt)
        if not ok:
            print("  Sign-on BLOCKED!")
            return

        print("  Sign-on accepted. Listening 3s...")
        results = listen(h, timeout_ms=3000)

        got_rid2 = any(rid == 2 for rid, _ in results)
        got_rid14 = any(rid == 14 for rid, _ in results)
        print(f"\n  Sign-on: RID2={'YES' if got_rid2 else 'NO'} "
              f"RID14={'YES' if got_rid14 else 'NO'}")

        fr15 = h.get_feature_report(15, 64)
        print(f"  FR15 after sign-on: {hexline(fr15)}")

        # Drain
        time.sleep(0.5)
        h.set_nonblocking(1)
        while h.read(256):
            pass

        # ============================================================
        # STEP 2: FWU Enable
        # ============================================================
        print(f"\n{'='*60}")
        print("STEP 2: FWU_ENABLE_REQ")
        print("=" * 60)

        pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x01]))
        print(f"  TX: {hexline(pkt[:6])}")
        ok = timed_write(h, pkt)
        if not ok:
            print("  FWU write BLOCKED!")
        else:
            print("  FWU write accepted. Listening 15s for DEVICE_NOTIFY_IND...")
            results = listen(h, timeout_ms=15000)

            rid3_found = any(rid == 3 for rid, _ in results)
            rid5_found = any(rid == 5 for rid, _ in results)
            print(f"\n  FWU: RID3={'YES ★' if rid3_found else 'NO'} "
                  f"RID5={'YES' if rid5_found else 'NO'}")

            if not rid3_found:
                # Try additional commands while FWU is enabled
                print(f"\n  --- Trying additional commands while FWU enabled ---")

                # Try UPDATE_REQ
                cmds = [
                    (bytes([0x4F, 0x03, 0x00]), "UPDATE_REQ dev=0"),
                    (bytes([0x4F, 0x0C, 0x00]), "STATUS_IND query"),
                ]
                for payload, label in cmds:
                    pkt = build_fwu_pkt(payload)
                    print(f"\n  >>> {label}: {hexline(pkt[:3+len(payload)])}")
                    ok2 = timed_write(h, pkt)
                    if ok2:
                        listen(h, timeout_ms=3000)
                    else:
                        print("    BLOCKED")
                    time.sleep(0.3)

        # ============================================================
        # STEP 3: FWU Disable
        # ============================================================
        print(f"\n{'='*60}")
        print("STEP 3: FWU Disable")
        print("=" * 60)

        pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x00]))
        print(f"  TX: {hexline(pkt[:6])}")
        ok = timed_write(h, pkt)
        if ok:
            listen(h, timeout_ms=1000)
        else:
            print("  FWU disable BLOCKED")

        time.sleep(0.3)

        # ============================================================
        # STEP 4: Sign Off
        # ============================================================
        print(f"\n{'='*60}")
        print("STEP 4: Sign Off (RID 13, clear sign-on)")
        print("=" * 60)

        # Try signing off with signon=0
        signoff_values = [
            (0x00, "all clear"),
            (0x04, "mode=1, signon=0"),
            (0x10, "trigger only"),
            (0x14, "signon=0, mode=1, trigger=1"),
        ]

        for val, label in signoff_values:
            pkt = bytes([0x0D, val])
            print(f"\n  TX: [0D {val:02X}] — {label}")
            ok = timed_write(h, pkt)
            if ok:
                print("    Accepted!")
                listen(h, timeout_ms=1000)
                fr15 = h.get_feature_report(15, 64)
                print(f"    FR15: {hexline(fr15)}")
            else:
                print("    BLOCKED")
            time.sleep(0.3)

    except KeyboardInterrupt:
        print("\n\nInterrupted!")
    finally:
        # Try to clean up
        try:
            pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x00]))
            timed_write(h, pkt)
        except Exception:
            pass
        try:
            h.close()
        except Exception:
            pass

        fr15_check = None
        try:
            info2 = find_device()
            if info2:
                h2 = hid.device()
                h2.open_path(info2["path"])
                fr15_check = h2.get_feature_report(15, 64)
                h2.close()
        except Exception:
            pass

        if fr15_check:
            print(f"\n  FR15 final: {hexline(fr15_check)}")
        print("\nClosed.")


if __name__ == "__main__":
    main()
