#!/usr/bin/env python3
"""Probe PolyBus REST endpoints to find one that maps to SetDeviceFirmwareVersion.

Sends crafted requests via Frida-injected poly_bus_request to LensService.exe.
Tries known resource paths with method/params combinations.
"""
import frida
import json
import sys
import time

JS = r"""
let polybus;
Process.enumerateModules().forEach(m => { if (m.name.toLowerCase().indexOf('polybus') >= 0) polybus = m; });
const reqAddr = polybus.findExportByName('poly_bus_request');
const getUtf8Addr = polybus.findExportByName('poly_bus_get_utf8');
const freeAddr = polybus.findExportByName('poly_bus_free');
const polyBusRequest = new NativeFunction(reqAddr, 'int', ['pointer', 'pointer']);
const polyBusGetUtf8 = new NativeFunction(getUtf8Addr, 'pointer', ['pointer']);
const polyBusFree = new NativeFunction(freeAddr, 'int', ['pointer']);
rpc.exports = {
    s: function (s) {
        const req = Memory.allocUtf8String(s);
        const rh = Memory.alloc(Process.pointerSize);
        rh.writePointer(NULL);
        const r = polyBusRequest(req, rh);
        const h = rh.readPointer();
        let resp = null;
        if (!h.isNull()) {
            const p = polyBusGetUtf8(h);
            if (!p.isNull()) resp = p.readUtf8String();
            polyBusFree(h);
        }
        return { result: r, response: resp };
    }
};
"""

DEVICE_AC28 = "1125933431"  # Long-ID format that worked in earlier captures


def main():
    target_value = sys.argv[1] if len(sys.argv) > 1 else "0001.0000.0000.0001"

    mgr = frida.get_local_device()
    procs = [p for p in mgr.enumerate_processes() if p.name == "LensService.exe"]
    if not procs:
        sys.exit("LensService.exe not running")
    session = mgr.attach(procs[0].pid)
    script = session.create_script(JS)
    script.load()
    api = script.exports_sync

    rid = [1000]

    def send(method, resource_id, params, device_id=DEVICE_AC28, label=""):
        rid[0] += 1
        req = {
            "method": method,
            "request_id": rid[0],
            "device_id": device_id,
            "resource_id": resource_id,
        }
        if params is not None:
            req["params"] = params
        body = json.dumps(req)
        result = api.s(body)
        resp_str = result.get("response") or "(empty)"
        # parse if JSON
        try:
            resp = json.loads(resp_str)
            err = resp.get("error_desc", "")
            ok = resp.get("successful", None)
            handler = resp.get("handled_by", "")
            short = "ok" if ok else f"err: {err}"
            print(f"  {label}: [{handler}] {short}")
            if ok:
                print(f"    full response: {resp_str[:300]}")
        except Exception:
            print(f"  {label}: {resp_str[:200]}")

    print(f"=== Probing endpoints for SetID write of value {target_value!r} ===\n")

    # Combinations to try
    combos = [
        # (method, resource_id, params, label)
        ("SET", "/device/settings", {"name": "Set ID", "value": target_value}, "SET /device/settings name=Set ID"),
        ("SET", "/device/settings", {"name": "setId", "value": target_value}, "SET /device/settings name=setId"),
        ("SET", "/device/settings", {"name": "setid", "value": target_value}, "SET /device/settings name=setid"),
        ("SET", "/device/settings", {"name": "SetID", "value": target_value}, "SET /device/settings name=SetID"),
        ("SET", "/device/settings", {"name": "Firmware Version", "value": target_value}, "SET /device/settings name=Firmware Version"),
        ("POST", "/device/firmware", {"name": "set_setid", "value": target_value}, "POST /device/firmware set_setid"),
        ("POST", "/device/firmware", {"name": "set_set_id", "value": target_value}, "POST /device/firmware set_set_id"),
        ("POST", "/device/firmware", {"name": "write_setid", "value": target_value}, "POST /device/firmware write_setid"),
        ("POST", "/device/firmware", {"name": "set_version", "value": target_value}, "POST /device/firmware set_version"),
        ("POST", "/device/firmware", {"name": "setid", "value": target_value}, "POST /device/firmware setid"),
        ("SET", "/device/firmware", {"name": "set_id", "value": target_value}, "SET /device/firmware set_id"),
        ("SET", "/device/firmware", {"value": target_value, "type": "setid"}, "SET /device/firmware type=setid"),
        ("POST", "/device/identity", {"value": target_value}, "POST /device/identity"),
        ("SET", "/device/identity", {"value": target_value}, "SET /device/identity"),
        ("SET", "/device/setid", {"value": target_value}, "SET /device/setid"),
        ("POST", "/device/setid", {"value": target_value}, "POST /device/setid"),
        ("POST", "/device/control/set_id", {"value": target_value}, "POST /device/control/set_id"),
        ("POST", "/device/control/setid", {"value": target_value}, "POST /device/control/setid"),
        ("POST", "/device/control/setFirmwareVersion", {"value": target_value}, "POST /device/control/setFirmwareVersion"),
        ("SET", "/device/generic", {"name": "Set ID", "value": target_value}, "SET /device/generic name=Set ID"),
        ("SET", "/device/generic", {"name": "setid", "value": target_value}, "SET /device/generic name=setid"),
        ("SET", "/device/generic", {"name": "Firmware Version", "value": target_value}, "SET /device/generic name=Firmware Version"),
        ("POST", "/device/generic", {"name": "Set ID", "value": target_value}, "POST /device/generic name=Set ID"),
    ]

    for method, rid_path, params, label in combos:
        send(method, rid_path, params, label=label)

    session.detach()


if __name__ == "__main__":
    main()
