#!/usr/bin/env python3
"""Capture the full HID byte-level protocol used during a SetID write.

Goal: produce a transcript precise enough to reimplement SetID::Start
ourselves in pure Python, bypassing LegacyHost entirely. Hooks every
HidD_* call and the DeviceIoControl IOCTLs hidclass.sys uses for
SET_FEATURE / GET_FEATURE / SET_OUTPUT_REPORT / GET_INPUT_REPORT.

IOCTLs of interest (defined in hidclass.h):
    IOCTL_HID_GET_FEATURE        = 0x000B0192
    IOCTL_HID_SET_FEATURE        = 0x000B0191
    IOCTL_HID_GET_INPUT_REPORT   = 0x000B01A2
    IOCTL_HID_SET_OUTPUT_REPORT  = 0x000B01A4

We attach to LegacyHost.exe (after polytool fix-setid spawns it). For each
captured call we emit:
    - call site (HidD_SetFeature vs IOCTL)
    - handle and resolved device path (so we can tell which AC28 received it)
    - full payload hex
"""
import frida
import sys
import time

JS = r"""
const k32 = Process.getModuleByName('kernel32.dll');
const cf  = k32.findExportByName('CreateFileW');
const dio = k32.findExportByName('DeviceIoControl');
const ch  = k32.findExportByName('CloseHandle');

let hid = null;
try { hid = Process.getModuleByName('hid.dll'); } catch (e) {}

// handle -> path
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
if (ch) {
    Interceptor.attach(ch, {
        onEnter: function(args) {
            handles.delete(args[0].toString());
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

// Capture every IOCTL in the HID device-type range (0x000B0000-0x000B1FFF)
// since Poly's driver uses non-standard function codes alongside the
// well-known IOCTL_HID_GET_FEATURE / SET_FEATURE.
if (dio) {
    Interceptor.attach(dio, {
        onEnter: function(args) {
            const code = args[1].toInt32() >>> 0;
            if (code < 0xB0000 || code >= 0xB2000) return;
            const h = args[0].toString();
            const path = handles.get(h);
            if (!path) return;  // only care about HID handles we tracked
            const inBuf = args[2];
            const inLen = args[3].toInt32();
            const outBuf = args[4];
            const outLen = args[5].toInt32();
            this.code = code;
            this.path = path;
            this.outBuf = outBuf;
            this.outLen = outLen;
            const payload = inLen > 0 ? bytesHex(inBuf, inLen) : '';
            send({type:'ioctl_in', code: '0x' + code.toString(16),
                  path: path, inLen: inLen, outLen: outLen, hex: payload});
        },
        onLeave: function(ret) {
            if (!this.code) return;
            if (this.outLen > 0) {
                const hex = bytesHex(this.outBuf, this.outLen);
                send({type:'ioctl_out', code: '0x' + this.code.toString(16),
                      path: this.path, outLen: this.outLen, hex: hex,
                      ok: ret.toInt32() !== 0});
            }
        }
    });
}

// HidD_* convenience APIs (mostly thin wrappers around the IOCTLs above)
if (hid) {
    for (const fn of ['HidD_SetFeature', 'HidD_GetFeature',
                      'HidD_SetOutputReport', 'HidD_GetInputReport']) {
        const sym = hid.findExportByName(fn);
        if (!sym) continue;
        Interceptor.attach(sym, {
            onEnter: function(args) {
                const h = args[0].toString();
                const path = handles.get(h) || ('?h=' + h);
                const len = args[2].toInt32();
                this.fn = fn;
                this.path = path;
                this.buf = args[1];
                this.len = len;
                if (fn === 'HidD_SetFeature' || fn === 'HidD_SetOutputReport') {
                    send({type:'hidd_call', fn: fn, path: path, len: len,
                          hex: bytesHex(args[1], len)});
                } else {
                    send({type:'hidd_call', fn: fn, path: path, len: len, hex: ''});
                }
            },
            onLeave: function(ret) {
                if (this.fn === 'HidD_GetFeature' || this.fn === 'HidD_GetInputReport') {
                    send({type:'hidd_result', fn: this.fn, path: this.path,
                          ok: ret.toInt32() !== 0,
                          hex: bytesHex(this.buf, this.len)});
                }
            }
        });
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

    def short_path(p):
        # \\?\hid#vid_047f&pid_ac28&mi_00&col06#7&17c1e0fb&0&0005#{...}
        # Keep enough to identify the physical instance
        if "&pid_" in p:
            try:
                seg = p.split("#")
                return f"{seg[1].split('&', 3)[1]}/{seg[2][:18]}"
            except Exception:
                pass
        return p[:60]

    def on_message(msg, data):
        if msg.get("type") != "send":
            return
        p = msg.get("payload", {})
        t = p.get("type")
        if t == "ioctl_in":
            print(f"[IOCTL {p['code']:>7s} in={p['inLen']:>3d}/out={p['outLen']:>3d}] "
                  f"{short_path(p['path'])}  hex: {p['hex']}")
        elif t == "ioctl_out":
            ok = "OK" if p.get('ok') else "ERR"
            print(f"[IOCTL {p['code']:>7s} <-{ok} len={p['outLen']:>3d}] "
                  f"{short_path(p['path'])}  hex: {p['hex']}")
        elif t == "hidd_call":
            tag = "->" if p['fn'].startswith("HidD_Set") else "GET"
            print(f"[{p['fn']:<22s} {tag} len={p['len']:>3d}] "
                  f"{short_path(p['path'])}  hex: {p['hex']}")
        elif t == "hidd_result":
            print(f"[{p['fn']:<22s} <- ok={p['ok']:1d} len={len(p['hex'].split())}] "
                  f"{short_path(p['path'])}  hex: {p['hex']}")

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
