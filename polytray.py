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
WINDOW_TITLE = "PolyTray - FFFF Auto-Fix"
WINDOW_SIZE = "640x440"


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

        cols = ("name", "tattoo", "fw", "status")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=8)
        self.tree.heading("name", text="Device")
        self.tree.heading("tattoo", text="Tattoo")
        self.tree.heading("fw", text="Firmware")
        self.tree.heading("status", text="Status")
        self.tree.column("name", width=240, anchor="w")
        self.tree.column("tattoo", width=110, anchor="w")
        self.tree.column("fw", width=90, anchor="w")
        self.tree.column("status", width=180, anchor="w")
        self.tree.tag_configure("ffff", foreground="#cc1f1a", font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("ok", foreground="#22863a")
        self.tree.tag_configure("fixing", foreground="#0366d6", font=("Segoe UI", 9, "italic"))
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
        # Hydrate tattoo serials etc. from cache
        _hydrate_from_lcs_cache(devices)

        # Compute status per device
        rows = []
        ffff_devices = []
        for dev in devices:
            try_read_device_info(dev)
            diag = diagnose_setid(dev.serial, cache=cache)
            state = diag.get("state", "unknown")
            if dev.serial in self._fixing_serials:
                status_text = "Fixing..."
                tag = "fixing"
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
            rows.append((dev, diag, status_text, tag))

        # Schedule UI refresh on the Tk thread
        self.root.after(0, self._refresh_table, rows)

        # On every cycle (including first), alert for any FFFF device
        # we haven't already prompted about and isn't currently being fixed.
        for dev, diag in ffff_devices:
            if dev.serial in self._known_serials:
                continue
            if dev.serial in self._fixing_serials:
                continue
            self._known_serials.add(dev.serial)
            self.root.after(0, self._handle_ffff_device, dev, diag)

    def _refresh_table(self, rows):
        """Replace the device list with a fresh snapshot."""
        # Remember selection to restore
        sel = self.tree.selection()
        sel_iid = sel[0] if sel else None

        # Wipe + repopulate (keyed by serial as iid)
        for iid in self.tree.get_children():
            self.tree.delete(iid)

        for dev, diag, status_text, tag in rows:
            name = dev.display_name or dev.friendly_name or "Unknown"
            tattoo = dev.tattoo_serial or "-"
            fw = dev.firmware_display
            self.tree.insert("", "end", iid=dev.serial,
                             values=(name, tattoo, fw, status_text),
                             tags=(tag,))
        # Restore selection if still present
        if sel_iid and sel_iid in self.tree.get_children():
            self.tree.selection_set(sel_iid)

        self._set_status(f"{len(rows)} device(s) connected | "
                         f"polling every {POLL_INTERVAL_SEC}s")

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
                self.root.after(0, self._show_success, dev, result)
            else:
                self.root.after(0, self._show_failure, dev, result, log_lines)
        except Exception as e:
            self._fixing_serials.discard(dev.serial)
            tb = traceback.format_exc()
            self.root.after(0, lambda: messagebox.showerror(
                "Fix crashed",
                f"Unexpected error fixing {dev.serial[:8]}:\n\n{e}\n\n{tb[-500:]}",
                parent=self.root))

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
