"""
SetID NVRAM Fix — permanent fix for the FFFFFFFF firmware version bug.

Some Savi DECT headsets (notably AC28 / Savi 7320) ship from factory with the
SetID NVRAM region unprogrammed (all 0xFFFF). The cloud firmware bundle for
these devices does NOT include a setid component, so the normal Poly Lens
update flow never writes it. The result: Poly Studio reads
firmwareVersion.setId = {build:ffff,...} and chokes — blank version display,
no firmware updates offered, capability gates disabled.

This module fixes the issue permanently by writing a synthetic setid value
into NVRAM. The mechanism uses Poly's own LegacyDfu.exe binary fed a forged
firmware bundle whose rules.json contains only a setid component:

    {"type": "setid", "pid": "0xAC28", "version": "0001.0000.0000.0001",
     "filename": "", "maxDuration": 2}

LegacyDfu connects to LegacyHost.exe via the \\\\.\\pipe\\LegacyHostDfuServer
named pipe; LegacyHost recognizes the setid component and writes it to NVRAM
through PLTDeviceManager. Reverse-engineered via Frida traffic capture against
the running LCS.

Format: the dotted version string is "<major>.<minor>.<revision>.<build>"
(version-significance order). The device's JSON response uses keys in
alphabetical order (build, major, minor, revision) which can mislead.

Usage (programmatic):
    from setid_fix import fix_setid
    result = fix_setid(serial="049160FE...", version="0.0.0.1")

Usage (CLI):
    python3 polytool.py fix-setid              # fix all devices needing it
    python3 polytool.py fix-setid <serial>     # fix specific device
    python3 polytool.py fix-setid --dry-run    # show what would happen
"""

import json
import os
import re
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path


LEGACY_DFU_PATHS = [
    r"C:\Program Files\Poly\Lens Control Service\eDfu\LegacyDfu.exe",
    r"C:\Program Files (x86)\Poly\Lens Control Service\eDfu\LegacyDfu.exe",
]

LEGACY_HOST_PATHS = [
    r"C:\Program Files\Poly\Poly Studio\LegacyHost\LegacyHost.exe",
    r"C:\Program Files (x86)\Poly\Poly Studio\LegacyHost\LegacyHost.exe",
]

DFU_PIPE_NAME = r"\\.\pipe\LegacyHostDfuServer"

DEFAULT_SETID = "0001.0000.0000.0001"


# ── Detection ────────────────────────────────────────────────────────────────

def is_ff_setid(setid):
    """Return True if a setId block represents unprogrammed NVRAM (all FFFFs)."""
    if isinstance(setid, dict):
        return all(
            isinstance(v, str) and v.lower() == "ffff"
            for v in setid.values()
        )
    if isinstance(setid, str):
        return bool(re.fullmatch(r"(?:ffff[.-]?){3,4}f*", setid.lower()))
    return False


def parse_setid_string(s):
    """Parse a dotted SetID string into its 4 components.

    The on-wire format is "<major>.<minor>.<revision>.<build>". We accept
    decimal or zero-padded hex; the device stores raw 16-bit values.
    """
    if not s:
        return None
    parts = s.split(".")
    if len(parts) != 4:
        raise ValueError(f"SetID must have 4 dot-separated parts, got {len(parts)}: {s!r}")
    return {
        "major": parts[0],
        "minor": parts[1],
        "revision": parts[2],
        "build": parts[3],
    }


# ── Tool location ────────────────────────────────────────────────────────────

def find_legacy_dfu():
    """Find LegacyDfu.exe on disk. Returns Path or None."""
    for p in LEGACY_DFU_PATHS:
        if Path(p).exists():
            return Path(p)
    return None


def find_legacy_host():
    """Find LegacyHost.exe on disk. Returns Path or None."""
    for p in LEGACY_HOST_PATHS:
        if Path(p).exists():
            return Path(p)
    return None


def is_dfu_pipe_up():
    """Check whether \\\\.\\pipe\\LegacyHostDfuServer exists.

    LegacyHost only creates this pipe while running. LegacyDfu requires it.
    Uses WaitNamedPipeW: returns nonzero if a pipe instance is available;
    GetLastError gives ERROR_FILE_NOT_FOUND (2) when the pipe doesn't exist
    at all, vs ERROR_SEM_TIMEOUT (121) when it exists but no free instance.
    Either of "available" or "exists-but-busy" means LegacyHost is up.
    """
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.SetLastError(0)
        result = k.WaitNamedPipeW(DFU_PIPE_NAME, 0)
        if result:
            return True  # pipe ready
        err = ctypes.get_last_error()
        # ERROR_SEM_TIMEOUT (121) means pipe exists but all instances busy
        # ERROR_PIPE_BUSY (231) likewise — pipe exists, just no free slot
        return err in (121, 231)
    except Exception:
        return False


def ensure_legacy_host_running(timeout=15):
    """Start LegacyHost.exe if it isn't already, wait for the DFU pipe.

    Returns True if pipe is ready. Note: requires Poly Lens Control Service
    to be installed; LegacyHost is part of the Poly Studio install.
    """
    if is_dfu_pipe_up():
        return True

    exe = find_legacy_host()
    if not exe:
        return False

    # Spawn detached so we don't tie our process lifetime to it
    subprocess.Popen(
        [str(exe)],
        creationflags=0x00000008,  # DETACHED_PROCESS
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_dfu_pipe_up():
            return True
        time.sleep(0.5)
    return False


# ── Bundle forging ──────────────────────────────────────────────────────────

def build_setid_bundle(pid_hex, version, out_path):
    """Write a minimal firmware bundle .zip containing only a setid component.

    pid_hex is e.g. "0xAC28" (matches Poly's rules.json convention).
    version is the dotted "<major>.<minor>.<revision>.<build>" string.
    """
    parse_setid_string(version)  # validate format

    rules = {
        "version": version,
        "type": "firmware",
        "components": [{
            "type": "setid",
            "pid": pid_hex,
            "version": version,
            "description": "polytool fix-setid: NVRAM SetID write",
            "filename": "",
            "maxDuration": 2,
        }],
    }
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("rules.json", json.dumps(rules, indent=4))


# ── Execution ────────────────────────────────────────────────────────────────

def run_legacy_dfu(zip_path, vid, pid, serial, timeout=120):
    """Invoke LegacyDfu.exe against the bundle. Returns (success, output).

    success means the process exited 0 AND output contains "DFU Complete: 100".
    """
    exe = find_legacy_dfu()
    if not exe:
        return False, "LegacyDfu.exe not found — install Poly Lens Control Service"

    cmd = [
        str(exe),
        "-v", str(vid),
        "-p", str(pid),
        "-f", str(zip_path),
        "--serial", serial,
        "--ignore_crc",
        "--ignore_version_check",
        "--loglevel", "3",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0 and "DFU Complete:   100" in out
        return ok, out
    except subprocess.TimeoutExpired:
        return False, f"LegacyDfu timed out after {timeout}s"


# ── High-level workflow ─────────────────────────────────────────────────────

def fix_setid(serial, vid=0x047F, pid=None, pid_hex=None, version=None,
              dry_run=False, log=print):
    """Write a fresh SetID to a Poly device's NVRAM.

    serial   — full device serial (32-char hex like "049160FE6715450F8A4FB...")
    vid/pid  — USB IDs. pid is decimal (44072), pid_hex is "0xAC28".
    version  — dotted "<major>.<minor>.<revision>.<build>". Defaults to
               "0001.0000.0000.0001" if None.
    dry_run  — build the bundle but don't run LegacyDfu.

    Returns dict with 'success' bool and 'message' explaining what happened.
    """
    if pid is None and pid_hex is None:
        return {"success": False, "message": "must supply pid or pid_hex"}
    if pid is None:
        pid = int(pid_hex, 16)
    if pid_hex is None:
        pid_hex = f"0x{pid:04X}"
    version = version or DEFAULT_SETID

    # Validate version string up front
    try:
        parse_setid_string(version)
    except ValueError as e:
        return {"success": False, "message": str(e)}

    log(f"  Target: VID=0x{vid:04X} PID={pid_hex} serial={serial}")
    log(f"  SetID to write: {version}  (major.minor.revision.build)")

    # Forge the bundle
    tmpdir = Path(tempfile.mkdtemp(prefix="polytool_setid_"))
    bundle_path = tmpdir / f"setid_{pid_hex.lower()}.zip"
    build_setid_bundle(pid_hex, version, bundle_path)
    log(f"  Forged bundle: {bundle_path}")

    if dry_run:
        return {"success": True, "message": f"dry-run — bundle at {bundle_path}",
                "bundle_path": str(bundle_path)}

    # Make sure LegacyHost is up so the DFU pipe exists
    if not ensure_legacy_host_running():
        return {"success": False,
                "message": "LegacyHost.exe could not start or DFU pipe never appeared"}
    log("  LegacyHost DFU pipe is up")

    # Run LegacyDfu
    log("  Running LegacyDfu.exe (this triggers the actual NVRAM write)...")
    ok, output = run_legacy_dfu(bundle_path, vid, pid, serial)

    # Try to clean up the temp file
    try:
        bundle_path.unlink()
        tmpdir.rmdir()
    except Exception:
        pass

    if ok:
        return {"success": True,
                "message": f"SetID write completed. Restart Poly Lens to see {version} as firmware version.",
                "version_written": version,
                "output": output}
    return {"success": False,
            "message": "LegacyDfu did not report 'DFU Complete: 100'",
            "output": output}


# ── CLI command ─────────────────────────────────────────────────────────────

def cmd_fix_setid(args):
    """`polytool fix-setid` entry point."""
    from devices import discover_devices, try_read_device_info, out

    raw_devices = discover_devices()
    for dev in raw_devices:
        try_read_device_info(dev)

    # Filter target devices
    if args.device == "all":
        targets = raw_devices
    else:
        sel = args.device.lower()
        targets = [
            d for d in raw_devices
            if sel in d.serial.lower() or sel in (d.product_name or "").lower()
            or sel == f"{d.pid:04x}"
        ]

    if not targets:
        out.error("No matching devices found.")
        return

    for dev in targets:
        out.print(f"\n{dev.product_name or 'Device'}  ({dev.vid_hex}:{dev.pid_hex})  serial={dev.serial}")

        # Always force the write if --force; otherwise skip devices that don't
        # need it (we'd need a current-SetID readback to be sure, omitted for
        # this initial CLI — user can pass --force when in doubt).
        if not args.force and not args.yes:
            ans = input(f"    Write SetID {args.version!r} to this device? [y/N] ").strip().lower()
            if ans != "y":
                out.print("    skipped")
                continue

        result = fix_setid(
            serial=dev.serial,
            vid=dev.vid,
            pid=dev.pid,
            version=args.version,
            dry_run=args.dry_run,
            log=lambda s: out.print(f"  {s}"),
        )
        if result["success"]:
            out.print(f"  OK: {result['message']}")
        else:
            out.error(f"  FAILED: {result['message']}")
            if args.verbose and result.get("output"):
                out.print(result["output"])


__all__ = [
    "is_ff_setid", "parse_setid_string",
    "find_legacy_dfu", "find_legacy_host", "is_dfu_pipe_up",
    "ensure_legacy_host_running",
    "build_setid_bundle", "run_legacy_dfu",
    "fix_setid", "cmd_fix_setid",
    "DEFAULT_SETID",
]
