#!/usr/bin/env python3
"""Minimal HID-write capture: WriteFile + HidD_SetFeature/SetOutputReport
on HID.DLL only. Earlier multi-module enumeration was hanging Frida v17."""
import frida, time, json, sys

JS = r"""
const k32 = Process.getModuleByName('kernel32.dll');
const hid = Process.getModuleByName('HID.DLL');

const cf = k32.findExportByName('CreateFileW');
const wf = k32.findExportByName('WriteFile');
const ch = k32.findExportByName('CloseHandle');

const handles = new Map();

function hex(buf, len) {
    const n = Math.min(len, 64);
    try {
        const data = buf.readByteArray(n);
        return Array.from(new Uint8Array(data))
            .map(b => b.toString(16).padStart(2,'0')).join(' ');
    } catch (e) { return '<read err>'; }
}

if (cf) {
    Interceptor.attach(cf, {
        onEnter: function(args) {
            try { this.name = args[0].isNull() ? '' : args[0].readUtf16String(); }
            catch (e) { this.name = ''; }
        },
        onLeave: function(ret) {
            const lc = (this.name || '').toLowerCase();
            if (lc.indexOf('hid') < 0 && lc.indexOf('vid_047f') < 0) return;
            handles.set(ret.toString(), this.name);
        }
    });
}

if (ch) {
    Interceptor.attach(ch, {
        onEnter: function(args) { handles.delete(args[0].toString()); }
    });
}

if (wf) {
    Interceptor.attach(wf, {
        onEnter: function(args) {
            const h = args[0].toString();
            const path = handles.get(h);
            if (!path) return;
            const len = args[2].toInt32();
            send({k: 'wf', path: path, len: len, hex: hex(args[1], len)});
        }
    });
}

if (hid) {
    for (const fn of ['HidD_SetFeature', 'HidD_SetOutputReport',
                      'HidD_GetFeature', 'HidD_GetInputReport']) {
        const sym = hid.findExportByName(fn);
        if (!sym) {
            send({k: 'no_export', fn: fn});
            continue;
        }
        send({k: 'hooked', fn: fn});
        Interceptor.attach(sym, {
            onEnter: function(args) {
                const h = args[0].toString();
                const path = handles.get(h) || ('?h=' + h);
                const len = args[2].toInt32();
                this.fn = fn;
                this.path = path;
                this.buf = args[1];
                this.len = len;
                if (fn.indexOf('Set') >= 0) {
                    send({k: 'hidd_set', fn: fn, path: path, len: len,
                          hex: hex(args[1], len)});
                }
            },
            onLeave: function(ret) {
                if (this.fn && this.fn.indexOf('Get') >= 0) {
                    send({k: 'hidd_get', fn: this.fn, path: this.path,
                          len: this.len, ok: ret.toInt32() !== 0,
                          hex: hex(this.buf, this.len)});
                }
            }
        });
    }
}

send({k: 'ready'});
"""


def main():
    mgr = frida.get_local_device()
    procs = [p for p in mgr.enumerate_processes() if p.name == 'LegacyHost.exe']
    if not procs:
        sys.exit("LegacyHost.exe not running")
    print(f"attaching to pid {procs[0].pid}")
    s = mgr.attach(procs[0].pid)
    script = s.create_script(JS)

    def on_msg(msg, data):
        if msg.get('type') != 'send': return
        p = msg['payload']
        k = p.get('k', '?')
        if k == 'wf':
            print(f"[WriteFile len={p['len']}]  {p['path'][-50:]}")
            print(f"  hex: {p['hex']}")
        elif k == 'hidd_set':
            print(f"[{p['fn']} len={p['len']}]  {p['path'][-50:]}")
            print(f"  hex: {p['hex']}")
        elif k == 'hidd_get':
            print(f"[{p['fn']} ok={p['ok']} len={p['len']}]  {p['path'][-50:]}")
            print(f"  hex: {p['hex']}")
        elif k == 'hooked':
            print(f"hooked {p['fn']}")
        elif k == 'no_export':
            print(f"NO EXPORT: {p['fn']}")
        elif k == 'ready':
            print("READY — trigger fix-setid now")

    script.on('message', on_msg)
    script.load()
    print("hooks installed; waiting...")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        s.detach()


if __name__ == "__main__":
    main()
