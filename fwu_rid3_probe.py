#!/usr/bin/env python3
"""
FWU API Probe via Report ID 3 (HIDPipeData) — Savi 8220 targeted.

From RE of Poly Lens macOS (libDFUManager.dylib disassembly):

  FRAMING PROTOCOL (FwuInternals::TxFragments / RxReport):
    First/only fragment: [ReportID] [0x20] [PayloadLen] [data...]
    Continuation:        [ReportID] [0x80] [data...]

  REPORT ID RESOLUTION (FwuApiDFU::prepare_for_update):
    DFUData: GetReportID(usagePage, 0x30) → Report ID 3 on Savi 8220
    DFUAck:  GetReportID(usagePage, 0x88) → secondary ack channel
    Size:    GetValueArraySize(usagePage, 0x30) + 1 → 64 bytes total

  FWU API MESSAGES (0x4Fxx):
    0x4F00 = API_FWU_ENABLE_REQ    (Host→Device)  Enable=%X
    0x4F01 = API_FWU_ENABLE_CFM    (Device→Host)  Status=%X
    0x4F02 = API_FWU_DEVICE_NOTIFY_IND (Device→Host) Present=%X DeviceNr=%X ID=%08X
    0x4F03 = API_FWU_UPDATE_REQ    (Host→Device)
    0x4F04 = API_FWU_UPDATE_CFM    (Device→Host)
    0x4F05 = API_FWU_UPDATE_IND    (Device→Host)
    0x4F06 = API_FWU_UPDATE_RES    (Host→Device)
    0x4F07 = API_FWU_GET_BLOCK_IND (Device→Host)
    0x4F08 = API_FWU_GET_BLOCK_RES (Host→Device)
    0x4F09 = API_FWU_GET_CRC_IND   (Device→Host)
    0x4F0A = API_FWU_GET_CRC_RES   (Host→Device)
    0x4F0B = API_FWU_COMPLETE_IND  (Device→Host)
    0x4F0C = API_FWU_STATUS_IND    (Device→Host)

Usage:
  python3 fwu_rid3_probe.py [--phase N]
    --phase 0: Passive listen + read feature reports (safe, read-only)
    --phase 1: Send FWU_ENABLE_REQ with correct 0x20 framing on RID 3
    --phase 2: Send FWU_ENABLE_REQ via alternative framings (fallback)
    --phase 3: Try via feature report ID 15
"""

import sys
import time
import signal
import argparse
import hid

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
TARGET_USAGE_PAGE = 0xFFA2

# FWU API fragmentation markers
FRAG_START = 0x20
FRAG_CONT = 0x80

# FWU API message codes
API_FWU_ENABLE_REQ = bytes([0x4F, 0x00])
API_FWU_ENABLE_CFM = bytes([0x4F, 0x01])
API_FWU_DEVICE_NOTIFY_IND = bytes([0x4F, 0x02])
API_FWU_STATUS_IND = bytes([0x4F, 0x0C])


def hexdump(data, prefix="    "):
    if not data:
        print(f"{prefix}(empty)")
        return
    for i in range(0, len(data), 16):
        hex_part = " ".join(f"{b:02X}" for b in data[i:i+16])
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data[i:i+16])
        print(f"{prefix}{i:04X}: {hex_part:<48s} {asc_part}")


def decode_fwu_response(data):
    """Try to decode an FWU API response from raw HID report data."""
    if not data or len(data) < 2:
        return

    # Check for fragmentation header
    if data[0] == FRAG_START and len(data) >= 3:
        payload_len = data[1]
        payload = data[2:2+payload_len]
        print(f"    Decoded: START fragment, payload_len={payload_len}")
        if len(payload) >= 2:
            msg_code = (payload[0] << 8) | payload[1]
            print(f"    Message code: 0x{msg_code:04X}")
            if payload[0] == 0x4F:
                names = {
                    0x01: "API_FWU_ENABLE_CFM",
                    0x02: "API_FWU_DEVICE_NOTIFY_IND",
                    0x04: "API_FWU_UPDATE_CFM",
                    0x05: "API_FWU_UPDATE_IND",
                    0x07: "API_FWU_GET_BLOCK_IND",
                    0x09: "API_FWU_GET_CRC_IND",
                    0x0B: "API_FWU_COMPLETE_IND",
                    0x0C: "API_FWU_STATUS_IND",
                    0x0D: "API_FWU_MULTI_CRC_IND",
                    0x11: "API_FWU_PROGRESS_IND",
                    0x13: "API_FWU_PLT_IND",
                }
                name = names.get(payload[1], f"Unknown 0x4F{payload[1]:02X}")
                print(f"    --> {name}")
                if len(payload) > 2:
                    print(f"    Params: {' '.join(f'{b:02X}' for b in payload[2:])}")
    elif data[0] == FRAG_CONT:
        print(f"    Decoded: CONTINUATION fragment")
        print(f"    Data: {' '.join(f'{b:02X}' for b in data[1:])}")
    else:
        # Might not have fragmentation header — check raw
        if len(data) >= 2 and data[0] == 0x4F:
            msg_code = (data[0] << 8) | data[1]
            names = {0x4F01: "ENABLE_CFM", 0x4F02: "DEVICE_NOTIFY_IND",
                     0x4F0C: "STATUS_IND"}
            name = names.get(msg_code, f"0x{msg_code:04X}")
            print(f"    Raw FWU message? {name}")


def find_ffa2_device():
    """Find Poly device on FFA2 usage page."""
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS and d["usage_page"] == TARGET_USAGE_PAGE:
            return d
    return None


def timed_write(h, data, label, timeout=3):
    """Write with SIGALRM timeout protection."""
    timed_out = [False]
    def handler(signum, frame):
        timed_out[0] = True
        raise TimeoutError("Write timed out")

    old_handler = signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout)
    try:
        h.write(data)
        signal.alarm(0)
        return True
    except TimeoutError:
        print(f"    TIMEOUT — write blocked ({label})")
        return False
    except Exception as e:
        signal.alarm(0)
        print(f"    Error ({label}): {e}")
        return False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def timed_feature_write(h, data, label, timeout=3):
    """send_feature_report with SIGALRM timeout protection."""
    timed_out = [False]
    def handler(signum, frame):
        timed_out[0] = True
        raise TimeoutError("Write timed out")

    old_handler = signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout)
    try:
        h.send_feature_report(data)
        signal.alarm(0)
        return True
    except TimeoutError:
        print(f"    TIMEOUT — feature write blocked ({label})")
        return False
    except Exception as e:
        signal.alarm(0)
        print(f"    Error ({label}): {e}")
        return False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def read_responses(h, timeout_ms=3000, label=""):
    """Read all available input reports within timeout."""
    h.set_nonblocking(1)
    elapsed = 0
    responses = []
    while elapsed < timeout_ms:
        data = h.read(256)
        if data:
            print(f"  <<< [{elapsed}ms] {label} ({len(data)} bytes):")
            hexdump(data, "      ")
            decode_fwu_response(data)
            responses.append(bytes(data))
        time.sleep(0.01)
        elapsed += 10
    return responses


def build_fwu_packet(report_id, fwu_payload, report_size=64):
    """Build an FWU API HID report with 0x20 START framing.

    Format: [ReportID] [0x20] [PayloadLen] [payload...] [zero padding]
    Total size = report_size bytes.
    """
    pkt = bytearray(report_size)
    pkt[0] = report_id
    pkt[1] = FRAG_START  # 0x20 = start of message
    pkt[2] = len(fwu_payload)
    for i, b in enumerate(fwu_payload):
        if 3 + i < report_size:
            pkt[3 + i] = b
    return bytes(pkt)


def phase0_passive(h, info):
    """Read-only phase: feature reports + passive listen."""
    print("\n" + "=" * 60)
    print("PHASE 0: Read-Only Scan")
    print("=" * 60)

    # Read all feature reports
    print("\n  Feature reports:")
    for rid in range(256):
        try:
            data = h.get_feature_report(rid, 512)
            if data and len(data) > 0:
                nonzero = any(b != 0 for b in data)
                marker = " ***" if nonzero else ""
                print(f"    RID {rid:3d} (0x{rid:02X}) [{len(data):3d} bytes]{marker}: "
                      f"{' '.join(f'{b:02X}' for b in data[:32])}"
                      f"{' ...' if len(data) > 32 else ''}")
        except Exception:
            pass

    # Passive listen
    print(f"\n  Passive listen (3s)...")
    h.set_nonblocking(1)
    count = 0
    start = time.time()
    while time.time() - start < 3:
        data = h.read(256)
        if data:
            count += 1
            elapsed = int((time.time() - start) * 1000)
            print(f"    [{elapsed}ms] Input #{count} ({len(data)} bytes):")
            hexdump(data, "      ")
            decode_fwu_response(data)
        time.sleep(0.01)
    if count == 0:
        print("    No unsolicited input reports.")


def phase1_correct_framing(h):
    """Send FWU_ENABLE_REQ with the correct 0x20 framing on Report ID 3."""
    print("\n" + "=" * 60)
    print("PHASE 1: FWU_ENABLE_REQ with 0x20 Framing on RID 3")
    print("=" * 60)
    print("  Protocol: [RID=3] [0x20=START] [len] [0x4F] [0x00] [Enable=1]")

    # Build FWU_ENABLE_REQ: message code 0x4F00 + Enable=1
    fwu_payload = bytes([0x4F, 0x00, 0x01])  # FWU_ENABLE_REQ, enable=1
    pkt = build_fwu_packet(0x03, fwu_payload, report_size=64)

    print(f"\n  >>> Sending FWU_ENABLE_REQ (Enable=1)")
    print(f"      Packet ({len(pkt)} bytes):")
    hexdump(pkt[:16], "      ")

    ok = timed_write(h, pkt, "FWU_ENABLE_REQ RID3")
    if ok:
        print("      Write accepted!")
        responses = read_responses(h, timeout_ms=5000, label="FWU_ENABLE_CFM")
        if responses:
            print("\n  *** GOT RESPONSE TO FWU_ENABLE_REQ! ***")
            return True
        else:
            print("      No response within 5 seconds.")

            # Try reading feature report 15 to see if state changed
            try:
                fr15 = h.get_feature_report(15, 64)
                print(f"      FR15 after: {' '.join(f'{b:02X}' for b in fr15)}")
            except Exception:
                pass

    # Also try with Enable=0 (disable) which should be safer
    print(f"\n  >>> Sending FWU_ENABLE_REQ (Enable=0) — disable/query")
    fwu_payload_off = bytes([0x4F, 0x00, 0x00])  # FWU_ENABLE_REQ, enable=0
    pkt_off = build_fwu_packet(0x03, fwu_payload_off, report_size=64)
    hexdump(pkt_off[:16], "      ")

    ok = timed_write(h, pkt_off, "FWU_ENABLE_REQ disable")
    if ok:
        print("      Write accepted!")
        responses = read_responses(h, timeout_ms=3000, label="FWU_ENABLE_CFM (disable)")
        if responses:
            print("\n  *** GOT RESPONSE! ***")
            return True

    return False


def phase2_alt_framings(h):
    """Try alternative framings in case the 0x20 framing needs adjustment."""
    print("\n" + "=" * 60)
    print("PHASE 2: Alternative Framings on RID 3")
    print("=" * 60)

    fwu_enable = bytes([0x4F, 0x00, 0x01])  # FWU_ENABLE_REQ enable=1

    alternatives = [
        # Maybe report size is 63 not 64 (report ID not counted in size)
        ("63-byte report",
         build_fwu_packet(0x03, fwu_enable, report_size=63)),

        # Maybe the payload length byte counts differently
        ("len=5 (includes msg code + overhead)",
         bytearray([0x03, 0x20, 0x05, 0x4F, 0x00, 0x01]) + bytearray(58)),

        # Maybe 0x20 is not the start marker and it's just a raw payload
        ("raw [4F 00 01] no framing",
         bytearray([0x03, 0x4F, 0x00, 0x01]) + bytearray(60)),

        # Maybe the report ID for DFUData is different (try 88 = 0x58, the FF58 pipe)
        ("RID=0x58 + 0x20 framing",
         build_fwu_packet(0x58, fwu_enable, report_size=64)),

        # Try with report ID 0 (default if no explicit report IDs)
        ("RID=0x00 + 0x20 framing",
         build_fwu_packet(0x00, fwu_enable, report_size=64)),

        # Maybe length is 2-byte little-endian
        ("2-byte LE length [03 00]",
         bytearray([0x03, 0x20, 0x03, 0x00, 0x4F, 0x00, 0x01]) + bytearray(57)),
    ]

    for label, pkt in alternatives:
        print(f"\n  >>> Trying: {label}")
        print(f"      First 10 bytes: {' '.join(f'{b:02X}' for b in pkt[:10])}")

        ok = timed_write(h, bytes(pkt), label)
        if ok:
            print("      Write accepted!")
            responses = read_responses(h, timeout_ms=2000, label=label)
            if responses:
                print(f"      *** GOT RESPONSE! ***")
                return True
            else:
                print("      No response.")
        time.sleep(0.3)

    return False


def phase3_feature_rid15(h):
    """Try FWU_ENABLE_REQ via feature report ID 15."""
    print("\n" + "=" * 60)
    print("PHASE 3: FWU API via Feature Report ID 15")
    print("=" * 60)

    # Feature report 15 is 5 bytes: [0F xx xx xx xx]
    try:
        current = h.get_feature_report(15, 64)
        print(f"  Current FR15: {' '.join(f'{b:02X}' for b in current)}")
    except Exception as e:
        print(f"  Can't read FR15: {e}")
        return False

    framings = [
        # With 0x20 framing inside feature report
        ("FR15 with 0x20 framing: [0F 20 03 4F 00]",
         bytes([0x0F, 0x20, 0x03, 0x4F, 0x00])),

        # Raw FWU command in feature report
        ("FR15 raw: [0F 4F 00 01 00]",
         bytes([0x0F, 0x4F, 0x00, 0x01, 0x00])),

        # Usage 0x88 (DFUAck) might map to feature report 15
        ("FR15 as DFUAck: [0F 20 03 4F 00 01]",
         bytes([0x0F, 0x20, 0x03, 0x4F, 0x00, 0x01])),
    ]

    for label, pkt in framings:
        print(f"\n  >>> Trying {label}")
        ok = timed_feature_write(h, pkt, label)
        if ok:
            print("      Feature write accepted!")
            time.sleep(0.3)
            try:
                resp = h.get_feature_report(15, 64)
                print(f"      FR15 after: {' '.join(f'{b:02X}' for b in resp)}")
                if list(resp) != list(current):
                    print("      *** VALUE CHANGED! ***")
            except Exception:
                pass

            responses = read_responses(h, timeout_ms=2000, label=label)
            if responses:
                print(f"      *** GOT INPUT RESPONSE! ***")
                return True

        time.sleep(0.3)

    return False


def main():
    parser = argparse.ArgumentParser(description="FWU API Probe (0x20 framing)")
    parser.add_argument("--phase", type=int, default=None,
                        help="Run specific phase (0-3). Default: run all.")
    args = parser.parse_args()

    info = find_ffa2_device()
    if not info:
        print("No Poly device found on FFA2 usage page.")
        print("\nAll Poly HID interfaces:")
        for d in hid.enumerate():
            if d["vendor_id"] in POLY_VIDS:
                print(f"  VID:0x{d['vendor_id']:04X} PID:0x{d['product_id']:04X} "
                      f"Usage:0x{d['usage_page']:04X}:0x{d['usage']:04X} "
                      f"IF:{d['interface_number']} "
                      f"Product:{d['product_string']}")
        sys.exit(1)

    print(f"Device: {info['product_string']}")
    print(f"  VID:PID:  0x{info['vendor_id']:04X}:0x{info['product_id']:04X}")
    print(f"  Usage:    0x{info['usage_page']:04X}:0x{info['usage']:04X}")
    print(f"  Serial:   {info['serial_number']}")
    print(f"  Path:     {info['path']}")

    h = hid.device()
    h.open_path(info["path"])
    print("  Opened successfully.\n")

    try:
        if args.phase is None or args.phase == 0:
            phase0_passive(h, info)
        if args.phase is None or args.phase == 1:
            phase1_correct_framing(h)
        if args.phase is None or args.phase == 2:
            phase2_alt_framings(h)
        if args.phase is None or args.phase == 3:
            phase3_feature_rid15(h)

        print("\n" + "=" * 60)
        print("PROBE COMPLETE")
        print("=" * 60)
        print("""
If no responses were received, possible reasons:
1. Poly Lens may be holding the HID device exclusively
   → Quit Poly Lens first, then retry
2. The base station (not headset) handles FWU
   → The headset PID 0xACFF is a child device;
     FWU goes through the base (AC20/AC29/AC26/AC31/AC39)
3. macOS IOKit may not deliver reports to two processes
   → Only one process can receive input reports
""")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        h.close()
        print("Device closed.")


if __name__ == "__main__":
    main()
