#!/usr/bin/env python3
"""Probe whether DECT devices accept SetSetting writes for "Set ID".

PolyBus exposes settings by string name. LCS reads "Set ID" via
GetSettingAsync but never writes it. This probe tests whether the device
itself accepts a write for that name.

Usage: python3 probes/setid_probe.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from native_bridge import NativeBridge


def main():
    b = NativeBridge()
    if not b.start():
        # start() returns False but bridge can still be functional (32-bit proxy mode)
        pass
    time.sleep(6)

    devs = b.get_devices()
    if not devs:
        print("No native devices found")
        return 1

    # Pick AC28 (the one with the FF setId)
    target = None
    for nid, d in devs.items():
        if d.get("modelId", "").lower() == "ac28":
            target = (nid, d)
            break
    if not target:
        target = list(devs.items())[0]

    nid, dev = target
    print(f"Target: {dev.get('name')} PID={hex(dev.get('pid', 0))} nativeId={nid}")
    print(f"Current firmwareVersion: {json.dumps(dev.get('firmwareVersion'))}")

    # ── Step 1: Try the standard SetDeviceSettings frame with name="Set ID"
    # Our existing bridge.set_setting uses {"id": hex_id, "value": str}.
    # Try the LCS-style {"name": "Set ID", "value": ...} variant.
    test_value = "0000.043A.0429.0BDE"  # build.major.minor.revision (matches bundle 1082.1065.3038)

    print(f"\n=== Test 1: GetGeneralSettings (LIST all generic settings) ===")
    b.send("GetGeneralSettings", {"deviceId": str(nid)})
    time.sleep(2)
    msgs = b.recv(timeout=2.0)
    for m in msgs:
        mt = m.get("messageType", "?")
        payload = m.get("payload")
        if "general" in mt.lower() or "generic" in mt.lower() or "error" in mt.lower():
            ps = json.dumps(payload)[:500]
            print(f"  << {mt}: {ps}")

    print(f"\n=== Test 2: SetGeneralSettings name='Set ID' ===")
    print(f"  Sending value='{test_value}'")
    b.send("SetGeneralSettings", {
        "deviceId": str(nid),
        "settings": [{"name": "Set ID", "value": test_value}],
    })
    time.sleep(2)
    msgs = b.recv(timeout=2.0)
    for m in msgs:
        mt = m.get("messageType", "?")
        payload = m.get("payload")
        if "general" in mt.lower() or "generic" in mt.lower() or "set" in mt.lower() or "error" in mt.lower():
            ps = json.dumps(payload)[:500]
            print(f"  << {mt}: {ps}")

    # ── Step 2: Re-query the device to see if firmwareVersion.setId changed
    print(f"\n=== Step 2: Re-read device state ===")
    time.sleep(2)
    devs2 = b.get_devices()
    dev2 = devs2.get(nid, {})
    new_fw = dev2.get("firmwareVersion", {})
    print(f"firmwareVersion now: {json.dumps(new_fw)}")
    if new_fw.get("setId") != dev.get("firmwareVersion", {}).get("setId"):
        print("  *** SetID CHANGED — write succeeded! ***")
    else:
        print("  setId unchanged (write rejected or persistence pending)")

    # ── Step 3: GetSingleDeviceSetting with various name forms
    for name_variant in ["Set ID", "SetID", "setId", "set_id", "setid"]:
        print(f"\n=== GetSingleDeviceSetting name={name_variant!r} ===")
        b.send("GetSingleDeviceSetting", {
            "deviceId": str(nid),
            "name": name_variant,
        })
        time.sleep(1)
        msgs = b.recv(timeout=1.5)
        for m in msgs:
            mt = m.get("messageType", "?")
            ps = json.dumps(m.get("payload"))[:200]
            if "single" in mt.lower() or "setting" in mt.lower() or "error" in mt.lower():
                print(f"  << {mt}: {ps}")

    b.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
