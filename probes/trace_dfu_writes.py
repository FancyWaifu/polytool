#!/usr/bin/env python3
"""Hook LegacyHost during a real DFU to see component-by-component HID writes."""
import frida
import sys
import time

JS = r"""
const k32 = Process.getModuleByName('kernel32.dll');
const wf = k32.findExportByName('WriteFile');
const rf = k32.findExportByName('ReadFile');
const cf = k32.findExportByName('CreateFileW');

let writes = 0;
let reads = 0;
let opens = 0;

if (cf) {
    Interceptor.attach(cf, {
        onEnter: function (args) {
            const name = args[0].isNull() ? '' : args[0].readUtf16String();
            if (name && (name.toLowerCase().indexOf('hid') >= 0 || name.toLowerCase().indexOf('vid_047f') >= 0)) {
                opens++;
                send({type:'open', name: name});
            }
        }
    });
}

if (wf) {
    Interceptor.attach(wf, {
        onEnter: function (args) {
            const handle = args[0];
            const buf = args[1];
            const len = args[2].toInt32();
            if (len > 0 && len < 256) {
                writes++;
                if (writes < 200 || writes % 1000 == 0) {
                    let bytes = '';
                    try {
                        const data = buf.readByteArray(Math.min(len, 32));
                        bytes = Array.from(new Uint8Array(data)).map(b => b.toString(16).padStart(2,'0')).join(' ');
                    } catch (e) {}
                    send({type:'write', count: writes, len: len, hex: bytes});
                }
            }
        }
    });
}

setInterval(function() {
    send({type:'stats', writes: writes, reads: reads, opens: opens});
}, 5000);
"""


def main():
    target = "LegacyHost.exe"
    mgr = frida.get_local_device()
    procs = [p for p in mgr.enumerate_processes() if p.name == target]
    if not procs:
        sys.exit(f"{target} not running")
    pid = procs[0].pid
    print(f"Attaching to {target} (pid {pid})")

    session = mgr.attach(pid)
    script = session.create_script(JS)

    def on_message(msg, data):
        if msg.get("type") == "send":
            p = msg.get("payload", {})
            t = p.get("type")
            if t == "stats":
                print(f"[stats] writes={p['writes']} reads={p['reads']} opens={p['opens']}")
            elif t == "open":
                print(f"[open #{p.get('opens','?')}] {p.get('name')}")
            elif t == "write":
                print(f"[write #{p['count']} len={p['len']}] {p['hex']}")

    script.on("message", on_message)
    script.load()
    print("Hook installed. Trigger a DFU. Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
        session.detach()


if __name__ == "__main__":
    main()
