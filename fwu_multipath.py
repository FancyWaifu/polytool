#!/usr/bin/env python3
"""
FWU API Multi-Interface Probe — Listen on ALL HID interfaces simultaneously.

On macOS, each top-level HID collection gets its own IOHIDDevice handle.
The Savi 8220 has multiple interfaces:
  - Consumer Control (0x000C) — what Poly Lens legacyhost uses
  - Vendor FFA2:0x0003 — what our probes have been using
  - Possibly others

The FWU API writes go to RID 3 and succeed on any interface, but the
FWU responses (input reports) may only arrive on a specific interface.

This script opens ALL interfaces for the Poly device and listens on
all of them simultaneously while sending FWU commands.
"""

import sys
import time
import signal
import threading
import hid

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}

FRAG_START = 0x20
RID_DATA = 0x03


def hexline(data):
    return ' '.join(f'{b:02X}' for b in data)


def find_all_poly_interfaces():
    """Find ALL HID interfaces for Poly devices."""
    results = []
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS:
            results.append(d)
    return results


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


def build_fwu_pkt(payload, report_size=64):
    pkt = bytearray(report_size)
    pkt[0] = RID_DATA
    pkt[1] = FRAG_START
    pkt[2] = len(payload)
    for i, b in enumerate(payload):
        if 3 + i < report_size:
            pkt[3 + i] = b
    return bytes(pkt)


def listener_thread(h, label, stop_event, results):
    """Background thread that reads input reports from a device handle."""
    h.set_nonblocking(1)
    while not stop_event.is_set():
        try:
            data = h.read(256)
            if data:
                ts = time.time()
                results.append((ts, label, bytes(data)))
                rid = data[0] if data else 0
                payload = data[1:] if len(data) > 1 else b''
                print(f"  <<< [{label}] RID={rid} ({len(data)}B): {hexline(data)}")

                # Decode if it looks like FWU response
                if rid == RID_DATA and len(payload) >= 2:
                    if payload[0] == FRAG_START:
                        plen = payload[1]
                        msg = payload[2:2+plen]
                        if msg and msg[0] == 0x4F:
                            names = {
                                0x01: "ENABLE_CFM", 0x02: "DEVICE_NOTIFY_IND",
                                0x04: "UPDATE_CFM", 0x0C: "STATUS_IND",
                            }
                            name = names.get(msg[1], f"0x4F{msg[1]:02X}")
                            print(f"       → *** FWU {name} *** : {hexline(msg)}")
                        else:
                            print(f"       → Data START len={plen}: {hexline(msg[:20])}")
                    elif payload[0] == 0x80:
                        print(f"       → Data CONT: {hexline(payload[1:20])}")
                elif rid != 0x05:  # Skip the talk button noise
                    print(f"       → Payload: {hexline(payload[:20])}")
        except Exception:
            pass
        time.sleep(0.005)


def main():
    interfaces = find_all_poly_interfaces()
    if not interfaces:
        print("No Poly devices found.")
        sys.exit(1)

    # Group by VID:PID
    by_vidpid = {}
    for d in interfaces:
        key = (d["vendor_id"], d["product_id"])
        if key not in by_vidpid:
            by_vidpid[key] = []
        by_vidpid[key].append(d)

    print("=== All Poly HID Interfaces ===\n")
    for (vid, pid), devs in by_vidpid.items():
        print(f"  VID:0x{vid:04X} PID:0x{pid:04X} — {devs[0]['product_string']}")
        for i, d in enumerate(devs):
            print(f"    [{i}] Usage: 0x{d['usage_page']:04X}:0x{d['usage']:04X}  "
                  f"IF:{d['interface_number']}  Path: {d['path']}")
        print()

    # Open ALL interfaces for the target device
    target_vid_pid = None
    for (vid, pid), devs in by_vidpid.items():
        if pid in (0xACFF, 0xACFE, 0xAC29, 0xAC20, 0xAC26):
            target_vid_pid = (vid, pid)
            break

    if not target_vid_pid:
        # Just use the first Poly device
        target_vid_pid = list(by_vidpid.keys())[0]

    target_devs = by_vidpid[target_vid_pid]
    print(f"Target: VID:0x{target_vid_pid[0]:04X} PID:0x{target_vid_pid[1]:04X}")
    print(f"  Opening {len(target_devs)} interface(s)...\n")

    handles = []
    labels = []
    for i, d in enumerate(target_devs):
        up = d['usage_page']
        u = d['usage']
        label = f"UP{up:04X}:{u:04X}"
        try:
            h = hid.device()
            h.open_path(d["path"])
            handles.append(h)
            labels.append(label)
            print(f"  Opened [{label}] ✓")
        except Exception as e:
            print(f"  Failed [{label}]: {e}")

    if not handles:
        print("No interfaces could be opened!")
        sys.exit(1)

    # Start listener threads on ALL handles
    stop_event = threading.Event()
    results = []
    threads = []

    for h, label in zip(handles, labels):
        t = threading.Thread(target=listener_thread,
                             args=(h, label, stop_event, results),
                             daemon=True)
        t.start()
        threads.append(t)

    print(f"\n  Listening on {len(handles)} interface(s)...")
    print(f"  Draining any pending reports...\n")
    time.sleep(1)
    drain_count = len(results)
    if drain_count:
        print(f"  (drained {drain_count} pending report(s))\n")
    results.clear()

    # Send FWU_ENABLE_REQ on each interface and see where responses come
    print("=" * 60)
    print("SENDING FWU_ENABLE_REQ (Enable=1) ON EACH INTERFACE")
    print("=" * 60)

    fwu_enable = build_fwu_pkt(bytes([0x4F, 0x00, 0x01]))

    for h, label in zip(handles, labels):
        print(f"\n  >>> Sending on [{label}]")
        print(f"      TX: {hexline(fwu_enable[:8])}...")

        results_before = len(results)
        ok = timed_write(h, fwu_enable)

        if ok:
            print(f"      Write accepted!")
        else:
            print(f"      BLOCKED/TIMEOUT")
            continue

        # Wait for responses from ANY interface
        time.sleep(3)
        new_results = results[results_before:]
        print(f"      Got {len(new_results)} response(s) from ALL interfaces")
        for ts, src, data in new_results:
            print(f"        [{src}] RID={data[0]} : {hexline(data)}")

        # Disable
        fwu_disable = build_fwu_pkt(bytes([0x4F, 0x00, 0x00]))
        timed_write(h, fwu_disable)
        time.sleep(0.5)
        # Drain
        results.clear()

    # Extended listen after final enable
    print("\n" + "=" * 60)
    print("EXTENDED LISTEN — Enable on all interfaces, listen 15s")
    print("=" * 60)

    results.clear()

    # Enable on all interfaces
    for h, label in zip(handles, labels):
        print(f"  Enabling on [{label}]...")
        timed_write(h, fwu_enable)
        time.sleep(0.2)

    print(f"\n  Listening for 15 seconds on all {len(handles)} interfaces...")
    print("  (Press Ctrl+C to stop early)\n")

    try:
        start = time.time()
        last_count = 0
        while time.time() - start < 15:
            time.sleep(1)
            elapsed = int(time.time() - start)
            if len(results) > last_count:
                last_count = len(results)
            else:
                print(f"  [{elapsed}s] waiting... ({len(results)} total responses)")
    except KeyboardInterrupt:
        print("\n  Interrupted.")

    print(f"\n  Total responses during extended listen: {len(results)}")
    for ts, src, data in results:
        rid = data[0] if data else 0
        if rid != 0x05:  # Skip talk button noise
            print(f"    [{src}] RID={rid} ({len(data)}B): {hexline(data)}")

    # Cleanup: disable on all
    print("\n  Disabling FWU on all interfaces...")
    for h, label in zip(handles, labels):
        try:
            timed_write(h, fwu_disable)
        except Exception:
            pass
        time.sleep(0.1)

    # Stop threads and close
    stop_event.set()
    for t in threads:
        t.join(timeout=1)

    for h in handles:
        try:
            h.close()
        except Exception:
            pass

    print("\nAll interfaces closed.")


if __name__ == "__main__":
    main()
