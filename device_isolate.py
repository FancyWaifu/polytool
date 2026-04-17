"""
Device isolation for multi-device DFU operations.

Why this exists: DFUManager.dll's SetID::get_device routes by PID alone,
not by serial. When multiple devices share the same VID:PID (e.g. three
Savi 7320s plugged in), LegacyHost picks an arbitrary one for the setid
write. If that arbitrary pick is on firmware 10.82+ (which silently
rejects setid writes), every fix-setid attempt fails with code:9 even
when --serial points at a 10.71 unit.

Workaround: temporarily disable the HID children of every other
same-VID:PID device so LegacyHost only sees the target. We can't disable
the USB Composite parent (PowerShell's Disable-PnpDevice returns
CIM_ERR_FAILED on composite-class devices), but disabling each HID grand-
child works fine and is enough to make the device invisible to
PolyBus/LegacyHost.

Always re-enables in a finally block. If the polytool process crashes
mid-isolation, the user can re-enable manually via Device Manager or by
running `polytool reset-pnp` (TODO: add that command).

Requires admin (uses Disable-PnpDevice / Enable-PnpDevice).
"""

import contextlib
import ctypes
import re
import subprocess
import sys
from pathlib import Path


def _ps(cmd: str, timeout: int = 30) -> str:
    """Run a PowerShell command and return its stdout (stderr included on
    error). All commands here are short and idempotent."""
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0 and not r.stdout:
        return r.stderr or ""
    return r.stdout or ""


def is_admin() -> bool:
    """Return True if the current process is elevated. Disable-PnpDevice
    silently fails for non-admin callers, so we want to fail loudly up
    front rather than mid-isolation."""
    if sys.platform != "win32":
        return False
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def list_same_pid_devices(vid: int, pid: int) -> list:
    """Return all currently-attached USB Composite devices matching VID:PID.

    Returns list of dicts: {serial, instance_id}. Serial is the 32-char
    GUID-like string that polytool uses elsewhere; instance_id is the
    PnP path needed to walk to HID children.
    """
    pattern = f"USB\\\\VID_{vid:04X}&PID_{pid:04X}"
    cmd = (
        f"Get-PnpDevice -PresentOnly | "
        f"Where-Object {{ $_.InstanceId -match '^{pattern}\\\\[0-9A-Fa-f]+$' }} | "
        f"Select-Object -ExpandProperty InstanceId"
    )
    out = _ps(cmd)
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # Last backslash-separated component is the serial
        parts = line.rsplit("\\", 1)
        if len(parts) == 2:
            devices.append({"instance_id": line, "serial": parts[1]})
    return devices


def get_hid_descendants(usb_composite_instance_id: str) -> list:
    """Walk the PnP tree from a USB Composite device down to its HID
    children. Returns list of HID instance IDs (strings).

    The chain is:
      USB Composite (USB\\VID_...&PID_...\\<serial>)
        -> USB Function (USB\\VID_...&PID_...&MI_NN\\<bus-prefix>)
          -> HID Children (HID\\VID_...&PID_...&MI_NN&ColXX\\<bus-prefix>...)
    """
    # Step 1: get the USB Function (MI_*) children of the composite
    cmd1 = (
        f"(Get-PnpDeviceProperty -InstanceId '{usb_composite_instance_id}' "
        f"-KeyName 'DEVPKEY_Device_Children').Data"
    )
    func_children = [
        line.strip() for line in _ps(cmd1).splitlines() if line.strip()
    ]

    # Step 2: get the HID children of each function child
    hid_children = []
    for fc in func_children:
        cmd2 = (
            f"(Get-PnpDeviceProperty -InstanceId '{fc}' "
            f"-KeyName 'DEVPKEY_Device_Children').Data"
        )
        for line in _ps(cmd2).splitlines():
            line = line.strip()
            if line.startswith("HID\\"):
                hid_children.append(line)
    return hid_children


def disable_devices(instance_ids: list, log=print) -> list:
    """Disable a batch of PnP devices. Returns the subset that were
    successfully disabled (so the caller knows what to re-enable)."""
    if not instance_ids:
        return []
    # Build a single PowerShell call that processes all IDs — much faster
    # than spawning one PS per device.
    quoted = ",".join(f"'{i}'" for i in instance_ids)
    cmd = (
        f"$ids = @({quoted}); "
        f"foreach ($id in $ids) {{ "
        f"  try {{ "
        f"    Disable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop; "
        f"    Write-Host \"OK $id\" "
        f"  }} catch {{ "
        f"    Write-Host \"FAIL $id $($_.Exception.Message)\" "
        f"  }} "
        f"}}"
    )
    out = _ps(cmd)
    disabled = []
    for line in out.splitlines():
        if line.startswith("OK "):
            disabled.append(line[3:].strip())
        elif line.startswith("FAIL "):
            log(f"  isolate: failed to disable {line[5:]}")
    return disabled


def enable_devices(instance_ids: list, log=print) -> None:
    """Re-enable a batch of PnP devices. Errors are logged but never
    raised — re-enable runs in a finally block and we don't want a
    secondary failure to mask the original."""
    if not instance_ids:
        return
    quoted = ",".join(f"'{i}'" for i in instance_ids)
    cmd = (
        f"$ids = @({quoted}); "
        f"foreach ($id in $ids) {{ "
        f"  try {{ "
        f"    Enable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop "
        f"  }} catch {{ "
        f"    Write-Host \"FAIL $id $($_.Exception.Message)\" "
        f"  }} "
        f"}}"
    )
    out = _ps(cmd)
    for line in out.splitlines():
        if line.startswith("FAIL "):
            log(f"  isolate: failed to re-enable {line[5:]}")


@contextlib.contextmanager
def isolate(target_serial: str, vid: int, pid: int, log=print):
    """Context manager that hides every other same-VID:PID device from
    LegacyHost for the duration of the with-block, then restores them.

    Usage:
        with isolate(serial, 0x047F, 0xAC28):
            run_legacy_dfu(...)

    No-op (yields immediately) when:
      - Not on Windows
      - Only one same-PID device exists (no isolation needed)
      - Process is not elevated (logged warning, but proceed without
        isolation since the user might still get lucky with routing)
    """
    if sys.platform != "win32":
        yield
        return

    siblings = list_same_pid_devices(vid, pid)
    others = [d for d in siblings if d["serial"].lower() != target_serial.lower()]

    if not others:
        log(f"  isolate: only one VID:PID 0x{vid:04X}:0x{pid:04X} present, "
            f"no isolation needed")
        yield
        return

    if not is_admin():
        log(f"  isolate: WARNING — {len(others)} sibling device(s) detected "
            f"but polytool is not running elevated, so isolation can't run. "
            f"DFU may route to the wrong device. Re-run polytool from an "
            f"elevated shell to get isolation.")
        yield
        return

    # Collect HID children for every sibling (not the target)
    to_disable = []
    for d in others:
        kids = get_hid_descendants(d["instance_id"])
        log(f"  isolate: hiding sibling serial={d['serial'][:8]}... "
            f"({len(kids)} HID children)")
        to_disable.extend(kids)

    if not to_disable:
        log("  isolate: no HID children found to disable — proceeding anyway")
        yield
        return

    # The full isolation flow:
    #
    #   1. Stop LCS  - releases the HID handles LCS + LegacyHost +
    #                  CallControlApp hold open on every collection. With
    #                  those handles up, Disable-PnpDevice fails on the
    #                  in-use collections (col03, col04 in our testing).
    #   2. Disable sibling HID children - Windows accepts the disable now
    #                  that no process holds the handle.
    #   3. Start LCS  - spawns LegacyHost as a child. LegacyHost (and
    #                  PolyBus inside LCS) only enumerate the target
    #                  device at this point, so SetID::get_device's
    #                  PID-only routing has only one option.
    #   4. yield      - caller runs ensure_legacy_host_running + LegacyDfu.
    #   5. Stop LCS again - to release the handles before re-enabling.
    #   6. Re-enable siblings.
    #   7. Start LCS  - back to the normal multi-device state.
    #
    # We can't skip stopping LCS (handles block disable) and we can't run
    # LegacyHost standalone (it doesn't create the DFU pipe without LCS
    # parenting it — verified via standalone spawn returning err=161 on
    # WaitNamedPipeW). The double-restart is unavoidable.
    lcs_was_running = _stop_lcs(log=log)

    log(f"  isolate: disabling {len(to_disable)} HID interfaces...")
    disabled = disable_devices(to_disable, log=log)
    log(f"  isolate: {len(disabled)}/{len(to_disable)} disabled successfully")

    if len(disabled) < len(to_disable):
        log("  isolate: NOTE - some interfaces failed to disable even with "
            "LCS stopped - DFU may still route incorrectly")

    if lcs_was_running:
        _start_lcs(log=log)
        # Give LCS a moment to spawn LegacyHost and re-enumerate
        import time
        time.sleep(3)

    try:
        yield
    finally:
        # Order matters: stop LCS so re-enable can take effect, then
        # re-enable, then restart LCS to pick up the now-visible siblings.
        _stop_lcs(log=log)
        log(f"  isolate: re-enabling {len(disabled)} HID interfaces...")
        enable_devices(disabled, log=log)
        if lcs_was_running:
            _start_lcs(log=log)
        log("  isolate: done")


_LCS_SERVICE_NAME = "Poly Lens Control Service"

# These are launched at user logon by HKCU Run entries:
#   com.poly.lens.client.watchdog.lh = ProcessWatchdog wrapping LegacyHost.exe
#   com.poly.lens.client.watchdog.cc = ProcessWatchdog wrapping CallControlApp
# We have to kill them during isolation (they hold HID handles), and we
# have to relaunch them afterwards or the user is left with a broken Poly
# Studio until next logon — Studio shows zero devices because LegacyHost
# (which manages DECT/Voyager) is dead and nothing respawns it.
_WATCHDOG_EXE = r"C:\Program Files\Poly\Poly Studio\ProcessWatchdog\PolyLensProcessWatchdog.exe"
_WATCHED_TARGETS = [
    r"C:\Program Files\Poly\Poly Studio\LegacyHost\LegacyHost.exe",
    r"C:\Program Files\Poly\Poly Studio\CallControlApp\PolyLensCallControlApp.exe",
]


def _stop_lcs(log=print) -> bool:
    """Stop the Poly LCS service so it releases its HID handles. Also
    explicitly kills LegacyHost and the call-control app, since `net stop`
    only signals the service and child processes may linger briefly.
    Returns True if LCS was running before we stopped it (so the caller
    knows to restart it later)."""
    # Was it running?
    r = subprocess.run(["sc.exe", "query", _LCS_SERVICE_NAME],
                       capture_output=True, text=True)
    was_running = "RUNNING" in (r.stdout or "")

    if was_running:
        log("  isolate: stopping Poly Lens Control Service...")
        subprocess.run(["net.exe", "stop", _LCS_SERVICE_NAME],
                       capture_output=True, text=True, timeout=30)

    # Belt-and-suspenders: kill any Poly children that may still be alive.
    # The watchdog must be killed too — otherwise it'll respawn LegacyHost
    # while we're trying to disable HID children.
    for proc in ("LegacyHost.exe", "PolyLensCallControlApp.exe",
                 "PolyLensProcessWatchdog.exe"):
        subprocess.run(["taskkill.exe", "/F", "/IM", proc],
                       capture_output=True, text=True)
    return was_running


def _start_lcs(log=print) -> None:
    """Restart the Poly LCS service AND the user-session watchdogs that
    keep LegacyHost / CallControlApp alive. Best-effort — failures logged
    but never raised because we run in a finally block.

    The watchdog relaunch is what makes Poly Studio (running standalone,
    no polytool/lensserver involved) see devices again. Skipping it leaves
    the user with a broken Studio until the next logon."""
    log("  isolate: restarting Poly Lens Control Service...")
    r = subprocess.run(["net.exe", "start", _LCS_SERVICE_NAME],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        log(f"  isolate: WARNING - LCS restart failed: "
            f"{(r.stderr or r.stdout).strip()[:100]}")

    # Relaunch the per-user watchdogs (each watchdog respawns its target
    # if the target dies). DETACHED_PROCESS so they outlive polytool.
    if not Path(_WATCHDOG_EXE).exists():
        return
    DETACHED = 0x00000008
    for target in _WATCHED_TARGETS:
        if not Path(target).exists():
            continue
        try:
            subprocess.Popen(
                [_WATCHDOG_EXE, target],
                creationflags=DETACHED,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception as e:
            log(f"  isolate: WARNING - watchdog spawn failed for "
                f"{Path(target).name}: {e}")
    log("  isolate: watchdogs relaunched (LegacyHost + CallControlApp)")


__all__ = [
    "is_admin",
    "list_same_pid_devices",
    "get_hid_descendants",
    "disable_devices",
    "enable_devices",
    "isolate",
]
