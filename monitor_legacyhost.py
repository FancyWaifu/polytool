#!/usr/bin/env python3
"""
Monitor Poly Lens legacyhost activity.

Tails the LegacyHostApp.log for HID report data (Rcv/Snd) and IPC
messages (DEVICE_REQUEST/RESPONSE, STARTDFU, etc).

Also monitors the Clockwork log and LCS log for DFU events.

Usage:
  python3 monitor_legacyhost.py [--all]
    Default: monitor legacyhost log only
    --all: monitor all three log files
"""

import os
import sys
import time
import json
import argparse
import re

if sys.platform == "win32":
    _pdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    LOG_DIR_LEGACY = os.path.join(_pdata, "Plantronics", "legacyhost", "Poly",
                                  "LegacyHostApp", "Logs")
    LOG_DIR_CLOCKWORK = os.path.join(_pdata, "Poly", "Logs", "Clockwork")
    LOG_DIR_LCS = os.path.join(_pdata, "Poly", "Logs", "Lens Control Service")
else:
    LOG_DIR_LEGACY = os.path.expanduser(
        "~/Library/Application Support/Plantronics/legacyhost/Poly/LegacyHostApp/Logs"
    )
    LOG_DIR_CLOCKWORK = os.path.expanduser(
        "~/Library/Application Support/Poly/Logs/Clockwork"
    )
    LOG_DIR_LCS = os.path.expanduser(
        "~/Library/Application Support/Poly/Logs/Lens Control Service"
    )

# Patterns we care about
INTERESTING_PATTERNS = [
    r'Rcv\(<-\)',          # Incoming HID reports
    r'Snd\(->\)',          # Outgoing HID reports
    r'STARTDFU',          # DFU start command
    r'DFU',               # Any DFU reference
    r'FWU',               # Firmware update
    r'DEVICE_REQUEST',    # IPC requests
    r'DEVICE_RESPONSE',   # IPC responses
    r'ATTACH',            # Device attach
    r'DETACH',            # Device detach
    r'HidPipe',           # HID pipe data
    r'TxReport',          # Report transmission
    r'SetReportInfo',     # Report ID configuration
    r'FwuApi',            # FWU API handler
    r'onReportData',      # Report data callback
    r'write.*report',     # Report writes
    r'feature.*report',   # Feature report ops
    r'report.*id',        # Report ID references
]

INTERESTING_RE = re.compile('|'.join(INTERESTING_PATTERNS), re.IGNORECASE)


def tail_file(path, follow=True):
    """Generator that yields new lines from a file (like tail -f)."""
    try:
        with open(path, 'r', errors='replace') as f:
            # Seek to end
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    yield line.rstrip('\n')
                elif follow:
                    time.sleep(0.1)
                else:
                    break
    except FileNotFoundError:
        print(f"  File not found: {path}")
    except KeyboardInterrupt:
        pass


def format_line(source, line):
    """Highlight interesting parts of a log line."""
    # Extract timestamp
    ts_match = re.match(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z?)', line)
    ts = ts_match.group(1)[-12:] if ts_match else ""

    # Color coding (ANSI)
    if 'Rcv(<-)' in line or 'Snd(->)' in line:
        return f"\033[1;32m[{source}] {ts} {line}\033[0m"  # Green for HID data
    elif 'DFU' in line.upper() or 'FWU' in line.upper():
        return f"\033[1;33m[{source}] {ts} {line}\033[0m"  # Yellow for DFU
    elif 'ERROR' in line or 'EROR' in line:
        return f"\033[1;31m[{source}] {ts} {line}\033[0m"  # Red for errors
    elif 'STARTDFU' in line or 'ATTACH' in line:
        return f"\033[1;36m[{source}] {ts} {line}\033[0m"  # Cyan for commands
    else:
        return f"[{source}] {ts} {line}"


def find_log_file(log_dir, pattern="*.log"):
    """Find the most recent log file in a directory."""
    if not os.path.isdir(log_dir):
        return None
    logs = []
    for f in os.listdir(log_dir):
        if f.endswith('.log'):
            full = os.path.join(log_dir, f)
            logs.append((os.path.getmtime(full), full))
    if logs:
        logs.sort(reverse=True)
        return logs[0][1]
    return None


def main():
    parser = argparse.ArgumentParser(description="Monitor Poly Lens logs")
    parser.add_argument("--all", action="store_true",
                        help="Monitor all log sources")
    parser.add_argument("--raw", action="store_true",
                        help="Show all lines, not just interesting ones")
    args = parser.parse_args()

    # Find log files
    legacy_log = find_log_file(LOG_DIR_LEGACY)
    clockwork_log = find_log_file(LOG_DIR_CLOCKWORK)
    lcs_log = find_log_file(LOG_DIR_LCS)

    print("=== Poly Lens Log Monitor ===")
    print(f"  Legacy Host: {legacy_log or 'NOT FOUND'}")
    if args.all:
        print(f"  Clockwork:   {clockwork_log or 'NOT FOUND'}")
        print(f"  LCS:         {lcs_log or 'NOT FOUND'}")
    print()
    print("Watching for: HID reports (Rcv/Snd), DFU events, IPC messages")
    print("Press Ctrl+C to stop.\n")

    # Simple single-threaded approach: poll all files
    files = {}
    if legacy_log:
        f = open(legacy_log, 'r', errors='replace')
        f.seek(0, 2)  # Seek to end
        files['LH'] = f
    if args.all:
        if clockwork_log:
            f = open(clockwork_log, 'r', errors='replace')
            f.seek(0, 2)
            files['CW'] = f
        if lcs_log:
            f = open(lcs_log, 'r', errors='replace')
            f.seek(0, 2)
            files['LCS'] = f

    if not files:
        print("No log files found!")
        sys.exit(1)

    try:
        while True:
            any_data = False
            for source, f in files.items():
                line = f.readline()
                while line:
                    any_data = True
                    line = line.rstrip('\n')
                    if args.raw or INTERESTING_RE.search(line):
                        print(format_line(source, line))
                    line = f.readline()
            if not any_data:
                time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n\nStopped.")
    finally:
        for f in files.values():
            f.close()


if __name__ == "__main__":
    main()
