#!/usr/bin/env python3
"""
Unified FWU/HID protocol probe for Poly devices.

Consolidates all research scripts into a single tool with subcommands.
Each test preserves the key protocol discoveries from the original probes.

Usage:
  python3 -m probes.fwu_probe --test <name> [options]

Tests:
  passive       Read-only scan: feature reports + passive listen
  signon        RID 13 sign-on protocol (value 0x15 → RID 2 response)
  enable        FWU enable/disable cycle with full observation
  correct       LE-corrected FWU protocol (the correct byte ordering)
  scan          Scan all FWU command codes (0x4F00-0x4F13)
  families      Test non-FWU message families (0x4E, etc.)
  multi         Multi-interface simultaneous listen
  exclusive     Kill legacyhost for exclusive FWU access
  bladerunner   BladeRunner protocol probe (handshake, GetSetting)
  enumerate     Enumerate all Poly HID interfaces with feature report scan
  all           Run passive → signon → correct (the useful progression)
"""

import sys
import os
import time
import struct
import threading
import argparse

# Allow running as `python3 probes/fwu_probe.py` from project root
sys.path.insert(0, os.path.dirname(__file__))
from hid_helpers import *


# ── Test: Passive ─────────────────────────────────────────────────────────────

def test_passive(h):
    """Read-only: feature reports + passive listen. Completely safe."""
    print("\n" + "=" * 60)
    print("PASSIVE: Read-Only Scan")
    print("=" * 60)

    features = read_all_features(h)
    print_features(features, "Feature reports")

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
        time.sleep(0.01)
    if count == 0:
        print("    No unsolicited input reports.")


# ── Test: Sign-On ─────────────────────────────────────────────────────────────

def test_signon(h):
    """RID 13 sign-on protocol discovery.

    From HID descriptor analysis:
      RID 13 (OUTPUT, 1 byte):
        bits 0-1: Usage 0x8F (sign-on state, range -2 to 1)
        bits 2-3: Usage 0x77 (mode selector, range 1 to 2)
        bit 4:    Usage 0xF2 (trigger flag, range 0 to 1)
      RID 2 (INPUT, 1 byte):
        Usage 0x8F: 1 bit, 0xEE: 1 bit, 0x77: 1 bit, 0x80: 1 bit, 0xC1: 1 bit

    Key finding: value 0x15 (signon=1, mode=1, trigger=1) works.
    """
    print("\n" + "=" * 60)
    print("SIGNON: RID 13 → RID 2 Protocol")
    print("=" * 60)

    # Read initial state
    try:
        fr15 = h.get_feature_report(15, 64)
        print(f"  FR15 before: {hexline(fr15)}")
    except Exception:
        pass

    drain(h)

    # Try the known-working sign-on values
    signon_attempts = [
        ("signon=1, mode=1, trigger=1", 0x15),
        ("signon=1, mode=1, trigger=0", 0x05),
        ("signon=1, mode=2, trigger=1", 0x19),
    ]

    for label, value in signon_attempts:
        pkt = bytes([0x0D, value & 0xFF])
        print(f"\n  >>> {label} → [0D {value:02X}] = {value:08b}")
        ok = timed_write(h, pkt)
        if ok:
            print(f"      Write accepted!")
            results = listen(h, timeout_ms=2000, quiet_rids={RID_ACK})
            got_rid2 = any(rid == 2 for rid, _ in results)
            if got_rid2:
                print(f"      *** GOT RID 2 SIGN-ON RESPONSE! ***")
                # Try FWU after successful sign-on
                print(f"\n      Sending FWU_ENABLE_REQ after sign-on...")
                fwu_pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x01]))
                ok2 = timed_write(h, fwu_pkt)
                if ok2:
                    print(f"      FWU write accepted, listening 10s...")
                    fwu_results = listen(h, timeout_ms=10000)
                    if any(rid == RID_DATA for rid, _ in fwu_results):
                        print(f"\n      *** FWU RESPONSE ON RID 3! ***")
                    # Disable FWU
                    timed_write(h, build_fwu_pkt(bytes([0x4F, 0x00, 0x00])))
                break
        else:
            print(f"      BLOCKED/TIMEOUT")
        time.sleep(0.3)

    # Sign off
    do_signoff(h)


# ── Test: Enable ──────────────────────────────────────────────────────────────

def test_enable(h):
    """FWU enable/disable cycle with feature report diff.

    Protocol flow:
      TX: [RID=3] [0x20] [len] [payload...] padded to 64
      RX Ack: Report ID 5 (DFUAck usage 0x88)
      RX Data: Report ID 3 with 0x20/0x80 framing
    """
    print("\n" + "=" * 60)
    print("ENABLE: FWU Enable/Disable Cycle")
    print("=" * 60)

    features_before = read_all_features(h)
    print_features(features_before, "Feature reports BEFORE")
    drain(h)

    # Enable
    pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x01]))
    print(f"\n  >>> FWU_ENABLE_REQ (enable=1): {hexline(pkt[:6])}")
    ok = timed_write(h, pkt)
    if ok:
        listen(h, timeout_ms=5000)

    features_after = read_all_features(h)
    for rid, data in sorted(features_after.items()):
        changed = " *** CHANGED" if features_before.get(rid) != data else ""
        if changed:
            print(f"    FR{rid:3d}: {hexline(data)}{changed}")

    # Extended listen for DEVICE_NOTIFY_IND (may be delayed for DECT)
    print(f"\n  Extended listen (10s)...")
    listen(h, timeout_ms=10000)

    # Disable
    pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x00]))
    print(f"\n  >>> FWU_ENABLE_REQ (disable)")
    timed_write(h, pkt)
    listen(h, timeout_ms=3000)


# ── Test: Correct ─────────────────────────────────────────────────────────────

def test_correct(h, args):
    """The correct FWU protocol with LE byte ordering.

    Key finding from libDFUManager.dylib disassembly:
      CVM API primitives use little-endian 16-bit IDs.
      0x4F00 (ENABLE_REQ) → wire bytes [0x00, 0x4F] not [0x4F, 0x00]

    Message format (from SendApiFwuEnableReq):
      ENABLE_REQ:  [0x00, 0x4F, enable, 0x07]  (4 bytes)
      UPDATE_REQ:  [0x03, 0x4F, devnr, mode]   (4 bytes)

    HID framing (from TxFragments):
      First fragment: [ReportID] [0x20] [PayloadLen] [payload...] padded
    """
    print("\n" + "=" * 60)
    print("CORRECT: LE-Corrected FWU Protocol")
    print("=" * 60)

    drain(h)

    # Step 1: Sign on
    print(f"\n--- Step 1: Sign On ---")
    ok = do_signon(h)
    if not ok:
        print("  Sign-on failed, continuing anyway...")
    drain(h)

    # Step 2: FWU Enable (correct LE byte ordering)
    print(f"\n--- Step 2: FWU_ENABLE_REQ (correct LE) ---")
    pkt = build_fwu_msg(API_FWU_ENABLE_REQ, 0x01, 0x07)
    print(f"  TX: {hexline(pkt[:8])}")
    print(f"  (payload: {hexline(pkt[3:7])} = ENABLE_REQ enable=1 ver=7)")

    ok = timed_write(h, pkt)
    if ok:
        listen_time = args.listen_time if hasattr(args, 'listen_time') else 20
        print(f"  Listening {listen_time}s for ENABLE_CFM + DEVICE_NOTIFY_IND...")
        results = listen(h, timeout_ms=listen_time * 1000)
        rid3_found = any(rid == RID_DATA for rid, _ in results)
        print(f"\n  RID3={'YES' if rid3_found else 'no'}")

        if not rid3_found:
            # Also try FWS_INIT_REQ (SetMode sends both)
            print(f"\n  Also sending FWS_INIT_REQ...")
            pkt2 = build_fwu_msg(API_FWS_INIT_REQ, 0x01)
            timed_write(h, pkt2)
            listen(h, timeout_ms=5000)
    else:
        print("  BLOCKED!")

    # Step 3: UPDATE_REQ
    print(f"\n--- Step 3: UPDATE_REQ ---")
    pkt = build_fwu_msg(API_FWU_UPDATE_REQ, 0x00, 0x00)
    print(f"  TX UPDATE_REQ(dev=0, mode=FWU): {hexline(pkt[:8])}")
    ok = timed_write(h, pkt)
    if ok:
        listen(h, timeout_ms=5000)

    # Cleanup
    print(f"\n--- Cleanup ---")
    timed_write(h, build_fwu_msg(API_FWU_ENABLE_REQ, 0x00, 0x07))
    listen(h, timeout_ms=2000)
    do_signoff(h)


# ── Test: Scan ────────────────────────────────────────────────────────────────

def test_scan(h):
    """Scan all FWU API command codes to see which get responses."""
    print("\n" + "=" * 60)
    print("SCAN: All FWU API Command Codes (0x4F00-0x4F13)")
    print("=" * 60)

    drain(h)
    for cmd_lo in range(0x14):
        name = FWU_MSG_NAMES.get(cmd_lo, f"0x4F{cmd_lo:02X}")
        payload = bytes([0x4F, cmd_lo, 0x00])
        pkt = build_fwu_pkt(payload)
        print(f"\n  >>> 0x4F{cmd_lo:02X} ({name})")
        ok = timed_write(h, pkt)
        if ok:
            results = listen(h, timeout_ms=1000, quiet_rids={RID_ACK})
            if any(rid == RID_DATA for rid, _ in results):
                print(f"      *** GOT RID 3 RESPONSE! ***")
        time.sleep(0.2)


# ── Test: Message Families ────────────────────────────────────────────────────

def test_families(h):
    """Test non-FWU message families (0x40-0x50, etc.)."""
    print("\n" + "=" * 60)
    print("FAMILIES: Non-FWU Message Codes")
    print("=" * 60)

    drain(h)
    test_codes = [
        (bytes([0x00, 0x00]), "Null (0x0000)"),
        (bytes([0x40, 0x00]), "Family 0x40"),
        (bytes([0x41, 0x00]), "Family 0x41"),
        (bytes([0x4E, 0x00]), "FWS_INIT (0x4E00)"),
        (bytes([0x4E, 0x04]), "FWS_STATUS (0x4E04)"),
        (bytes([0x50, 0x00]), "Family 0x50"),
        (bytes([0x80, 0x00]), "Family 0x80"),
        (bytes([0xFF, 0x00]), "Family 0xFF"),
    ]
    for payload, label in test_codes:
        pkt = build_fwu_pkt(payload)
        print(f"\n  >>> {label}")
        ok = timed_write(h, pkt)
        if ok:
            results = listen(h, timeout_ms=1000, quiet_rids={RID_ACK})
            if any(rid == RID_DATA for rid, _ in results):
                print(f"      *** RID 3 RESPONSE! ***")
        time.sleep(0.2)


# ── Test: Multi-Interface ─────────────────────────────────────────────────────

def test_multi(h_unused):
    """Open ALL Poly HID interfaces and listen simultaneously.

    FWU writes go to RID 3 and succeed on any interface, but responses
    may only arrive on a specific interface. This test finds which one.
    """
    print("\n" + "=" * 60)
    print("MULTI: Multi-Interface Simultaneous Listen")
    print("=" * 60)

    interfaces = find_all_poly_interfaces()
    if not interfaces:
        print("  No Poly devices found.")
        return

    # Group by VID:PID
    by_vidpid = {}
    for d in interfaces:
        key = (d["vendor_id"], d["product_id"])
        by_vidpid.setdefault(key, []).append(d)

    for (vid, pid), devs in by_vidpid.items():
        print(f"\n  VID:0x{vid:04X} PID:0x{pid:04X} — {devs[0]['product_string']}")
        for i, d in enumerate(devs):
            print(f"    [{i}] Usage: 0x{d['usage_page']:04X}:0x{d['usage']:04X}  "
                  f"IF:{d['interface_number']}")

    # Open all interfaces for the first device
    target_devs = list(by_vidpid.values())[0]
    handles = []
    labels = []
    for d in target_devs:
        label = f"UP{d['usage_page']:04X}:{d['usage']:04X}"
        try:
            h = open_device(d)
            handles.append(h)
            labels.append(label)
            print(f"\n  Opened [{label}]")
        except Exception as e:
            print(f"\n  Failed [{label}]: {e}")

    if not handles:
        print("  No interfaces could be opened!")
        return

    # Start listener threads
    stop_event = threading.Event()
    results = []

    def listener(h, label):
        h.set_nonblocking(1)
        while not stop_event.is_set():
            try:
                data = h.read(256)
                if data:
                    rid = data[0]
                    results.append((label, rid, bytes(data)))
                    if rid != RID_ACK:
                        print(f"  <<< [{label}] RID={rid} ({len(data)}B): {hexline(data[:20])}")
                        if rid == RID_DATA:
                            msg = decode_fwu_msg(data)
                            if msg:
                                print(f"       → FWU {msg[1]}: {hexline(msg[2])}")
            except Exception:
                pass
            time.sleep(0.005)

    threads = []
    for h, label in zip(handles, labels):
        t = threading.Thread(target=listener, args=(h, label), daemon=True)
        t.start()
        threads.append(t)

    time.sleep(1)
    results.clear()

    # Send FWU enable on each interface
    fwu_enable = build_fwu_pkt(bytes([0x4F, 0x00, 0x01]))
    fwu_disable = build_fwu_pkt(bytes([0x4F, 0x00, 0x00]))

    for h, label in zip(handles, labels):
        print(f"\n  >>> Sending FWU_ENABLE on [{label}]")
        before = len(results)
        timed_write(h, fwu_enable)
        time.sleep(3)
        new = results[before:]
        print(f"      Got {len(new)} response(s) from ALL interfaces")
        timed_write(h, fwu_disable)
        time.sleep(0.5)
        results.clear()

    # Cleanup
    stop_event.set()
    for t in threads:
        t.join(timeout=1)
    for h in handles:
        try:
            h.close()
        except Exception:
            pass
    print("\n  All interfaces closed.")


# ── Test: Exclusive ───────────────────────────────────────────────────────────

def test_exclusive(h_unused):
    """Kill legacyhost for exclusive device access, then try FWU.

    legacyhost may be consuming RID 3 input reports before we read them,
    or holding device state that prevents FWU responses.
    """
    print("\n" + "=" * 60)
    print("EXCLUSIVE: Kill legacyhost, then FWU")
    print("=" * 60)

    if is_legacyhost_running():
        kill_poly()
    time.sleep(2)

    # USB reset for clean state
    print("  USB reset...")
    usb_reset()

    info = find_device()
    if not info:
        print("  No Poly device found after killing legacyhost!")
        return
    print_device_info(info)

    h = open_device(info)
    drain(h)

    try:
        # Read initial FR15
        try:
            fr15 = h.get_feature_report(15, 64)
            print(f"  FR15: {hexline(fr15)}")
        except Exception:
            pass

        # Sign on
        do_signon(h)
        drain(h)

        # FWU Enable
        pkt = build_fwu_pkt(bytes([0x4F, 0x00, 0x01]))
        print(f"\n  >>> FWU_ENABLE_REQ (enable=1): {hexline(pkt[:6])}")
        ok = timed_write(h, pkt)
        if ok:
            print("  Listening 15s for FWU responses...")
            listen(h, timeout_ms=15000)
        else:
            print("  BLOCKED!")

        # Disable + sign off
        timed_write(h, build_fwu_pkt(bytes([0x4F, 0x00, 0x00])))
        listen(h, timeout_ms=2000)
        do_signoff(h)

    except KeyboardInterrupt:
        print("\n  Interrupted!")
    finally:
        try:
            timed_write(h, build_fwu_pkt(bytes([0x4F, 0x00, 0x00])))
        except Exception:
            pass
        h.close()


# ── Test: BladeRunner ─────────────────────────────────────────────────────────

def test_bladerunner(h):
    """BladeRunner protocol probe.

    H→D type 0 (HostProtocolVersion), expect D→H type 8 (ProtocolVersion)
    or type 11 (ProtocolRejection). Then GetSetting for device info.
    """
    print("\n" + "=" * 60)
    print("BLADERUNNER: Protocol Probe")
    print("=" * 60)

    # Step 0: Passive listen
    print("\n  --- Passive listen (3s) ---")
    h.set_nonblocking(1)
    count = 0
    start = time.time()
    while time.time() - start < 3:
        data = h.read(256)
        if data:
            count += 1
            elapsed = int((time.time() - start) * 1000)
            print(f"    [{elapsed}ms] ({len(data)} bytes):")
            hexdump(data, "      ")
        time.sleep(0.01)
    print(f"    {count} unsolicited report(s)")

    # Step 1: Handshake formats
    print("\n  --- Protocol Handshake ---")
    handshake_payload = [
        0x00,        # MsgType = HostProtocolVersion
        0x00, 0x00,  # ID = 0
        0x00, 0x01,  # Payload length = 1
        0x03,        # Protocol version 3
    ]

    for rid, label in [(0x00, "RID=0"), (0x01, "RID=1")]:
        pkt = build_raw_pkt(rid, bytes(handshake_payload))
        print(f"\n  >>> Handshake {label}: {hexline(pkt[:10])}")
        try:
            h.write(pkt)
            h.set_nonblocking(1)
            elapsed = 0
            while elapsed < 3000:
                resp = h.read(256)
                if resp:
                    print(f"    [{elapsed}ms] Response ({len(resp)} bytes):")
                    hexdump(resp, "      ")
                    if resp[0] == 0x08:
                        print("    --> ProtocolVersion (type 8)!")
                    elif resp[0] == 0x0B:
                        print("    --> ProtocolRejection (type 11)!")
                    break
                time.sleep(0.01)
                elapsed += 10
            else:
                print("    No response.")
        except Exception as e:
            print(f"    Error: {e}")

    # Step 2: Feature reports
    print("\n  --- Feature reports 1-20 ---")
    for report_id in range(1, 21):
        try:
            data = h.get_feature_report(report_id, 64)
            if data and any(b != 0 for b in data):
                print(f"    FR{report_id}: {hexline(data[:32])}")
        except Exception:
            pass


# ── Test: Enumerate ───────────────────────────────────────────────────────────

def test_enumerate(h_unused):
    """Enumerate all Poly HID interfaces with full feature report scan."""
    print("\n" + "=" * 60)
    print("ENUMERATE: All Poly HID Interfaces")
    print("=" * 60)

    interfaces = find_all_poly_interfaces()
    if not interfaces:
        print("  No Poly devices found.")
        return

    # Group by path
    paths = {}
    for d in interfaces:
        paths.setdefault(d["path"], []).append(d)

    print(f"\n  {len(interfaces)} interface(s) across {len(paths)} path(s):\n")
    for path, devs in paths.items():
        for d in devs:
            print(f"  {d['product_string']}")
            print(f"    VID:PID: 0x{d['vendor_id']:04X}:0x{d['product_id']:04X}")
            print(f"    Usage:   0x{d['usage_page']:04X}:0x{d['usage']:04X}")
            print(f"    IF:      {d['interface_number']}")
            print(f"    Serial:  {d['serial_number']}")

        # Feature report scan
        try:
            h = open_device(devs[0])
            features = read_all_features(h)
            if features:
                print_features(features, f"Feature reports")
            # Passive listen
            h.set_nonblocking(1)
            count = 0
            start = time.time()
            while time.time() - start < 2:
                data = h.read(512)
                if data:
                    count += 1
                    print(f"    Input #{count}: {hexline(data[:20])}")
                time.sleep(0.01)
            if count == 0:
                print(f"    No unsolicited input reports.")
            h.close()
        except Exception as e:
            print(f"    Could not open: {e}")
        print()


# ── Test Registry ─────────────────────────────────────────────────────────────

MENU_ITEMS = [
    ("passive",     "Read-only scan: feature reports + passive listen"),
    ("signon",      "RID 13 sign-on protocol (0x15 -> RID 2)"),
    ("enable",      "FWU enable/disable cycle with feature report diff"),
    ("correct",     "LE-corrected FWU protocol (correct byte ordering)"),
    ("scan",        "Scan all FWU command codes (0x4F00-0x4F13)"),
    ("families",    "Test non-FWU message families"),
    ("multi",       "Multi-interface simultaneous listen"),
    ("exclusive",   "Kill legacyhost for exclusive FWU access"),
    ("bladerunner", "BladeRunner protocol probe"),
    ("enumerate",   "Enumerate all Poly HID interfaces"),
]

TESTS = {
    "passive": test_passive,
    "signon": test_signon,
    "enable": test_enable,
    "correct": test_correct,
    "scan": test_scan,
    "families": test_families,
    "multi": test_multi,
    "exclusive": test_exclusive,
    "bladerunner": test_bladerunner,
    "enumerate": test_enumerate,
}

# Tests that manage their own device handle (don't pass h from main)
SELF_MANAGED = {"multi", "exclusive", "enumerate"}


# ── Interactive Menu ──────────────────────────────────────────────────────────

def print_menu():
    """Print the interactive test menu."""
    print()
    print("=" * 60)
    print("  Poly FWU Protocol Probe")
    print("=" * 60)
    for i, (name, desc) in enumerate(MENU_ITEMS, 1):
        print(f"  {i:2d}) {name:<14s} {desc}")
    print()
    print(f"   a) all            Run passive + signon + correct")
    print(f"   k) kill           Kill Poly Lens processes")
    print(f"   r) reset          USB reset device")
    print(f"   q) quit")
    print()


def run_test(test_name, h, args):
    """Run a single test, handling device open/close for self-managed tests.

    Returns the device handle (may be a new one if the test reopened it).
    """
    test_fn = TESTS[test_name]

    if test_name in SELF_MANAGED:
        # Close shared handle — these tests manage their own
        if h:
            try:
                h.close()
            except Exception:
                pass
        if test_name == "correct":
            test_fn(None, args)
        else:
            test_fn(None)
        # Reopen for next test
        info = find_device()
        if info:
            h = open_device(info)
            print(f"  Reopened device.")
        else:
            h = None
    elif test_name == "correct":
        test_fn(h, args)
    else:
        test_fn(h)

    return h


def interactive_menu(args):
    """Run the interactive menu loop."""
    h = None

    # Try to open device up front
    info = find_device()
    if info:
        print_device_info(info)
        h = open_device(info)
        print("  Opened.\n")
    else:
        print("No Poly device found (some tests may still work).\n")

    try:
        while True:
            print_menu()
            try:
                choice = input("  Select> ").strip().lower()
            except EOFError:
                break

            if not choice:
                continue

            if choice == 'q':
                break
            elif choice == 'k':
                print("\nKilling Poly Lens...")
                kill_poly()
                time.sleep(2)
                print("Done.")
                # Reopen device
                if h:
                    try:
                        h.close()
                    except Exception:
                        pass
                info = find_device()
                if info:
                    h = open_device(info)
                    print_device_info(info)
                    print("  Reopened device.")
                else:
                    h = None
                    print("  No device found after kill.")
                continue
            elif choice == 'r':
                print("\nUSB reset...")
                if h:
                    try:
                        h.close()
                    except Exception:
                        pass
                    h = None
                usb_reset()
                info = find_device()
                if info:
                    h = open_device(info)
                    print_device_info(info)
                    print("  Reopened device.")
                else:
                    print("  No device found after reset.")
                continue
            elif choice == 'a':
                test_names = ["passive", "signon", "correct"]
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(MENU_ITEMS):
                    test_names = [MENU_ITEMS[idx][0]]
                else:
                    print(f"  Invalid choice: {choice}")
                    continue
            elif choice in TESTS:
                test_names = [choice]
            else:
                print(f"  Invalid choice: {choice}")
                continue

            # Check device is available for tests that need it
            for test_name in test_names:
                if test_name not in SELF_MANAGED and h is None:
                    info = find_device()
                    if info:
                        h = open_device(info)
                        print_device_info(info)
                    else:
                        print("No Poly device found. Try 'k' to kill Poly Lens first.")
                        break

                try:
                    h = run_test(test_name, h, args)
                except KeyboardInterrupt:
                    print("\n  Test interrupted.")
                    # Try FWU cleanup
                    if h:
                        try:
                            timed_write(h, build_fwu_pkt(bytes([0x4F, 0x00, 0x00])))
                        except Exception:
                            pass
                        try:
                            timed_write(h, bytes([0x0D, 0x00]))
                        except Exception:
                            pass
                except Exception as e:
                    print(f"\n  Test error: {e}")

            print("\n  Press Enter to continue...")
            try:
                input()
            except EOFError:
                break

    finally:
        if h:
            try:
                h.close()
            except Exception:
                pass
            print("Device closed.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Unified FWU/HID protocol probe for Poly devices",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Run with no arguments for interactive menu, or specify a test:

  passive       Read-only scan: feature reports + passive listen
  signon        RID 13 sign-on protocol (0x15 -> RID 2)
  enable        FWU enable/disable cycle with feature report diff
  correct       LE-corrected FWU protocol (correct byte ordering)
  scan          Scan all FWU command codes (0x4F00-0x4F13)
  families      Test non-FWU message families
  multi         Multi-interface simultaneous listen
  exclusive     Kill legacyhost for exclusive FWU access
  bladerunner   BladeRunner protocol probe
  enumerate     Enumerate all Poly HID interfaces
  all           Run passive + signon + correct
""")
    parser.add_argument("--test", "-t", default=None,
                        help="Test to run (omit for interactive menu)")
    parser.add_argument("--kill", "-k", action="store_true",
                        help="Kill Poly Lens before starting")
    parser.add_argument("--reset", action="store_true",
                        help="USB reset before starting")
    parser.add_argument("--listen-time", type=int, default=20,
                        help="Listen time in seconds for 'correct' test (default: 20)")
    args = parser.parse_args()

    if args.kill:
        print("Killing Poly Lens...")
        kill_poly()
        time.sleep(2)

    if args.reset:
        print("USB reset...")
        usb_reset()

    # No --test specified → interactive menu
    if args.test is None:
        interactive_menu(args)
        return

    # CLI mode: run specified test(s)
    if args.test == "all":
        tests_to_run = ["passive", "signon", "correct"]
    elif args.test in TESTS:
        tests_to_run = [args.test]
    else:
        print(f"Unknown test: {args.test}")
        print(f"Available: {', '.join(TESTS.keys())}, all")
        sys.exit(1)

    # For self-managed tests, just run them directly
    if len(tests_to_run) == 1 and tests_to_run[0] in SELF_MANAGED:
        test_fn = TESTS[tests_to_run[0]]
        try:
            if tests_to_run[0] == "correct":
                test_fn(None, args)
            else:
                test_fn(None)
        except KeyboardInterrupt:
            print("\nInterrupted.")
        return

    # Open device for non-self-managed tests
    info = find_device()
    if not info:
        print("No Poly device found on FFA2 usage page.")
        all_poly = find_all_poly_interfaces()
        if all_poly:
            print("\nAll Poly HID interfaces:")
            for d in all_poly:
                print(f"  VID:0x{d['vendor_id']:04X} PID:0x{d['product_id']:04X} "
                      f"Usage:0x{d['usage_page']:04X}:0x{d['usage']:04X} "
                      f"Product:{d['product_string']}")
        sys.exit(1)

    print_device_info(info)
    h = open_device(info)
    print("  Opened.\n")

    try:
        for test_name in tests_to_run:
            h = run_test(test_name, h, args)
    except KeyboardInterrupt:
        print("\n\nInterrupted!")
        if h:
            try:
                timed_write(h, build_fwu_pkt(bytes([0x4F, 0x00, 0x00])))
            except Exception:
                pass
            try:
                timed_write(h, bytes([0x0D, 0x00]))
            except Exception:
                pass
    finally:
        if h:
            try:
                h.close()
            except Exception:
                pass
        print("\nClosed.")


if __name__ == "__main__":
    main()
