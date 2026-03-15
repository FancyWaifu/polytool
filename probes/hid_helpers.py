#!/usr/bin/env python3
"""
Shared HID/FWU utilities for Poly device probing.

Extracted from the various probe scripts used to reverse-engineer
the Savi 8220 (W8220T) and other Poly device protocols.
"""

import sys
import time
import signal
import struct
import subprocess
import hid

# ── Constants ────────────────────────────────────────────────────────────────

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
TARGET_USAGE_PAGE = 0xFFA2

FRAG_START = 0x20
FRAG_CONT = 0x80
RID_DATA = 0x03
RID_ACK = 0x05

# CVM API Primitive IDs (little-endian 16-bit on wire)
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

# Legacy name mapping (0x4Fxx low byte only, used by older framing)
FWU_MSG_NAMES = {
    0x00: "ENABLE_REQ", 0x01: "ENABLE_CFM", 0x02: "DEVICE_NOTIFY_IND",
    0x03: "UPDATE_REQ", 0x04: "UPDATE_CFM", 0x05: "UPDATE_IND",
    0x06: "UPDATE_RES", 0x07: "GET_BLOCK_IND", 0x08: "GET_BLOCK_RES",
    0x09: "GET_CRC_IND", 0x0A: "GET_CRC_RES", 0x0B: "COMPLETE_IND",
    0x0C: "STATUS_IND", 0x0D: "MULTI_CRC_IND", 0x0E: "MULTI_CRC_RES",
    0x0F: "CRC32_IND", 0x10: "CRC32_RES", 0x11: "PROGRESS_IND",
    0x12: "PLT_MSG", 0x13: "PLT_IND",
}

FWU_MODES = {
    0xFF: "Up-to-date",
    0x00: "FWU",
    0x01: "Main",
    0x10: "FWU,Aux",
    0x11: "Main,Aux",
}

# API constants
API_FWU_ENABLE_REQ = 0x4F00
API_FWU_ENABLE_CFM = 0x4F01
API_FWU_DEVICE_NOTIFY_IND = 0x4F02
API_FWU_UPDATE_REQ = 0x4F03
API_FWU_STATUS_IND = 0x4F0C
API_FWU_GET_BLOCK_IND = 0x4F07
API_FWU_UPDATE_IND = 0x4F05
API_FWS_INIT_REQ = 0x4F81


# ── Display ──────────────────────────────────────────────────────────────────

def hexline(data):
    """One-line hex string."""
    return ' '.join(f'{b:02X}' for b in data)


def hexdump(data, prefix="    "):
    """Multi-line hex dump with ASCII."""
    if not data:
        print(f"{prefix}(empty)")
        return
    for i in range(0, len(data), 16):
        hex_part = " ".join(f"{b:02X}" for b in data[i:i+16])
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data[i:i+16])
        print(f"{prefix}{i:04X}: {hex_part:<48s} {asc_part}")


# ── Device Discovery ─────────────────────────────────────────────────────────

def find_device(usage_page=TARGET_USAGE_PAGE):
    """Find a Poly device, preferring the given usage page with fallback."""
    if usage_page is not None:
        for d in hid.enumerate():
            if d["vendor_id"] in POLY_VIDS and d["usage_page"] == usage_page:
                return d
    for d in hid.enumerate():
        if d["vendor_id"] in POLY_VIDS:
            return d
    return None


def find_all_poly_interfaces():
    """Find ALL HID interfaces for ALL Poly devices."""
    return [d for d in hid.enumerate() if d["vendor_id"] in POLY_VIDS]


def open_device(info):
    """Open a HID device from its enumeration info."""
    h = hid.device()
    h.open_path(info["path"])
    return h


def print_device_info(info):
    """Print device identification."""
    print(f"Device: {info['product_string']} "
          f"(0x{info['vendor_id']:04X}:0x{info['product_id']:04X})")
    print(f"  Usage:  0x{info['usage_page']:04X}:0x{info['usage']:04X}")
    if info.get('serial_number'):
        print(f"  Serial: {info['serial_number']}")


# ── I/O with Timeout ─────────────────────────────────────────────────────────

def timed_write(h, data, timeout=5):
    """Write output report with SIGALRM timeout (macOS HID blocks forever
    on unsupported report IDs)."""
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


def timed_feature_write(h, data, timeout=3):
    """send_feature_report with SIGALRM timeout."""
    def handler(signum, frame):
        raise TimeoutError()
    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout)
    try:
        h.send_feature_report(data)
        signal.alarm(0)
        return True
    except TimeoutError:
        return False
    except Exception as e:
        signal.alarm(0)
        print(f"    Feature write error: {e}")
        return False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def drain(h):
    """Read and discard all pending input reports."""
    h.set_nonblocking(1)
    count = 0
    while h.read(256):
        count += 1
    return count


# ── Packet Building ──────────────────────────────────────────────────────────

def build_fwu_pkt(payload, report_size=64):
    """Build a 0x20-framed FWU packet (old-style [0x4F, cmd, ...] payload).

    Format: [RID=3] [0x20] [len] [payload...] [padding]
    """
    pkt = bytearray(report_size)
    pkt[0] = RID_DATA
    pkt[1] = FRAG_START
    pkt[2] = len(payload)
    pkt[3:3+len(payload)] = payload
    return bytes(pkt)


def build_fwu_msg(primitive_id, *params, report_size=64):
    """Build a properly framed FWU message with correct LE byte ordering.

    Key finding from libDFUManager.dylib RE:
      CVM API primitives use little-endian 16-bit IDs on wire.
      0x4F00 (ENABLE_REQ) → wire bytes [0x00, 0x4F] not [0x4F, 0x00]

    Format: [RID=3] [0x20] [PayloadLen] [prim_id LE16] [params...] [padding]
    """
    mail = struct.pack('<H', primitive_id) + bytes(params)
    pkt = bytearray(report_size)
    pkt[0] = RID_DATA
    pkt[1] = FRAG_START
    pkt[2] = len(mail)
    pkt[3:3+len(mail)] = mail
    return bytes(pkt)


def build_raw_pkt(rid, raw_bytes, report_size=64):
    """Build HID report without 0x20 framing — raw payload after RID."""
    pkt = bytearray(report_size)
    pkt[0] = rid
    pkt[1:1+len(raw_bytes)] = raw_bytes
    return bytes(pkt)


# ── Message Decoding ─────────────────────────────────────────────────────────

def decode_fwu_msg(data):
    """Decode a reassembled FWU message from RID 3 input data.

    Returns (prim_id, name, params) or None.
    Uses correct LE 16-bit primitive ID decoding.
    """
    raw = bytes(data) if not isinstance(data, bytes) else data
    if len(raw) < 4 or raw[1] != FRAG_START:
        return None

    payload_len = raw[2]
    payload = raw[3:3+payload_len]

    if len(payload) < 2:
        return None

    prim_id = struct.unpack_from('<H', payload, 0)[0]
    params = payload[2:]
    name = PRIM_NAMES.get(prim_id, f"UNKNOWN_0x{prim_id:04X}")
    return prim_id, name, params


def print_fwu_details(prim_id, params):
    """Print decoded details for specific FWU message types."""
    if prim_id == API_FWU_ENABLE_CFM and len(params) >= 1:
        status = params[0]
        s = {0: "SUCCESS", 1: "BUSY", 2: "NOT_SUPPORTED"}.get(status, f"0x{status:02X}")
        print(f"           Status: {s}")

    elif prim_id == API_FWU_DEVICE_NOTIFY_IND and len(params) >= 6:
        present = params[0]
        devnr = params[1]
        dev_id = struct.unpack_from('<I', params, 2)[0]
        print(f"           Present={present} DeviceNr={devnr} ID=0x{dev_id:08X}")
        if len(params) >= 14:
            offset = struct.unpack_from('<I', params, 6)[0]
            mode = params[10]
            mode_str = FWU_MODES.get(mode, f"0x{mode:02X}")
            print(f"           Offset=0x{offset:X} Mode={mode_str}")
            if len(params) >= 16:
                link_date = params[11:16]
                print(f"           LinkDate: {hexline(link_date)}")

    elif prim_id == API_FWU_STATUS_IND and len(params) >= 1:
        print(f"           Busy={params[0]}")
        if len(params) >= 5:
            print(f"           Dev0: status=0x{params[1]:02X}, Dev1: status=0x{params[3]:02X}")

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


# ── Listening ─────────────────────────────────────────────────────────────────

def listen(h, timeout_ms=5000, quiet_rids=None):
    """Listen and decode ALL input reports.

    Returns list of (rid, data) tuples.
    Prints decoded FWU messages, sign-on responses, etc.
    """
    if quiet_rids is None:
        quiet_rids = set()
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

            if rid in quiet_rids:
                results.append((rid, bytes(data)))
                time.sleep(0.005)
                elapsed += 5
                continue

            if rid == RID_DATA:
                print(f"    {ts} *** RID 3 ({len(data)}B): {hexline(data[:20])}...")
                msg = decode_fwu_msg(data)
                if msg:
                    prim_id, name, params = msg
                    print(f"         → FWU {name} (0x{prim_id:04X}): {hexline(params)}")
                    print_fwu_details(prim_id, params)
                else:
                    print(f"         (raw payload: {hexline(data[1:min(20, len(data))])})")
            elif rid == 0x02:
                bits = data[1] if len(data) > 1 else 0
                print(f"    {ts} RID 2 (sign-on): {hexline(data)}  "
                      f"8F={bits&1} EE={(bits>>1)&1} 77={(bits>>2)&1} "
                      f"80={(bits>>3)&1} C1={(bits>>4)&1}")
            elif rid == RID_ACK:
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


# ── Sign-On / Sign-Off ────────────────────────────────────────────────────────

def do_signon(h):
    """Sign on via RID 13: [0D 15] → expect RID 2 response.

    RID 13 output format (1 byte):
      bits 0-1: Usage 0x8F (sign-on state, -2 to 1)
      bits 2-3: Usage 0x77 (mode, 1 to 2)
      bit 4:    Usage 0xF2 (trigger, 0 to 1)
    Value 0x15 = signon=1, mode=1, trigger=1
    """
    pkt = bytes([0x0D, 0x15])
    print(f"  TX sign-on: [0D 15]")
    ok = timed_write(h, pkt)
    if not ok:
        print("  Sign-on BLOCKED!")
        return False
    results = listen(h, timeout_ms=3000)
    got_rid2 = any(rid == 2 for rid, _ in results)
    print(f"  Sign-on: {'OK' if got_rid2 else 'NO RESPONSE'}")
    return got_rid2


def do_signoff(h):
    """Sign off via RID 13: [0D 00]."""
    pkt = bytes([0x0D, 0x00])
    print(f"  TX sign-off: [0D 00]")
    timed_write(h, pkt)
    listen(h, timeout_ms=1000)


# ── Process Management ────────────────────────────────────────────────────────

def kill_poly():
    """Kill all Poly Lens processes."""
    for proc in ["legacyhost", "LensService", "PolyLauncher"]:
        subprocess.run(["pkill", "-f", proc], capture_output=True)
    time.sleep(1)
    result = subprocess.run(["pgrep", "-f", "legacyhost"], capture_output=True)
    if result.stdout.strip():
        subprocess.run(["pkill", "-9", "-f", "legacyhost"], capture_output=True)
        time.sleep(1)


def is_legacyhost_running():
    """Check if legacyhost is currently running."""
    try:
        result = subprocess.run(["pgrep", "-f", "legacyhost"],
                                capture_output=True, text=True)
        return bool(result.stdout.strip())
    except Exception:
        return False


def usb_reset(vid=0x047F, pid=0xACFF):
    """Reset USB device to clear wedged state."""
    try:
        import usb.core
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev:
            dev.reset()
            time.sleep(3)
            return True
    except Exception:
        pass
    return False


# ── Feature Report Helpers ────────────────────────────────────────────────────

def read_all_features(h):
    """Read all feature reports, return dict of {rid: bytes}."""
    features = {}
    for rid in range(256):
        try:
            data = h.get_feature_report(rid, 512)
            if data and any(b != 0 for b in data):
                features[rid] = bytes(data)
        except Exception:
            pass
    return features


def print_features(features, label=""):
    """Print a feature report dict."""
    if label:
        print(f"  --- {label} ---")
    for rid, data in sorted(features.items()):
        print(f"    FR{rid:3d}: {hexline(data[:40])}"
              f"{'...' if len(data) > 40 else ''}")
