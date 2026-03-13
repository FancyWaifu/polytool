#!/usr/bin/env python3
"""
FWU API Protocol Probe — Poly Savi 8220 (W8220T) firmware update protocol.

Reverse-engineered from DFUManager.dll / PLTDeviceManager.dll.

The FWU API uses 0x4Fxx message codes over HID reports on the FFA2 usage page.
The device uses a pull model — it requests blocks from the host.

This probe tries to communicate with the base station to understand the
HID report format for carrying these messages.
"""

import sys
import time
import struct
import hid

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
TARGET_USAGE_PAGE = 0xFFA2


def hexdump(data, prefix="    "):
    if not data:
        print(f"{prefix}(empty)")
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


def open_device(info):
    h = hid.device()
    h.open_path(info["path"])
    return h


def read_responses(h, timeout_ms=2000, label=""):
    """Read all available responses within timeout."""
    h.set_nonblocking(1)
    elapsed = 0
    responses = []
    while elapsed < timeout_ms:
        data = h.read(512)
        if data:
            print(f"  <<< [{elapsed}ms] {label} Response ({len(data)} bytes):")
            hexdump(data)
            responses.append(bytes(data))
        time.sleep(0.01)
        elapsed += 10
    return responses


def probe_feature_reports(h):
    """Exhaustive scan of all feature reports."""
    print("\n=== Feature Report Scan (0-255) ===")
    found = 0
    for rid in range(256):
        try:
            data = h.get_feature_report(rid, 512)
            if data and any(b != 0 for b in data):
                found += 1
                print(f"\n  Report ID {rid} (0x{rid:02X}), {len(data)} bytes:")
                hexdump(data)
        except Exception:
            pass
    print(f"\n  Total feature reports with data: {found}")


def probe_report_sizes(h):
    """Try to determine what report IDs the device accepts for output."""
    print("\n=== Output Report ID Probe ===")
    print("  Sending minimal reports with different IDs to see what the device accepts...")

    for rid in range(0, 32):
        try:
            # Try writing a 2-byte report (report ID + 1 byte)
            # If the device doesn't support this report ID, write() will fail
            pkt = bytes([rid, 0x00])
            h.write(pkt)
            print(f"  Report ID {rid} (0x{rid:02X}): write accepted (2 bytes)")
        except Exception as e:
            err = str(e)
            if err and "error" in err.lower():
                pass  # Expected for unsupported report IDs
            # Try larger sizes
            for size in [5, 8, 16, 32, 64]:
                try:
                    pkt = bytes([rid]) + bytes(size - 1)
                    h.write(pkt)
                    print(f"  Report ID {rid} (0x{rid:02X}): write accepted ({size} bytes)")
                    break
                except Exception:
                    pass


def probe_fwu_via_feature_report(h):
    """Try sending FWU API commands via set_feature_report.

    The FWU API message codes are 0x4Fxx. On Plantronics devices, these
    may be carried inside feature reports or output reports.

    Feature report 15 returned [0F 00 01 0F 20]. Let's see if we can
    send commands through it.
    """
    print("\n=== FWU API Probe via Feature Reports ===")

    # First, decode what report 15 means
    data = h.get_feature_report(15, 64)
    print(f"  Current Report 15: {' '.join(f'{b:02X}' for b in data)}")
    print(f"    Byte 0: 0x{data[0]:02X} (Report ID)")
    print(f"    Byte 1: 0x{data[1]:02X}")
    print(f"    Byte 2: 0x{data[2]:02X}")
    print(f"    Byte 3: 0x{data[3]:02X} (decimal {data[3]})")
    print(f"    Byte 4: 0x{data[4]:02X} (decimal {data[4]})")

    # Byte 3 = 0x0F and byte 4 = 0x20
    # 0x0F could be the headsetType (from headsetTypeMapping.json: ACFF -> 0x0F)
    # 0x20 = 32 could be a capability bitmask or report size

    # Try to use report 15 to send a command
    print("\n  Trying to set feature report 15 with FWU enable command...")
    # FWU_ENABLE_REQ = 0x4F00 (the REQ counterpart to 0x4F01 ENABLE_CFM)
    try:
        # Try: [ReportID=15, 0x4F, 0x00, 0x01, 0x00] = FWU enable, enable=1
        cmd = bytes([0x0F, 0x4F, 0x00, 0x01, 0x00])
        h.send_feature_report(cmd)
        print("  Sent! Reading response...")
        time.sleep(0.5)
        resp = h.get_feature_report(15, 64)
        print(f"  Report 15 after: {' '.join(f'{b:02X}' for b in resp)}")

        # Also check for input reports
        responses = read_responses(h, timeout_ms=2000, label="FWU Enable")
    except Exception as e:
        print(f"  Error: {e}")


def probe_hid_pipe_data(h):
    """Try the HIDPipeData approach.

    From RE: BaseHostCommand2::writeRawHidPipeData is used for BladeRunner
    protocol tunneling. The HIDPipeData usage might be a specific report ID
    on the FFA2 interface that carries arbitrary payloads.

    Let's try different report IDs with FWU API payloads.
    """
    print("\n=== HIDPipeData / FWU API Output Report Probe ===")

    # The FWU messages are 0x4Fxx. Try wrapping them in different report formats.
    # Common Plantronics output report sizes: 5 bytes, 8 bytes, 20 bytes, 64 bytes

    fwu_enable_payload = bytes([0x4F, 0x00, 0x01])  # FWU_ENABLE_REQ, enable=1

    # Try each report ID (0-15) with the FWU payload at different offsets
    for rid in range(0, 16):
        for total_size in [5, 8, 20, 32, 64]:
            try:
                pkt = bytearray(total_size)
                pkt[0] = rid
                # Put FWU command at byte 1
                pkt[1] = 0x4F
                pkt[2] = 0x00
                pkt[3] = 0x01  # enable=1
                h.write(bytes(pkt))

                # Quick check for response
                h.set_nonblocking(1)
                time.sleep(0.1)
                resp = h.read(256)
                if resp:
                    print(f"\n  !!! Response to Report ID {rid}, size {total_size}:")
                    hexdump(resp)
                    # Read more
                    read_responses(h, timeout_ms=1000, label=f"RID={rid}")
                    return  # Found working format!
            except Exception:
                pass

    print("  No responses to any output report format with FWU payload.")

    # Try via set_feature_report with different IDs
    print("\n  Trying FWU payload via set_feature_report...")
    for rid in range(0, 16):
        for total_size in [5, 8, 20, 64]:
            try:
                pkt = bytearray(total_size)
                pkt[0] = rid
                pkt[1] = 0x4F
                pkt[2] = 0x00
                pkt[3] = 0x01
                h.send_feature_report(bytes(pkt))

                time.sleep(0.1)
                # Read feature report back
                try:
                    resp = h.get_feature_report(rid, 64)
                    if resp and any(b != 0 for b in resp):
                        if resp != bytes([rid]) + bytes(len(resp)-1):  # Not just zeros
                            print(f"\n  Feature report {rid} changed after send (size {total_size}):")
                            hexdump(resp)
                except Exception:
                    pass

                # Check for input reports too
                h.set_nonblocking(1)
                resp = h.read(256)
                if resp:
                    print(f"\n  !!! Input report after feature set (RID={rid}, size={total_size}):")
                    hexdump(resp)
                    return
            except Exception:
                pass

    print("  No responses via feature reports either.")


def probe_usage_specific(h):
    """Try writing to specific FFA2 usages.

    From the device settings JSON (ac29.json), the Savi 8220 base uses
    specific usages on the FFA2 page for different settings.
    Known writable usages: 0x7F (restoreDefaults), 0x4E (muteTone), etc.

    Let's try reading some known usages via feature reports.
    The mapping from usage to report ID is device-specific.
    """
    print("\n=== Known FFA2 Usage Probe ===")
    print("  Reading all feature reports 0-255 to map the full report space...")

    report_map = {}
    for rid in range(256):
        try:
            data = h.get_feature_report(rid, 512)
            if data:
                report_map[rid] = data
        except Exception:
            pass

    print(f"  Readable feature reports: {sorted(report_map.keys())}")
    for rid, data in sorted(report_map.items()):
        if any(b != 0 for b in data):
            print(f"    Report {rid:3d} (0x{rid:02X}): [{len(data)} bytes] {' '.join(f'{b:02X}' for b in data)}")


def main():
    info = find_device()
    if not info:
        print("No Poly device found on FFA2 usage page.")
        sys.exit(1)

    print(f"Device: {info['product_string']} (VID:0x{info['vendor_id']:04X} PID:0x{info['product_id']:04X})")
    print(f"Serial: {info['serial_number']}")

    h = open_device(info)
    print("Opened.\n")

    try:
        # Step 1: Map the full feature report space
        probe_usage_specific(h)

        # Step 2: Try output report IDs
        probe_report_sizes(h)

        # Step 3: Try FWU API via feature reports
        probe_fwu_via_feature_report(h)

        # Step 4: Try HIDPipeData
        probe_hid_pipe_data(h)

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        h.close()
        print("\nClosed.")


if __name__ == "__main__":
    main()
