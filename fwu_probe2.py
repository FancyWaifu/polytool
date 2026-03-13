#!/usr/bin/env python3
"""
Safe FWU API Probe v2 — Read-only operations first, then careful writes.

On macOS, hid.write() to an unsupported report ID blocks forever.
So we ONLY do reads first, and only attempt writes with timeouts.
"""

import sys
import time
import signal
import hid

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}


def hexdump(data, prefix="    "):
    if not data:
        return
    for i in range(0, len(data), 16):
        hex_part = " ".join(f"{b:02X}" for b in data[i:i+16])
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data[i:i+16])
        print(f"{prefix}{i:04X}: {hex_part:<48s} {asc_part}")


def find_all_poly_interfaces():
    """Find ALL HID interfaces for ALL Poly devices."""
    results = []
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS:
            results.append(d)
    return results


def safe_read_feature_reports(path, label=""):
    """Read-only scan of all feature reports on a device path."""
    h = hid.device()
    h.open_path(path)

    print(f"\n{'='*60}")
    print(f"Feature Report Scan: {label}")
    print(f"Path: {path}")
    print(f"{'='*60}")

    found = {}
    for rid in range(256):
        try:
            data = h.get_feature_report(rid, 512)
            if data and len(data) > 0:
                found[rid] = bytes(data)
        except Exception:
            pass

    if found:
        for rid, data in sorted(found.items()):
            nonzero = any(b != 0 for b in data)
            marker = " ***" if nonzero else ""
            hex_str = " ".join(f"{b:02X}" for b in data[:40])
            if len(data) > 40:
                hex_str += " ..."
            print(f"  RID {rid:3d} (0x{rid:02X}) [{len(data):3d} bytes]{marker}: {hex_str}")
    else:
        print("  No readable feature reports.")

    # Also try passive read for 2 seconds
    print(f"\n  Passive input read (2s)...")
    h.set_nonblocking(1)
    count = 0
    start = time.time()
    while time.time() - start < 2:
        data = h.read(512)
        if data:
            count += 1
            print(f"  Input report #{count} ({len(data)} bytes):")
            hexdump(data)
        time.sleep(0.01)
    if count == 0:
        print("  No unsolicited input reports.")

    h.close()
    return found


def safe_send_feature_report(path, report_data, label=""):
    """Send a feature report with a timeout via alarm signal."""
    print(f"\n  >>> SET Feature Report: {label}")
    hexdump(report_data, "      ")

    h = hid.device()
    h.open_path(path)

    # Use alarm to timeout blocking writes
    timed_out = [False]
    def handler(signum, frame):
        timed_out[0] = True
        raise TimeoutError("Write timed out")

    old_handler = signal.signal(signal.SIGALRM, handler)
    signal.alarm(3)  # 3 second timeout

    try:
        h.send_feature_report(report_data)
        signal.alarm(0)  # Cancel alarm
        print("      Send succeeded!")

        # Read back
        time.sleep(0.3)
        rid = report_data[0]
        try:
            resp = h.get_feature_report(rid, 64)
            print(f"      Read-back RID {rid}: {' '.join(f'{b:02X}' for b in resp)}")
        except Exception:
            pass

        # Check for input reports
        h.set_nonblocking(1)
        for _ in range(100):
            data = h.read(256)
            if data:
                print(f"      Input response ({len(data)} bytes):")
                hexdump(data, "        ")
                break
            time.sleep(0.01)

    except TimeoutError:
        print("      TIMEOUT — write blocked (unsupported report)")
    except Exception as e:
        print(f"      Error: {e}")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        try:
            h.close()
        except Exception:
            pass


def safe_write_output_report(path, report_data, label=""):
    """Send an output report with a timeout."""
    print(f"\n  >>> WRITE Output Report: {label}")
    hexdump(report_data, "      ")

    h = hid.device()
    h.open_path(path)

    timed_out = [False]
    def handler(signum, frame):
        timed_out[0] = True
        raise TimeoutError("Write timed out")

    old_handler = signal.signal(signal.SIGALRM, handler)
    signal.alarm(3)

    try:
        h.write(report_data)
        signal.alarm(0)
        print("      Write succeeded!")

        # Read response
        h.set_nonblocking(1)
        time.sleep(0.2)
        for _ in range(200):
            data = h.read(256)
            if data:
                print(f"      Response ({len(data)} bytes):")
                hexdump(data, "        ")
                break
            time.sleep(0.01)
        else:
            print("      No response within 2s.")

    except TimeoutError:
        print("      TIMEOUT — write blocked (unsupported report ID/size)")
    except Exception as e:
        print(f"      Error: {e}")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        try:
            h.close()
        except Exception:
            pass


def main():
    print("=== Poly HID Full Interface Probe ===\n")

    interfaces = find_all_poly_interfaces()
    if not interfaces:
        print("No Poly devices found.")
        sys.exit(1)

    # Group by path (macOS may show same path for multiple usages)
    paths = {}
    for d in interfaces:
        key = d["path"]
        if key not in paths:
            paths[key] = []
        paths[key].append(d)

    print(f"Found {len(interfaces)} interface(s) across {len(paths)} path(s):\n")
    for path, devs in paths.items():
        for d in devs:
            print(f"  Path: {path}")
            print(f"    Product:    {d['product_string']}")
            print(f"    VID:PID:    0x{d['vendor_id']:04X}:0x{d['product_id']:04X}")
            print(f"    Usage:      0x{d['usage_page']:04X}:0x{d['usage']:04X}")
            print(f"    Interface:  {d['interface_number']}")
            print(f"    Serial:     {d['serial_number']}")
            print()

    # Phase 1: Read-only scan of all feature reports on each unique path
    print("\n" + "="*60)
    print("PHASE 1: Read-only Feature Report Scan")
    print("="*60)

    for path, devs in paths.items():
        usages = ', '.join(f'0x{d["usage_page"]:04X}' for d in devs)
        label = f"{devs[0]['product_string']} [{usages}]"
        reports = safe_read_feature_reports(path, label)

    # Phase 2: Try sending feature reports (with timeout protection)
    print("\n" + "="*60)
    print("PHASE 2: Feature Report Write Probes")
    print("="*60)

    path = list(paths.keys())[0]

    # Try setting feature report 15 with the same data (identity write)
    print("\n--- Test: Re-write feature report 15 with same data ---")
    safe_send_feature_report(path, bytes([0x0F, 0x00, 0x01, 0x0F, 0x20]),
                             "Identity write (same data)")

    # Try setting feature report 15 with FWU enable
    print("\n--- Test: Feature report 15 with FWU enable (0x4F00) ---")
    safe_send_feature_report(path, bytes([0x0F, 0x4F, 0x00, 0x01, 0x00]),
                             "FWU_ENABLE_REQ via FR15")

    # Phase 3: Try output reports (with timeout protection)
    print("\n" + "="*60)
    print("PHASE 3: Output Report Write Probes")
    print("="*60)

    # From the AC29 settings JSON, usages like 0x4E are 16-bit writable
    # The HID descriptor maps usages to report IDs
    # Feature report 15 (usage unknown) is the only one we can read
    # Let's try writing output reports with the report ID that matches
    # what the device expects

    # Common Plantronics output report patterns:
    # [ReportID] [Usage_hi] [Usage_lo] [Value_hi] [Value_lo]
    # Or simply [ReportID] [Command] [Params...]

    # Try report ID 0 (default for devices without explicit IDs)
    for size in [5, 8, 16, 32, 64]:
        pkt = bytearray(size)
        pkt[0] = 0x00  # Report ID 0
        pkt[1] = 0x4F  # FWU
        pkt[2] = 0x00  # ENABLE_REQ
        pkt[3] = 0x01  # enable=1
        safe_write_output_report(path, bytes(pkt),
                                 f"FWU_ENABLE via RID=0, size={size}")

    print("\n\nDone.")


if __name__ == "__main__":
    main()
