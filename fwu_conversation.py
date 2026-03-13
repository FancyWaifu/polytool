#!/usr/bin/env python3
"""
FWU API Conversation — Full protocol handshake with Savi 8220.

Now that we know the framing works:
  TX (Host→Device): [RID=3] [0x20] [PayloadLen] [0x4F] [CmdLo] [params...] [pad]
  RX Ack (Device→Host): Report ID 5 (DFUAck usage 0x88)
  RX Data (Device→Host): Report ID 3 with 0x20/0x80 framing

Protocol flow:
  1. FWU_ENABLE_REQ (0x4F00, Enable=1) → FWU_ENABLE_CFM on RID 5
  2. Wait for DEVICE_NOTIFY_IND (0x4F02) on RID 3 — lists updatable devices
  3. FWU_STATUS_IND (0x4F0C) may arrive — reports current state
  4. FWU_ENABLE_REQ (0x4F00, Enable=0) to cleanly disable

Usage:
  python3 fwu_conversation.py [--disable-only] [--listen-time N]
"""

import sys
import time
import signal
import argparse
import hid

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
TARGET_USAGE_PAGE = 0xFFA2

# Framing
FRAG_START = 0x20
FRAG_CONT = 0x80

# Report IDs (resolved from HID descriptor on Savi 8220)
RID_DFU_DATA = 0x03   # Usage 0x30 (HIDPipeData) — for sending commands
RID_DFU_ACK = 0x05    # Usage 0x88 (DFUAck) — for receiving confirmations
RID_SETTINGS = 0x0E   # Usage 0x85/0xB4 — device settings
RID_FEATURE = 0x0F    # Usage 0x80/0xC1 — feature report

# FWU API message names
FWU_MSG_NAMES = {
    0x00: "API_FWU_ENABLE_REQ",
    0x01: "API_FWU_ENABLE_CFM",
    0x02: "API_FWU_DEVICE_NOTIFY_IND",
    0x03: "API_FWU_UPDATE_REQ",
    0x04: "API_FWU_UPDATE_CFM",
    0x05: "API_FWU_UPDATE_IND",
    0x06: "API_FWU_UPDATE_RES",
    0x07: "API_FWU_GET_BLOCK_IND",
    0x08: "API_FWU_GET_BLOCK_RES",
    0x09: "API_FWU_GET_CRC_IND",
    0x0A: "API_FWU_GET_CRC_RES",
    0x0B: "API_FWU_COMPLETE_IND",
    0x0C: "API_FWU_STATUS_IND",
    0x0D: "API_FWU_MULTI_CRC_IND",
    0x0E: "API_FWU_MULTI_CRC_RES",
    0x0F: "API_FWU_CRC32_IND",
    0x10: "API_FWU_CRC32_RES",
    0x11: "API_FWU_PROGRESS_IND",
    0x12: "API_FWU_PLT_MSG",
    0x13: "API_FWU_PLT_IND",
}


def hexdump(data, prefix="    "):
    if not data:
        print(f"{prefix}(empty)")
        return
    for i in range(0, len(data), 16):
        hex_part = " ".join(f"{b:02X}" for b in data[i:i+16])
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data[i:i+16])
        print(f"{prefix}{i:04X}: {hex_part:<48s} {asc_part}")


def decode_report(data):
    """Decode a received HID report."""
    if not data or len(data) < 1:
        return

    rid = data[0]
    payload = data[1:]

    if rid == RID_DFU_ACK:
        # DFUAck report — short acknowledgment
        print(f"    [DFUAck RID={rid}] payload: {' '.join(f'{b:02X}' for b in payload)}")
        if len(payload) >= 1:
            status = payload[0]
            print(f"    Status/Value: 0x{status:02X} ({status})")
            # Try to interpret as FWU response
            if status <= 0x13:
                name = FWU_MSG_NAMES.get(status, f"Unknown 0x{status:02X}")
                print(f"    Possible FWU msg code: {name}")
        return "ack", payload

    elif rid == RID_DFU_DATA:
        # DFUData report — check for 0x20/0x80 framing
        if len(payload) >= 1:
            if payload[0] == FRAG_START:
                if len(payload) >= 2:
                    plen = payload[1]
                    msg = payload[2:2+plen]
                    print(f"    [DFUData RID={rid}] START fragment, payload_len={plen}")
                    if len(msg) >= 2 and msg[0] == 0x4F:
                        name = FWU_MSG_NAMES.get(msg[1], f"Unknown 0x4F{msg[1]:02X}")
                        print(f"    FWU Message: {name} (0x4F{msg[1]:02X})")
                        decode_fwu_message(msg)
                    else:
                        print(f"    Raw payload: {' '.join(f'{b:02X}' for b in msg)}")
                    return "data_start", msg
            elif payload[0] == FRAG_CONT:
                print(f"    [DFUData RID={rid}] CONTINUATION fragment")
                print(f"    Data: {' '.join(f'{b:02X}' for b in payload[1:])}")
                return "data_cont", payload[1:]
            else:
                print(f"    [DFUData RID={rid}] Unknown framing byte: 0x{payload[0]:02X}")
                print(f"    Raw: {' '.join(f'{b:02X}' for b in payload)}")
                return "data_raw", payload

    elif rid == RID_SETTINGS:
        print(f"    [Settings RID={rid}] {' '.join(f'{b:02X}' for b in payload)}")
        return "settings", payload

    else:
        print(f"    [RID={rid} (0x{rid:02X})] {' '.join(f'{b:02X}' for b in payload)}")
        return "other", payload


def decode_fwu_message(msg):
    """Decode a complete FWU API message payload."""
    if len(msg) < 2 or msg[0] != 0x4F:
        return

    cmd = msg[1]
    params = msg[2:]

    if cmd == 0x01:  # ENABLE_CFM
        if len(params) >= 1:
            status = params[0]
            status_names = {0: "Success", 1: "Busy", 2: "Error"}
            print(f"    ENABLE_CFM: Status=0x{status:02X} ({status_names.get(status, '?')})")

    elif cmd == 0x02:  # DEVICE_NOTIFY_IND
        if len(params) >= 7:
            present = params[0]
            device_nr = params[1]
            device_id = (params[2] << 24) | (params[3] << 16) | (params[4] << 8) | params[5]
            print(f"    DEVICE_NOTIFY_IND: Present={present} DeviceNr={device_nr} "
                  f"ID=0x{device_id:08X}")
            if len(params) >= 8:
                offset = params[6]
                print(f"    Offset={offset}")
            if len(params) >= 9:
                mode = params[7]
                mode_names = {0: "Normal", 1: "Forced"}
                print(f"    Mode={mode} ({mode_names.get(mode, '?')})")
            # Extended fields: LinkDate, Range, Aux, Name
            if len(params) >= 14:
                link_date = params[8:13]
                print(f"    LinkDate: {'.'.join(f'{b:02X}' for b in link_date)}")
            if len(params) >= 16:
                range_start = params[13]
                range_end = params[14]
                print(f"    Range: {range_start}:{range_end}")

    elif cmd == 0x04:  # UPDATE_CFM
        if len(params) >= 2:
            device_nr = params[0]
            status = params[1]
            print(f"    UPDATE_CFM: DeviceNr={device_nr} Status=0x{status:02X}")

    elif cmd == 0x05:  # UPDATE_IND
        if len(params) >= 5:
            device_nr = params[0]
            device_id = (params[1] << 24) | (params[2] << 16) | (params[3] << 8) | params[4]
            print(f"    UPDATE_IND: DeviceNr={device_nr} ID=0x{device_id:08X}")

    elif cmd == 0x07:  # GET_BLOCK_IND
        if len(params) >= 9:
            device_nr = params[0]
            ctx = params[1]
            addr = (params[2] << 24) | (params[3] << 16) | (params[4] << 8) | params[5]
            size = (params[6] << 24) | (params[7] << 16) | (params[8] << 8) | (params[9] if len(params) > 9 else 0)
            print(f"    GET_BLOCK_IND: DeviceNr={device_nr} Ctx={ctx} "
                  f"Addr=0x{addr:08X} Size={size}")

    elif cmd == 0x0B:  # COMPLETE_IND
        if len(params) >= 1:
            ctx = params[0]
            print(f"    COMPLETE_IND: Ctx={ctx}")

    elif cmd == 0x0C:  # STATUS_IND
        if len(params) >= 1:
            busy = params[0]
            print(f"    STATUS_IND: Busy={busy}")
            if len(params) >= 5:
                print(f"    Details: [{params[1]:02X}]={params[2]:02X} [{params[3]:02X}]={params[4]:02X}")

    else:
        print(f"    Params ({len(params)} bytes): {' '.join(f'{b:02X}' for b in params[:32])}")


def find_ffa2_device():
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS and d["usage_page"] == TARGET_USAGE_PAGE:
            return d
    return None


def timed_write(h, data, label, timeout=3):
    timed_out = [False]
    def handler(signum, frame):
        timed_out[0] = True
        raise TimeoutError()
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


def build_fwu_packet(fwu_payload, report_size=64):
    """Build FWU API HID report: [RID=3] [0x20] [len] [payload] [pad]"""
    pkt = bytearray(report_size)
    pkt[0] = RID_DFU_DATA
    pkt[1] = FRAG_START
    pkt[2] = len(fwu_payload)
    for i, b in enumerate(fwu_payload):
        if 3 + i < report_size:
            pkt[3 + i] = b
    return bytes(pkt)


def send_fwu(h, fwu_payload, label=""):
    """Send an FWU API message and return True if write succeeded."""
    pkt = build_fwu_packet(fwu_payload)
    print(f"\n  >>> {label}")
    print(f"      TX: {' '.join(f'{b:02X}' for b in pkt[:3+len(fwu_payload)])}")
    return timed_write(h, pkt, label)


def listen(h, timeout_ms=5000, label=""):
    """Listen for input reports and decode them."""
    h.set_nonblocking(1)
    elapsed = 0
    responses = []
    while elapsed < timeout_ms:
        data = h.read(256)
        if data:
            print(f"\n  <<< [{elapsed}ms] Response ({len(data)} bytes):")
            hexdump(data, "      ")
            result = decode_report(data)
            responses.append((data, result))
        time.sleep(0.01)
        elapsed += 10
    if not responses:
        print(f"      (no response within {timeout_ms}ms)")
    return responses


def read_feature_report(h, rid):
    """Read and display a feature report."""
    try:
        data = h.get_feature_report(rid, 64)
        print(f"  FR{rid}: {' '.join(f'{b:02X}' for b in data)}")
        return data
    except Exception as e:
        print(f"  FR{rid}: Error: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="FWU API Conversation")
    parser.add_argument("--disable-only", action="store_true",
                        help="Only send FWU_ENABLE disable (cleanup)")
    parser.add_argument("--listen-time", type=int, default=10,
                        help="Seconds to listen after enable (default: 10)")
    parser.add_argument("--status-only", action="store_true",
                        help="Only read device status (feature reports)")
    args = parser.parse_args()

    info = find_ffa2_device()
    if not info:
        print("No Poly device found on FFA2 usage page.")
        for d in hid.enumerate():
            if d["vendor_id"] in POLY_VIDS:
                print(f"  VID:0x{d['vendor_id']:04X} PID:0x{d['product_id']:04X} "
                      f"Usage:0x{d['usage_page']:04X}:0x{d['usage']:04X} "
                      f"Product:{d['product_string']}")
        sys.exit(1)

    print(f"Device: {info['product_string']}")
    print(f"  VID:PID:  0x{info['vendor_id']:04X}:0x{info['product_id']:04X}")
    print(f"  Usage:    0x{info['usage_page']:04X}:0x{info['usage']:04X}")

    h = hid.device()
    h.open_path(info["path"])
    print("  Opened.\n")

    try:
        # Always read current state first
        print("=== Current Device State ===")
        read_feature_report(h, 15)

        if args.status_only:
            return

        if args.disable_only:
            print("\n=== Sending FWU_ENABLE_REQ (Enable=0) — Disable ===")
            send_fwu(h, bytes([0x4F, 0x00, 0x00]), "FWU_ENABLE_REQ (disable)")
            listen(h, timeout_ms=3000, label="disable response")
            print("\n=== After Disable ===")
            read_feature_report(h, 15)
            return

        # Step 1: Enable FWU
        print("=" * 60)
        print("STEP 1: FWU_ENABLE_REQ (Enable=1)")
        print("=" * 60)
        ok = send_fwu(h, bytes([0x4F, 0x00, 0x01]), "FWU_ENABLE_REQ (enable=1)")
        if not ok:
            print("  Write failed!")
            return

        # Listen for CFM and any DEVICE_NOTIFY_IND
        print(f"\n  Listening for {args.listen_time}s...")
        print("  Expecting: FWU_ENABLE_CFM (RID 5), then DEVICE_NOTIFY_IND (RID 3)")
        responses = listen(h, timeout_ms=args.listen_time * 1000)

        print(f"\n  Total responses: {len(responses)}")

        # Check feature report state
        print("\n=== Device State After Enable ===")
        read_feature_report(h, 15)

        # Step 2: Disable FWU (cleanup)
        print("\n" + "=" * 60)
        print("STEP 2: FWU_ENABLE_REQ (Enable=0) — Cleanup")
        print("=" * 60)
        send_fwu(h, bytes([0x4F, 0x00, 0x00]), "FWU_ENABLE_REQ (disable)")
        listen(h, timeout_ms=3000)

        print("\n=== Device State After Disable ===")
        read_feature_report(h, 15)

    except KeyboardInterrupt:
        print("\n\nInterrupted! Sending FWU disable for safety...")
        try:
            send_fwu(h, bytes([0x4F, 0x00, 0x00]), "FWU_ENABLE_REQ (disable)")
            time.sleep(0.5)
        except Exception:
            pass
    finally:
        h.close()
        print("\nDevice closed.")


if __name__ == "__main__":
    main()
