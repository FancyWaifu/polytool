#!/usr/bin/env python3
"""
HIDPipe + FWU Probe — After sign-on, try various pipe commands.

We've confirmed:
  - RID 13 write → RID 2 sign-on response ✓
  - RID 14 settings arrive after sign-on ✓
  - FWU_ENABLE_REQ → DFUAck on RID 5 ✓
  - No RID 3 data ever ✗

Hypotheses to test:
  1. HIDPipe needs a "channel open" before FWU works
  2. Device needs a non-FWU command first (e.g., settings query)
  3. DEVICE_NOTIFY_IND only fires if headset is connected
  4. We need to send UPDATE_REQ to get RID 3 data
  5. The pipe uses a different message family (not 0x4F)
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


def build_pkt(rid, payload, report_size=64):
    """Build HID report with 0x20 framing."""
    pkt = bytearray(report_size)
    pkt[0] = rid
    pkt[1] = FRAG_START
    pkt[2] = len(payload)
    for i, b in enumerate(payload):
        if 3 + i < report_size:
            pkt[3 + i] = b
    return bytes(pkt)


def build_raw_pkt(rid, raw_bytes, report_size=64):
    """Build HID report without framing — raw payload."""
    pkt = bytearray(report_size)
    pkt[0] = rid
    for i, b in enumerate(raw_bytes):
        if 1 + i < report_size:
            pkt[1 + i] = b
    return bytes(pkt)


def listen(h, timeout_ms=5000, quiet_rids=None, label=""):
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
                      f"({len(data)}B): {hexline(data[:20])}"
                      f"{'...' if len(data) > 20 else ''}")
                if rid == RID_DATA:
                    print(f"             *** RID 3 RESPONSE! ***")
                    payload = data[1:]
                    if payload and payload[0] == FRAG_START and len(payload) >= 2:
                        plen = payload[1]
                        msg = payload[2:2+plen]
                        print(f"             → Decoded: len={plen} data={hexline(msg[:32])}")
            results.append((rid, bytes(data)))
        time.sleep(0.005)
        elapsed += 5
    return results


def send_and_listen(h, label, pkt, timeout_ms=3000, quiet_rids=None):
    """Send a packet and listen for responses."""
    if quiet_rids is None:
        quiet_rids = set()
    print(f"\n  >>> {label}")
    print(f"      TX: {hexline(pkt[:12])}{'...' if len(pkt) > 12 else ''}")
    ok = timed_write(h, pkt)
    if not ok:
        print(f"      BLOCKED/TIMEOUT")
        return []
    print(f"      Accepted")
    return listen(h, timeout_ms=timeout_ms, quiet_rids=quiet_rids)


def do_signon(h):
    """Sign on via RID 13 and wait for RID 2 + RID 14."""
    print("\n  --- Sign-on (RID 13 → RID 2) ---")
    pkt = bytes([0x0D, 0x15])
    ok = timed_write(h, pkt)
    if not ok:
        print("  Sign-on write FAILED!")
        return False
    results = listen(h, timeout_ms=3000)
    got_rid2 = any(rid == 2 for rid, _ in results)
    if got_rid2:
        print("  Sign-on OK ✓")
    else:
        print("  No RID 2 response!")
    return got_rid2


def main():
    # Kill Poly Lens
    print("Killing Poly Lens...")
    for proc in ["legacyhost", "LensService", "PolyLauncher"]:
        subprocess.run(["pkill", "-f", proc], capture_output=True)
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
        # Drain
        h.set_nonblocking(1)
        while h.read(256):
            pass

        # Sign on
        if not do_signon(h):
            print("Sign-on failed, continuing anyway...")

        # Drain sign-on reports
        time.sleep(0.5)
        h.set_nonblocking(1)
        while h.read(256):
            pass

        # ============================================================
        # TEST A: Known Poly protocol message families
        # ============================================================
        print(f"\n{'='*60}")
        print("TEST A: Non-FWU message families on HIDPipe")
        print("=" * 60)
        print("  Poly uses message families: 0x4F=FWU, 0x4E=FWS, etc.")
        print("  Try other families to see if pipe responds to anything.\n")

        msg_families = [
            # Query-like commands in various families
            (bytes([0x00, 0x00]), "Null msg (0x0000)"),
            (bytes([0x01, 0x00]), "Generic 0x0100"),
            (bytes([0x40, 0x00]), "Family 0x40"),
            (bytes([0x41, 0x00]), "Family 0x41"),
            (bytes([0x42, 0x00]), "Family 0x42"),
            (bytes([0x43, 0x00]), "Family 0x43"),
            (bytes([0x44, 0x00]), "Family 0x44"),
            (bytes([0x45, 0x00]), "Family 0x45"),
            (bytes([0x46, 0x00]), "Family 0x46"),
            (bytes([0x47, 0x00]), "Family 0x47"),
            (bytes([0x48, 0x00]), "Family 0x48"),
            (bytes([0x49, 0x00]), "Family 0x49"),
            (bytes([0x4A, 0x00]), "Family 0x4A"),
            (bytes([0x4B, 0x00]), "Family 0x4B"),
            (bytes([0x4C, 0x00]), "Family 0x4C"),
            (bytes([0x4D, 0x00]), "Family 0x4D"),
            (bytes([0x4E, 0x00]), "FWS (0x4E00)"),
            (bytes([0x4E, 0x01]), "FWS (0x4E01)"),
            (bytes([0x4E, 0x04]), "FWS status (0x4E04)"),
            (bytes([0x4F, 0x00, 0x01]), "FWU Enable (0x4F00)"),
            (bytes([0x50, 0x00]), "Family 0x50"),
            (bytes([0x80, 0x00]), "Family 0x80"),
            (bytes([0xFF, 0x00]), "Family 0xFF"),
        ]

        for payload, label in msg_families:
            pkt = build_pkt(RID_DATA, payload)
            results = send_and_listen(h, label, pkt, timeout_ms=500,
                                       quiet_rids={0x05})
            rid3 = [d for rid, d in results if rid == RID_DATA]
            if rid3:
                print(f"      *** GOT RID 3 RESPONSE TO {label}! ***")
                break
            time.sleep(0.1)

        # Drain
        h.set_nonblocking(1)
        while h.read(256):
            pass

        # ============================================================
        # TEST B: Raw (unframed) commands on RID 3
        # ============================================================
        print(f"\n{'='*60}")
        print("TEST B: Raw (unframed) commands on RID 3")
        print("=" * 60)
        print("  Maybe the device doesn't use 0x20 framing for all commands.\n")

        raw_attempts = [
            (bytes([0x4F, 0x00, 0x01]), "Raw FWU Enable"),
            (bytes([0x00]), "Raw 0x00"),
            (bytes([0x01]), "Raw 0x01"),
            (bytes([0x4E, 0x00]), "Raw FWS 0x4E00"),
            (bytes([0x20, 0x03, 0x4F, 0x00, 0x01]), "0x20-framed without RID"),
        ]

        for raw_payload, label in raw_attempts:
            pkt = build_raw_pkt(RID_DATA, raw_payload)
            results = send_and_listen(h, label, pkt, timeout_ms=500,
                                       quiet_rids={0x05})
            rid3 = [d for rid, d in results if rid == RID_DATA]
            if rid3:
                print(f"      *** GOT RID 3 RESPONSE! ***")
                break
            time.sleep(0.1)

        # Drain
        h.set_nonblocking(1)
        while h.read(256):
            pass

        # ============================================================
        # TEST C: FWU commands after fresh sign-on + enable
        # ============================================================
        print(f"\n{'='*60}")
        print("TEST C: FWU UPDATE_REQ and other commands after enable")
        print("=" * 60)
        print("  Maybe DEVICE_NOTIFY_IND only comes with UPDATE_REQ.\n")

        # Fresh sign-on
        do_signon(h)
        time.sleep(0.5)
        h.set_nonblocking(1)
        while h.read(256):
            pass

        # Enable FWU
        pkt = build_pkt(RID_DATA, bytes([0x4F, 0x00, 0x01]))
        send_and_listen(h, "FWU_ENABLE_REQ", pkt, timeout_ms=2000)

        # Drain
        h.set_nonblocking(1)
        while h.read(256):
            pass

        # Try UPDATE_REQ for device 0
        fwu_commands = [
            (bytes([0x4F, 0x03, 0x00, 0x00, 0x00, 0x00, 0x01]),
             "UPDATE_REQ dev=0 id=0x00000001"),
            (bytes([0x4F, 0x03, 0x00]),
             "UPDATE_REQ dev=0 (minimal)"),
            (bytes([0x4F, 0x0C]),
             "STATUS_IND query (0x4F0C)"),
            (bytes([0x4F, 0x12, 0x00]),
             "PLT_MSG (0x4F12)"),
            (bytes([0x4F, 0x02]),
             "DEVICE_NOTIFY_IND request? (0x4F02)"),
        ]

        for payload, label in fwu_commands:
            pkt = build_pkt(RID_DATA, payload)
            results = send_and_listen(h, label, pkt, timeout_ms=2000,
                                       quiet_rids={0x05})
            rid3 = [d for rid, d in results if rid == RID_DATA]
            if rid3:
                print(f"      *** FWU RID 3 RESPONSE! ***")
            time.sleep(0.3)

        # Disable FWU
        pkt = build_pkt(RID_DATA, bytes([0x4F, 0x00, 0x00]))
        timed_write(h, pkt)

        # Drain
        time.sleep(0.5)
        h.set_nonblocking(1)
        while h.read(256):
            pass

        # ============================================================
        # TEST D: RID 0x51 output (usage 0x31, 4 bytes)
        # ============================================================
        print(f"\n{'='*60}")
        print("TEST D: RID 0x51 (Usage 0x31) — companion pipe?")
        print("=" * 60)
        print("  Usage 0x31 is adjacent to 0x30 (HIDPipeData).")
        print("  Might be a command/control channel for the pipe.\n")

        rid51_attempts = [
            (bytes([0x51, 0x00, 0x00, 0x00, 0x01]), "RID 0x51: init/open"),
            (bytes([0x51, 0x01, 0x00, 0x00, 0x00]), "RID 0x51: cmd=1"),
            (bytes([0x51, 0xFF, 0xFF, 0xFF, 0xFF]), "RID 0x51: all FF"),
        ]

        for pkt_data, label in rid51_attempts:
            results = send_and_listen(h, label, pkt_data, timeout_ms=1000,
                                       quiet_rids={0x05})
            if results:
                for rid, data in results:
                    if rid not in (0x05,):
                        print(f"      *** RESPONSE RID {rid}! ***")
            time.sleep(0.2)

        # ============================================================
        # TEST E: RID 0xFD output (usage 0xFE, 1 byte)
        # ============================================================
        print(f"\n{'='*60}")
        print("TEST E: RID 0xFD (Usage 0xFE) — control report")
        print("=" * 60)

        for val in [0x00, 0x01, 0xFF]:
            pkt = bytes([0xFD, val])
            results = send_and_listen(h, f"RID 0xFD value=0x{val:02X}",
                                       pkt, timeout_ms=1000, quiet_rids={0x05})
            if any(rid not in (0x05,) for rid, _ in results):
                print(f"      *** NON-ACK RESPONSE! ***")
            time.sleep(0.2)

        # ============================================================
        # TEST F: Monitor legacyhost startup
        # ============================================================
        print(f"\n{'='*60}")
        print("TEST F: Restart legacyhost and monitor what it does")
        print("=" * 60)
        print("  Starting legacyhost in background, listening for 10s on HID...")

        # Close our handle first — legacyhost needs the device
        h.close()

        # Start legacyhost
        legacyhost_path = ("/Applications/Poly Studio.app/Contents/Helpers/"
                          "LegacyHostApp.app/Contents/MacOS/legacyhost")
        proc = subprocess.Popen([legacyhost_path],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        print(f"  Started legacyhost (PID {proc.pid})")

        # Wait for it to start, then open device and listen
        time.sleep(3)

        info = find_device()
        if info:
            h = hid.device()
            h.open_path(info["path"])
            print(f"  Opened device on {info['usage_page']:04X}:{info['usage']:04X}")

            print("  Listening 10s for ANY reports (legacyhost may trigger FWU)...")
            results = listen(h, timeout_ms=10000, quiet_rids=set())
            print(f"\n  Total reports received: {len(results)}")
            for rid, data in results:
                print(f"    RID {rid:3d}: {hexline(data[:20])}")
            h.close()
        else:
            print("  Could not reopen device!")

        # Check legacyhost log
        log_path = ("/Users/bryson.allen/Library/Application Support/"
                   "Plantronics/legacyhost/Poly/LegacyHostApp/Logs/"
                   "LegacyHostApp.log")
        try:
            # Get last 30 lines of log
            result = subprocess.run(["tail", "-30", log_path],
                                   capture_output=True, text=True)
            print(f"\n  --- Last 30 lines of legacyhost log ---")
            for line in result.stdout.strip().split('\n'):
                # Filter for interesting lines
                for keyword in ['Snd', 'Rcv', 'DFU', 'FWU', 'SignOn',
                               'signon', 'HidPipe', 'TxReport', 'RxReport',
                               'attach', 'open', 'DEVICE_REQUEST']:
                    if keyword.lower() in line.lower():
                        print(f"    {line.strip()}")
                        break
        except Exception:
            pass

        print("\nDone.")
        return  # Don't try to close h again

    except KeyboardInterrupt:
        print("\n\nInterrupted!")
        try:
            pkt = build_pkt(RID_DATA, bytes([0x4F, 0x00, 0x00]))
            timed_write(h, pkt)
        except Exception:
            pass
    finally:
        try:
            h.close()
        except Exception:
            pass
        print("\nClosed.")


if __name__ == "__main__":
    main()
