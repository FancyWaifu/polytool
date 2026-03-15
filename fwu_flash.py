#!/usr/bin/env python3
"""
FWU Flash — complete firmware update for Poly Savi 8220 via FWU API.

Protocol fully reverse-engineered from libDFUManager.dylib disassembly.
CVM API uses little-endian 16-bit primitive IDs.

Usage:
  python3 fwu_flash.py                    # re-flash current firmware
  python3 fwu_flash.py --dry-run          # protocol test, no actual flash
  python3 fwu_flash.py --file path.fwu    # flash specific FWU file
"""

import sys
import time
import struct
import signal
import argparse
import subprocess
import zipfile
import zlib
from pathlib import Path

import hid

POLY_VIDS = {0x047F, 0x0965, 0x03F0, 0x1BD7}
TARGET_USAGE_PAGE = 0xFFA2
RID_DATA = 0x03
RID_ACK = 0x05
REPORT_SIZE = 64  # including report ID byte
FRAG_PAYLOAD_FIRST = REPORT_SIZE - 3  # 61 bytes (rid + marker + len)
FRAG_PAYLOAD_CONT = REPORT_SIZE - 2   # 62 bytes (rid + marker)

# CRC-CCITT table (from libDFUManager.dylib)
CRC_TABLE = [
    0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
    0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF,
    0x1231, 0x0210, 0x3273, 0x2252, 0x52B5, 0x4294, 0x72F7, 0x62D6,
    0x9339, 0x8318, 0xB37B, 0xA35A, 0xD3BD, 0xC39C, 0xF3FF, 0xE3DE,
    0x2462, 0x3443, 0x0420, 0x1401, 0x64E6, 0x74C7, 0x44A4, 0x5485,
    0xA56A, 0xB54B, 0x8528, 0x9509, 0xE5EE, 0xF5CF, 0xC5AC, 0xD58D,
    0x3653, 0x2672, 0x1611, 0x0630, 0x76D7, 0x66F6, 0x5695, 0x46B4,
    0xB75B, 0xA77A, 0x9719, 0x8738, 0xF7DF, 0xE7FE, 0xD79D, 0xC7BC,
    0x48C4, 0x58E5, 0x6886, 0x78A7, 0x0840, 0x1861, 0x2802, 0x3823,
    0xC9CC, 0xD9ED, 0xE98E, 0xF9AF, 0x8948, 0x9969, 0xA90A, 0xB92B,
    0x5AF5, 0x4AD4, 0x7AB7, 0x6A96, 0x1A71, 0x0A50, 0x3A33, 0x2A12,
    0xDBFD, 0xCBDC, 0xFBBF, 0xEB9E, 0x9B79, 0x8B58, 0xBB3B, 0xAB1A,
    0x6CA6, 0x7C87, 0x4CE4, 0x5CC5, 0x2C22, 0x3C03, 0x0C60, 0x1C41,
    0xEDAE, 0xFD8F, 0xCDEC, 0xDDCD, 0xAD2A, 0xBD0B, 0x8D68, 0x9D49,
    0x7E97, 0x6EB6, 0x5ED5, 0x4EF4, 0x3E13, 0x2E32, 0x1E51, 0x0E70,
    0xFF9F, 0xEFBE, 0xDFDD, 0xCFFC, 0xBF1B, 0xAF3A, 0x9F59, 0x8F78,
    0x9188, 0x81A9, 0xB1CA, 0xA1EB, 0xD10C, 0xC12D, 0xF14E, 0xE16F,
    0x1080, 0x00A1, 0x30C2, 0x20E3, 0x5004, 0x4025, 0x7046, 0x6067,
    0x83B9, 0x9398, 0xA3FB, 0xB3DA, 0xC33D, 0xD31C, 0xE37F, 0xF35E,
    0x02B1, 0x1290, 0x22F3, 0x32D2, 0x4235, 0x5214, 0x6277, 0x7256,
    0xB5EA, 0xA5CB, 0x95A8, 0x8589, 0xF56E, 0xE54F, 0xD52C, 0xC50D,
    0x34E2, 0x24C3, 0x14A0, 0x0481, 0x7466, 0x6447, 0x5424, 0x4405,
    0xA7DB, 0xB7FA, 0x8799, 0x97B8, 0xE75F, 0xF77E, 0xC71D, 0xD73C,
    0x26D3, 0x36F2, 0x0691, 0x16B0, 0x6657, 0x7676, 0x4615, 0x5634,
    0xD94C, 0xC96D, 0xF90E, 0xE92F, 0x99C8, 0x89E9, 0xB98A, 0xA9AB,
    0x5844, 0x4865, 0x7806, 0x6827, 0x18C0, 0x08E1, 0x3882, 0x28A3,
    0xCB7D, 0xDB5C, 0xEB3F, 0xFB1E, 0x8BF9, 0x9BD8, 0xABBB, 0xBB9A,
    0x4A75, 0x5A54, 0x6A37, 0x7A16, 0x0AF1, 0x1AD0, 0x2AB3, 0x3A92,
    0xFD2E, 0xED0F, 0xDD6C, 0xCD4D, 0xBDAA, 0xAD8B, 0x9DE8, 0x8DC9,
    0x7C26, 0x6C07, 0x5C64, 0x4C45, 0x3CA2, 0x2C83, 0x1CE0, 0x0CC1,
    0xEF1F, 0xFF3E, 0xCF5D, 0xDF7C, 0xAF9B, 0xBFBA, 0x8FD9, 0x9FF8,
    0x6E17, 0x7E36, 0x4E55, 0x5E74, 0x2E93, 0x3EB2, 0x0ED1, 0x1EF0,
]


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        idx = (crc ^ b) & 0xFF
        crc = CRC_TABLE[idx] ^ (crc >> 8)
    return crc & 0xFFFF


def crc32x(data: bytes) -> int:
    """CRC32 without final XOR (Poly variant of standard CRC-32)."""
    return zlib.crc32(data) ^ 0xFFFFFFFF


def hexline(data, n=20):
    return ' '.join(f'{b:02X}' for b in data[:n])


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
}


class FwuFile:
    """Parse a .fwu firmware file."""

    def __init__(self, path_or_data):
        if isinstance(path_or_data, (str, Path)):
            self.data = Path(path_or_data).read_bytes()
        else:
            self.data = bytes(path_or_data)

        if self.data[:3] != b'FWU':
            raise ValueError("Not a valid FWU file (missing magic)")

        h = self.data[:64]
        self.version = h[3]
        self.device_id = struct.unpack_from('<I', h, 0x10)[0]
        self.link_date = h[0x14:0x19]
        self.range_start = struct.unpack_from('<I', h, 0x24)[0]
        self.range_size = struct.unpack_from('<I', h, 0x28)[0]

        # Verify header CRC
        stored_crc = struct.unpack_from('<H', h, 0x3C)[0]
        calc_crc = crc16(h[:0x3C])
        if stored_crc != calc_crc:
            raise ValueError(f"Header CRC mismatch: stored=0x{stored_crc:04X} calc=0x{calc_crc:04X}")

        # Data starts after the 64-byte header
        self.header_size = 64

    def get_block(self, addr, size):
        """Read a block from the file mapped to flash address."""
        offset = (addr - self.range_start) + self.header_size
        end = offset + size
        if offset < self.header_size:
            return b'\xFF' * size
        if offset >= len(self.data):
            return b'\xFF' * size
        chunk = self.data[offset:end]
        if len(chunk) < size:
            chunk += b'\xFF' * (size - len(chunk))
        return chunk

    def get_crc(self, addr, size):
        """Calculate CRC16 of a block at flash address."""
        block = self.get_block(addr, size)
        return crc16(block)


class FwuFlasher:
    """FWU API protocol implementation for firmware flashing."""

    def __init__(self, dry_run=False):
        self.h = None
        self.dry_run = dry_run
        self.ack_toggle = 0
        self.blocks_sent = 0
        self.bytes_sent = 0
        self.total_bytes = 0

    def find_device(self):
        for d in hid.enumerate():
            if d["vendor_id"] in POLY_VIDS and d["usage_page"] == TARGET_USAGE_PAGE:
                return d
        return None

    def open(self, info):
        self.h = hid.device()
        self.h.open_path(info["path"])
        self.h.set_nonblocking(1)
        # Drain
        while self.h.read(256):
            pass

    def close(self):
        if self.h:
            try:
                self.h.close()
            except Exception:
                pass
            self.h = None

    def timed_write(self, data, timeout=5):
        def handler(signum, frame):
            raise TimeoutError()
        old = signal.signal(signal.SIGALRM, handler)
        signal.alarm(timeout)
        try:
            self.h.write(data)
            signal.alarm(0)
            return True
        except TimeoutError:
            return False
        except Exception as e:
            signal.alarm(0)
            print(f"  Write error: {e}")
            return False
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def build_cvm_msg(self, prim_id, params=b''):
        """Build a CVM API mail."""
        return struct.pack('<H', prim_id) + params

    def fragment_and_send(self, mail, timeout_ms=5000):
        """Fragment a CVM mail into HID reports and send with ACK gating.
        Returns list of any RID 3 messages received during transfer."""
        responses = []

        # Build fragment list
        fragments = []
        remaining = mail
        is_first = True

        while remaining:
            pkt = bytearray(REPORT_SIZE)
            pkt[0] = RID_DATA

            if is_first:
                pkt[1] = 0x20  # FRAG_START
                pkt[2] = len(mail)  # total payload length
                chunk_size = min(len(remaining), FRAG_PAYLOAD_FIRST)
                pkt[3:3+chunk_size] = remaining[:chunk_size]
                remaining = remaining[chunk_size:]
                is_first = False
            else:
                pkt[1] = 0x80  # FRAG_CONT
                chunk_size = min(len(remaining), FRAG_PAYLOAD_CONT)
                pkt[2:2+chunk_size] = remaining[:chunk_size]
                remaining = remaining[chunk_size:]

            fragments.append(bytes(pkt))

        # Send each fragment, wait for ACK between them
        for i, frag in enumerate(fragments):
            if not self.timed_write(frag):
                print(f"  Fragment {i}/{len(fragments)} BLOCKED!")
                return responses

            # Wait for ACK and collect any responses
            deadline = time.time() + timeout_ms / 1000
            got_ack = False
            while time.time() < deadline:
                try:
                    data = self.h.read(512)
                except OSError:
                    break
                if not data:
                    time.sleep(0.001)
                    continue

                rid = data[0]
                if rid == RID_ACK:
                    got_ack = True
                    break
                elif rid == RID_DATA:
                    responses.append(self.decode_msg(bytes(data)))
                # Ignore other RIDs during fragment send

            if not got_ack and len(fragments) > 1:
                print(f"  No ACK for fragment {i}/{len(fragments)}")
                # Try to continue anyway

        return responses

    def decode_msg(self, raw):
        """Decode a RID 3 message into (prim_id, name, params)."""
        if isinstance(raw, list):
            raw = bytes(raw)
        if len(raw) < 4 or raw[1] != 0x20:
            return None
        plen = raw[2]
        payload = raw[3:3+plen]
        if len(payload) < 2:
            return None
        prim_id = struct.unpack_from('<H', payload, 0)[0]
        name = PRIM_NAMES.get(prim_id, f"0x{prim_id:04X}")
        return (prim_id, name, payload[2:])

    def wait_for_msg(self, expected_prim=None, timeout_ms=30000):
        """Wait for a specific FWU message (or any). Returns list of all messages received."""
        messages = []
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            try:
                data = self.h.read(512)
            except OSError:
                break
            if not data:
                time.sleep(0.003)
                continue
            rid = data[0]
            if rid == RID_DATA:
                msg = self.decode_msg(bytes(data))
                if msg:
                    messages.append(msg)
                    if expected_prim and msg[0] == expected_prim:
                        return messages
            elif rid == 0x02:
                pass  # sign-on response, ignore
            elif rid == 0x0E:
                pass  # settings, ignore
            elif rid == RID_ACK:
                pass  # stray ACK
        return messages

    def sign_on(self):
        print("  Sign on...")
        if not self.timed_write(bytes([0x0D, 0x15])):
            raise RuntimeError("Sign-on BLOCKED")
        msgs = self.wait_for_msg(timeout_ms=3000)
        time.sleep(0.3)
        while self.h.read(256):
            pass
        print("  Sign on OK")

    def sign_off(self):
        print("  Sign off...")
        self.timed_write(bytes([0x0D, 0x00]))
        time.sleep(0.5)

    def enable_fwu(self):
        print("  FWU Enable...")
        mail = self.build_cvm_msg(0x4F00, bytes([0x01, 0x07]))
        self.fragment_and_send(mail)

        # Wait for ENABLE_CFM + DEVICE_NOTIFY_IND
        messages = self.wait_for_msg(timeout_ms=20000)
        enable_ok = False
        notify = None

        for prim_id, name, params in messages:
            if prim_id == 0x4F01:  # ENABLE_CFM
                status = params[0] if params else 0xFF
                enable_ok = (status == 0)
                print(f"    ENABLE_CFM: status={'SUCCESS' if enable_ok else f'FAIL(0x{status:02X})'}")
            elif prim_id == 0x4F02:  # DEVICE_NOTIFY_IND
                if len(params) >= 10:
                    present = params[0]
                    devnr = params[1]
                    dev_id = struct.unpack_from('<I', params, 2)[0]
                    offset = struct.unpack_from('<I', params, 6)[0]
                    mode = params[10]
                    link_date = params[11:16] if len(params) >= 16 else b'\xFF' * 5
                    range_start = struct.unpack_from('<I', params, 16)[0] if len(params) >= 20 else 0
                    range_size = struct.unpack_from('<I', params, 20)[0] if len(params) >= 24 else 0
                    print(f"    DEVICE_NOTIFY_IND: present={present} devnr={devnr} "
                          f"ID=0x{dev_id:08X} mode=0x{mode:02X}")
                    print(f"      Range: 0x{range_start:X}..0x{range_start+range_size:X} "
                          f"({range_size} bytes)")
                    notify = {
                        'present': present, 'devnr': devnr, 'dev_id': dev_id,
                        'offset': offset, 'mode': mode, 'link_date': link_date,
                        'range_start': range_start, 'range_size': range_size,
                    }
            elif prim_id == 0x4F0C:  # STATUS_IND
                print(f"    STATUS_IND: {hexline(params)}")

        if not enable_ok:
            raise RuntimeError("FWU Enable failed")
        if not notify or not notify['present']:
            raise RuntimeError("No device reported by DEVICE_NOTIFY_IND")

        return notify

    def start_update(self, devnr, mode=0x00):
        print(f"  UPDATE_REQ (dev={devnr}, mode=0x{mode:02X})...")
        mail = self.build_cvm_msg(0x4F03, bytes([devnr, mode]))
        self.fragment_and_send(mail)

        # Wait for UPDATE_CFM + UPDATE_IND
        messages = self.wait_for_msg(timeout_ms=10000)
        update_info = None

        for prim_id, name, params in messages:
            if prim_id == 0x4F04:  # UPDATE_CFM
                status = params[1] if len(params) > 1 else params[0] if params else 0xFF
                print(f"    UPDATE_CFM: status=0x{status:02X}")
            elif prim_id == 0x4F05:  # UPDATE_IND
                if len(params) >= 6:
                    devnr_resp = params[0]
                    dev_id = struct.unpack_from('<I', params, 1)[0]
                    offset = struct.unpack_from('<I', params, 5)[0]
                    mode_resp = params[9] if len(params) > 9 else 0
                    link_date = params[10:15] if len(params) >= 15 else b'\xFF' * 5
                    range_start = struct.unpack_from('<I', params, 15)[0] if len(params) >= 19 else 0
                    range_size = struct.unpack_from('<I', params, 19)[0] if len(params) >= 23 else 0
                    print(f"    UPDATE_IND: devnr={devnr_resp} ID=0x{dev_id:08X} "
                          f"mode=0x{mode_resp:02X}")
                    print(f"      LinkDate: {hexline(link_date, 5)}")
                    print(f"      Range: 0x{range_start:X}..0x{range_start+range_size:X}")
                    update_info = {
                        'devnr': devnr_resp, 'dev_id': dev_id, 'offset': offset,
                        'mode': mode_resp, 'link_date': link_date,
                        'range_start': range_start, 'range_size': range_size,
                    }
            elif prim_id == 0x4F0C:  # STATUS_IND
                print(f"    STATUS_IND: {hexline(params)}")

        return update_info

    def send_update_res(self, devnr, fwu_file, ctx=1):
        """Send UPDATE_RES with file info to start the block transfer."""
        print(f"  UPDATE_RES (ctx=0x{ctx:X}, range=0x{fwu_file.range_start:X}:0x{fwu_file.range_size:X})...")
        params = bytes([devnr])
        params += fwu_file.link_date[:4]  # first 4 bytes of link date
        params += fwu_file.link_date[4:5]  # 5th byte
        params += struct.pack('<I', ctx)
        params += struct.pack('<I', 1)  # range_count = 1
        params += struct.pack('<II', fwu_file.range_start, fwu_file.range_size)
        mail = self.build_cvm_msg(0x4F06, params)
        return self.fragment_and_send(mail)

    def handle_block_request(self, params, fwu_file, ctx):
        """Handle GET_BLOCK_IND by sending GET_BLOCK_RES."""
        devnr = params[0]
        req_ctx = struct.unpack_from('<I', params, 1)[0]
        addr = struct.unpack_from('<I', params, 5)[0]
        size = struct.unpack_from('<I', params, 9)[0]

        # Read data from FWU file
        block_data = fwu_file.get_block(addr, size)

        # Build GET_BLOCK_RES
        resp_params = bytes([devnr])
        resp_params += struct.pack('<III', req_ctx, addr, size)
        resp_params += block_data
        mail = self.build_cvm_msg(0x4F08, resp_params)

        self.blocks_sent += 1
        self.bytes_sent += size

        return self.fragment_and_send(mail)

    def handle_crc_request(self, params, fwu_file, ctx):
        """Handle GET_CRC_IND by sending GET_CRC_RES."""
        devnr = params[0]
        req_ctx = struct.unpack_from('<I', params, 1)[0]
        addr = struct.unpack_from('<I', params, 5)[0]
        size = struct.unpack_from('<I', params, 9)[0]

        calc_crc = fwu_file.get_crc(addr, size)
        print(f"    CRC @ 0x{addr:X} ({size}B): 0x{calc_crc:04X}")

        resp_params = bytes([devnr])
        resp_params += struct.pack('<III', req_ctx, addr, size)
        resp_params += struct.pack('<H', calc_crc)
        mail = self.build_cvm_msg(0x4F0A, resp_params)
        return self.fragment_and_send(mail)

    def handle_crc32_ind(self, params, fwu_file):
        """Handle CRC32_IND by computing CRC32 for requested blocks."""
        devnr = params[0]
        ctx = struct.unpack_from('<I', params, 1)[0]
        adr = struct.unpack_from('<I', params, 5)[0]
        n_sizes = params[9]

        sizes = []
        for i in range(n_sizes):
            off = 10 + i * 2
            if off + 2 <= len(params):
                sizes.append(struct.unpack_from('<H', params, off)[0])

        # Compute CRC32 for sequential blocks
        # Adr is relative to the declared range_start
        crcs = []
        running_offset = adr
        batch_bytes = 0

        for block_size in sizes:
            abs_addr = fwu_file.range_start + running_offset
            block_data = fwu_file.get_block(abs_addr, block_size)
            crc = crc32x(block_data)
            crcs.append((block_size, crc & 0xFFFFFFFF))
            running_offset += block_size
            batch_bytes += block_size

        self.bytes_sent += batch_bytes
        pct = (self.bytes_sent / self.total_bytes * 100) if self.total_bytes else 0
        print(f"\r  CRC32 verify: {len(crcs)} blocks, {batch_bytes}B @ 0x{adr:X} "
              f"[{pct:.1f}% {self.bytes_sent}/{self.total_bytes}]", end='', flush=True)

        # Build CRC32_RES (0x4F15)
        resp_params = bytes([devnr])
        resp_params += struct.pack('<I', ctx)
        resp_params += struct.pack('<I', adr)
        resp_params += bytes([len(crcs)])
        for block_size, crc in crcs:
            resp_params += struct.pack('<H', block_size)
            resp_params += struct.pack('<I', crc)

        mail = self.build_cvm_msg(0x4F15, resp_params)
        return self.fragment_and_send(mail)

    def disable_fwu(self):
        print("  FWU Disable...")
        mail = self.build_cvm_msg(0x4F00, bytes([0x00, 0x07]))
        self.fragment_and_send(mail)
        self.wait_for_msg(expected_prim=0x4F01, timeout_ms=5000)

    def flash(self, fwu_file: FwuFile):
        """Execute the complete FWU flash protocol."""
        ctx = 1  # host-assigned context ID
        self.total_bytes = fwu_file.range_size

        # Step 1: Sign on
        self.sign_on()

        try:
            # Step 2: Enable FWU
            notify = self.enable_fwu()

            # Verify device ID matches
            if notify['dev_id'] != fwu_file.device_id:
                print(f"  WARNING: Device ID mismatch! Device=0x{notify['dev_id']:08X} "
                      f"File=0x{fwu_file.device_id:08X}")
                if not self.dry_run:
                    raise RuntimeError("Device ID mismatch — aborting for safety")

            # Step 3: UPDATE_REQ
            update_info = self.start_update(notify['devnr'], mode=0x00)
            if not update_info:
                raise RuntimeError("No UPDATE_IND received")

            if self.dry_run:
                print(f"\n  *** DRY RUN COMPLETE ***")
                print(f"  Protocol handshake successful — device is ready for firmware transfer.")
                print(f"  File: {fwu_file.range_size} bytes at 0x{fwu_file.range_start:X}")
                return

            # Step 4: Send UPDATE_RES to start block transfer
            self.send_update_res(notify['devnr'], fwu_file, ctx)

            # Step 5: Block transfer loop
            print(f"\n  Starting block transfer ({fwu_file.range_size} bytes)...")
            start_time = time.time()
            complete = False

            read_errors = 0
            while not complete:
                # Read next message from device
                try:
                    data = self.h.read(512)
                    read_errors = 0
                except OSError:
                    read_errors += 1
                    if read_errors <= 3:
                        print(f"\n  Read error — device may be resetting ({read_errors})")
                    if read_errors > 10:
                        raise RuntimeError("Device disconnected (too many read errors)")
                    time.sleep(2)
                    continue

                if not data:
                    time.sleep(0.001)
                    continue

                rid = data[0]
                if rid != RID_DATA:
                    continue

                msg = self.decode_msg(bytes(data))
                if not msg:
                    continue

                prim_id, name, params = msg

                if prim_id == 0x4F07:  # GET_BLOCK_IND
                    addr = struct.unpack_from('<I', params, 5)[0]
                    size = struct.unpack_from('<I', params, 9)[0]
                    pct = (self.bytes_sent / self.total_bytes * 100) if self.total_bytes else 0
                    elapsed = time.time() - start_time
                    print(f"\r  Block {self.blocks_sent}: 0x{addr:X} ({size}B) "
                          f"[{pct:.1f}% {self.bytes_sent}/{self.total_bytes} "
                          f"{elapsed:.0f}s]", end='', flush=True)
                    responses = self.handle_block_request(params, fwu_file, ctx)
                    # Process any responses received during block send
                    for resp in (responses or []):
                        if resp and resp[0] == 0x4F0B:
                            complete = True

                elif prim_id == 0x4F14:  # CRC32_IND
                    responses = self.handle_crc32_ind(params, fwu_file)
                    for resp in (responses or []):
                        if resp and resp[0] == 0x4F0B:
                            complete = True

                elif prim_id == 0x4F09:  # GET_CRC_IND
                    print()  # newline after progress
                    self.handle_crc_request(params, fwu_file, ctx)

                elif prim_id == 0x4F0B:  # COMPLETE_IND
                    print()
                    complete_ctx = struct.unpack_from('<I', params, 0)[0] if len(params) >= 4 else 0
                    print(f"  COMPLETE_IND! ctx=0x{complete_ctx:X}")
                    complete = True

                elif prim_id == 0x4F0C:  # STATUS_IND
                    pass  # normal during transfer

                elif prim_id == 0x4F16:  # PROGRESS_IND
                    pass

                else:
                    print(f"\n  Unexpected: {name} {hexline(params)}")

            elapsed = time.time() - start_time
            print(f"\n  Transfer complete! {self.bytes_sent} bytes in {elapsed:.1f}s "
                  f"({self.bytes_sent/elapsed/1024:.1f} KB/s)")

        finally:
            # Always try to clean up
            try:
                self.disable_fwu()
            except Exception as e:
                print(f"  Disable error: {e}")
            self.sign_off()


def find_fwu_file():
    """Find the headset firmware file in the cached zip."""
    cache_dir = Path.home() / ".polytool" / "firmware_cache"
    for zp in cache_dir.glob("*ACFF*.zip"):
        try:
            z = zipfile.ZipFile(zp)
            for name in z.namelist():
                if name.endswith('.fwu') and 'Puffin' in name:
                    return z.read(name), name
        except Exception:
            continue
    return None, None


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
    parser = argparse.ArgumentParser(description="FWU Flash for Poly Savi 8220")
    parser.add_argument("--dry-run", action="store_true", help="Protocol test only, no actual flash")
    parser.add_argument("--file", type=str, help="Path to .fwu file")
    parser.add_argument("--no-kill", action="store_true", help="Don't kill Poly Lens")
    args = parser.parse_args()

    # Load firmware file
    if args.file:
        fwu_data = Path(args.file).read_bytes()
        fwu_name = args.file
    else:
        fwu_data, fwu_name = find_fwu_file()
        if not fwu_data:
            print("No firmware file found. Download first with 'polytool.py updates'")
            sys.exit(1)

    fwu = FwuFile(fwu_data)
    print(f"Firmware: {fwu_name}")
    print(f"  Device ID: 0x{fwu.device_id:08X}")
    print(f"  Link date: {hexline(fwu.link_date, 5)}")
    print(f"  Flash range: 0x{fwu.range_start:X}..0x{fwu.range_start+fwu.range_size:X} "
          f"({fwu.range_size} bytes)")
    print(f"  File data: {len(fwu.data) - fwu.header_size} bytes")

    if args.dry_run:
        print("\n*** DRY RUN — will not send firmware data ***\n")

    # Kill Poly Lens (all processes)
    if not args.no_kill:
        print("\nKilling Poly Lens...")
        for proc in ["legacyhost", "LensService", "PolyLauncher",
                      "Poly Studio", "CallControlApp"]:
            subprocess.run(["pkill", "-9", "-f", proc], capture_output=True)
        time.sleep(2)

    # USB reset for clean state
    print("USB reset...")
    usb_reset()

    # Find device
    flasher = FwuFlasher(dry_run=args.dry_run)
    info = flasher.find_device()
    if not info:
        print("No Poly device found.")
        sys.exit(1)

    print(f"\nDevice: {info['product_string']} "
          f"(0x{info['vendor_id']:04X}:0x{info['product_id']:04X})")

    flasher.open(info)
    print("  Opened.\n")

    try:
        flasher.flash(fwu)
        print("\nFirmware update completed successfully!")
    except KeyboardInterrupt:
        print("\n\nAborted by user!")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
    finally:
        flasher.close()
        print("Closed.")


if __name__ == "__main__":
    main()
