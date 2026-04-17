#!/usr/bin/env python3
"""Trace LegacyHost.exe HID-handle routing during a setid DFU.

Goal: determine whether the NVRAM write goes to the device whose serial we
passed (--serial 820F03A0...) or to a different AC28 sharing the same VID/PID.

Method:
  1. Hook CreateFileW — record every HID device opened, mapping the returned
     handle to its full device path (which contains the device instance ID
     and lets us tell AC28-A from AC28-B).
  2. Hook CloseHandle — drop the handle from the map when released.
  3. Hook WriteFile/ReadFile — for any traced handle, log the write/read with
     the device path and a hex preview of the payload.
  4. Hook HidD_SetFeature/HidD_GetFeature/HidD_SetOutputReport — the actual
     calls Poly's stack uses to talk to the device.
  5. Tally writes per device path so we can see which device received the
     setid write.

Usage:
  1. Make sure LegacyHost.exe is running (polytool fix-setid spawns it).
  2. Run this probe in another terminal: python3 probes/trace_setid_routing.py
  3. Run polytool fix-setid <serial> --yes --force in a third terminal.
  4. Watch the live output here. After "DFU complete" appears, the summary
     will show which device path got the writes.
"""
import frida
import sys
import time

JS = r"""
const k32 = Process.getModuleByName('kernel32.dll');
const cf  = k32.findExportByName('CreateFileW');
const wf  = k32.findExportByName('WriteFile');
const rf  = k32.findExportByName('ReadFile');
const ch  = k32.findExportByName('CloseHandle');
const dio = k32.findExportByName('DeviceIoControl');

// hidapi exports — Poly's wrapper goes through these
let hid = null;
try { hid = Process.getModuleByName('hid.dll'); } catch (e) {}

// Map handle -> full device path (only for paths we care about: HID + Poly VID)
const handles = new Map();
const writeCounts = new Map();
const readCounts = new Map();
const createCounts = new Map();

function isInteresting(name) {
    if (!name) return false;
    const lc = name.toLowerCase();
    return lc.includes('hid') || lc.includes('vid_047f') || lc.includes('vid_05a7');
}

if (cf) {
    Interceptor.attach(cf, {
        onEnter: function(args) {
            try {
                this.name = args[0].isNull() ? '' : args[0].readUtf16String();
            } catch (e) { this.name = ''; }
        },
        onLeave: function(ret) {
            if (!isInteresting(this.name)) return;
            const h = ret.toString();
            handles.set(h, this.name);
            createCounts.set(this.name, (createCounts.get(this.name) || 0) + 1);
            send({type:'open', handle: h, path: this.name});
        }
    });
}

if (ch) {
    Interceptor.attach(ch, {
        onEnter: function(args) {
            const h = args[0].toString();
            if (handles.has(h)) {
                send({type:'close', handle: h, path: handles.get(h)});
                handles.delete(h);
            }
        }
    });
}

function bytesPreview(buf, len) {
    const n = Math.min(len, 32);
    try {
        const data = buf.readByteArray(n);
        return Array.from(new Uint8Array(data))
            .map(b => b.toString(16).padStart(2,'0')).join(' ');
    } catch (e) { return ''; }
}

if (wf) {
    Interceptor.attach(wf, {
        onEnter: function(args) {
            const h = args[0].toString();
            if (!handles.has(h)) return;
            const path = handles.get(h);
            const len = args[2].toInt32();
            writeCounts.set(path, (writeCounts.get(path) || 0) + 1);
            const c = writeCounts.get(path);
            // Log first 8 writes per path + every 50th after
            if (c <= 8 || c % 50 === 0) {
                send({type:'write', path: path, count: c, len: len,
                      hex: bytesPreview(args[1], len)});
            }
        }
    });
}

if (rf) {
    Interceptor.attach(rf, {
        onEnter: function(args) {
            const h = args[0].toString();
            if (!handles.has(h)) return;
            const path = handles.get(h);
            readCounts.set(path, (readCounts.get(path) || 0) + 1);
        }
    });
}

if (dio) {
    Interceptor.attach(dio, {
        onEnter: function(args) {
            const h = args[0].toString();
            if (!handles.has(h)) return;
            const path = handles.get(h);
            const ioctl = args[1].toInt32() >>> 0;
            // HID IOCTL range starts at 0xb0000000-ish; relevant writes look like
            // IOCTL_HID_SET_FEATURE = 0xb0191
            send({type:'ioctl', path: path, ioctl: '0x'+ioctl.toString(16)});
        }
    });
}

// Hook hidapi setfeature/setoutput if present — these are the high-level
// HID writes Poly's setid path will use.
if (hid) {
    for (const fn of ['HidD_SetFeature', 'HidD_SetOutputReport',
                      'HidD_GetFeature', 'HidD_GetInputReport']) {
        const sym = hid.findExportByName(fn);
        if (!sym) continue;
        Interceptor.attach(sym, {
            onEnter: function(args) {
                const h = args[0].toString();
                if (!handles.has(h)) return;
                const path = handles.get(h);
                const len = args[2].toInt32();
                const hex = bytesPreview(args[1], len);
                send({type:'hidd', fn: fn, path: path, len: len, hex: hex});
            }
        });
    }
}

setInterval(function() {
    const summary = {};
    for (const [p, c] of writeCounts.entries()) summary[p] = c;
    send({type:'summary', writeCounts: summary,
          readCounts: Object.fromEntries(readCounts.entries()),
          openHandles: handles.size});
}, 5000);
"""


def main():
    target = "LegacyHost.exe"
    mgr = frida.get_local_device()
    procs = [p for p in mgr.enumerate_processes() if p.name == target]
    if not procs:
        sys.exit(f"{target} not running — start polytool fix-setid first to spawn it.")
    pid = procs[0].pid
    print(f"Attaching to {target} (pid {pid})\n")

    session = mgr.attach(pid)
    script = session.create_script(JS)

    seen_paths = set()

    def on_message(msg, data):
        if msg.get("type") != "send":
            return
        p = msg.get("payload", {})
        t = p.get("type")
        if t == "open":
            path = p.get("path", "")
            if path not in seen_paths:
                seen_paths.add(path)
                print(f"[+ open] {p.get('handle'):>8s}  {path}")
        elif t == "close":
            print(f"[- close] {p.get('handle'):>8s}  {p.get('path')}")
        elif t == "write":
            print(f"[write #{p['count']:>4d} len={p['len']:>3d}] "
                  f"{p['path'][:80]}\n        hex: {p['hex']}")
        elif t == "hidd":
            print(f"[{p['fn']:<22s} len={p['len']:>3d}] {p['path'][:80]}\n"
                  f"        hex: {p['hex']}")
        elif t == "ioctl":
            # only print HID-ish ioctls
            if "b" in p["ioctl"][:4]:
                print(f"[ioctl {p['ioctl']:>10s}] {p['path'][:80]}")
        elif t == "summary":
            print("\n=== summary ===")
            for path, c in sorted(p["writeCounts"].items(), key=lambda x: -x[1]):
                print(f"  writes={c:>5d}  {path}")
            print()

    script.on("message", on_message)
    script.load()
    print("Hook installed. Trigger a fix-setid in another terminal. Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
        session.detach()


if __name__ == "__main__":
    main()
