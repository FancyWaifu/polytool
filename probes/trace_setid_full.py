"""Comprehensive HID + SetID-handler tracer for protocol RE.

Goal: capture every byte the SetID write sequence sends to the device,
correlated with the SetID state machine inside DFUManager.dll.

Hooks:
  - kernel32 CreateFileW              - track HID handle->path mapping
  - kernel32 WriteFile / ReadFile     - generic byte I/O on tracked handles
  - kernel32 DeviceIoControl          - all IOCTLs on tracked handles
  - hid.dll  HidD_SetFeature, GetFeature, SetOutputReport, GetInputReport
  - DFUManager.dll - SetID state machine (via string xrefs)

Strategy: capture once with a SUCCESSFUL setid write, then twice more
with different values, and diff to identify the byte that encodes the
value. We can then reproduce in pure Python via hidapi.

Output: JSONL log to /tmp/setid_full_<timestamp>.jsonl - structured for
offline analysis. Each event is a dict with timestamp, kind, and payload.

Usage:
  1. Make sure LegacyHost.exe is running
  2. python3 probes/trace_setid_full.py [output-suffix]
  3. Trigger fix-setid in another terminal (with --no-isolate so we get a
     clean single-device trace)
  4. Ctrl+C when done; analyze the JSONL
"""

import frida
import json
import sys
import time

OUTPUT = f"/tmp/setid_full_{int(time.time())}"
if len(sys.argv) > 1:
    OUTPUT = f"/tmp/setid_full_{sys.argv[1]}"

JS = r"""
const k32 = Process.getModuleByName('kernel32.dll');
const hid = Process.getModuleByName('HID.DLL');

const cf  = k32.findExportByName('CreateFileW');
const wf  = k32.findExportByName('WriteFile');
const rf  = k32.findExportByName('ReadFile');
const dio = k32.findExportByName('DeviceIoControl');
const ch  = k32.findExportByName('CloseHandle');

const handles = new Map();  // handle -> path
const counts = { writes: 0, reads: 0, ioctls: 0, hidd: 0 };

function ts() { return Date.now(); }

function isHidPath(name) {
    if (!name) return false;
    const lc = name.toLowerCase();
    return lc.indexOf('hid') >= 0 || lc.indexOf('vid_047f') >= 0;
}

function bytesHex(buf, len) {
    const n = Math.min(len, 96);
    try {
        const data = buf.readByteArray(n);
        return Array.from(new Uint8Array(data))
            .map(b => b.toString(16).padStart(2, '0')).join(' ');
    } catch (e) { return '<read_err>'; }
}

if (cf) {
    Interceptor.attach(cf, {
        onEnter: function(args) {
            try { this.name = args[0].isNull() ? '' : args[0].readUtf16String(); }
            catch (e) { this.name = ''; }
        },
        onLeave: function(ret) {
            if (!isHidPath(this.name)) return;
            handles.set(ret.toString(), this.name);
            send({t: ts(), k: 'open', h: ret.toString(), path: this.name});
        }
    });
}

if (ch) {
    Interceptor.attach(ch, {
        onEnter: function(args) {
            const h = args[0].toString();
            if (handles.has(h)) {
                send({t: ts(), k: 'close', h: h, path: handles.get(h)});
                handles.delete(h);
            }
        }
    });
}

if (wf) {
    Interceptor.attach(wf, {
        onEnter: function(args) {
            const h = args[0].toString();
            const path = handles.get(h);
            if (!path) return;
            const len = args[2].toInt32();
            counts.writes++;
            send({t: ts(), k: 'wf', h: h, path: path, len: len,
                  hex: bytesHex(args[1], len)});
        }
    });
}

if (rf) {
    Interceptor.attach(rf, {
        onEnter: function(args) {
            const h = args[0].toString();
            const path = handles.get(h);
            if (!path) return;
            this.path = path;
            this.h = h;
            this.buf = args[1];
            this.len = args[2].toInt32();
        },
        onLeave: function(ret) {
            if (!this.path) return;
            counts.reads++;
            send({t: ts(), k: 'rf', h: this.h, path: this.path, len: this.len,
                  hex: bytesHex(this.buf, this.len)});
        }
    });
}

if (dio) {
    Interceptor.attach(dio, {
        onEnter: function(args) {
            const h = args[0].toString();
            const path = handles.get(h);
            if (!path) return;
            const code = args[1].toInt32() >>> 0;
            // Only HID-class IOCTLs (0x000B0000-0x000B1FFF)
            if (code < 0xB0000 || code >= 0xB2000) return;
            this.h = h; this.path = path;
            this.code = code;
            this.inLen = args[3].toInt32();
            this.outBuf = args[4];
            this.outLen = args[5].toInt32();
            counts.ioctls++;
            const hex = this.inLen > 0 ? bytesHex(args[2], this.inLen) : '';
            send({t: ts(), k: 'ioctl_in', code: '0x' + code.toString(16),
                  h: h, path: path, inLen: this.inLen, outLen: this.outLen,
                  hex: hex});
        },
        onLeave: function(ret) {
            if (!this.path) return;
            if (this.outLen > 0) {
                send({t: ts(), k: 'ioctl_out', code: '0x' + this.code.toString(16),
                      h: this.h, path: this.path, outLen: this.outLen,
                      ok: ret.toInt32() !== 0,
                      hex: bytesHex(this.outBuf, this.outLen)});
            }
        }
    });
}

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
                this.fn = fn; this.path = path; this.buf = args[1]; this.len = len;
                counts.hidd++;
                if (fn.indexOf('Set') >= 0) {
                    send({t: ts(), k: 'hidd_set', fn: fn, path: path, len: len,
                          hex: bytesHex(args[1], len)});
                }
            },
            onLeave: function(ret) {
                if (this.fn && this.fn.indexOf('Get') >= 0) {
                    send({t: ts(), k: 'hidd_get', fn: this.fn, path: this.path,
                          len: this.len, ok: ret.toInt32() !== 0,
                          hex: bytesHex(this.buf, this.len)});
                }
            }
        });
    }
}

// String-based xrefs for SetID state machine inside DFUManager.dll
// (so events get tagged with which protocol phase they belong to)
let dfu = null;
try { dfu = Process.getModuleByName('DFUManager.dll'); } catch (e) {}
if (dfu) {
    // Scan for the marker strings; their xrefs lead to the relevant funcs.
    const markers = [
        'Performing SetID update.',
        'Failed to enable SetID write mode',
        'Failed to set setID',
        'Failed to disable SetID write mode',
        'Failed getting SetID after update',
        'SetID update succeeded.',
    ];
    // We can't easily hook the functions without IDA-like analysis, so
    // instead we hook strstr/wcsstr-style log emitters - but those vary.
    // For now just report the module base so post-analysis can correlate.
    send({t: ts(), k: 'dfu_module', name: dfu.name, base: dfu.base.toString(),
          size: dfu.size});
}

setInterval(function() {
    send({t: ts(), k: 'stats',
          writes: counts.writes, reads: counts.reads,
          ioctls: counts.ioctls, hidd: counts.hidd,
          handles: handles.size});
}, 5000);
send({t: ts(), k: 'ready'});
"""


def main():
    target = "LegacyHost.exe"
    mgr = frida.get_local_device()
    procs = [p for p in mgr.enumerate_processes() if p.name == target]
    if not procs:
        sys.exit(f"{target} not running")
    pid = procs[0].pid
    print(f"Attaching to {target} (pid {pid})")
    print(f"Logging to {OUTPUT}.jsonl")

    fp = open(OUTPUT + ".jsonl", "w")
    session = mgr.attach(pid)
    script = session.create_script(JS)

    seen_paths = set()

    def on_message(msg, data):
        if msg.get("type") != "send":
            return
        p = msg.get("payload", {})
        # Pretty-print to stdout for live observability
        k = p.get("k", "?")
        if k == "ready":
            print("[ready] hooks installed")
        elif k == "stats":
            print(f"[stats] writes={p['writes']} reads={p['reads']} "
                  f"ioctls={p['ioctls']} hidd={p['hidd']} handles={p['handles']}")
        elif k == "open" and p["path"] not in seen_paths:
            seen_paths.add(p["path"])
            print(f"[open] {p['path']}")
        elif k in ("hidd_set", "hidd_get"):
            print(f"[{k} {p.get('fn','?')} len={p['len']}] "
                  f"{p['path'][-50:]}\n  hex: {p['hex']}")
        elif k == "ioctl_in" and p["inLen"] > 0:
            print(f"[ioctl_in {p['code']} in={p['inLen']}] "
                  f"{p['path'][-50:]}\n  hex: {p['hex']}")
        # Always log raw event to JSONL for offline analysis
        fp.write(json.dumps(p) + "\n")
        fp.flush()

    script.on("message", on_message)
    script.load()
    print("Hook installed. Trigger fix-setid in another terminal.")
    print("Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\nStopped. Log: {OUTPUT}.jsonl")
        session.detach()
        fp.close()


if __name__ == "__main__":
    main()
