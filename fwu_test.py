#!/usr/bin/env python3
"""
Quick FWU protocol diagnostic — test each step interactively.
"""

import sys
import time
import struct
import signal
import subprocess
import hid

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
TARGET_USAGE_PAGE = 0xFFA2
RID_DATA = 0x03

def hexline(data, n=32):
    return ' '.join(f'{b:02X}' for b in data[:n])

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
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)

def listen(h, timeout_ms=5000):
    h.set_nonblocking(1)
    elapsed = 0
    results = []
    while elapsed < timeout_ms:
        try:
            data = h.read(512)
        except OSError:
            break
        if data:
            rid = data[0]
            print(f"    [{elapsed:5d}ms] RID {rid} ({len(data)}B): {hexline(data)}")
            if rid == RID_DATA and len(data) > 3 and data[1] == 0x20:
                plen = data[2]
                payload = bytes(data[3:3+plen])
                if len(payload) >= 2:
                    prim = struct.unpack_from('<H', payload, 0)[0]
                    print(f"             → FWU 0x{prim:04X}: {hexline(payload[2:])}")
            results.append((rid, bytes(data)))
        time.sleep(0.005)
        elapsed += 5
    return results

def build_fwu_msg(prim_id, *params, report_size=64):
    mail = struct.pack('<H', prim_id) + bytes(params)
    pkt = bytearray(report_size)
    pkt[0] = RID_DATA
    pkt[1] = 0x20
    pkt[2] = len(mail)
    pkt[3:3+len(mail)] = mail
    return bytes(pkt)

# Kill Poly Lens
print("Killing Poly processes...")
for proc in ["legacyhost", "LensService", "PolyLauncher", "Poly Studio",
             "CallControlApp"]:
    subprocess.run(["pkill", "-9", "-f", proc], capture_output=True)
time.sleep(2)

# USB reset
print("Attempting USB reset...")
try:
    import usb.core
    dev = usb.core.find(idVendor=0x047F, idProduct=0xACFF)
    if dev:
        dev.reset()
        print("  USB reset OK, waiting 3s...")
        time.sleep(3)
    else:
        print("  No pyusb device found (reset skipped)")
except ImportError:
    print("  pyusb not installed — skipping USB reset")
    print("  Install with: pip3 install pyusb")
except Exception as e:
    print(f"  USB reset failed: {e}")

# Show ALL Poly interfaces
print("\nAll Poly HID interfaces:")
all_interfaces = []
for d in hid.enumerate():
    if d["vendor_id"] in POLY_VIDS:
        all_interfaces.append(d)
        print(f"  VID:0x{d['vendor_id']:04X} PID:0x{d['product_id']:04X} "
              f"Usage:0x{d['usage_page']:04X}:0x{d['usage']:04X} "
              f"IF:{d['interface_number']} "
              f"Product:{d['product_string']}")
print()

# Find FFA2 device
info = None
for d in all_interfaces:
    if d["usage_page"] == TARGET_USAGE_PAGE:
        info = d
        break

if not info:
    print("No FFA2 device found!")
    if all_interfaces:
        print("Trying first Poly device instead...")
        info = all_interfaces[0]
    else:
        print("No Poly devices at all.")
        sys.exit(1)

print(f"Using: {info['product_string']} (0x{info['vendor_id']:04X}:0x{info['product_id']:04X})")
print(f"  Usage: 0x{info['usage_page']:04X}:0x{info['usage']:04X}")
print(f"  IF: {info['interface_number']}")
print(f"  Path: {info['path']}")

h = hid.device()
h.open_path(info["path"])
print("  Opened.\n")

# Read feature report 15 (state indicator)
try:
    fr15 = h.get_feature_report(15, 64)
    print(f"  FR15: {hexline(fr15)}")
except Exception as e:
    print(f"  FR15 read failed: {e}")

# Drain
h.set_nonblocking(1)
drained = 0
while h.read(256):
    drained += 1
if drained:
    print(f"  Drained {drained} pending reports")
print()

try:
    # Step 1: Sign on
    input("Press Enter to SIGN ON...")
    print("  TX: [0D 15]")
    ok = timed_write(h, bytes([0x0D, 0x15]))
    print(f"  Write: {'OK' if ok else 'BLOCKED!'}")
    print("  Listening 5s...")
    results = listen(h, 5000)
    got_rid2 = any(r == 2 for r, _ in results)
    print(f"  Sign-on result: {'GOT RID 2' if got_rid2 else 'NO RID 2'}")
    print(f"  Total responses: {len(results)}")

    # Read FR15 again to see if state changed
    try:
        fr15 = h.get_feature_report(15, 64)
        print(f"  FR15 after sign-on: {hexline(fr15)}")
    except:
        pass
    print()

    # Drain
    time.sleep(0.5)
    h.set_nonblocking(1)
    while h.read(256):
        pass

    # Step 2: FWU Enable
    input("Press Enter to send FWU_ENABLE_REQ...")
    pkt = build_fwu_msg(0x4F00, 0x01, 0x07)
    print(f"  TX: {hexline(pkt[:10])}")
    ok = timed_write(h, pkt)
    print(f"  Write: {'OK' if ok else 'BLOCKED!'}")
    if ok:
        print("  Listening 30s for ENABLE_CFM + DEVICE_NOTIFY_IND...")
        print("  (DECT wireless can be slow — headset must be connected to base)")
        results = listen(h, 30000)
        print(f"  Got {len(results)} response(s)")
        for rid, data in results:
            if rid == RID_DATA:
                print(f"  *** RID 3 DATA! ***")
    print()

    # Step 3: Read FR15 again
    try:
        fr15 = h.get_feature_report(15, 64)
        print(f"  FR15 after enable: {hexline(fr15)}")
    except:
        pass
    print()

    # Step 4: Cleanup
    input("Press Enter to DISABLE + SIGN OFF...")
    pkt = build_fwu_msg(0x4F00, 0x00, 0x07)
    timed_write(h, pkt)
    listen(h, 2000)
    timed_write(h, bytes([0x0D, 0x00]))
    listen(h, 1000)
    print("  Done.")

except KeyboardInterrupt:
    print("\n  Interrupted!")
    try:
        timed_write(h, build_fwu_msg(0x4F00, 0x00, 0x07))
        timed_write(h, bytes([0x0D, 0x00]))
    except:
        pass
finally:
    h.close()
    print("Closed.")
