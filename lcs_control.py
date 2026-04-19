"""
Toggle the Poly Lens Control Service on/off persistently.

Why this exists: LCS aggressively re-claims the SocketPortNumber port
file every few seconds. When lensserver.py is running as our MITM, that
port-file race means Poly Studio occasionally lands on stock LCS
between our reclaims. The cleanest fix is to set LCS's startup mode to
Disabled so it never starts at all - then lensserver always wins.

But disabling LCS breaks our fix-setid / update path: LegacyDfu needs
the \\\\.\\pipe\\LegacyHostDfuServer pipe, and that pipe is only created
when LegacyHost is parented by LCS (we verified this with WaitNamedPipeW
returning err=161 for standalone-spawned LegacyHost).

So this module:
  1. Exposes disable_lcs() / enable_lcs() / get_lcs_startup() helpers
  2. Provides a context manager `lcs_temporarily_enabled()` that the
     setid_fix / install_bundle code uses to wake LCS for the duration
     of a DFU and then put it back the way the user set it.

CLI: polytool lcs {disable, enable, status}
"""

import contextlib
import re
import subprocess
import sys


_LCS_SERVICE_NAME = "Poly Lens Control Service"
_NO_WINDOW = 0x08000000


def _run(args, timeout=15):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout,
        creationflags=_NO_WINDOW if sys.platform == "win32" else 0,
    )


def get_lcs_startup() -> str:
    """Return 'auto', 'auto-delayed', 'manual', 'disabled', or 'unknown'.

    Reads via `sc qc` because Get-Service's StartType is auto-translated
    by PowerShell and loses the AUTO_START vs DEMAND_START distinction
    on some systems.
    """
    if sys.platform != "win32":
        return "unknown"
    r = _run(["sc.exe", "qc", _LCS_SERVICE_NAME])
    text = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"START_TYPE\s*:\s*\d+\s+(\S+)", text)
    if not m:
        return "unknown"
    raw = m.group(1).upper()
    # SC's labels: AUTO_START / AUTO_START (DELAYED) / DEMAND_START / DISABLED
    if "DISABLED" in raw:
        return "disabled"
    if "DEMAND" in raw:
        return "manual"
    if "DELAYED" in text.upper():
        return "auto-delayed"
    if "AUTO" in raw:
        return "auto"
    return "unknown"


def get_lcs_running() -> bool:
    """True iff the LCS service is currently RUNNING."""
    if sys.platform != "win32":
        return False
    r = _run(["sc.exe", "query", _LCS_SERVICE_NAME])
    return "RUNNING" in (r.stdout or "")


def _set_startup(mode: str, log=print):
    """Set LCS startup type. mode: 'auto' | 'manual' | 'disabled'."""
    sc_mode = {"auto": "auto", "manual": "demand", "disabled": "disabled"}[mode]
    r = _run(["sc.exe", "config", _LCS_SERVICE_NAME, f"start={sc_mode}"])
    if r.returncode != 0:
        log(f"  lcs: sc config failed: {(r.stderr or r.stdout).strip()[:200]}")
        return False
    return True


def disable_lcs(log=print) -> bool:
    """Stop LCS now AND set startup to Disabled so it never auto-starts.

    Also kills the user-session helpers (LegacyHost / CallControlApp /
    watchdog) that LCS would otherwise have spawned. Returns True on
    success. Requires admin.
    """
    if sys.platform != "win32":
        log("  lcs: not on Windows")
        return False
    log("  lcs: setting startup type to Disabled...")
    if not _set_startup("disabled", log=log):
        return False
    log("  lcs: stopping service...")
    _run(["net.exe", "stop", _LCS_SERVICE_NAME], timeout=30)
    # Kill the user-session children too - they don't get cleaned up by
    # `net stop` (they're under the user's session, not the service).
    for proc in ("LegacyHost.exe", "PolyLensCallControlApp.exe",
                 "PolyLensProcessWatchdog.exe"):
        _run(["taskkill.exe", "/F", "/IM", proc])
    log("  lcs: stopped + disabled")
    return True


def enable_lcs(log=print, start_now: bool = True) -> bool:
    """Restore LCS to Automatic startup. Optionally start it now AND
    relaunch the per-user process watchdogs that LCS depends on for
    LegacyHost / CallControlApp.

    The watchdogs (com.poly.lens.client.watchdog.lh /
    .watchdog.cc) are normally launched at user logon by HKCU Run keys.
    When we manually start LCS outside that flow, the watchdogs aren't
    running, which means LegacyHost never spawns - and without
    LegacyHost, LCS can't actually communicate with DECT devices. Poly
    Studio then shows zero working headsets even though LCS is "up."
    """
    if sys.platform != "win32":
        log("  lcs: not on Windows")
        return False
    log("  lcs: setting startup type to Automatic...")
    if not _set_startup("auto", log=log):
        return False
    if start_now:
        log("  lcs: starting service...")
        r = _run(["net.exe", "start", _LCS_SERVICE_NAME], timeout=30)
        if r.returncode != 0 and "already" not in (r.stdout or "").lower():
            log(f"  lcs: start failed: {(r.stderr or r.stdout).strip()[:200]}")
            return False
        _relaunch_poly_watchdogs(log=log)
    log("  lcs: enabled")
    return True


_WATCHDOG_EXE = r"C:\Program Files\Poly\Poly Studio\ProcessWatchdog\PolyLensProcessWatchdog.exe"
_WATCHED_TARGETS = [
    r"C:\Program Files\Poly\Poly Studio\LegacyHost\LegacyHost.exe",
    r"C:\Program Files\Poly\Poly Studio\CallControlApp\PolyLensCallControlApp.exe",
]


def _relaunch_poly_watchdogs(log=print) -> None:
    """Spawn the two PolyLensProcessWatchdog instances that respectively
    keep LegacyHost.exe and PolyLensCallControlApp.exe alive. Idempotent
    (does nothing if the watchdogs are already up). Best-effort - failure
    to spawn is logged but not raised."""
    from pathlib import Path
    if not Path(_WATCHDOG_EXE).exists():
        log("  lcs: watchdog binary not found - skipping")
        return
    # Skip if a watchdog process is already running (rough check)
    r = _run(["tasklist.exe", "/FI", "IMAGENAME eq PolyLensProcessWatchdog.exe", "/FO", "CSV"])
    if "PolyLensProcessWatchdog" in (r.stdout or ""):
        log("  lcs: watchdogs already running")
        return
    DETACHED = 0x00000008
    for target in _WATCHED_TARGETS:
        if not Path(target).exists():
            continue
        try:
            subprocess.Popen(
                [_WATCHDOG_EXE, target],
                creationflags=DETACHED | _NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception as e:
            log(f"  lcs: watchdog spawn failed for {Path(target).name}: {e}")
    log("  lcs: relaunched watchdogs (LegacyHost + CallControlApp)")


@contextlib.contextmanager
def lcs_temporarily_enabled(log=print):
    """Wake LCS for the duration of the with-block, restore prior state
    on exit. Used by fix-setid / install_bundle so DFU still works when
    the user has LCS persistently disabled.

    No-op when LCS is already Auto/Manual + Running. Also no-op when not
    on Windows.
    """
    if sys.platform != "win32":
        yield
        return

    prior_startup = get_lcs_startup()
    was_running = get_lcs_running()

    if prior_startup == "disabled" or not was_running:
        log(f"  lcs: temporarily enabling (prior startup={prior_startup}, "
            f"running={was_running})")
        if prior_startup == "disabled":
            _set_startup("manual", log=log)
        _run(["net.exe", "start", _LCS_SERVICE_NAME], timeout=30)
        # Give LCS a moment to spawn LegacyHost and open the DFU pipe.
        import time
        time.sleep(3)

    try:
        yield
    finally:
        # Restore: only flip back to Disabled if that's what the user had
        # set. Don't touch Auto - users who run LCS normally shouldn't see
        # their service flapping.
        if prior_startup == "disabled":
            log("  lcs: restoring prior Disabled state...")
            _run(["net.exe", "stop", _LCS_SERVICE_NAME], timeout=30)
            for proc in ("LegacyHost.exe", "PolyLensCallControlApp.exe",
                         "PolyLensProcessWatchdog.exe"):
                _run(["taskkill.exe", "/F", "/IM", proc])
            _set_startup("disabled", log=log)


# ── CLI handlers ────────────────────────────────────────────────────────────

def cmd_lcs(args):
    """`polytool lcs {disable, enable, status}`."""
    from devices import out
    action = getattr(args, "action", "status")
    if action == "status":
        startup = get_lcs_startup()
        running = get_lcs_running()
        out.print(f"  Service:   Poly Lens Control Service")
        out.print(f"  Startup:   {startup}")
        out.print(f"  Running:   {'yes' if running else 'no'}")
        if startup == "disabled":
            out.print("")
            out.print("  LCS is permanently off. Poly Studio will use lensserver.py")
            out.print("  exclusively. fix-setid / update temporarily wake LCS for the")
            out.print("  DFU pipe and put it back when done.")
            out.print("  To restore: polytool lcs enable")
        elif startup == "auto":
            out.print("")
            out.print("  LCS is in normal Auto-start mode. It will compete with")
            out.print("  lensserver.py for the SocketPortNumber port file.")
            out.print("  To prevent: polytool lcs disable")
    elif action == "disable":
        if not _is_admin():
            out.error("  needs admin - re-run polytool from an elevated shell")
            return
        ok = disable_lcs(log=lambda s: out.print(s))
        (out.success if ok else out.error)(
            "  Done." if ok else "  Failed - see log above.")
    elif action == "enable":
        if not _is_admin():
            out.error("  needs admin - re-run polytool from an elevated shell")
            return
        ok = enable_lcs(log=lambda s: out.print(s))
        (out.success if ok else out.error)(
            "  Done." if ok else "  Failed - see log above.")
    else:
        out.error(f"  unknown action: {action!r} (use disable/enable/status)")


def _is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


__all__ = [
    "get_lcs_startup", "get_lcs_running",
    "disable_lcs", "enable_lcs", "lcs_temporarily_enabled",
    "cmd_lcs",
]
