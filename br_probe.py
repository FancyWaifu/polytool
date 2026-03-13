#!/usr/bin/env python3
"""
BladeRunner Protocol Probe — Safe HID exploration tool.

Sends individual BladeRunner commands to a Poly device and logs
raw hex responses. Used to map the exact wire protocol.

Usage: python3 br_probe.py [--step N]
  --step 0: Just open device and read any unsolicited reports (passive listen)
  --step 1: Send protocol handshake only
  --step 2: Send GetSetting (query firmware version)
  --step 3: Send GetSetting (query battery)
"""

import sys
import time
import struct
import argparse
import hid

# ── Config ───────────────────────────────────────────────────────────────────

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
TARGET_USAGE_PAGE = 0xFFA2  # Poly vendor command channel


def hexdump(data, prefix="  "):
    """Pretty hex dump of bytes."""
    if not data:
        print(f"{prefix}(empty)")
        return
    line = " ".join(f"{b:02X}" for b in data)
    ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    # Print in 16-byte rows
    for i in range(0, len(data), 16):
        hex_part = " ".join(f"{b:02X}" for b in data[i:i+16])
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data[i:i+16])
        print(f"{prefix}{i:04X}: {hex_part:<48s} {asc_part}")


def find_device():
    """Find Poly device on FFA2 usage page."""
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS and d["usage_page"] == TARGET_USAGE_PAGE:
            return d
    return None


def open_device(info):
    """Open HID device."""
    h = hid.device()
    h.open_path(info["path"])
    return h


def send_and_recv(h, data, label="", timeout_ms=3000):
    """Send data, print it, then read and print response."""
    print(f"\n{'='*60}")
    print(f">>> SEND {label}")
    hexdump(data)
    h.write(data)

    print(f"\n<<< RECV (timeout {timeout_ms}ms)")
    h.set_nonblocking(1)
    elapsed = 0
    responses = []
    while elapsed < timeout_ms:
        resp = h.read(256)
        if resp:
            print(f"  [{elapsed}ms] Got {len(resp)} bytes:")
            hexdump(resp, "    ")
            responses.append(bytes(resp))
            # Keep reading for a bit in case there are multiple responses
            elapsed = max(0, elapsed - 500)
        time.sleep(0.01)
        elapsed += 10
    if not responses:
        print("  (no response)")
    return responses


def build_packet(report_id, payload):
    """Build a raw HID output report. Pad to 64 bytes."""
    pkt = bytearray(64)
    pkt[0] = report_id
    for i, b in enumerate(payload):
        if 1 + i < 64:
            pkt[1 + i] = b
    return bytes(pkt)


# ── Probe Steps ──────────────────────────────────────────────────────────────

def step0_passive_listen(h):
    """Just listen for any unsolicited reports from the device."""
    print("\n[Step 0] Passive listen — reading for 5 seconds...")
    h.set_nonblocking(1)
    count = 0
    start = time.time()
    while time.time() - start < 5:
        data = h.read(256)
        if data:
            count += 1
            elapsed = int((time.time() - start) * 1000)
            print(f"\n  [{elapsed}ms] Report #{count} ({len(data)} bytes):")
            hexdump(data, "    ")
        time.sleep(0.01)
    print(f"\n  Total unsolicited reports: {count}")


def step1_handshake(h):
    """Send BladeRunner protocol version handshake.

    H->D type 0 (HostProtocolVersion).
    Expect D->H type 8 (ProtocolVersion) or type 11 (ProtocolRejection).

    We try several possible packet formats since we're not sure of the
    exact layout yet.
    """
    print("\n[Step 1] Protocol Handshake")
    print("  Trying format A: [ReportID=0x00] [MsgType=0x00] [ID=0x0000] [PayloadLen=0x0001] [Version=0x03]")

    # Format A: report_id=0, then BR header
    pkt_a = build_packet(0x00, [
        0x00,        # MsgType = HostProtocolVersion
        0x00, 0x00,  # ID = 0
        0x00, 0x01,  # Payload length = 1
        0x03,        # Protocol version 3
    ])
    responses = send_and_recv(h, pkt_a, "Handshake format A", timeout_ms=3000)

    if responses:
        print("\n  >>> Got response to format A! Analyzing...")
        for resp in responses:
            if len(resp) >= 5:
                print(f"  Byte[0] = 0x{resp[0]:02X} (possible MsgType)")
                print(f"  Byte[1] = 0x{resp[1]:02X}")
                print(f"  Byte[2] = 0x{resp[2]:02X}")
                print(f"  Byte[3] = 0x{resp[3]:02X}")
                print(f"  Byte[4] = 0x{resp[4]:02X}")
                if resp[0] == 0x08:
                    print("  --> This looks like ProtocolVersion (type 8)!")
                elif resp[0] == 0x0B:
                    print("  --> This looks like ProtocolRejection (type 11)!")
        return responses

    # Format B: report_id=0x01, then BR header
    print("\n  No response to format A. Trying format B: report_id=0x01")
    pkt_b = build_packet(0x01, [
        0x00,        # MsgType
        0x00, 0x00,  # ID
        0x00, 0x01,  # Payload length
        0x03,        # Version
    ])
    responses = send_and_recv(h, pkt_b, "Handshake format B", timeout_ms=3000)

    if responses:
        print("\n  >>> Got response to format B!")
        return responses

    # Format C: Maybe the FFA2 channel uses a different framing
    # Try sending just raw bytes without report ID prefix
    print("\n  No response to format B. Trying format C: raw BR packet (no report ID)")
    raw = bytes([
        0x00,        # MsgType
        0x00, 0x00,  # ID
        0x00, 0x01,  # Payload length
        0x03,        # Version
    ]) + bytes(58)
    responses = send_and_recv(h, raw, "Handshake format C", timeout_ms=3000)

    if responses:
        print("\n  >>> Got response to format C!")
        return responses

    print("\n  No response to any handshake format.")
    print("  The device may not speak BladeRunner on this interface,")
    print("  or may need a different framing/report ID.")
    return []


def step2_get_firmware_version(h):
    """Try GetSetting for firmware version info.

    H->D type 1 (GetSetting). Common setting IDs to try.
    """
    print("\n[Step 2] Get Settings — querying device info")

    # Try several setting IDs
    setting_ids = [
        (0x0001, "DFU Transfer Size"),
        (0x0002, "Max Block Size"),
        (0x0003, "Firmware Version (guess)"),
        (0x0004, "Serial Number (guess)"),
        (0x0005, "Battery Level (guess)"),
        (0x0100, "Device Info (guess)"),
        (0xFF00, "Product ID (guess)"),
    ]

    for setting_id, label in setting_ids:
        pkt = build_packet(0x00, [
            0x01,                          # MsgType = GetSetting
            (setting_id >> 8) & 0xFF,      # ID high
            setting_id & 0xFF,             # ID low
            0x00, 0x00,                    # Payload length = 0
        ])
        responses = send_and_recv(h, pkt, f"GetSetting 0x{setting_id:04X} ({label})", timeout_ms=1500)
        if responses:
            for resp in responses:
                if len(resp) >= 5:
                    msg_type = resp[0]
                    resp_id = (resp[1] << 8) | resp[2]
                    plen = (resp[3] << 8) | resp[4]
                    print(f"  Parsed: type={msg_type} id=0x{resp_id:04X} payload_len={plen}")
                    if msg_type == 4:
                        print(f"  --> SettingSuccess!")
                    elif msg_type == 5:
                        print(f"  --> SettingException")


def step3_get_battery(h):
    """Try various approaches to read battery level."""
    print("\n[Step 3] Battery query attempts")

    # Try feature reports first
    print("  Trying feature reports 1-20...")
    for report_id in range(1, 21):
        try:
            data = h.get_feature_report(report_id, 64)
            if data and any(b != 0 for b in data):
                print(f"\n  Feature Report {report_id} ({len(data)} bytes):")
                hexdump(data, "    ")
        except Exception as e:
            pass  # Many report IDs won't be supported

    # Try PerformCommand for battery
    print("\n  Trying PerformCommand for battery status...")
    for cmd_id in [0x0005, 0x0010, 0x0020, 0x0100]:
        pkt = build_packet(0x00, [
            0x02,                       # MsgType = PerformCommand
            (cmd_id >> 8) & 0xFF,
            cmd_id & 0xFF,
            0x00, 0x00,                 # Payload length = 0
        ])
        responses = send_and_recv(h, pkt, f"PerformCommand 0x{cmd_id:04X}", timeout_ms=1500)
        if responses:
            for resp in responses:
                if len(resp) >= 5:
                    msg_type = resp[0]
                    if msg_type == 6:
                        print(f"  --> CommandSuccess!")
                    elif msg_type == 7:
                        print(f"  --> CommandException")


def main():
    parser = argparse.ArgumentParser(description="BladeRunner Protocol Probe")
    parser.add_argument("--step", type=int, default=0, choices=[0, 1, 2, 3],
                        help="Probe step (0=passive, 1=handshake, 2=settings, 3=battery)")
    parser.add_argument("--all", action="store_true", help="Run all steps")
    args = parser.parse_args()

    info = find_device()
    if not info:
        print("No Poly device found on FFA2 usage page.")
        sys.exit(1)

    print(f"Found: {info['product_string']} (VID:0x{info['vendor_id']:04X} PID:0x{info['product_id']:04X})")
    print(f"Serial: {info['serial_number']}")
    print(f"Usage: 0x{info['usage_page']:04X}:0x{info['usage']:04X}")
    print(f"Path: {info['path']}")

    h = open_device(info)
    print("Device opened successfully.")

    try:
        if args.all:
            step0_passive_listen(h)
            step1_handshake(h)
            step2_get_firmware_version(h)
            step3_get_battery(h)
        elif args.step == 0:
            step0_passive_listen(h)
        elif args.step == 1:
            step1_handshake(h)
        elif args.step == 2:
            step2_get_firmware_version(h)
        elif args.step == 3:
            step3_get_battery(h)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        h.close()
        print("\nDevice closed.")


if __name__ == "__main__":
    main()
