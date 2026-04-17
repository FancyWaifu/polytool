#!/usr/bin/env python3
"""Find where LegacyHost is actually issuing the SetID write.

The earlier IOCTL trace showed only reads (every captured ioctl had
inLen=0), so the write must happen via a different path: WriteFile,
HidD_SetFeature/SetOutputReport (which we may have missed because the
symbol lookup happened before hid.dll was loaded), or a custom Poly
driver.

This script:
  1. Lists every loaded module so we can see what writers exist.
  2. Hooks WriteFile (with payload capture, filtered to HID-ish handles).
  3. Hooks HidD_* by enumerating exports of every loaded module containing
     "hid" — catches HidApi.dll, hidapi.dll, custom Poly wrappers, etc.
  4. Prints stack trace on the first SetFeature-style write so we can see
     where in DFUManager.dll it originates.
"""
import frida
import sys
import time

JS = r"""
const k32 = Process.getModuleByName('kernel32.dll');
const cf  = k32.findExportByName('CreateFileW');
const wf  = k32.findExportByName('WriteFile');

const handles = new Map();

function isInteresting(name) {
    if (!name) return false;
    const lc = name.toLowerCase();
    return lc.indexOf('hid') >= 0 || lc.indexOf('vid_047f') >= 0;
}

if (cf) {
    Interceptor.attach(cf, {
        onEnter: function(args) {
            try { this.name = args[0].isNull() ? '' : args[0].readUtf16String(); }
            catch (e) { this.name = ''; }
        },
        onLeave: function(ret) {
            if (!isInteresting(this.name)) return;
            handles.set(ret.toString(), this.name);
        }
    });
}

function bytesHex(buf, len) {
    const n = Math.min(len, 64);
    try {
        const data = buf.readByteArray(n);
        return Array.from(new Uint8Array(data))
            .map(b => b.toString(16).padStart(2,'0')).join(' ');
    } catch (e) { return ''; }
}

// Enumerate every loaded module — emit once at startup so we know what
// libraries are in play.
const mods = Process.enumerateModules();
const interestingMods = mods.filter(m => {
    const n = m.name.toLowerCase();
    return n.includes('hid') || n.includes('plt') || n.includes('dfu') ||
           n.includes('fwu') || n.includes('poly') || n.includes('clockwork');
});
send({type:'modules', mods: interestingMods.map(m => m.name)});

// WriteFile capture for HID-ish handles
if (wf) {
    Interceptor.attach(wf, {
        onEnter: function(args) {
            const h = args[0].toString();
            const path = handles.get(h);
            if (!path) return;
            const len = args[2].toInt32();
            send({type:'wf', path: path, len: len, hex: bytesHex(args[1], len),
                  stack: Thread.backtrace(this.context, Backtracer.ACCURATE)
                      .slice(0, 6)
                      .map(DebugSymbol.fromAddress).map(s => s.toString())
            });
        }
    });
}

// Hook HidD_* on every loaded module that exports them
for (const mod of mods) {
    let exports;
    try { exports = mod.enumerateExports(); } catch (e) { continue; }
    for (const exp of exports) {
        if (!exp.name.startsWith('HidD_')) continue;
        if (exp.name === 'HidD_SetFeature' || exp.name === 'HidD_SetOutputReport') {
            try {
                Interceptor.attach(exp.address, {
                    onEnter: function(args) {
                        const h = args[0].toString();
                        const path = handles.get(h) || ('?h=' + h);
                        const len = args[2].toInt32();
                        send({type:'hidd_set', mod: mod.name, fn: exp.name,
                              path: path, len: len,
                              hex: bytesHex(args[1], len),
                              stack: Thread.backtrace(this.context, Backtracer.ACCURATE)
                                  .slice(0, 6)
                                  .map(DebugSymbol.fromAddress).map(s => s.toString())
                        });
                    }
                });
                send({type:'hooked', mod: mod.name, fn: exp.name});
            } catch (e) {}
        }
    }
}
"""


def main():
    target = "LegacyHost.exe"
    mgr = frida.get_local_device()
    procs = [p for p in mgr.enumerate_processes() if p.name == target]
    if not procs:
        sys.exit(f"{target} not running")
    pid = procs[0].pid
    print(f"Attaching to {target} (pid {pid})\n")

    session = mgr.attach(pid)
    script = session.create_script(JS)

    def on_message(msg, data):
        if msg.get("type") != "send":
            return
        p = msg.get("payload", {})
        t = p.get("type")
        if t == "modules":
            print("[modules]", ", ".join(p["mods"]))
        elif t == "hooked":
            print(f"[hooked] {p['mod']}::{p['fn']}")
        elif t == "wf":
            print(f"[WriteFile len={p['len']}] {p['path'][-40:]}")
            print(f"  hex: {p['hex']}")
            for s in p["stack"]:
                print(f"     {s}")
        elif t == "hidd_set":
            print(f"[{p['fn']} via {p['mod']} len={p['len']}] {p['path'][-40:]}")
            print(f"  hex: {p['hex']}")
            for s in p["stack"]:
                print(f"     {s}")

    script.on("message", on_message)
    script.load()
    print("Hooked. Trigger fix-setid in another terminal. Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        session.detach()


if __name__ == "__main__":
    main()
