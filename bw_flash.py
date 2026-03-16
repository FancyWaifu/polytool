#!/usr/bin/env python3
"""
Blackwire Flash — firmware update for Poly Blackwire 3220 via Conexant EEPROM patching.

Protocol reverse-engineered from libDFUManager.dylib (ConexantDFU).
Device is CX2070x (codename "CHAN_"), uses HID RID 4 (out) / RID 5 (in) on Usage Page 0xFFA0.

Commands: read=0x20, write=0x60 (0x40|0x20). Write-enable via register 0x1000 bit 7.

Usage:
  python3 bw_flash.py                   # flash downloaded firmware
  python3 bw_flash.py --verify-only     # read EEPROM and compare, no write
  python3 bw_flash.py --file path.ptc   # flash specific PTC file
"""

import sys
import time
import argparse
import subprocess
import zipfile
from pathlib import Path

import hid

POLY_VID = 0x047F
BW3220_PID = 0xC056
USAGE_PAGE = 0xFFA0

# Command byte = write_bit(0x40) | eeprom_select(0x20)
CMD_EEPROM_READ = 0x20
CMD_EEPROM_WRITE = 0x60   # 0x40 | 0x20
CMD_REG_READ = 0x00
CMD_REG_WRITE = 0x40    # WARNING: writing wrong registers can wedge the device!
RID_OUT = 0x04
MAX_CHUNK = 30  # max bytes per HID transfer
WRITE_ENABLE_REG = 0x1000  # CX2070x EEPROM write-enable register (bit 7)


def parse_srecords(data):
    """Parse S-record text into (address, data_bytes) records."""
    if isinstance(data, bytes):
        data = data.decode('ascii')
    records = []
    for line in data.strip().splitlines():
        if not line.startswith('S3'):
            continue
        byte_count = int(line[2:4], 16)
        addr = int(line[4:12], 16)
        data_len = byte_count - 5  # minus 4 addr bytes and 1 checksum
        data_hex = line[12:12 + data_len * 2]
        record_data = bytes.fromhex(data_hex)
        # Verify checksum
        raw = bytes.fromhex(line[2:])
        if (sum(raw) & 0xFF) != 0xFF:
            print(f"  WARNING: bad checksum at S3 addr 0x{addr:04X}")
        records.append((addr, record_data))
    return records


def find_ptc_file():
    """Find the PTC firmware file in the cached zip."""
    cache_dir = Path.home() / ".polytool" / "firmware_cache"
    for zp in cache_dir.glob("*C056*.zip"):
        try:
            z = zipfile.ZipFile(zp)
            for name in z.namelist():
                if name.endswith('.ptc'):
                    return z.read(name), name
        except Exception:
            continue
    return None, None


class BlackwireFlasher:
    def __init__(self):
        self.h = None

    def find_device(self):
        for d in hid.enumerate():
            if d["vendor_id"] == POLY_VID and d["product_id"] == BW3220_PID \
               and d["usage_page"] == USAGE_PAGE:
                return d
        return None

    def open(self, info):
        self.h = hid.device()
        self.h.open_path(info["path"])
        self.h.set_nonblocking(0)

    def close(self):
        if self.h:
            try:
                self.h.close()
            except Exception:
                pass
            self.h = None

    # --- Low-level memory access ---

    def _mem_read(self, cmd, addr, length):
        """Read `length` bytes from memory at `addr` using given command byte."""
        pkt = [RID_OUT, cmd, length, (addr >> 8) & 0xFF, addr & 0xFF] + [0x00] * 32
        self.h.write(pkt)
        resp = self.h.read(64, timeout_ms=2000)
        if not resp or len(resp) < 1 + length:
            return None
        return bytes(resp[1:1 + length])

    def _mem_write(self, cmd, addr, data):
        """Write `data` bytes to memory at `addr` using given command byte."""
        length = len(data)
        pkt = [RID_OUT, cmd, length, (addr >> 8) & 0xFF, addr & 0xFF]
        pkt += list(data)
        pkt += [0x00] * (37 - len(pkt))  # pad to output report size
        self.h.write(pkt)
        # EEPROM writes (cmd=0x60) don't send a response
        time.sleep(0.01)
        return True

    def read_eeprom(self, addr, length):
        return self._mem_read(CMD_EEPROM_READ, addr, length)

    def write_eeprom(self, addr, data):
        return self._mem_write(CMD_EEPROM_WRITE, addr, data)

    def read_reg(self, addr, length=1):
        return self._mem_read(CMD_REG_READ, addr, length)

    def write_reg(self, addr, data):
        return self._mem_write(CMD_REG_WRITE, addr, data)

    # --- EEPROM write enable ---

    def enable_eeprom_writes(self):
        """Set CX2070x register 0x1000 bit 7 to enable EEPROM writes."""
        cur = self.read_reg(WRITE_ENABLE_REG, 1)
        if cur is None:
            print("  Warning: could not read write-enable register, proceeding anyway")
            return
        val = cur[0] | 0x80
        self.write_reg(WRITE_ENABLE_REG, bytes([val]))
        time.sleep(0.002)  # 1.25ms per Poly Lens code

    # --- Flash operations ---

    def verify_block(self, addr, expected):
        actual = self.read_eeprom(addr, len(expected))
        if actual is None:
            return False
        return actual == expected

    def flash(self, records, verify_only=False):
        """Flash all PTC records to EEPROM.

        Automatically preserves device-unique data (serial number, calibration)
        that the firmware PTC file would otherwise overwrite with generic values.
        """
        from device_identity import backup_device_identity, restore_device_identity

        total_bytes = sum(len(d) for _, d in records)
        written = 0
        mismatches = 0
        skipped = 0

        if not verify_only:
            # Back up device-unique regions BEFORE enabling writes
            print("  Backing up device identity...")
            identity_backup = backup_device_identity(self.h, "cx2070x")

            print("  Enabling EEPROM writes (reg 0x1000 bit 7)...")
            self.enable_eeprom_writes()

        print(f"  {len(records)} records, {total_bytes} bytes total\n")

        start_time = time.time()

        for i, (addr, data) in enumerate(records):
            if verify_only:
                for offset in range(0, len(data), MAX_CHUNK):
                    chunk = data[offset:offset + MAX_CHUNK]
                    actual = self.read_eeprom(addr + offset, len(chunk))
                    if actual != chunk:
                        mismatches += len(chunk)
                    else:
                        skipped += len(chunk)
                written += len(data)
            else:
                for offset in range(0, len(data), MAX_CHUNK):
                    chunk = data[offset:offset + MAX_CHUNK]
                    chunk_addr = addr + offset

                    # Check if already correct
                    actual = self.read_eeprom(chunk_addr, len(chunk))
                    if actual == chunk:
                        skipped += len(chunk)
                        written += len(chunk)
                        continue

                    # Write
                    if not self.write_eeprom(chunk_addr, chunk):
                        print(f"\n  Write failed at 0x{chunk_addr:04X}")
                        return False

                    # Verify
                    time.sleep(0.01)
                    if not self.verify_block(chunk_addr, chunk):
                        print(f"\n  Verify failed at 0x{chunk_addr:04X}")
                        mismatches += len(chunk)
                    else:
                        written += len(chunk)

            # Progress
            pct = written / total_bytes * 100
            elapsed = time.time() - start_time
            rate = written / elapsed if elapsed > 0 else 0
            action = "Verified" if verify_only else "Flashed"
            print(f"\r  {action}: {written}/{total_bytes} ({pct:.0f}%) "
                  f"skip={skipped} err={mismatches} "
                  f"[{elapsed:.0f}s {rate:.0f} B/s]", end='', flush=True)

        print()
        elapsed = time.time() - start_time

        if verify_only:
            if mismatches > 0:
                print(f"\n  Verify: {mismatches} bytes differ, {skipped} bytes match")
            else:
                print(f"\n  Verify: ALL {total_bytes} bytes match!")
        else:
            print(f"\n  Done: {written} bytes in {elapsed:.1f}s, "
                  f"{skipped} skipped (already correct), {mismatches} verify errors")

            # Restore device-unique data that was overwritten by the PTC
            print("\n  Restoring device identity...")
            restore_device_identity(self.h, "cx2070x", identity_backup)
            print("  Device identity restored.")

        return mismatches == 0


def main():
    parser = argparse.ArgumentParser(description="Blackwire 3220 Firmware Flash")
    parser.add_argument("--verify-only", action="store_true", help="Compare only, don't write")
    parser.add_argument("--file", type=str, help="Path to .ptc file")
    parser.add_argument("--no-kill", action="store_true", help="Don't kill Poly Lens")
    args = parser.parse_args()

    # Load firmware
    if args.file:
        ptc_data = Path(args.file).read_bytes()
        ptc_name = args.file
    else:
        ptc_data, ptc_name = find_ptc_file()
        if not ptc_data:
            print("No firmware file found. Download first with 'polytool.py updates'")
            sys.exit(1)

    records = parse_srecords(ptc_data)
    total_bytes = sum(len(d) for _, d in records)
    addr_range = (min(a for a, _ in records), max(a + len(d) for a, d in records))
    print(f"Firmware: {ptc_name}")
    print(f"  Records: {len(records)}, Data: {total_bytes} bytes")
    print(f"  Address range: 0x{addr_range[0]:04X}..0x{addr_range[1]:04X}")

    if args.verify_only:
        print("\n*** VERIFY ONLY — no writes ***\n")

    # Kill Poly Lens
    if not args.no_kill:
        print("\nKilling Poly Lens...")
        for proc in ["legacyhost", "LensService", "PolyLauncher"]:
            subprocess.run(["pkill", "-f", proc], capture_output=True)
        time.sleep(1)

    # Find and open device
    flasher = BlackwireFlasher()
    info = flasher.find_device()
    if not info:
        print("No Blackwire 3220 found.")
        sys.exit(1)

    print(f"\nDevice: {info['product_string']} "
          f"(0x{info['vendor_id']:04X}:0x{info['product_id']:04X})")

    flasher.open(info)
    print("  Opened.\n")

    try:
        ok = flasher.flash(records, verify_only=args.verify_only)
        if ok:
            print("\nFirmware update completed successfully!")
        else:
            print("\nFirmware update completed with errors.")
            sys.exit(1)
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
