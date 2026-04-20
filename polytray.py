"""
PolyTray - Windows GUI tool that auto-detects + fixes the FFFFFFFF
firmware-version bug on Poly headsets.

Sits in a small always-visible window. Polls for connected devices
every few seconds. When a new headset shows up with the FFFF SetID
NVRAM defect, pops a dialog asking the user to fix it. Click Yes and
it runs `polytool fix-setid` in the background, shows progress, and
reports the result.

No additional dependencies - uses only Tkinter (built-in to Python on
Windows). Re-uses polytool's existing fix-setid pipeline so the proxy
mode / auto-isolate / LCS-bounce handling all carry over.

Usage:
    python3 polytray.py

To install as an auto-start app:
    polytool install-tray   (TODO - see service.py for the pattern)
"""

import os
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import font as tkfont
from tkinter import messagebox, ttk

# Re-use polytool internals
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from devices import discover_devices, try_read_device_info, _hydrate_from_lcs_cache
from setid_fix import diagnose_setid, fix_setid, read_lcs_device_cache


POLL_INTERVAL_SEC = 4
BATTERY_RETRY_SEC = 60   # how often to re-attempt fix-setid for a low-battery device
WINDOW_TITLE = "PolyTray - FFFF Auto-Fix"
WINDOW_SIZE = "780x460"

# Phrases LegacyDfu emits when the headset's battery is below the threshold.
# fix_setid surfaces these in result['message'] when the DFU pipeline fails.
_BATTERY_ERROR_MARKERS = (
    "Battery level on the device too low",
    "code:5",
    "code: 5",
)


def _is_battery_error(msg: str) -> bool:
    return any(m in (msg or "") for m in _BATTERY_ERROR_MARKERS)


class PolyTray:
    """Main GUI app."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.geometry(WINDOW_SIZE)
        # Stay on top so the user notices the alerts
        self.root.attributes("-topmost", False)

        # State
        self._known_serials = set()       # serials we've already alerted on
        self._fixing_serials = set()      # actively being fixed (don't double-fire)
        # Devices waiting for battery to charge before we retry the fix.
        # Keyed by serial -> {dev, attempts, last_attempt_ts, last_msg}.
        # Background timer (_battery_retry_loop) re-attempts every 60s.
        self._waiting_for_battery: dict[str, dict] = {}
        self._auto_fix = tk.BooleanVar(value=False)  # ask first by default
        self._stop_event = threading.Event()

        self._build_ui()
        self._start_poller()

        # Clean shutdown on window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top header
        header = tk.Frame(self.root, bg="#1e1e1e", height=70)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        title_font = tkfont.Font(family="Segoe UI", size=14, weight="bold")
        sub_font = tkfont.Font(family="Segoe UI", size=9)
        tk.Label(header, text="PolyTray  -  FFFF Auto-Fix",
                 fg="white", bg="#1e1e1e", font=title_font).pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(header, text="Plug in a Poly headset. FFFF state will be detected and fixed automatically.",
                 fg="#bababa", bg="#1e1e1e", font=sub_font).pack(anchor="w", padx=12)

        # Auto-fix toggle
        toggle_frame = tk.Frame(self.root)
        toggle_frame.pack(fill=tk.X, padx=12, pady=(8, 0))
        ttk.Checkbutton(
            toggle_frame, text="Auto-fix without prompting (skip the confirmation dialog)",
            variable=self._auto_fix
        ).pack(anchor="w")

        # Device list
        list_frame = tk.Frame(self.root)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        cols = ("name", "tattoo", "battery", "fw", "status")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=8)
        self.tree.heading("name", text="Device")
        self.tree.heading("tattoo", text="Tattoo")
        self.tree.heading("battery", text="Battery")
        self.tree.heading("fw", text="Firmware")
        self.tree.heading("status", text="Status")
        self.tree.column("name", width=210, anchor="w")
        self.tree.column("tattoo", width=100, anchor="w")
        self.tree.column("battery", width=90, anchor="w")
        self.tree.column("fw", width=80, anchor="w")
        self.tree.column("status", width=250, anchor="w")
        self.tree.tag_configure("ffff", foreground="#cc1f1a", font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("ok", foreground="#22863a")
        self.tree.tag_configure("fixing", foreground="#0366d6", font=("Segoe UI", 9, "italic"))
        self.tree.tag_configure("waiting", foreground="#b08800", font=("Segoe UI", 9, "italic"))
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scroll.set)

        # Right-click menu: Fix Now (also covers re-fix)
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Fix this device now", command=self._fix_selected)
        self.tree.bind("<Button-3>", self._on_right_click)

        # Status bar
        self.status_var = tk.StringVar(value="Watching for devices...")
        status = tk.Label(self.root, textvariable=self.status_var,
                          anchor="w", bd=1, relief="sunken",
                          bg="#f0f0f0", fg="#333", padx=8, pady=2)
        status.pack(fill=tk.X, side=tk.BOTTOM)

    def _on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.menu.post(event.x_root, event.y_root)

    def _fix_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        serial = self.tree.item(sel[0], "values")[1]  # tattoo column - lookup by serial via the iid
        # Use iid (we stored serial as iid)
        full_serial = sel[0]
        self._trigger_fix(full_serial)

    # ── Background polling ─────────────────────────────────────────────

    def _start_poller(self):
        t = threading.Thread(target=self._poll_loop, daemon=True, name="polytray-poll")
        t.start()
        # Separate thread that re-attempts fix-setid for devices waiting on
        # battery charge. Cheaper than polling the battery itself - we
        # just call fix-setid every 60s and let LegacyDfu's own check be
        # the ground truth.
        bt = threading.Thread(target=self._battery_retry_loop, daemon=True,
                              name="polytray-battery-retry")
        bt.start()
        # Lazy native-bridge reader: read battery levels into a cache the
        # poller can show in the UI. Native bridge is heavy so we keep
        # the same instance alive and refresh in a slow loop.
        nb = threading.Thread(target=self._battery_cache_loop, daemon=True,
                              name="polytray-battery-cache")
        nb.start()
        self._battery_cache: dict[int, dict] = {}  # pid -> {level, charging, docked}
        self._native_bridge = None  # lazily created

    def _poll_loop(self):
        first_pass = True
        while not self._stop_event.is_set():
            try:
                self._poll_once(first_pass)
            except Exception as e:
                # Don't kill the poller on transient errors
                self._set_status(f"poll error: {e!s}")
                traceback.print_exc()
            first_pass = False
            # Wait but stay responsive to shutdown
            for _ in range(POLL_INTERVAL_SEC * 4):
                if self._stop_event.is_set():
                    return
                time.sleep(0.25)

    def _poll_once(self, first_pass: bool):
        """One poll cycle. Refreshes the device list, detects new FFFFs."""
        devices = discover_devices()
        cache = read_lcs_device_cache()
        _hydrate_from_lcs_cache(devices)

        # Devices that have left (e.g. unplugged) should be removed from
        # the waiting set so we don't keep retrying a phantom.
        present_serials = {d.serial for d in devices if d.serial}
        for s in list(self._waiting_for_battery):
            if s not in present_serials:
                self._waiting_for_battery.pop(s, None)

        rows = []
        ffff_devices = []
        for dev in devices:
            try_read_device_info(dev)
            diag = diagnose_setid(dev.serial, cache=cache)
            state = diag.get("state", "unknown")
            wait_info = self._waiting_for_battery.get(dev.serial)
            battery_str = self._format_battery(dev)

            if dev.serial in self._fixing_serials:
                status_text = "Fixing..."
                tag = "fixing"
            elif wait_info:
                # Battery-low retry mode
                attempts = wait_info["attempts"]
                next_in = max(0, BATTERY_RETRY_SEC -
                              int(time.time() - wait_info["last_attempt_ts"]))
                status_text = (f"Waiting for battery (attempt {attempts}, "
                               f"retry in {next_in}s)")
                tag = "waiting"
            elif state == "ff":
                status_text = "FFFF DETECTED - needs fix"
                tag = "ffff"
                ffff_devices.append((dev, diag))
            elif state == "real":
                status_text = "OK (setid programmed)"
                tag = "ok"
            else:
                status_text = "OK"
                tag = "ok"
            rows.append((dev, diag, battery_str, status_text, tag))

        self.root.after(0, self._refresh_table, rows)

        for dev, diag in ffff_devices:
            if dev.serial in self._known_serials:
                continue
            if dev.serial in self._fixing_serials:
                continue
            if dev.serial in self._waiting_for_battery:
                continue
            self._known_serials.add(dev.serial)
            self.root.after(0, self._handle_ffff_device, dev, diag)

    def _format_battery(self, dev) -> str:
        """Look up cached battery for this device by PID. Cached state is
        refreshed by _battery_cache_loop - may show 'n/a' on the very
        first few polls before the native bridge spins up."""
        bat = self._battery_cache.get(dev.pid)
        if not bat:
            return "n/a"
        level = bat.get("level", -1)
        # Native bridge uses 0-5 scale on DECT; convert to percentage.
        if 0 <= level <= 5:
            pct = level * 20
        elif level > 5:
            pct = min(100, level)
        else:
            return "n/a"
        suffix = ""
        if bat.get("charging"):
            suffix = " (charging)"
        elif bat.get("docked"):
            suffix = " (docked)"
        return f"{pct}%{suffix}"

    def _refresh_table(self, rows):
        """Replace the device list with a fresh snapshot."""
        sel = self.tree.selection()
        sel_iid = sel[0] if sel else None

        for iid in self.tree.get_children():
            self.tree.delete(iid)

        for dev, diag, battery_str, status_text, tag in rows:
            name = dev.display_name or dev.friendly_name or "Unknown"
            tattoo = dev.tattoo_serial or "-"
            fw = dev.firmware_display
            self.tree.insert("", "end", iid=dev.serial,
                             values=(name, tattoo, battery_str, fw, status_text),
                             tags=(tag,))
        if sel_iid and sel_iid in self.tree.get_children():
            self.tree.selection_set(sel_iid)

        waiting = len(self._waiting_for_battery)
        suffix = f" | {waiting} waiting for charge" if waiting else ""
        self._set_status(f"{len(rows)} device(s) connected | "
                         f"polling every {POLL_INTERVAL_SEC}s{suffix}")

    # ── Battery cache (slow background reader) ──────────────────────────

    def _battery_cache_loop(self):
        """Maintain self._battery_cache by polling the native bridge.
        Heavy first call (~8s to spin up the bridge) so we keep the
        instance alive and refresh slowly (every 10s)."""
        while not self._stop_event.is_set():
            try:
                if self._native_bridge is None:
                    from native_bridge import NativeBridge
                    nb = NativeBridge()
                    nb.start()
                    # Give the worker time to enumerate devices
                    time.sleep(8)
                    self._native_bridge = nb
                ndevs = self._native_bridge.get_devices() or {}
                fresh = {}
                for nid, ndev in ndevs.items():
                    bat = self._native_bridge.get_battery(nid)
                    pid = ndev.get("pid")
                    if bat and pid:
                        fresh[pid] = bat
                self._battery_cache = fresh
            except Exception:
                pass  # Native bridge can have transient errors; keep going
            for _ in range(40):  # ~10s, but check stop frequently
                if self._stop_event.is_set():
                    return
                time.sleep(0.25)

    # ── Battery-low retry loop ──────────────────────────────────────────

    def _battery_retry_loop(self):
        """Retry fix-setid for devices that were waiting on battery
        charge. Each device gets one attempt every BATTERY_RETRY_SEC."""
        while not self._stop_event.is_set():
            now = time.time()
            for serial, info in list(self._waiting_for_battery.items()):
                if now - info["last_attempt_ts"] < BATTERY_RETRY_SEC:
                    continue
                if serial in self._fixing_serials:
                    continue
                # Re-fetch the PolyDevice (it might have changed since
                # the original failure - new info, etc.)
                fresh = None
                for d in discover_devices():
                    if d.serial == serial:
                        fresh = d
                        break
                if not fresh:
                    self._waiting_for_battery.pop(serial, None)
                    continue
                info["dev"] = fresh
                info["attempts"] += 1
                info["last_attempt_ts"] = now
                self._fixing_serials.add(serial)
                threading.Thread(
                    target=self._run_fix, args=(fresh,), daemon=True,
                    name=f"battery-retry-{serial[:8]}"
                ).start()
            for _ in range(20):  # check every 5s if anything is due
                if self._stop_event.is_set():
                    return
                time.sleep(0.25)

    def _handle_ffff_device(self, dev, diag):
        """A new FFFF-state device appeared. Either auto-fix or prompt."""
        if dev.serial in self._fixing_serials:
            return
        if self._auto_fix.get():
            self._trigger_fix(dev.serial, dev=dev)
            return
        # Bring window to front + ask
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.attributes("-topmost", False)
        ans = messagebox.askyesno(
            "FFFF Detected on Poly Headset",
            f"Detected the FFFFFFFF firmware-version bug:\n\n"
            f"  Device:  {dev.display_name or dev.friendly_name}\n"
            f"  Tattoo:  {dev.tattoo_serial or 'n/a'}\n"
            f"  Serial:  {dev.serial}\n"
            f"  State:   {diag.get('firmware_version', '?')}\n\n"
            f"Fix now? Takes ~30 seconds.\n"
            f"(Tip: enable 'Auto-fix without prompting' to skip this dialog.)",
            parent=self.root,
        )
        if ans:
            self._trigger_fix(dev.serial, dev=dev)

    # ── Fix execution ───────────────────────────────────────────────────

    def _trigger_fix(self, serial: str, dev=None):
        if serial in self._fixing_serials:
            return
        if not dev:
            # Look up by serial
            for d in discover_devices():
                if d.serial == serial:
                    dev = d
                    break
            if not dev:
                messagebox.showerror("Device gone",
                    f"Could not find the device with serial {serial[:8]}... anymore.",
                    parent=self.root)
                return
        self._fixing_serials.add(serial)
        threading.Thread(target=self._run_fix, args=(dev,), daemon=True,
                         name=f"fix-{serial[:8]}").start()

    def _run_fix(self, dev):
        try:
            self._set_status(f"Fixing {dev.display_name or dev.friendly_name}...")
            log_lines = []

            def _log(line):
                log_lines.append(str(line))
                # Update status bar live (just the latest line)
                self._set_status(f"Fixing: {str(line)[-80:]}")

            result = fix_setid(
                serial=dev.serial,
                vid=dev.vid,
                pid=dev.pid,
                isolate_siblings=True,
                log=_log,
            )
            self._fixing_serials.discard(dev.serial)

            if result.get("success"):
                # Clean up any waiting state - the fix landed
                self._waiting_for_battery.pop(dev.serial, None)
                self.root.after(0, self._show_success, dev, result)
            elif _is_battery_error(result.get("message", "")) or _is_battery_error("\n".join(log_lines)):
                # Don't show an error dialog - silently move into waiting
                # mode and let the battery retry loop handle it.
                wait = self._waiting_for_battery.get(dev.serial)
                if not wait:
                    wait = {"dev": dev, "attempts": 1,
                            "last_attempt_ts": time.time(),
                            "last_msg": result.get("message", "")}
                    self._waiting_for_battery[dev.serial] = wait
                else:
                    wait["last_attempt_ts"] = time.time()
                    wait["last_msg"] = result.get("message", "")
                # First-time battery wait? Show a one-shot heads-up so the
                # user knows they need to charge the headset.
                if wait["attempts"] == 1:
                    self.root.after(0, self._show_battery_wait_notice, dev)
            else:
                self._waiting_for_battery.pop(dev.serial, None)
                self.root.after(0, self._show_failure, dev, result, log_lines)
        except Exception as e:
            self._fixing_serials.discard(dev.serial)
            tb = traceback.format_exc()
            self.root.after(0, lambda: messagebox.showerror(
                "Fix crashed",
                f"Unexpected error fixing {dev.serial[:8]}:\n\n{e}\n\n{tb[-500:]}",
                parent=self.root))

    def _show_battery_wait_notice(self, dev):
        """One-shot popup the first time we hit battery-too-low for a
        device. Tells the user we're now waiting + how to speed it up."""
        messagebox.showinfo(
            "Charge the Headset",
            f"The headset's battery is too low for the SetID write right now.\n\n"
            f"  Device:  {dev.display_name or dev.friendly_name}\n"
            f"  Tattoo:  {dev.tattoo_serial or 'n/a'}\n\n"
            f"PolyTray will automatically retry every {BATTERY_RETRY_SEC}s.\n"
            f"Place the headset in the charging cradle - takes ~10 minutes\n"
            f"to reach a usable level. The status column shows progress.",
            parent=self.root,
        )

    def _show_success(self, dev, result):
        self._set_status(f"Fixed: {dev.display_name or dev.friendly_name}")
        messagebox.showinfo(
            "Fixed!",
            f"SetID NVRAM written successfully.\n\n"
            f"  Device:  {dev.display_name or dev.friendly_name}\n"
            f"  Tattoo:  {dev.tattoo_serial or 'n/a'}\n"
            f"  Wrote:   {result.get('version_written', '?')}\n\n"
            f"Re-dock the headset to refresh Poly Studio's display.",
            parent=self.root,
        )

    def _show_failure(self, dev, result, log_lines):
        self._set_status(f"Fix failed: {dev.display_name or dev.friendly_name}")
        msg = (f"Could not fix {dev.display_name or dev.friendly_name}:\n\n"
               f"{result.get('message', 'unknown error')}\n\n"
               f"Common causes:\n"
               f"  - Headset battery too low (charge it on the dock for a few minutes)\n"
               f"  - Firmware too new (10.82+ closes the SetID write path)\n"
               f"  - Another device of the same model is interfering\n\n"
               f"Last log lines:\n" + "\n".join(log_lines[-6:]))
        messagebox.showerror("Fix Failed", msg, parent=self.root)

    # ── Lifecycle ───────────────────────────────────────────────────────

    def _set_status(self, msg: str):
        self.root.after(0, self.status_var.set, msg)

    def _on_close(self):
        self._stop_event.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    if sys.platform != "win32":
        print("PolyTray is Windows-only.", file=sys.stderr)
        sys.exit(1)
    PolyTray().run()


if __name__ == "__main__":
    main()
