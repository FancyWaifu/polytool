#!/usr/bin/env python3
"""
FWU Sign-On + Feature Report Probe.

From legacyhost log analysis:
  1. Device opens → calls initSignOnExclusive(true)
  2. Device sends RID 2 report (02 01) and RID 14 (FW version)
  3. Then HIDPipeData communication starts

Feature Report 15 values observed:
  Before Poly Lens: 0F 00 01 0F 20
  After Poly Lens:  0F 01 19 0F 00

Byte 1 changed 0x00→0x01 (possibly sign-on state).

This script tries:
  Test 1: Write to FR15 to initiate sign-on
  Test 2: Send FWU commands as feature reports on RID 3
  Test 3: Try writing sign-on via output reports
  Test 4: Send raw HIDPipeData with different command bytes
"""

import sys
import time
import signal
import hid

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
TARGET_USAGE_PAGE = 0xFFA2


def hexline(data):
    return ' '.join(f'{b:02X}' for b in data)


def find_device():
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
        return False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def timed_feature_write(h, data, timeout=3):
    def handler(signum, frame):
        raise TimeoutError()
    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout)
    try:
        h.send_feature_report(data)
        signal.alarm(0)
        return True
    except TimeoutError:
        return False
    except Exception as e:
        signal.alarm(0)
        print(f"      Feature write error: {e}")
        return False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def read_all(h, timeout_ms=3000, label=""):
    """Read all input reports within timeout."""
    h.set_nonblocking(1)
    responses = []
    elapsed = 0
    while elapsed < timeout_ms:
        data = h.read(512)
        if data:
            rid = data[0] if data else 0
            print(f"    <<< [{elapsed}ms] RID={rid} ({len(data)}B): {hexline(data)}")
            if rid == 0x03 and len(data) > 2:
                # Potential FWU data response!
                print(f"    *** RID 3 RESPONSE! Payload: {hexline(data[1:])}")
            elif rid == 0x0E:
                print(f"    → Settings report (RID 14)")
            elif rid == 0x02:
                print(f"    → Sign-on response (RID 2)?")
            responses.append(bytes(data))
        time.sleep(0.005)
        elapsed += 5
    return responses


def test1_signon_fr15(h):
    """Try to sign on via Feature Report 15."""
    print("\n" + "=" * 60)
    print("TEST 1: Sign-On via Feature Report 15")
    print("=" * 60)

    # Read current FR15
    fr15 = h.get_feature_report(15, 64)
    print(f"\n  Current FR15: {hexline(fr15)}")

    # Try various sign-on values
    attempts = [
        # Restore "before Poly Lens" state
        ("Reset to 00 01 0F 20", bytes([0x0F, 0x00, 0x01, 0x0F, 0x20])),
        # Set sign-on byte to 1
        ("Set byte1=0x01", bytes([0x0F, 0x01, 0x01, 0x0F, 0x20])),
        # Set sign-on byte to 0x80 (exclusive mode?)
        ("Set byte1=0x80", bytes([0x0F, 0x80, 0x01, 0x0F, 0x20])),
        # Try current value with byte1=0x02
        ("Set byte1=0x02", bytes([0x0F, 0x02, 0x01, 0x0F, 0x20])),
        # Write current value back (identity write)
        ("Identity write", bytes(fr15)),
    ]

    for label, data in attempts:
        print(f"\n  >>> {label}: {hexline(data)}")
        ok = timed_feature_write(h, data)
        if ok:
            print(f"      Write OK")
            # Check for input reports
            responses = read_all(h, timeout_ms=2000)
            if not responses:
                print(f"      No input reports")
            # Read FR15 back
            fr15_after = h.get_feature_report(15, 64)
            changed = " *** CHANGED" if list(fr15_after) != list(fr15) else ""
            print(f"      FR15 now: {hexline(fr15_after)}{changed}")
            fr15 = fr15_after
        else:
            print(f"      BLOCKED/TIMEOUT")
        time.sleep(0.3)


def test2_fwu_as_feature(h):
    """Try sending FWU commands as feature reports on RID 3."""
    print("\n" + "=" * 60)
    print("TEST 2: FWU_ENABLE via Feature Report on RID 3")
    print("=" * 60)
    print("  Instead of h.write() (output report), try h.send_feature_report()")

    # Build FWU_ENABLE_REQ with 0x20 framing as feature report
    fwu_payload = bytes([0x4F, 0x00, 0x01])  # FWU_ENABLE_REQ enable=1

    attempts = [
        # Same framing as output report but via feature
        ("FR3: [03 20 03 4F 00 01] pad64",
         bytes([0x03, 0x20, 0x03, 0x4F, 0x00, 0x01]) + bytes(58)),
        # Shorter — just the essential bytes
        ("FR3: [03 20 03 4F 00 01]",
         bytes([0x03, 0x20, 0x03, 0x4F, 0x00, 0x01])),
        # Without framing
        ("FR3: [03 4F 00 01] raw",
         bytes([0x03, 0x4F, 0x00, 0x01])),
        # Try 5-byte (matching FR15 size)
        ("FR3: [03 20 03 4F 00] 5B",
         bytes([0x03, 0x20, 0x03, 0x4F, 0x00])),
    ]

    for label, data in attempts:
        print(f"\n  >>> {label}: {hexline(data[:10])}{'...' if len(data) > 10 else ''}")
        ok = timed_feature_write(h, data)
        if ok:
            print(f"      Feature write accepted!")
            responses = read_all(h, timeout_ms=3000)
            if not responses:
                print(f"      No input reports")
            # Also check if FR3 changed
            try:
                fr3 = h.get_feature_report(3, 64)
                if any(b != 0 for b in fr3):
                    print(f"      FR3 readback: {hexline(fr3[:20])}")
            except Exception:
                pass
        else:
            print(f"      BLOCKED/TIMEOUT")

        # Disable
        disable = bytes([0x03, 0x20, 0x03, 0x4F, 0x00, 0x00]) + bytes(58)
        timed_feature_write(h, disable)
        time.sleep(0.3)


def test3_signon_output(h):
    """Try sign-on via output reports on various RIDs."""
    print("\n" + "=" * 60)
    print("TEST 3: Sign-On via Output Reports")
    print("=" * 60)

    # legacyhost's initSignOnExclusive might send a specific output report
    # Try common sign-on patterns

    attempts = [
        # RID 14 (0x0E) — settings report, maybe writable
        ("RID14 sign-on: [0E 01]",
         bytes([0x0E, 0x01]) + bytes(16)),
        # RID 14 with exclusive flag
        ("RID14 exclusive: [0E 80 01]",
         bytes([0x0E, 0x80, 0x01]) + bytes(15)),
        # RID 3 with simple command (not FWU, just HIDPipe init)
        ("RID3 pipe init: [03 00]",
         bytes([0x03, 0x00]) + bytes(62)),
        # RID 3 with sign-on command
        ("RID3 sign-on: [03 01 00]",
         bytes([0x03, 0x01, 0x00]) + bytes(61)),
        # RID 254 (0xFE) — sometimes used for control
        ("RID254: [FE 01]",
         bytes([0xFE, 0x01]) + bytes(2)),
        # RID 0 — default report
        ("RID0 sign-on: [00 01 00]",
         bytes([0x00, 0x01, 0x00]) + bytes(61)),
    ]

    for label, data in attempts:
        print(f"\n  >>> {label}: {hexline(data[:8])}")
        ok = timed_write(h, data)
        if ok:
            print(f"      Write OK")
            responses = read_all(h, timeout_ms=2000)
            if not responses:
                print(f"      No response")
        else:
            print(f"      BLOCKED/TIMEOUT")
        time.sleep(0.3)


def test4_hidpipe_commands(h):
    """Try different HIDPipeData command bytes."""
    print("\n" + "=" * 60)
    print("TEST 4: HIDPipeData with Different Command Bytes")
    print("=" * 60)
    print("  The first byte after RID in HIDPipeData might be a pipe command.")
    print("  0x20 is START frag, but other values might be control commands.\n")

    # Try various first bytes on RID 3
    for cmd_byte in [0x00, 0x01, 0x02, 0x03, 0x04, 0x08, 0x10,
                     0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0, 0xFF]:
        pkt = bytearray(64)
        pkt[0] = 0x03  # RID 3
        pkt[1] = cmd_byte
        pkt[2] = 0x03  # length
        pkt[3] = 0x4F  # FWU
        pkt[4] = 0x00  # ENABLE_REQ
        pkt[5] = 0x01  # enable=1

        ok = timed_write(h, bytes(pkt))
        if ok:
            h.set_nonblocking(1)
            time.sleep(0.05)
            data = h.read(256)
            if data and data[0] != 0x05:  # Skip talk button noise
                print(f"    cmd=0x{cmd_byte:02X}: *** RX RID={data[0]} : {hexline(data)} ***")
            elif data:
                pass  # Skip RID 5 noise
            else:
                pass  # No response
            # Read one more time
            time.sleep(0.05)
            data = h.read(256)
            if data and data[0] != 0x05:
                print(f"    cmd=0x{cmd_byte:02X}: *** RX2 RID={data[0]} : {hexline(data)} ***")
        else:
            print(f"    cmd=0x{cmd_byte:02X}: BLOCKED")

        time.sleep(0.2)

    # Disable
    pkt = bytearray(64)
    pkt[0] = 0x03
    pkt[1] = 0x20
    pkt[2] = 0x03
    pkt[3] = 0x4F
    pkt[4] = 0x00
    pkt[5] = 0x00
    timed_write(h, bytes(pkt))

    print("\n  (Skipped RID 5 talk-button noise in output)")


def main():
    parser = argparse.ArgumentParser(description="FWU Sign-On Probe")
    parser.add_argument("--test", type=int, default=None)
    args = parser.parse_args()

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
        if args.test is None:
            # Run most promising tests
            test1_signon_fr15(h)
            test4_hidpipe_commands(h)
        elif args.test == 1:
            test1_signon_fr15(h)
        elif args.test == 2:
            test2_fwu_as_feature(h)
        elif args.test == 3:
            test3_signon_output(h)
        elif args.test == 4:
            test4_hidpipe_commands(h)

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        h.close()
        print("\nClosed.")


if __name__ == "__main__":
    import argparse
    main()
