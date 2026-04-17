#!/usr/bin/env python3
"""Inject a poly_bus_request call into the running LensService.exe process.

LCS already has PolyBus initialized with working device handles. We attach
via Frida and call poly_bus_request directly with our crafted JSON to:
  1. GET "Set ID" first (verify connection works, see current value)
  2. Try a SET (only if --write specified)

Usage:
  python3 probes/polybus_inject.py get-setid <device_id>
  python3 probes/polybus_inject.py set-setid <device_id> <value> --write
  python3 probes/polybus_inject.py list-settings <device_id>

Device IDs (full HID path):
  AC28: USB\VID_047F&PID_AC28\049160FE6715450F8A4FB2C44EA91AD2
  AC27: USB\VID_047F&PID_AC27\8319A383DABA4E73B2FFE262392F486C
"""
import argparse
import json
import sys
import time

import frida


JS_INJECTOR = r"""
let polybus;
Process.enumerateModules().forEach(m => {
    if (m.name.toLowerCase().indexOf('polybus') >= 0) polybus = m;
});
if (!polybus) throw new Error('PolyBus.dll not found in target process');

const reqAddr = polybus.findExportByName('poly_bus_request');
const getUtf8Addr = polybus.findExportByName('poly_bus_get_utf8');
const freeAddr = polybus.findExportByName('poly_bus_free');

const polyBusRequest = new NativeFunction(reqAddr, 'int', ['pointer', 'pointer']);
const polyBusGetUtf8 = new NativeFunction(getUtf8Addr, 'pointer', ['pointer']);
const polyBusFree = new NativeFunction(freeAddr, 'int', ['pointer']);

rpc.exports = {
    sendRequest: function (jsonStr) {
        // Allocate a null-terminated UTF-8 buffer for the request
        const reqBytes = Memory.allocUtf8String(jsonStr);
        // Allocate space for the response handle
        const respHandlePtr = Memory.alloc(Process.pointerSize);
        respHandlePtr.writePointer(NULL);

        const result = polyBusRequest(reqBytes, respHandlePtr);
        const respHandle = respHandlePtr.readPointer();

        let respStr = null;
        if (!respHandle.isNull()) {
            const utf8Ptr = polyBusGetUtf8(respHandle);
            if (!utf8Ptr.isNull()) {
                respStr = utf8Ptr.readUtf8String();
            }
            polyBusFree(respHandle);
        }
        return { result: result, response: respStr };
    }
};
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["get-setid", "set-setid", "list-settings", "raw"])
    p.add_argument("device_id", nargs="?", default=None)
    p.add_argument("value", nargs="?", default=None)
    p.add_argument("--write", action="store_true",
                   help="Required to actually send a SET (otherwise dry-run)")
    p.add_argument("--target", default="LensService.exe")
    p.add_argument("--raw-json", help="Raw JSON string (for 'raw' command)")
    args = p.parse_args()

    mgr = frida.get_local_device()
    procs = [pr for pr in mgr.enumerate_processes() if pr.name.lower() == args.target.lower()]
    if not procs:
        sys.exit(f"Process not found: {args.target} — start the Poly Lens Control Service first")
    pid = procs[0].pid
    print(f"Attaching to {args.target} (pid {pid})")

    session = mgr.attach(pid)
    script = session.create_script(JS_INJECTOR)
    script.load()
    api = script.exports_sync

    def send(req):
        body = json.dumps(req)
        print(f">> {body}")
        result = api.send_request(body)
        print(f"   result={result['result']}")
        if result["response"]:
            try:
                parsed = json.loads(result["response"])
                print(f"<< {json.dumps(parsed, indent=2)}")
                return parsed
            except json.JSONDecodeError:
                print(f"<< (raw) {result['response']}")
                return None
        else:
            print("<< (no response)")
            return None

    request_id = 1

    if args.command == "get-setid":
        send({
            "params": {"name": "Set ID"},
            "method": "GET",
            "resource_id": "/device/settings",
            "device_id": args.device_id,
            "request_id": request_id,
        })

    elif args.command == "list-settings":
        send({
            "params": {"get_values": False},
            "method": "LIST",
            "resource_id": "/device/settings",
            "device_id": args.device_id,
            "request_id": request_id,
        })

    elif args.command == "set-setid":
        if not args.value:
            sys.exit("set-setid requires <value>")
        if not args.write:
            print("DRY RUN — would send the following SET request. Add --write to send.")
            print(json.dumps({
                "params": {"name": "Set ID", "value": args.value},
                "method": "SET",
                "resource_id": "/device/settings",
                "device_id": args.device_id,
                "request_id": request_id,
            }, indent=2))
            return

        # First read current value
        print("\n--- Step 1: read current Set ID ---")
        send({
            "params": {"name": "Set ID"},
            "method": "GET",
            "resource_id": "/device/settings",
            "device_id": args.device_id,
            "request_id": request_id,
        })
        request_id += 1

        # Then write
        print(f"\n--- Step 2: WRITE Set ID = {args.value!r} ---")
        send({
            "params": {"name": "Set ID", "value": args.value},
            "method": "SET",
            "resource_id": "/device/settings",
            "device_id": args.device_id,
            "request_id": request_id,
        })
        request_id += 1

        # Wait briefly then read back
        time.sleep(2)
        print("\n--- Step 3: read back Set ID after write ---")
        send({
            "params": {"name": "Set ID"},
            "method": "GET",
            "resource_id": "/device/settings",
            "device_id": args.device_id,
            "request_id": request_id,
        })

    elif args.command == "raw":
        if not args.raw_json:
            sys.exit("raw requires --raw-json")
        send(json.loads(args.raw_json))

    session.detach()


if __name__ == "__main__":
    main()
