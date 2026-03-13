#!/usr/bin/env python3
"""
FWU protocol test with CORRECT byte ordering from libDFUManager.dylib RE.

Key finding: CVM API primitives use little-endian 16-bit IDs.
  0x4F00 (ENABLE_REQ) → wire bytes [0x00, 0x4F] not [0x4F, 0x00]

Message format (from SendApiFwuEnableReq disassembly):
  ENABLE_REQ:  [0x00, 0x4F, enable, 0x07]  (4 bytes)
  UPDATE_REQ:  [0x03, 0x4F, devnr, mode]   (4 bytes)

HID framing (from TxFragments disassembly):
  First fragment: [ReportID] [0x20] [PayloadLen] [payload...] padded to reportSize
  Continuation:   [ReportID] [0x80] [data...]

Flow:
  1. Sign on: [0x0D, 0x15]
  2. ENABLE_REQ: [0x00, 0x4F, 0x01, 0x07]
  3. Wait for ENABLE_CFM (0x4F01) and DEVICE_NOTIFY_IND (0x4F02)
  4. If device present: UPDATE_REQ, then handle GET_BLOCK_IND/GET_CRC_IND
  5. ENABLE_REQ disable: [0x00, 0x4F, 0x00, 0x07]
  6. Sign off: [0x0D, 0x00]
"""

import sys
import time
import signal
import struct
import subprocess
import hid

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
TARGET_USAGE_PAGE = 0xFFA2
RID_DATA = 0x03

# CVM API Primitive IDs (little-endian 16-bit)
API_FWU_ENABLE_REQ = 0x4F00
API_FWU_ENABLE_CFM = 0x4F01
API_FWU_DEVICE_NOTIFY_IND = 0x4F02
API_FWU_UPDATE_REQ = 0x4F03
API_FWU_UPDATE_CFM = 0x4F04
API_FWU_UPDATE_IND = 0x4F05
API_FWU_UPDATE_RES = 0x4F06
API_FWU_GET_BLOCK_IND = 0x4F07
API_FWU_GET_BLOCK_RES = 0x4F08
API_FWU_GET_CRC_IND = 0x4F09
API_FWU_GET_CRC_RES = 0x4F0A
API_FWU_COMPLETE_IND = 0x4F0B
API_FWU_STATUS_IND = 0x4F0C
API_FWU_MULTI_CRC_IND = 0x4F0D
API_FWU_PLT_IND = 0x4F13
API_FWU_CRC32_IND = 0x4F14
API_FWU_PROGRESS_IND = 0x4F16

# FWS API
API_FWS_INIT_REQ = 0x4F81
API_FWS_INIT_CFM = 0x4F83
API_FWS_STATUS_IND = 0x4F84
API_FWS_INFO_IND = 0x4F85
API_FWS_WRITE_EXT_DATA_CFM = 0x4F89

# FWU mode strings
FWU_MODES = {
    0xFF: "Up-to-date",
    0x00: "FWU",
    0x01: "Main",
    0x10: "FWU,Aux",
    0x11: "Main,Aux",
}

PRIM_NAMES = {
    0x4F00: "ENABLE_REQ", 0x4F01: "ENABLE_CFM",
    0x4F02: "DEVICE_NOTIFY_IND", 0x4F03: "UPDATE_REQ",
    0x4F04: "UPDATE_CFM", 0x4F05: "UPDATE_IND",
    0x4F06: "UPDATE_RES", 0x4F07: "GET_BLOCK_IND",
    0x4F08: "GET_BLOCK_RES", 0x4F09: "GET_CRC_IND",
    0x4F0A: "GET_CRC_RES", 0x4F0B: "COMPLETE_IND",
    0x4F0C: "STATUS_IND", 0x4F0D: "MULTI_CRC_IND",
    0x4F13: "PLT_IND", 0x4F14: "CRC32_IND",
    0x4F16: "PROGRESS_IND",
    0x4F81: "FWS_INIT_REQ", 0x4F83: "FWS_INIT_CFM",
    0x4F84: "FWS_STATUS_IND", 0x4F85: "FWS_INFO_IND",
    0x4F89: "FWS_WRITE_EXT_DATA_CFM",
}


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


def build_fwu_msg(primitive_id, *params, report_size=64):
    """Build a properly framed FWU message with correct LE byte ordering.

    The CVM API mail format:
      bytes[0:2] = primitive ID (16-bit little-endian)
      bytes[2:]  = parameters

    HID framing:
      [ReportID=3] [0x20] [PayloadLen] [mail bytes...] [padding...]
    """
    # Build the CVM mail
    mail = struct.pack('<H', primitive_id) + bytes(params)

    # Frame it
    pkt = bytearray(report_size)
    pkt[0] = RID_DATA
    pkt[1] = 0x20  # FRAG_START
    pkt[2] = len(mail)
    pkt[3:3+len(mail)] = mail
    return bytes(pkt)


def decode_fwu_msg(data):
    """Decode a reassembled FWU message from RID 3 input data."""
    raw = bytes(data) if not isinstance(data, bytes) else data
    if len(raw) < 4 or raw[1] != 0x20:
        return None

    payload_len = raw[2]
    payload = raw[3:3+payload_len]

    if len(payload) < 2:
        return None

    prim_id = struct.unpack_from('<H', payload, 0)[0]
    params = payload[2:]
    name = PRIM_NAMES.get(prim_id, f"UNKNOWN_0x{prim_id:04X}")
    return prim_id, name, params


def listen(h, timeout_ms=5000, label=""):
    """Listen and decode ALL input reports."""
    h.set_nonblocking(1)
    elapsed = 0
    results = []
    while elapsed < timeout_ms:
        try:
            data = h.read(512)
        except OSError:
            print(f"    [{elapsed:5d}ms] (read error, device may be resetting)")
            break
        if data:
            rid = data[0]
            ts = f"[{elapsed:5d}ms]"

            if rid == RID_DATA:
                print(f"    {ts} *** RID 3 ({len(data)}B): {hexline(data[:20])}...")
                msg = decode_fwu_msg(data)
                if msg:
                    prim_id, name, params = msg
                    print(f"         → FWU {name} (0x{prim_id:04X}): {hexline(params)}")

                    # Decode specific messages
                    if prim_id == API_FWU_ENABLE_CFM and len(params) >= 1:
                        status = params[0]
                        status_str = {0: "SUCCESS", 1: "BUSY", 2: "NOT_SUPPORTED"}.get(status, f"0x{status:02X}")
                        print(f"           Status: {status_str}")

                    elif prim_id == API_FWU_DEVICE_NOTIFY_IND and len(params) >= 6:
                        present = params[0]
                        devnr = params[1]
                        dev_id = struct.unpack_from('<I', params, 2)[0] if len(params) >= 6 else 0
                        print(f"           Present={present} DeviceNr={devnr} ID=0x{dev_id:08X}")
                        if len(params) >= 14:
                            offset = struct.unpack_from('<I', params, 6)[0]
                            mode = params[10]
                            mode_str = FWU_MODES.get(mode, f"0x{mode:02X}")
                            link_date = params[11:16] if len(params) >= 16 else b''
                            print(f"           Offset=0x{offset:X} Mode={mode_str}")
                            if link_date:
                                print(f"           LinkDate: {hexline(link_date)}")

                    elif prim_id == API_FWU_STATUS_IND and len(params) >= 1:
                        busy = params[0]
                        print(f"           Busy={busy}")
                        if len(params) >= 5:
                            s0_code = params[1]
                            s1_code = params[3] if len(params) >= 4 else 0
                            print(f"           Dev0: status=0x{s0_code:02X}, Dev1: status=0x{s1_code:02X}")

                    elif prim_id == API_FWU_UPDATE_IND and len(params) >= 6:
                        devnr = params[0]
                        dev_id = struct.unpack_from('<I', params, 1)[0]
                        print(f"           DeviceNr={devnr} ID=0x{dev_id:08X}")

                    elif prim_id == API_FWU_GET_BLOCK_IND and len(params) >= 9:
                        devnr = params[0]
                        ctx = struct.unpack_from('<I', params, 1)[0]
                        addr = struct.unpack_from('<I', params, 5)[0]
                        size = struct.unpack_from('<I', params, 9)[0] if len(params) >= 13 else 0
                        print(f"           DevNr={devnr} Ctx=0x{ctx:X} Addr=0x{addr:X} Size=0x{size:X}")

                else:
                    print(f"         (raw payload: {hexline(data[1:min(20, len(data))])})")

            elif rid == 0x02:
                bits = data[1] if len(data) > 1 else 0
                print(f"    {ts} RID 2 (sign-on): {hexline(data)}  "
                      f"8F={bits&1} EE={(bits>>1)&1} 77={(bits>>2)&1} "
                      f"80={(bits>>3)&1} C1={(bits>>4)&1}")
            elif rid == 0x05:
                val = data[1] if len(data) > 1 else 0
                print(f"    {ts} RID 5 (DFUAck): {hexline(data)}  toggle={val}")
            elif rid == 0x0E:
                print(f"    {ts} RID 14 (settings {len(data)}B): {hexline(data)}")
            elif rid == 0xFE:
                print(f"    {ts} RID 254 ({len(data)}B): {hexline(data)}")
            else:
                print(f"    {ts} RID {rid} ({len(data)}B): {hexline(data[:20])}")

            results.append((rid, bytes(data)))
        time.sleep(0.005)
        elapsed += 5
    return results


def usb_reset():
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

    # USB reset
    print("USB reset...")
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

    # Drain
    h.set_nonblocking(1)
    while h.read(256):
        pass

    try:
        # ============================================================
        # STEP 1: Sign on
        # ============================================================
        print(f"\n{'='*60}")
        print("STEP 1: Sign On")
        print("=" * 60)

        pkt = bytes([0x0D, 0x15])
        print(f"  TX: [0D 15]")
        ok = timed_write(h, pkt)
        if not ok:
            print("  BLOCKED! Device may be wedged. Try USB reset.")
            return

        print("  Listening 3s...")
        results = listen(h, timeout_ms=3000)
        got_rid2 = any(rid == 2 for rid, _ in results)
        print(f"  Sign-on: {'OK' if got_rid2 else 'NO RESPONSE'}")

        # Drain
        time.sleep(0.5)
        h.set_nonblocking(1)
        while h.read(256):
            pass

        # ============================================================
        # STEP 2: FWU Enable (CORRECT byte ordering)
        # ============================================================
        print(f"\n{'='*60}")
        print("STEP 2: FWU_ENABLE_REQ (correct LE byte order)")
        print("=" * 60)

        # Old (wrong): [0x4F, 0x00, 0x01] → prim_id = 0x004F (garbage)
        # New (correct): struct.pack('<H', 0x4F00) + [0x01, 0x07]
        #                → [0x00, 0x4F, 0x01, 0x07] → prim_id = 0x4F00
        pkt = build_fwu_msg(API_FWU_ENABLE_REQ, 0x01, 0x07)
        print(f"  TX: {hexline(pkt[:8])}")
        print(f"  (payload: {hexline(pkt[3:7])} = ENABLE_REQ enable=1 ver=7)")

        ok = timed_write(h, pkt)
        if not ok:
            print("  BLOCKED!")
        else:
            print("  Accepted! Listening 20s for ENABLE_CFM + DEVICE_NOTIFY_IND...")
            results = listen(h, timeout_ms=20000)

            rid3_found = any(rid == 3 for rid, _ in results)
            rid5_found = any(rid == 5 for rid, _ in results)
            print(f"\n  Response: RID3={'YES ★★★' if rid3_found else 'no'} "
                  f"RID5={'yes' if rid5_found else 'no'}")

            if not rid3_found:
                # Try FWS_INIT_REQ too (SetMode sends both)
                print(f"\n  --- Also sending FWS_INIT_REQ (SetMode sends both) ---")
                pkt = build_fwu_msg(API_FWS_INIT_REQ, 0x01)
                print(f"  TX: {hexline(pkt[:7])}")
                ok2 = timed_write(h, pkt)
                if ok2:
                    listen(h, timeout_ms=5000)

        # ============================================================
        # STEP 3: STATUS_IND query (if we got ENABLE_CFM)
        # ============================================================
        print(f"\n{'='*60}")
        print("STEP 3: Additional queries")
        print("=" * 60)

        # Try sending UPDATE_REQ for device 0
        pkt = build_fwu_msg(API_FWU_UPDATE_REQ, 0x00, 0x00)
        print(f"  TX UPDATE_REQ(dev=0, mode=FWU): {hexline(pkt[:8])}")
        ok = timed_write(h, pkt)
        if ok:
            results = listen(h, timeout_ms=5000)
            if any(rid == 3 for rid, _ in results):
                print("  ★ Got RID 3 response!")

        time.sleep(0.3)

        # ============================================================
        # STEP 4: FWU Disable + Sign Off
        # ============================================================
        print(f"\n{'='*60}")
        print("STEP 4: Cleanup")
        print("=" * 60)

        # FWU disable
        pkt = build_fwu_msg(API_FWU_ENABLE_REQ, 0x00, 0x07)
        print(f"  TX ENABLE_REQ(disable): {hexline(pkt[:8])}")
        timed_write(h, pkt)
        listen(h, timeout_ms=2000)

        # Sign off
        pkt = bytes([0x0D, 0x00])
        print(f"  TX sign-off: [0D 00]")
        timed_write(h, pkt)
        listen(h, timeout_ms=1000)

        print("\n  Done!")

    except KeyboardInterrupt:
        print("\n\nInterrupted!")
    finally:
        try:
            pkt = build_fwu_msg(API_FWU_ENABLE_REQ, 0x00, 0x07)
            timed_write(h, pkt)
        except Exception:
            pass
        try:
            timed_write(h, bytes([0x0D, 0x00]))
        except Exception:
            pass
        try:
            h.close()
        except Exception:
            pass
        print("Closed.")


if __name__ == "__main__":
    main()
