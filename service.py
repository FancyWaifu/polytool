"""
Service installation — register lensserver.py to auto-start on Windows.

Uses Task Scheduler (schtasks.exe), not the Service Control Manager, because:
  - SCM services run as LocalSystem by default, which doesn't have user-session
    HID-device access needed for the native bridge.
  - Task Scheduler "at logon" runs in the user's session, exactly where Poly
    Studio runs — same USB visibility, same %PROGRAMDATA% access for the
    SocketPortNumber port file, no UAC prompts after install.
  - schtasks.exe is built into Windows; no third-party dependency (NSSM, etc.).

Once installed, the flow at boot is:
  1. Windows boots; LCS auto-starts and writes its SocketPortNumber.
  2. User logs in; our scheduled task fires.
  3. lensserver.py starts, saves LCS's port file, writes its own.
  4. User opens Poly Studio; it reads our port file, connects to lensserver.
  5. lensserver synthesizes ProductName + FirmwareVersion correctly,
     forwards the rest to LCS underneath.

The task is registered per-user (no admin needed for install) and uses
pythonw.exe so it runs detached without a console window.
"""

import os
import subprocess
import sys
from pathlib import Path


TASK_NAME = "PolyToolLensServer"
DEFAULT_LOG = Path.home() / "AppData" / "Local" / "PolyTool" / "lensserver.log"
LAUNCHER_BAT = Path.home() / "AppData" / "Local" / "PolyTool" / "lensserver_launch.bat"


def _find_pythonw():
    """Locate pythonw.exe matching the current python.exe.

    sys.executable typically points at python.exe; pythonw.exe is its sibling
    and runs without allocating a console — required for unattended service-
    mode execution.
    """
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    # Fall back to python.exe; the task will create a hidden console window
    # but still work.
    return exe


def _polytool_dir():
    """Directory containing polytool.py and lensserver.py."""
    return Path(__file__).resolve().parent


def _lensserver_path():
    return _polytool_dir() / "lensserver.py"


def _build_command(extra_args=None):
    """Construct the full command line that the scheduled task will run."""
    py = _find_pythonw()
    ls = _lensserver_path()
    args = [str(py), str(ls), "--quiet"]
    if extra_args:
        args.extend(extra_args)
    return args


def _quote(s):
    """Quote a path for inclusion in a schtasks /TR string."""
    s = str(s)
    if " " in s and not (s.startswith('"') and s.endswith('"')):
        return f'"{s}"'
    return s


def _get_current_user_sid():
    """Return the current user's SID (used in the task XML Principal block)."""
    try:
        r = subprocess.run(["whoami", "/user"], capture_output=True, text=True)
        # Output: "USER NAME    SID\n--- ---\n<name>  S-1-5-21-..."
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("S-"):
                return line.split()[0]
            parts = line.split()
            if parts and parts[-1].startswith("S-"):
                return parts[-1]
    except Exception:
        pass
    return ""


def _build_task_xml(command_path, user_sid):
    """Render a Task Scheduler XML that runs `command_path` at logon and
    survives on-battery, idle, and other default-disabling conditions.

    Notable overrides vs the schtasks /Create defaults:
      DisallowStartIfOnBatteries  → false  (so it runs on laptops)
      StopIfGoingOnBatteries      → false  (so it doesn't get killed mid-call)
      AllowHardTerminate          → true   (so service-stop actually works)
      ExecutionTimeLimit          → PT0S   (no auto-kill — it's a daemon)
      MultipleInstancesPolicy     → IgnoreNew  (default; prevents dup instances)
    """
    principal = ""
    if user_sid:
        principal = f"<UserId>{user_sid}</UserId>"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>polytool install-service</Author>
    <Description>Auto-start lensserver.py so regular Poly Studio gets
distinguishable device names and firmware versions.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      {principal}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command_path}</Command>
    </Exec>
  </Actions>
</Task>
"""


def is_installed():
    """Return True if the scheduled task exists."""
    r = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", TASK_NAME],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def is_running():
    """Return True if the lensserver process is currently up.

    schtasks doesn't track child-process liveness reliably (the task fires
    once and exits), so check by process listing instead.
    """
    r = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV"],
        capture_output=True, text=True,
    )
    if "pythonw.exe" not in (r.stdout or ""):
        # Try plain python.exe too
        r = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
            capture_output=True, text=True,
        )
    out = (r.stdout or "")
    if "lensserver" in out.lower():
        return True
    # Cheap heuristic: read the port file and check if it differs from a
    # plausible LCS-default port range. Better: probe the port directly.
    return _probe_lensserver_running()


def _probe_lensserver_running():
    """Detect a running lensserver by reading its port file and trying to
    open the HTTP API endpoint we expose on port 8080 by default."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", 8080))
            return True
        finally:
            s.close()
    except Exception:
        return False


def install(force=False, log=print):
    """Register the scheduled task. Returns (ok: bool, message: str)."""
    if not _lensserver_path().exists():
        return False, f"lensserver.py not found at {_lensserver_path()}"

    if is_installed() and not force:
        return False, f"Task '{TASK_NAME}' already exists. Use --force to replace."

    # Make sure the log directory exists so lensserver can write to it.
    DEFAULT_LOG.parent.mkdir(parents=True, exist_ok=True)

    cmd = _build_command()
    # schtasks fires the task from C:\Windows\System32 with no working-dir
    # control, and its /TR parser mishandles `&&` and other shell operators.
    # Both problems vanish if /TR points at a tiny .bat wrapper that does the
    # cd + launch + log redirection itself.
    poly_dir = _polytool_dir()
    inner = " ".join(_quote(p) for p in cmd)
    bat_lines = [
        "@echo off",
        f'cd /d {_quote(poly_dir)}',
        f'{inner} > {_quote(DEFAULT_LOG)} 2>&1',
    ]
    LAUNCHER_BAT.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER_BAT.write_text("\r\n".join(bat_lines) + "\r\n")
    log(f"  Wrote launcher: {LAUNCHER_BAT}")

    # Use XML registration instead of bare /TR flags so we can override the
    # battery and idle defaults. With the defaults, schtasks /Create sets
    # DisallowStartIfOnBatteries=true, which silently leaves the task in
    # "Queued" state on laptops running on AC-but-charging-low or battery.
    xml_path = LAUNCHER_BAT.with_suffix(".xml")
    user_sid = _get_current_user_sid()
    xml_path.write_text(_build_task_xml(LAUNCHER_BAT, user_sid),
                        encoding="utf-16")
    log(f"  Wrote task XML: {xml_path}")

    args = ["schtasks.exe", "/Create",
            "/TN", TASK_NAME,
            "/XML", str(xml_path)]
    if force:
        args.append("/F")

    log(f"Registering task: {TASK_NAME}")
    log(f"  Action: {LAUNCHER_BAT}")
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"schtasks failed: {(r.stderr or r.stdout).strip()}"
    return True, f"Installed. lensserver will auto-start at next logon."


def uninstall(log=print):
    """Remove the scheduled task. Returns (ok, message)."""
    if not is_installed():
        return True, f"Task '{TASK_NAME}' is not installed (nothing to do)."
    r = subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, f"schtasks failed: {(r.stderr or r.stdout).strip()}"
    return True, "Task removed."


def start_now(log=print):
    """Trigger the scheduled task immediately (without waiting for logon)."""
    if not is_installed():
        return False, "Task not installed. Run `polytool install-service` first."
    r = subprocess.run(
        ["schtasks.exe", "/Run", "/TN", TASK_NAME],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, f"schtasks /Run failed: {(r.stderr or r.stdout).strip()}"
    return True, "Triggered."


def stop_now(log=print):
    """Stop the scheduled task — also kills any running lensserver process.

    schtasks /End only signals the task; it doesn't always reach python child
    processes. We follow up by hunting for any pythonw.exe whose command line
    references lensserver.py.
    """
    if is_installed():
        subprocess.run(
            ["schtasks.exe", "/End", "/TN", TASK_NAME],
            capture_output=True, text=True,
        )
    # Kill any lingering lensserver process. wmic is the only built-in that
    # exposes the command line for filtering.
    try:
        r = subprocess.run(
            ["wmic", "process", "where",
             "name='pythonw.exe' or name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True,
        )
        for line in (r.stdout or "").splitlines():
            if "lensserver" not in line.lower():
                continue
            parts = line.strip().split(",")
            # CSV: Node,CommandLine,ProcessId
            pid = parts[-1].strip()
            if pid.isdigit():
                subprocess.run(["taskkill.exe", "/F", "/PID", pid],
                               capture_output=True, text=True)
    except Exception:
        pass
    return True, "Stopped."


def status():
    """Return a dict describing install + run state."""
    return {
        "installed": is_installed(),
        "running": _probe_lensserver_running(),
        "task_name": TASK_NAME,
        "lensserver_path": str(_lensserver_path()),
        "python": str(_find_pythonw()),
    }


# ── CLI command handlers ─────────────────────────────────────────────────────

def cmd_install_service(args):
    """`polytool install-service` entry point."""
    from devices import out
    if os.name != "nt":
        out.error("install-service is Windows-only.")
        return
    ok, msg = install(force=args.force, log=lambda s: out.print(f"  {s}"))
    if ok:
        out.success(f"  {msg}")
        out.print("  To start it now without logging out:")
        out.print("      polytool service-start")
        out.print("  To remove later:")
        out.print("      polytool uninstall-service")
    else:
        out.error(f"  {msg}")


def cmd_uninstall_service(args):
    from devices import out
    if os.name != "nt":
        out.error("uninstall-service is Windows-only.")
        return
    ok, msg = uninstall(log=lambda s: out.print(f"  {s}"))
    (out.success if ok else out.error)(f"  {msg}")


def cmd_service_status(args):
    from devices import out
    if os.name != "nt":
        out.error("service-status is Windows-only.")
        return
    s = status()
    out.print(f"  Task name:        {s['task_name']}")
    out.print(f"  Installed:        {'yes' if s['installed'] else 'no'}")
    out.print(f"  Running:          {'yes' if s['running'] else 'no'}")
    out.print(f"  lensserver.py:    {s['lensserver_path']}")
    out.print(f"  python:           {s['python']}")


def cmd_service_start(args):
    from devices import out
    if os.name != "nt":
        out.error("service-start is Windows-only.")
        return
    ok, msg = start_now(log=lambda s: out.print(f"  {s}"))
    (out.success if ok else out.error)(f"  {msg}")


def cmd_service_stop(args):
    from devices import out
    if os.name != "nt":
        out.error("service-stop is Windows-only.")
        return
    ok, msg = stop_now(log=lambda s: out.print(f"  {s}"))
    (out.success if ok else out.error)(f"  {msg}")


__all__ = [
    "TASK_NAME",
    "install", "uninstall", "start_now", "stop_now", "status",
    "is_installed", "is_running",
    "cmd_install_service", "cmd_uninstall_service",
    "cmd_service_status", "cmd_service_start", "cmd_service_stop",
]
