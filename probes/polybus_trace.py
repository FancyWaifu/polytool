#!/usr/bin/env python3
"""Trace all poly_bus_request calls made by LensService.

Prints the JSON request body and a snippet of the response. Helps RE the
PolyBus.dll request format — especially what triggers SetDeviceFirmwareVersion.

Usage:
  python3 probes/polybus_trace.py [--filter SUBSTRING]
"""
import argparse
import sys
import time

import frida

JS_HOOK = r"""
let polybus;
try {
    polybus = Process.getModuleByName('PolyBus.dll');
} catch (e) {
    // Try with full path or wait for module load
    Process.enumerateModules().forEach(m => {
        if (m.name.toLowerCase().indexOf('polybus') >= 0) polybus = m;
    });
}
if (!polybus) {
    console.log('[!] PolyBus.dll not loaded yet — exiting');
    throw new Error('PolyBus.dll not found');
}
console.log('[+] PolyBus.dll base: ' + polybus.base + ' name=' + polybus.name);

const reqAddr = polybus.findExportByName('poly_bus_request');
const reqAsyncAddr = polybus.findExportByName('poly_bus_request_async');
console.log('[+] poly_bus_request: ' + reqAddr);
console.log('[+] poly_bus_request_async: ' + reqAsyncAddr);

function readReqBytes(reqPtr) {
    // Request is a byte[] from C# — passed as raw pointer to a .NET-managed array.
    // The first 8 bytes on .NET arrays are header (sync block + method table). Try
    // reading length at offset 4 (Length field for managed arrays on x86 is at
    // [ptr-4]) — actually for byte[] passed to native code via DllImport with
    // [MarshalAs(LPArray)] semantics, it's marshaled as plain pointer to data.
    // So just read until we hit a null terminator or use a sentinel scan.
    if (reqPtr.isNull()) return '<null>';
    try {
        // Try reading as null-terminated UTF-8 first
        const s = reqPtr.readUtf8String();
        if (s && s.length > 0) return s;
    } catch (e) {}
    try {
        return hexdump(reqPtr, { length: 256 });
    } catch (e) {
        return '<unreadable>';
    }
}

if (reqAddr) {
    Interceptor.attach(reqAddr, {
        onEnter: function (args) {
            this.req = args[0];
            this.respPtr = args[1];
            const body = readReqBytes(this.req);
            send({type: 'req', body: body.substring(0, 800)});
        },
        onLeave: function (retval) {
            // retval is the PolyBusDllResult enum int
            const resp = this.respPtr.readPointer();
            let respStr = '<empty>';
            try {
                if (!resp.isNull()) {
                    respStr = resp.readUtf8String();
                    if (respStr) respStr = respStr.substring(0, 600);
                }
            } catch (e) { respStr = '<unreadable>'; }
            send({type: 'resp', code: retval.toInt32(), body: respStr});
        }
    });
    console.log('[+] Hooked poly_bus_request');
}

if (reqAsyncAddr) {
    Interceptor.attach(reqAsyncAddr, {
        onEnter: function (args) {
            const body = readReqBytes(args[0]);
            send({type: 'req-async', body: body.substring(0, 800)});
        }
    });
    console.log('[+] Hooked poly_bus_request_async');
}
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--filter", help="Only print messages containing this substring")
    p.add_argument("--target", default="LensService.exe", help="Target process name")
    args = p.parse_args()

    mgr = frida.get_local_device()
    procs = [pr for pr in mgr.enumerate_processes() if pr.name.lower() == args.target.lower()]
    if not procs:
        print(f"Process not found: {args.target}", file=sys.stderr)
        sys.exit(1)
    pid = procs[0].pid
    print(f"Attaching to {args.target} (pid {pid})")

    session = mgr.attach(pid)
    script = session.create_script(JS_HOOK)

    counter = [0]

    def on_message(message, data):
        if message.get("type") != "send":
            print(f"[meta] {message}")
            return
        payload = message.get("payload", {})
        msg_type = payload.get("type", "?")
        body = payload.get("body", "")
        if args.filter and args.filter.lower() not in body.lower():
            return
        counter[0] += 1
        n = counter[0]
        if msg_type == "resp":
            code = payload.get("code", "?")
            print(f"--- #{n} RESP code={code} ---")
            print(f"  {body}")
        else:
            print(f"--- #{n} {msg_type.upper()} ---")
            print(f"  {body}")

    script.on("message", on_message)
    script.load()
    print("Hook installed. Waiting for traffic. Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
        session.detach()


if __name__ == "__main__":
    main()
